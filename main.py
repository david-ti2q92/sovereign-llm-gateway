import httpx
import json
import os
import uuid
from dotenv import load_dotenv 
from typing import Any
import jwt
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
import uvicorn

# --- CONFIGURATION (Loading from Environment) ---
# This keeps your Tailscale IPs out of GitHub
load_dotenv()
NODE_1_IP = os.getenv("NODE_1_IP", "localhost")
GATEWAY_HOST = os.getenv("GATEWAY_HOST", os.getenv("GATEWAY_IP", "0.0.0.0"))
PORT = int(os.getenv("PORT", 8090))
GATEWAY_AUTH_TOKEN = os.getenv("GATEWAY_AUTH_TOKEN", "")
MINT_MASTER_SECRET = os.getenv("MINT_MASTER_SECRET", "")
MCP_BACKEND_URL = os.getenv("MCP_BACKEND_URL", f"http://{NODE_1_IP}:9000/mcp/tools/invoke")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", f"http://{NODE_1_IP}:11434")
OPENAI_MODELS_BACKEND_URL = os.getenv("OPENAI_MODELS_BACKEND_URL", f"http://{NODE_1_IP}:11434/v1/models")
OPENAI_CHAT_BACKEND_URL = os.getenv("OPENAI_CHAT_BACKEND_URL", f"http://{NODE_1_IP}:11434/v1/chat/completions")
OPENAI_EMBEDDINGS_BACKEND_URL = os.getenv("OPENAI_EMBEDDINGS_BACKEND_URL", f"http://{NODE_1_IP}:11434/v1/embeddings")
TOOL_POLICY_PATH = os.getenv("TOOL_ACCESS_POLICY_PATH", "/home/ops/AgenticOS/services/llm-gateway/config/tool_access_policy.yaml")
EXPECTED_TOKEN_ISSUER = "household-authority-node-2a"

server = Server("llm-gateway-mcp")
mcp_bridge = SseServerTransport("/messages")


def load_tool_policy() -> dict[str, Any]:
    if os.path.exists(TOOL_POLICY_PATH):
        import yaml

        with open(TOOL_POLICY_PATH, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    return {
        "agents": {
            "agent-hermes": {"tools": ["llm_complete", "llm_list_models", "hitl_submit", "hitl_check"]},
            "agent-openhuman": {"tools": ["llm_complete", "llm_list_models", "hitl_submit", "hitl_check"]},
            "agent-openclaw": {"tools": ["llm_complete", "llm_list_models", "hitl_submit", "hitl_check"]},
            "claude-code": {"tools": ["llm_complete", "llm_list_models", "hitl_submit", "hitl_check"]},
        }
    }


def validate_tool_access(agent_id: str, tool_name: str) -> bool:
    policy = load_tool_policy()
    agent_policy = policy.get("agents", {}).get(agent_id)
    if not agent_policy:
        return False
    return tool_name in set(agent_policy.get("tools", []))


def tool_text(payload: dict[str, Any]):
    return [TextContent(type="text", text=json.dumps(payload, sort_keys=True))]


def ollama_generate_url() -> str:
    base_url = OLLAMA_BASE_URL.rstrip("/")
    if base_url.endswith("/api/generate"):
        return base_url
    return f"{base_url}/api/generate"

def is_authorized_request(request: Request) -> dict[str, Any] | None:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header.split(" ", 1)[1].strip()
    if not token or not MINT_MASTER_SECRET:
        return None

    try:
        claims = jwt.decode(
            token,
            MINT_MASTER_SECRET,
            algorithms=["HS256"],
            issuer=EXPECTED_TOKEN_ISSUER,
        )
    except jwt.InvalidTokenError:
        return None

    agent_id = claims.get("sub")
    scopes_claim = claims.get("scopes", [])

    if not isinstance(agent_id, str) or not agent_id:
        return None

    if isinstance(scopes_claim, str):
        scopes = [scopes_claim]
    elif isinstance(scopes_claim, (list, tuple, set)):
        scopes = [scope for scope in scopes_claim if isinstance(scope, str) and scope]
    else:
        return None

    return {"agent_id": agent_id, "scopes": scopes}


def validate_request_agent_id(request: Request, payload: dict[str, Any], tool_name: str):
    provided_agent_id = payload.get("agent_id")
    if not isinstance(provided_agent_id, str) or not provided_agent_id:
        return JSONResponse(
            {
                "schema_version": "v1",
                "error": "unauthorized",
                "detail": "agent_id is required",
                "tool": tool_name,
            },
            status_code=401,
        )

    if provided_agent_id != request.state.agent_id:
        return JSONResponse(
            {
                "schema_version": "v1",
                "error": "unauthorized",
                "detail": "agent_id does not match authenticated subject",
                "tool": tool_name,
                "agent_id": provided_agent_id,
            },
            status_code=401,
        )

    return None

class JWTAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        auth_context = is_authorized_request(request)
        if not auth_context:
            response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        state = scope.get("state")
        if not isinstance(state, dict):
            state = {}
            scope["state"] = state
        state["agent_id"] = auth_context["agent_id"]
        state["scopes"] = auth_context["scopes"]
        await self.app(scope, receive, send)


def auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {GATEWAY_AUTH_TOKEN}"} if GATEWAY_AUTH_TOKEN else {}


async def proxy_json(url: str, payload: Any) -> Any:
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, json=payload, headers=auth_header())
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()


async def proxy_stream(url: str, payload: Any):
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, json=payload, headers=auth_header()) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk

@server.list_tools()
async def mcp_list_tools():
    return [
        Tool(
            name="llm_complete",
            description="Submit a completion request to Ollama through the gateway.",
            inputSchema={
                "type": "object",
                "properties": {
                    "model": {"type": "string"},
                    "prompt": {"type": "string"},
                    "system": {"type": "string"},
                    "action_type": {"type": "string"},
                    "correlation_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "schema_version": {"type": "string"},
                    "options": {
                        "type": "object",
                        "properties": {
                            "temperature": {"type": "number"},
                            "max_tokens": {"type": "number"},
                        },
                    },
                },
                "required": ["model", "prompt", "action_type", "correlation_id", "agent_id", "schema_version"],
            },
        ),
        Tool(
            name="llm_list_models",
            description="Return the list of available model aliases.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema_version": {"type": "string"},
                    "agent_id": {"type": "string"},
                },
                "required": ["schema_version", "agent_id"],
            },
        ),
        Tool(
            name="hitl_submit",
            description="Submit a Tier 1 HITL approval request.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema_version": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "action_type": {"type": "string"},
                    "action_description": {"type": "string"},
                    "payload_preview": {"type": "string"},
                    "correlation_id": {"type": "string"},
                },
                "required": ["schema_version", "agent_id", "action_type", "action_description", "payload_preview", "correlation_id"],
            },
        ),
        Tool(
            name="hitl_check",
            description="Poll the status of a submitted HITL request.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema_version": {"type": "string"},
                    "hitl_request_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                },
                "required": ["schema_version", "hitl_request_id", "agent_id"],
            },
        ),
    ]

@server.call_tool()
async def mcp_call_tool(name: str, arguments: dict):
    request = server.request_context.request
    request_state = getattr(request, "state", None) if request is not None else None
    authenticated_agent_id = getattr(request_state, "agent_id", None)
    agent_id = arguments.get("agent_id", "")

    if authenticated_agent_id is None or agent_id != authenticated_agent_id:
        return tool_text(
            {
                "schema_version": "v1",
                "error": "unauthorized",
                "detail": "agent_id does not match authenticated subject",
                "tool": name,
                "agent_id": agent_id,
            }
        )

    if not validate_tool_access(agent_id, name):
        return tool_text({"schema_version": "v1", "error": "forbidden", "tool": name, "agent_id": agent_id})

    if name == "llm_complete":
        model = arguments.get("model", "llama3.1:8b")
        prompt = arguments.get("prompt", "")
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        system_prompt = arguments.get("system")
        if system_prompt:
            payload["system"] = system_prompt

        try:
            result = await proxy_json(ollama_generate_url(), payload)
        except httpx.HTTPError as exc:
            return tool_text({"schema_version": "v1", "error": "backend_unavailable", "detail": str(exc), "tool": name})

        content = result.get("response", "") if isinstance(result, dict) else ""
        return tool_text(
            {
                "schema_version": "v1",
                "completion_id": str(uuid.uuid4()),
                "model": model,
                "content": content,
                "action_type": arguments.get("action_type", "classify"),
                "hitl_status": "autonomous",
                "hitl_request_id": None,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            }
        )

    payload = {
        "tool": name,
        "arguments": arguments,
    }
    try:
        result = await proxy_json(MCP_BACKEND_URL, payload)
    except httpx.HTTPError as exc:
        return tool_text({"schema_version": "v1", "error": "backend_unavailable", "detail": str(exc), "tool": name})

    if isinstance(result, dict):
        return tool_text(result)
    return tool_text({"schema_version": "v1", "result": result})

async def list_models(request):
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.get(OPENAI_MODELS_BACKEND_URL, headers=auth_header())
        return JSONResponse(response.json(), status_code=response.status_code)


async def completion(request: Request):
    data = await request.json()
    identity_error = validate_request_agent_id(request, data, "llm_complete")
    if identity_error:
        return identity_error

    agent_id = data.get("agent_id", "")
    if not validate_tool_access(agent_id, "llm_complete"):
        return JSONResponse(
            {"schema_version": "v1", "error": "forbidden", "tool": "llm_complete", "agent_id": agent_id},
            status_code=403,
        )

    ollama_payload = {
        "model": data.get("model", "llama3.1:8b"),
        "prompt": data.get("prompt", ""),
        "stream": bool(data.get("stream", False)),
    }
    if data.get("system"):
        ollama_payload["system"] = data["system"]
    if isinstance(data.get("options"), dict):
        ollama_payload.update(data["options"])

    if ollama_payload["stream"]:
        return StreamingResponse(proxy_stream(ollama_generate_url(), ollama_payload), media_type="application/x-ndjson")

    result = await proxy_json(ollama_generate_url(), ollama_payload)
    return JSONResponse(result)


async def chat(request: Request):
    data = await request.json()
    identity_error = validate_request_agent_id(request, data, "llm_complete")
    if identity_error:
        return identity_error

    agent_id = data.get("agent_id", "")
    if not validate_tool_access(agent_id, "llm_complete"):
        return JSONResponse(
            {"schema_version": "v1", "error": "forbidden", "tool": "llm_complete", "agent_id": agent_id},
            status_code=403,
        )

    upstream_payload = {k: v for k, v in data.items() if k != "agent_id"}
    stream_requested = bool(upstream_payload.get("stream", False))
    if stream_requested:
        return StreamingResponse(proxy_stream(OPENAI_CHAT_BACKEND_URL, upstream_payload), media_type="text/event-stream")

    result = await proxy_json(OPENAI_CHAT_BACKEND_URL, upstream_payload)
    return JSONResponse(result)


async def openai_compat(request):
    data = await request.json()
    stream_requested = bool(data.get("stream", False))
    if stream_requested:
        return StreamingResponse(proxy_stream(OPENAI_CHAT_BACKEND_URL, data), media_type="text/event-stream")

    result = await proxy_json(OPENAI_CHAT_BACKEND_URL, data)
    return JSONResponse(result)

async def get_embeddings(request):
    data = await request.json()
    result = await proxy_json(OPENAI_EMBEDDINGS_BACKEND_URL, data)
    return JSONResponse(result)

async def handle_sse(request: Request):
    async with mcp_bridge.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
    return Response()


async def health(request: Request):
    return JSONResponse({"status": "ok", "service": "llm-gateway", "port": PORT})

starlette_app = Starlette(
    routes=[
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/completion", endpoint=completion, methods=["POST"]),
        Route("/chat", endpoint=chat, methods=["POST"]),
        Route("/v1/models", endpoint=list_models, methods=["GET"]),
        Route("/v1/chat/completions", endpoint=openai_compat, methods=["POST"]),
        Route("/v1/embeddings", endpoint=get_embeddings, methods=["POST"]),
        Mount(
            "/mcp",
            app=Starlette(
                routes=[
                    Route("/", endpoint=handle_sse, methods=["GET"]),
                    Mount("/messages", app=mcp_bridge.handle_post_message),
                ],
            ),
        ),
        Mount(
            "/sse",
            app=Starlette(
                routes=[
                    Route("/", endpoint=handle_sse, methods=["GET"]),
                    Mount("/messages", app=mcp_bridge.handle_post_message),
                ],
            ),
        ),
    ],
)
starlette_app.mount("/messages", app=mcp_bridge.handle_post_message)
starlette_app.router.redirect_slashes = True
starlette_app.add_middleware(JWTAuthMiddleware)

if __name__ == "__main__":
    uvicorn.run(starlette_app, host=GATEWAY_HOST, port=PORT)
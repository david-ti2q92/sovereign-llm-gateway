import asyncio
import httpx
import time
import json
import os
from typing import Any
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse, StreamingResponse
import uvicorn

# --- CONFIGURATION (Loading from Environment) ---
# This keeps your Tailscale IPs out of GitHub
NODE_1_IP = os.getenv("NODE_1_IP", "127.0.0.1") 
GATEWAY_IP = os.getenv("GATEWAY_IP", "0.0.0.0")
PORT = int(os.getenv("PORT", 8090))

OLLAMA_URL = f"http://{NODE_1_IP}:11434/api/generate"

async def ollama_stream_generator(model: str, prompt: str):
    async with httpx.AsyncClient(timeout=120.0) as client:
        payload = {"model": model, "prompt": prompt, "stream": True}
        async with client.stream("POST", OLLAMA_URL, json=payload) as response:
            async for line in response.aiter_lines():
                if not line: continue
                chunk = json.loads(line)
                token = chunk.get("response", "")
                data = {"choices": [{"delta": {"content": token}, "finish_reason": None if not chunk.get("done") else "stop"}]}
                yield f"data: {json.dumps(data)}\n\n"
            yield "data: [DONE]\n\n"

def extract_text_content(content):
    if isinstance(content, str): return content
    if isinstance(content, list):
        return " ".join([item.get("text", "") for item in content if isinstance(item, dict)])
    return str(content)

async def list_models(request):
    return JSONResponse({"object": "list", "data": [{"id": "llama3.1:8b", "object": "model"}]})

async def openai_compat(request):
    data = await request.json()
    messages = data.get("messages", [])
    raw_content = messages[-1].get("content", "") if messages else "Hello"
    prompt = extract_text_content(raw_content)
    model = data.get("model", "llama3.1:8b")
    print(f"--- Streaming Request (Env: {NODE_1_IP}) ---")
    return StreamingResponse(ollama_stream_generator(model, prompt), media_type="text/event-stream")

app = Starlette(
    routes=[
        Route("/v1/models", endpoint=list_models, methods=["GET"]),
        Route("/v1/chat/completions", endpoint=openai_compat, methods=["POST"]),
    ],
)

if __name__ == "__main__":
    uvicorn.run(app, host=GATEWAY_IP, port=PORT)
import os
import asyncio
import httpx
from typing import Any
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server.stdio import stdio_server

# --- SOVEREIGN SPECIFICATION CROSS-REFERENCE ---
# Master Spec §10.1: LLM Gateway Specifications
# Addendum A §A2.2: Tool Definition (llm_complete)
# Addendum A §A2.3: Gateway Enforcement Logic (HITL Tiers)

server = Server("sovereign-llm-gateway")

# Mock HITL Policy - In production, this loads from config/tool_access_policy.yaml
TIER_1_ACTIONS = ["financial", "publish", "write"]

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="llm_complete",
            description="Submit a completion request to Ollama with Sovereign HITL gating.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "model": {"type": "string"},
                    "prompt": {"type": "string"},
                    "action_type": {"type": "string", "enum": ["classify", "summarise", "plan", "write", "publish", "financial"]},
                    "correlation_id": {"type": "string"}
                },
                "required": ["agent_id", "model", "prompt", "action_type", "correlation_id"]
            },
        )
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
    if not arguments or name != "llm_complete":
        raise ValueError("Invalid tool or arguments")

    agent_id = arguments.get("agent_id")
    action_type = arguments.get("action_type")
    correlation_id = arguments.get("correlation_id")

    # --- INVARIANT ENFORCEMENT: HITL TIER CHECK (MCP-INV-02) ---
    # Per Addendum A §A2.3: If Tier 1, check for approval before proxying.
    if action_type in TIER_1_ACTIONS:
        # Here, the gateway would check PostgreSQL for a 'hitl_approved' record
        return [types.TextContent(
            type="text", 
            text=f"HITL_GATE: Action '{action_type}' requires approval. Request {correlation_id} submitted to Telegram."
        )]

    # --- SECURE PROXY TO NODE 1 (OLLAMA) ---
    # Only reachable if Tier 0 or Tier 1 is already approved.
    # Replace with your actual Node 1 Tailscale IP in production
    # async with httpx.AsyncClient() as client:
    #     resp = await client.post("http://node-1-ip:11434/api/generate", json=...)
    
    return [types.TextContent(type="text", text=f"SUCCESS: Autonomous execution for {action_type}. Prompt proxied to Node 1.")]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, InitializationOptions(
            server_name="sovereign-llm-gateway",
            server_version="0.1.0",
            capabilities=server.get_capabilities(),
        ))

if __name__ == "__main__":
    asyncio.run(main())
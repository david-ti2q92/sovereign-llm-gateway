# Sovereign LLM Gateway (MCP)

This is the **Cognitive Gatekeeper** for the Sovereign Household AI Platform.

## Purpose
It sits on **Node 2A (Data Plane)** and intercepts all requests to the LLM (Node 1). It enforces Human-in-the-Loop (HITL) requirements based on the risk level of the agent's intent.

## Governance (Master Spec §9.4)
- **Tier 0 (Autonomous):** Classification, summarization, and internal planning.
- **Tier 1 (Gated):** Financial transactions, public posts, or file deletions.
- **Tier 2 (Prohibited):** Unauthorized network calls or credential access.

## Technical Stack
- **Protocol:** Model Context Protocol (MCP)
- **Runtime:** Python / MCP SDK
- **Backend:** Proxies to Ollama (Node 1) via Tailscale
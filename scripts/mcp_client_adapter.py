"""MCP client adapter for the health-coach decoupled mode.

Connects to health-platform's MCP server over Streamable HTTP, discovers
tools dynamically, and provides:

  - Schema conversion: MCP tool schemas -> OpenAI function-calling format
  - Tool dispatch: call MCP tools by name with JSON arguments
  - Drop-in replacement for the in-process TOOL_DISPATCH used by
    chat_app.py and telegram_bot.py

Usage:
    client = PlatformClient(endpoint="http://mcp:8765/mcp", api_key="...")
    tools = await client.list_tools()
    schemas = mcp_tools_to_openai_schemas(tools)
    result = await client.call_tool("get_fitness_summary", {})
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 60.0


class MCPToolError(Exception):
    """Raised when an MCP tool call returns an error."""


# ---------------------------------------------------------------------------
# Schema conversion: MCP -> OpenAI function-calling
# ---------------------------------------------------------------------------

def mcp_tool_to_openai_schema(mcp_tool: dict) -> dict:
    """Convert a single MCP tool descriptor to OpenAI function-calling format."""
    input_schema = mcp_tool.get("inputSchema", {})
    return {
        "type": "function",
        "function": {
            "name": mcp_tool["name"],
            "description": mcp_tool.get("description", ""),
            "parameters": {
                "type": input_schema.get("type", "object"),
                "properties": input_schema.get("properties", {}),
                "required": input_schema.get("required", []),
            },
        },
    }


def mcp_tools_to_openai_schemas(mcp_tools: list[dict]) -> list[dict]:
    """Batch-convert MCP tool descriptors to OpenAI function-calling schemas."""
    return [mcp_tool_to_openai_schema(t) for t in mcp_tools]


# ---------------------------------------------------------------------------
# Tool dispatch map (replaces chat_tools_schema.TOOL_DISPATCH)
# ---------------------------------------------------------------------------

def build_dispatch_map(mcp_tools: list[dict]) -> dict[str, dict]:
    """Build a dispatch map from MCP tool descriptors.

    Returns {tool_name: {"param_kind": "none"|"kwargs", "schema": dict}}
    where param_kind indicates whether the tool takes parameters.
    """
    dispatch: dict[str, dict] = {}
    for tool in mcp_tools:
        props = tool.get("inputSchema", {}).get("properties", {})
        param_kind = "kwargs" if props else "none"
        dispatch[tool["name"]] = {
            "param_kind": param_kind,
            "schema": tool,
        }
    return dispatch


# ---------------------------------------------------------------------------
# MCP JSON-RPC client
# ---------------------------------------------------------------------------

class PlatformClient:
    """Async HTTP client for the health-platform MCP server."""

    def __init__(self, endpoint: str, api_key: str):
        self.endpoint = endpoint
        self.api_key = api_key
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _rpc(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC 2.0 request to the MCP server."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        async with httpx.AsyncClient() as http:
            resp = await http.post(
                self.endpoint,
                headers=self._headers(),
                json=payload,
                timeout=_REQUEST_TIMEOUT,
            )

        body = resp.json()

        if "error" in body:
            err = body["error"]
            msg = err.get("message", str(err))
            raise MCPToolError(msg)

        return body.get("result")

    async def list_tools(self) -> list[dict]:
        """Discover available tools from the MCP server."""
        result = await self._rpc("tools/list")
        return result.get("tools", []) if result else []

    async def call_tool(self, name: str, arguments: dict) -> Any:
        """Execute a tool on the MCP server and return the parsed result."""
        result = await self._rpc("tools/call", {
            "name": name,
            "arguments": arguments,
        })

        if not result:
            return None

        content = result.get("content", [])
        if not content:
            return None

        text_parts = [c["text"] for c in content if c.get("type") == "text"]
        if not text_parts:
            return None

        combined = text_parts[0] if len(text_parts) == 1 else "\n".join(text_parts)
        try:
            return json.loads(combined)
        except (json.JSONDecodeError, TypeError):
            return combined

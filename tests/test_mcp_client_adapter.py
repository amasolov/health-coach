"""Tests for the MCP client adapter (platform-coach decoupling).

The adapter connects to health-platform's MCP server as a client,
discovers tools, and converts MCP schemas to OpenAI function-calling format.
These tests mock the HTTP transport to avoid needing a running server.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Schema conversion: MCP tool schema -> OpenAI function-calling schema
# ---------------------------------------------------------------------------

class TestMCPToOpenAISchemaConversion:

    def test_converts_simple_tool(self):
        from scripts.mcp_client_adapter import mcp_tool_to_openai_schema

        mcp_tool = {
            "name": "get_fitness_summary",
            "description": "Get current fitness status.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        }
        result = mcp_tool_to_openai_schema(mcp_tool)
        assert result["type"] == "function"
        assert result["function"]["name"] == "get_fitness_summary"
        assert result["function"]["description"] == "Get current fitness status."
        assert result["function"]["parameters"]["type"] == "object"

    def test_converts_tool_with_params(self):
        from scripts.mcp_client_adapter import mcp_tool_to_openai_schema

        mcp_tool = {
            "name": "get_activities",
            "description": "List activities.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Start date"},
                    "days": {"type": "integer", "description": "Lookback days"},
                },
                "required": [],
            },
        }
        result = mcp_tool_to_openai_schema(mcp_tool)
        props = result["function"]["parameters"]["properties"]
        assert "start_date" in props
        assert "days" in props
        assert props["start_date"]["type"] == "string"

    def test_batch_converts_tools(self):
        from scripts.mcp_client_adapter import mcp_tools_to_openai_schemas

        mcp_tools = [
            {
                "name": "tool_a",
                "description": "Tool A.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "tool_b",
                "description": "Tool B.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}},
                },
            },
        ]
        results = mcp_tools_to_openai_schemas(mcp_tools)
        assert len(results) == 2
        assert results[0]["function"]["name"] == "tool_a"
        assert results[1]["function"]["name"] == "tool_b"


# ---------------------------------------------------------------------------
# Tool dispatch mapping
# ---------------------------------------------------------------------------

class TestToolDispatch:

    def test_builds_dispatch_map(self):
        from scripts.mcp_client_adapter import build_dispatch_map

        mcp_tools = [
            {"name": "get_fitness_summary", "description": "D", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "get_activities", "description": "D", "inputSchema": {"type": "object", "properties": {"days": {"type": "integer"}}}},
        ]
        dispatch = build_dispatch_map(mcp_tools)
        assert "get_fitness_summary" in dispatch
        assert "get_activities" in dispatch
        assert dispatch["get_fitness_summary"]["param_kind"] == "none"
        assert dispatch["get_activities"]["param_kind"] == "kwargs"


# ---------------------------------------------------------------------------
# PlatformClient
# ---------------------------------------------------------------------------

class TestPlatformClient:

    def test_init_stores_config(self):
        from scripts.mcp_client_adapter import PlatformClient
        client = PlatformClient(
            endpoint="http://mcp:8765/mcp",
            api_key="test-key-123",
        )
        assert client.endpoint == "http://mcp:8765/mcp"
        assert client.api_key == "test-key-123"

    @pytest.mark.anyio
    async def test_call_tool_sends_correct_request(self):
        from scripts.mcp_client_adapter import PlatformClient

        client = PlatformClient(
            endpoint="http://mcp:8765/mcp",
            api_key="test-key",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": '{"ctl": 55.0}'}],
            },
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
            result = await client.call_tool("get_fitness_summary", {})
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            assert "Bearer test-key" in str(call_kwargs)

    @pytest.mark.anyio
    async def test_call_tool_returns_parsed_content(self):
        from scripts.mcp_client_adapter import PlatformClient

        client = PlatformClient(
            endpoint="http://mcp:8765/mcp",
            api_key="test-key",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"ctl": 55.0})}],
            },
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            result = await client.call_tool("get_fitness_summary", {})
            assert result == {"ctl": 55.0}

    @pytest.mark.anyio
    async def test_call_tool_handles_error_response(self):
        from scripts.mcp_client_adapter import PlatformClient, MCPToolError

        client = PlatformClient(
            endpoint="http://mcp:8765/mcp",
            api_key="test-key",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "Tool execution failed"},
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            with pytest.raises(MCPToolError, match="Tool execution failed"):
                await client.call_tool("get_fitness_summary", {})

    @pytest.mark.anyio
    async def test_list_tools(self):
        from scripts.mcp_client_adapter import PlatformClient

        client = PlatformClient(
            endpoint="http://mcp:8765/mcp",
            api_key="test-key",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {"name": "get_fitness_summary", "description": "D",
                     "inputSchema": {"type": "object", "properties": {}}},
                ],
            },
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            tools = await client.list_tools()
            assert len(tools) == 1
            assert tools[0]["name"] == "get_fitness_summary"

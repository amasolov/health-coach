"""Tests for the dual-mode tool executor (platform-coach split).

Verifies that:
  - Direct mode uses TOOL_DISPATCH (in-process)
  - MCP mode calls the PlatformClient
  - Schema retrieval works in both modes
  - Credential dispatch routing is preserved
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


# ---------------------------------------------------------------------------
# Direct mode (default, no MCP_ENDPOINT)
# ---------------------------------------------------------------------------

class TestDirectMode:

    def test_direct_mode_by_default(self):
        with patch.dict("os.environ", {}, clear=False):
            import importlib
            import scripts.tool_executor as mod
            importlib.reload(mod)
            assert mod.is_mcp_mode() is False

    def test_get_schemas_returns_list(self):
        from scripts.tool_executor import get_tool_schemas
        schemas = get_tool_schemas()
        assert isinstance(schemas, list)
        assert len(schemas) > 0
        assert schemas[0]["type"] == "function"

    def test_get_dispatch_returns_dict(self):
        from scripts.tool_executor import get_tool_dispatch
        dispatch = get_tool_dispatch()
        assert isinstance(dispatch, dict)
        assert "get_fitness_summary" in dispatch

    def test_execute_uid_tool(self, user_id, user_slug):
        from scripts.tool_executor import execute_tool
        result = execute_tool(
            "get_fitness_summary", {}, user_id, user_slug, {},
        )
        assert isinstance(result, dict)
        assert "error" not in result or "ctl" in result

    def test_execute_slug_tool(self, user_id, user_slug):
        from scripts.tool_executor import execute_tool
        result = execute_tool(
            "get_athlete_profile", {}, user_id, user_slug, {},
        )
        assert isinstance(result, dict)

    def test_execute_unknown_tool(self, user_id, user_slug):
        from scripts.tool_executor import execute_tool
        result = execute_tool(
            "nonexistent_tool_xyz", {}, user_id, user_slug, {},
        )
        assert "error" in result

    def test_excluded_tools(self, user_id, user_slug):
        from scripts.tool_executor import execute_tool
        result = execute_tool(
            "get_fitness_summary", {}, user_id, user_slug, {},
            excluded_tools={"get_fitness_summary"},
        )
        assert "error" in result
        assert "not available" in result["error"]

    def test_creds_dispatch_sync_data(self, user_id, user_slug):
        from scripts.tool_executor import execute_tool
        from scripts.chat_tools_schema import TOOL_DISPATCH

        mock_fn = MagicMock(return_value={"synced": {}})
        original = TOOL_DISPATCH.get("sync_data")
        TOOL_DISPATCH["sync_data"] = (mock_fn, "creds")
        try:
            result = execute_tool(
                "sync_data", {},
                user_id, user_slug,
                {"hevy_api_key": "test-key"},
            )
            mock_fn.assert_called_once_with(user_slug, user_id, "test-key")
            assert result == {"synced": {}}
        finally:
            if original:
                TOOL_DISPATCH["sync_data"] = original


# ---------------------------------------------------------------------------
# MCP mode
# ---------------------------------------------------------------------------

class TestMCPMode:

    def test_mcp_mode_when_env_set(self):
        with patch.dict("os.environ", {
            "MCP_ENDPOINT": "http://mcp:8765/mcp",
            "MCP_API_KEY": "test-key",
        }):
            import importlib
            import scripts.tool_executor as mod
            importlib.reload(mod)
            assert mod.is_mcp_mode() is True
            # Reset to direct mode for other tests
            importlib.reload(mod)

    def test_mcp_execute_calls_client(self):
        from scripts.mcp_client_adapter import PlatformClient
        import scripts.tool_executor as mod

        mock_client = MagicMock(spec=PlatformClient)
        mock_client.call_tool = AsyncMock(return_value={"ctl": 55.0})

        with patch.object(mod, "_get_client", return_value=mock_client):
            result = mod._mcp_execute("get_fitness_summary", {})
            assert result == {"ctl": 55.0}
            mock_client.call_tool.assert_awaited_once_with("get_fitness_summary", {})

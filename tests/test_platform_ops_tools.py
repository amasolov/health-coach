"""Tests for the new platform ops tools (issue #26 / platform-coach split).

Covers: get_ops_log, get_service_health, list_users_summary,
        cross-channel context, and trigger_user_sync MCP registration.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# health_tools.get_ops_log
# ---------------------------------------------------------------------------

class TestGetOpsLog:

    def test_returns_recent_entries(self, user_id):
        from scripts import ops_emit, health_tools

        ops_emit.emit("sync", "garmin_sync", user_id=user_id, status="ok",
                       duration_ms=1200, new_activities=3)
        ops_emit.emit("sync", "hevy_sync", user_id=user_id, status="error",
                       duration_ms=500, error="timeout")

        rows = health_tools.get_ops_log(limit=10)
        assert isinstance(rows, list)
        assert len(rows) >= 2

    def test_filter_by_category(self, user_id):
        from scripts import ops_emit, health_tools

        ops_emit.emit("test_cat_filter", "cat_filter_event", user_id=user_id)

        rows = health_tools.get_ops_log(category="test_cat_filter", limit=50)
        assert len(rows) >= 1
        assert all(r["category"] == "test_cat_filter" for r in rows)

    def test_filter_by_user(self, user_id):
        from scripts import ops_emit, health_tools

        ops_emit.emit("sync", "test_event", user_id=user_id)

        rows = health_tools.get_ops_log(user_id=user_id, limit=50)
        assert all(r["user_id"] == user_id for r in rows)

    def test_limit_respected(self, user_id):
        from scripts import ops_emit, health_tools

        for i in range(5):
            ops_emit.emit("test", f"event_{i}", user_id=user_id)

        rows = health_tools.get_ops_log(limit=2)
        assert len(rows) <= 2

    def test_returns_empty_with_no_data(self):
        from scripts import health_tools
        rows = health_tools.get_ops_log(category="nonexistent_category_xyz", limit=10)
        assert rows == []


# ---------------------------------------------------------------------------
# health_tools.get_service_health
# ---------------------------------------------------------------------------

class TestGetServiceHealth:

    def test_returns_health_dict(self, user_id):
        from scripts import health_tools
        result = health_tools.get_service_health()
        assert isinstance(result, dict)
        assert "db_connected" in result
        assert result["db_connected"] is True

    def test_includes_user_count(self, user_id):
        from scripts import health_tools
        result = health_tools.get_service_health()
        assert "user_count" in result
        assert result["user_count"] >= 1

    def test_includes_last_sync(self, user_id):
        from scripts import ops_emit, health_tools

        ops_emit.emit("sync", "sync_cycle", status="ok", duration_ms=5000)

        result = health_tools.get_service_health()
        assert "last_sync" in result


# ---------------------------------------------------------------------------
# health_tools.list_users_summary
# ---------------------------------------------------------------------------

class TestListUsersSummary:

    def test_returns_user_list(self, user_id):
        from scripts import health_tools
        users = health_tools.list_users_summary()
        assert isinstance(users, list)
        assert len(users) >= 1

    def test_contains_expected_fields(self, user_id):
        from scripts import health_tools
        users = health_tools.list_users_summary()
        user = users[0]
        assert "slug" in user
        assert "display_name" in user
        assert "onboarding_complete" in user

    def test_excludes_secrets(self, user_id):
        from scripts import health_tools
        users = health_tools.list_users_summary()
        for u in users:
            assert "garmin_password" not in u
            assert "mcp_api_key" not in u
            assert "hevy_api_key" not in u


# ---------------------------------------------------------------------------
# health_tools.get_cross_channel_context
# ---------------------------------------------------------------------------

class TestGetCrossChannelContext:

    def test_telegram_channel(self, user_id):
        from scripts.cross_channel import save_telegram_message
        from scripts import health_tools

        save_telegram_message(user_id, 12345, "user", "hello from telegram")

        result = health_tools.get_cross_channel_context(
            user_id=user_id, channel="telegram",
        )
        assert isinstance(result, dict)
        assert "messages" in result

    def test_unknown_channel_returns_empty(self, user_id):
        from scripts import health_tools
        result = health_tools.get_cross_channel_context(
            user_id=user_id, channel="nonexistent",
        )
        assert result["messages"] == []

    def test_limit_and_hours(self, user_id):
        from scripts import health_tools
        result = health_tools.get_cross_channel_context(
            user_id=user_id, channel="telegram", limit=5, hours=1,
        )
        assert isinstance(result, dict)
        assert len(result["messages"]) <= 5


# ---------------------------------------------------------------------------
# MCP tool registration (unit-level, no running server)
# ---------------------------------------------------------------------------

class TestMCPToolRegistration:

    def test_new_ops_tools_registered(self):
        """Verify the MCP server module defines the expected new tools."""
        pytest.importorskip("fastmcp")
        from scripts import mcp_server
        tool_names = {t.name for t in mcp_server.mcp._tool_manager.tools.values()}
        expected_new = {
            "trigger_user_sync",
            "get_cross_channel_context",
            "get_ops_log",
            "get_service_health",
            "list_users",
            "register_user",
            "save_telegram_message",
        }
        missing = expected_new - tool_names
        assert not missing, f"Missing MCP tools: {missing}"

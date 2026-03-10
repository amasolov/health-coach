"""MCP protocol connectivity tests.

These tests connect to a running MCP server (the addon) to verify
tool registration and basic request/response flow. They require:
  - MCP_TEST_URL env var (e.g. http://192.168.1.100:8000/mcp)
  - MCP_TEST_TOKEN env var (bearer token for auth)

Skip automatically if not configured.
"""

import json
import os
import pytest

MCP_URL = os.environ.get("MCP_TEST_URL", "")
MCP_TOKEN = os.environ.get("MCP_TEST_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not MCP_URL or not MCP_TOKEN,
    reason="MCP_TEST_URL and MCP_TEST_TOKEN not set",
)


def _mcp_request(method: str, params: dict | None = None) -> dict:
    import httpx
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
    }
    if params:
        payload["params"] = params
    r = httpx.post(
        MCP_URL,
        headers={
            "Authorization": f"Bearer {MCP_TOKEN}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    assert r.status_code == 200, f"MCP returned {r.status_code}: {r.text[:200]}"
    return r.json()


class TestMCPConnectivity:

    def test_list_tools(self):
        result = _mcp_request("tools/list")
        assert "result" in result
        tools = result["result"].get("tools", [])
        assert len(tools) > 0, "Expected at least one registered tool"
        tool_names = {t["name"] for t in tools}
        assert "get_fitness_summary" in tool_names
        assert "search_ifit_library" in tool_names

    def test_call_read_tool(self):
        result = _mcp_request("tools/call", {
            "name": "get_fitness_summary",
            "arguments": {},
        })
        assert "result" in result or "error" in result
        if "result" in result:
            content = result["result"].get("content", [])
            assert len(content) > 0

    def test_call_search(self):
        result = _mcp_request("tools/call", {
            "name": "search_ifit_library",
            "arguments": {"query": "strength", "limit": 3},
        })
        assert "result" in result or "error" in result

    def test_expected_tools_registered(self):
        result = _mcp_request("tools/list")
        tool_names = {t["name"] for t in result["result"]["tools"]}
        expected = {
            "get_fitness_summary", "get_training_load", "get_activities",
            "get_body_composition", "get_vitals", "get_training_zones",
            "get_athlete_profile", "get_strength_sessions",
            "search_ifit_library", "search_ifit_programs",
            "get_ifit_program_details", "get_ifit_workout_details",
            "discover_ifit_series", "recommend_strength_workout",
            "create_hevy_routine_from_recommendation",
            "get_hevy_routine_review", "compare_hevy_workout",
            "apply_exercise_feedback", "report_exercise_correction",
        }
        missing = expected - tool_names
        assert not missing, f"Missing MCP tools: {missing}"

    def test_unauthenticated_rejected(self):
        import httpx
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        r = httpx.post(MCP_URL, json=payload, timeout=15)
        assert r.status_code in (401, 403) or "error" in r.json()

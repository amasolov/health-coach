"""Tests that all tool schemas, dispatch entries, and MCP registrations are consistent."""

import pytest
from scripts.chat_tools_schema import TOOL_SCHEMAS, TOOL_DISPATCH


class TestToolDispatch:

    def test_all_schemas_have_dispatch(self):
        schema_names = {t["function"]["name"] for t in TOOL_SCHEMAS}
        dispatch_names = set(TOOL_DISPATCH.keys())
        missing = schema_names - dispatch_names
        assert not missing, f"Schema tools missing from dispatch: {missing}"

    def test_all_dispatch_have_schemas(self):
        schema_names = {t["function"]["name"] for t in TOOL_SCHEMAS}
        dispatch_names = set(TOOL_DISPATCH.keys())
        extra = dispatch_names - schema_names
        assert not extra, f"Dispatch tools missing from schema: {extra}"

    def test_dispatch_functions_exist(self):
        for name, (fn, kind) in TOOL_DISPATCH.items():
            assert callable(fn), f"{name}: function is not callable"
            assert fn.__name__ == name or hasattr(fn, "__wrapped__"), \
                f"{name}: expected function named '{name}', got '{fn.__name__}'"

    def test_dispatch_kinds_valid(self):
        valid_kinds = {"uid", "slug", "none", "creds"}
        for name, (fn, kind) in TOOL_DISPATCH.items():
            assert kind in valid_kinds, f"{name}: invalid param_kind '{kind}'"

    def test_schema_has_required_fields(self):
        for schema in TOOL_SCHEMAS:
            assert "type" in schema
            assert "function" in schema
            func = schema["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func
            assert func["parameters"].get("type") == "object"

    def test_no_duplicate_schemas(self):
        names = [t["function"]["name"] for t in TOOL_SCHEMAS]
        assert len(names) == len(set(names)), f"Duplicate schema names: {[n for n in names if names.count(n) > 1]}"

    def test_tool_count(self):
        """Sanity check that we haven't accidentally lost tools."""
        assert len(TOOL_SCHEMAS) >= 40, f"Expected >=40 tools, got {len(TOOL_SCHEMAS)}"


class TestDisplayNames:

    def test_all_tools_have_display_names(self):
        """Every schema tool should have a display name in chat_app."""
        try:
            from scripts.chat_app import TOOL_DISPLAY_NAMES
        except ImportError:
            pytest.skip("chainlit not installed (display names test requires it)")
        schema_names = {t["function"]["name"] for t in TOOL_SCHEMAS}
        missing = schema_names - set(TOOL_DISPLAY_NAMES.keys())
        expected_missing = {"sync_data", "garmin_auth_status", "garmin_authenticate",
                            "garmin_submit_mfa", "garmin_fetch_profile",
                            "generate_fitness_assessment", "update_athlete_profile",
                            "get_onboarding_questions", "set_user_goals",
                            "set_user_integrations"}
        unexpected_missing = missing - expected_missing
        assert not unexpected_missing, f"Tools missing display names: {unexpected_missing}"

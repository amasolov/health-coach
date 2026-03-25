"""
Tests for per-request tool filtering.

Verifies that select_tools_for_message correctly narrows the tool set
based on user message intent, reducing schema tokens sent to the LLM.
"""

from __future__ import annotations

import pytest

from scripts.tool_filter import select_tools_for_message, TOOL_GROUPS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


ALL_TOOL_NAMES = [
    "get_fitness_summary", "get_training_load", "get_activities",
    "get_activity_detail", "get_body_composition", "get_vitals",
    "get_training_zones", "setup_running_hr_zones", "get_athlete_profile",
    "get_strength_sessions", "get_workout_summary",
    "list_treadmill_templates", "generate_treadmill_workout",
    "suggest_feature", "report_exercise_correction", "sync_data",
    "garmin_auth_status", "garmin_authenticate", "garmin_submit_mfa",
    "hevy_auth_status", "hevy_connect", "garmin_fetch_profile",
    "generate_fitness_assessment", "update_athlete_profile",
    "get_onboarding_questions", "set_user_goals", "get_user_goals",
    "get_action_items", "add_action_item", "update_action_item",
    "complete_action_item", "get_supported_integrations",
    "set_user_integrations", "get_user_integrations",
    "recommend_ifit_workout", "search_ifit_library",
    "get_ifit_workout_details", "recommend_strength_workout",
    "search_ifit_programs", "get_ifit_program_details",
    "discover_ifit_series", "create_hevy_routine_from_recommendation",
    "get_hevy_routine_review", "compare_hevy_workout",
    "apply_exercise_feedback", "get_routine_weight_recommendations",
    "generate_telegram_link_code", "search_knowledge_base",
    "list_knowledge_documents", "delete_knowledge_document",
    "check_weather", "recommend_outdoor_run", "rate_route",
    "manage_hevy_routines",
]

ALL_SCHEMAS = [_make_schema(n) for n in ALL_TOOL_NAMES]


def _names(schemas: list[dict]) -> set[str]:
    return {s["function"]["name"] for s in schemas}


# ---------------------------------------------------------------------------
# Fallback behaviour — unknown intent returns all tools
# ---------------------------------------------------------------------------

class TestFallbackToAll:

    def test_empty_message_returns_all(self):
        result = select_tools_for_message("", ALL_SCHEMAS)
        assert len(result) == len(ALL_SCHEMAS)

    def test_greeting_returns_all(self):
        result = select_tools_for_message("Hi there!", ALL_SCHEMAS)
        assert len(result) == len(ALL_SCHEMAS)

    def test_vague_message_returns_all(self):
        result = select_tools_for_message("Thanks, that's helpful", ALL_SCHEMAS)
        assert len(result) == len(ALL_SCHEMAS)


# ---------------------------------------------------------------------------
# Core tools always present
# ---------------------------------------------------------------------------

class TestCoreAlwaysPresent:

    def test_core_present_for_sleep_query(self):
        result = select_tools_for_message("How did I sleep?", ALL_SCHEMAS)
        names = _names(result)
        for tool in TOOL_GROUPS["core"]:
            assert tool in names, f"core tool {tool} missing"

    def test_core_present_for_workout_query(self):
        result = select_tools_for_message("Recommend me a workout", ALL_SCHEMAS)
        names = _names(result)
        for tool in TOOL_GROUPS["core"]:
            assert tool in names, f"core tool {tool} missing"


# ---------------------------------------------------------------------------
# Vitals / recovery / sleep
# ---------------------------------------------------------------------------

class TestVitalsIntent:

    def test_sleep_query_includes_vitals(self):
        result = select_tools_for_message("How did I sleep last night?", ALL_SCHEMAS)
        names = _names(result)
        assert "get_vitals" in names
        assert "get_body_composition" in names

    def test_sleep_query_excludes_ifit(self):
        result = select_tools_for_message("How did I sleep last night?", ALL_SCHEMAS)
        names = _names(result)
        assert "search_ifit_library" not in names
        assert "discover_ifit_series" not in names

    def test_sleep_query_excludes_setup(self):
        result = select_tools_for_message("How did I sleep last night?", ALL_SCHEMAS)
        names = _names(result)
        assert "garmin_authenticate" not in names
        assert "hevy_connect" not in names

    def test_sleep_query_excludes_treadmill(self):
        result = select_tools_for_message("How did I sleep last night?", ALL_SCHEMAS)
        names = _names(result)
        assert "list_treadmill_templates" not in names

    def test_recovery_includes_vitals(self):
        result = select_tools_for_message("How is my recovery?", ALL_SCHEMAS)
        assert "get_vitals" in _names(result)

    def test_hrv_includes_vitals(self):
        result = select_tools_for_message("What's my HRV trend?", ALL_SCHEMAS)
        assert "get_vitals" in _names(result)

    def test_how_am_i_doing_includes_vitals_and_fitness(self):
        result = select_tools_for_message("How am I doing today?", ALL_SCHEMAS)
        names = _names(result)
        assert "get_vitals" in names
        assert "get_fitness_summary" in names


# ---------------------------------------------------------------------------
# Workout recommendation
# ---------------------------------------------------------------------------

class TestRecommendIntent:

    def test_recommend_workout_includes_recommend_tools(self):
        result = select_tools_for_message("Recommend me a workout today", ALL_SCHEMAS)
        names = _names(result)
        assert "recommend_ifit_workout" in names
        assert "recommend_strength_workout" in names

    def test_recommend_includes_context_tools(self):
        result = select_tools_for_message("What should I do today?", ALL_SCHEMAS)
        names = _names(result)
        assert "get_vitals" in names
        assert "get_fitness_summary" in names
        assert "get_activities" in names

    def test_recommend_excludes_setup(self):
        result = select_tools_for_message("What workout should I do?", ALL_SCHEMAS)
        names = _names(result)
        assert "garmin_authenticate" not in names
        assert "get_onboarding_questions" not in names

    def test_what_should_i_do_tomorrow(self):
        result = select_tools_for_message("What should I do tomorrow?", ALL_SCHEMAS)
        names = _names(result)
        assert "recommend_ifit_workout" in names

    def test_ready_to_train(self):
        result = select_tools_for_message("Am I ready to train today?", ALL_SCHEMAS)
        names = _names(result)
        assert "get_vitals" in names
        assert "get_fitness_summary" in names

    def test_suggest_workout_includes_recommend(self):
        result = select_tools_for_message("Suggest a workout for today", ALL_SCHEMAS)
        names = _names(result)
        assert "recommend_ifit_workout" in names
        assert "recommend_strength_workout" in names

    def test_sync_and_suggest_includes_recommend(self):
        result = select_tools_for_message(
            "sync my profile and suggest a workout for today", ALL_SCHEMAS,
        )
        names = _names(result)
        assert "recommend_ifit_workout" in names
        assert "sync_data" in names

    def test_give_me_a_workout(self):
        result = select_tools_for_message("Give me a workout", ALL_SCHEMAS)
        names = _names(result)
        assert "recommend_ifit_workout" in names

    def test_plan_my_workout(self):
        result = select_tools_for_message("Plan my workout for today", ALL_SCHEMAS)
        names = _names(result)
        assert "recommend_ifit_workout" in names


# ---------------------------------------------------------------------------
# Strength / Hevy
# ---------------------------------------------------------------------------

class TestStrengthIntent:

    def test_strength_includes_hevy_tools(self):
        result = select_tools_for_message("Show my strength sessions", ALL_SCHEMAS)
        names = _names(result)
        assert "get_strength_sessions" in names
        assert "get_workout_summary" in names

    def test_bench_press_includes_weight_recs(self):
        result = select_tools_for_message("How much should I bench today?", ALL_SCHEMAS)
        names = _names(result)
        assert "get_routine_weight_recommendations" in names
        assert "get_strength_sessions" in names

    def test_routine_query(self):
        result = select_tools_for_message("Show me my Hevy routines", ALL_SCHEMAS)
        names = _names(result)
        assert "manage_hevy_routines" in names

    def test_strength_excludes_weather(self):
        result = select_tools_for_message("Show my strength sessions", ALL_SCHEMAS)
        names = _names(result)
        assert "check_weather" not in names


# ---------------------------------------------------------------------------
# iFit
# ---------------------------------------------------------------------------

class TestIfitIntent:

    def test_ifit_includes_library_tools(self):
        result = select_tools_for_message("Search iFit for a yoga workout", ALL_SCHEMAS)
        names = _names(result)
        assert "search_ifit_library" in names
        assert "get_ifit_workout_details" in names
        assert "search_ifit_programs" in names

    def test_ifit_includes_series_tools(self):
        result = select_tools_for_message("Show me iFit programs", ALL_SCHEMAS)
        names = _names(result)
        assert "discover_ifit_series" in names
        assert "get_ifit_program_details" in names


# ---------------------------------------------------------------------------
# Setup / connect
# ---------------------------------------------------------------------------

class TestSetupIntent:

    def test_garmin_connect_includes_auth(self):
        result = select_tools_for_message("Connect my Garmin", ALL_SCHEMAS)
        names = _names(result)
        assert "garmin_auth_status" in names
        assert "garmin_authenticate" in names

    def test_setup_includes_onboarding(self):
        result = select_tools_for_message("Set up my profile", ALL_SCHEMAS)
        names = _names(result)
        assert "get_onboarding_questions" in names
        assert "update_athlete_profile" in names

    def test_setup_excludes_ifit(self):
        result = select_tools_for_message("Connect my Garmin", ALL_SCHEMAS)
        names = _names(result)
        assert "search_ifit_library" not in names


# ---------------------------------------------------------------------------
# Weather / outdoor
# ---------------------------------------------------------------------------

class TestWeatherIntent:

    def test_weather_includes_weather_tools(self):
        result = select_tools_for_message("What's the weather like for a run?", ALL_SCHEMAS)
        names = _names(result)
        assert "check_weather" in names
        assert "recommend_outdoor_run" in names

    def test_outdoor_route(self):
        result = select_tools_for_message("Find me an outdoor route", ALL_SCHEMAS)
        names = _names(result)
        assert "recommend_outdoor_run" in names


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

class TestSyncIntent:

    def test_sync_includes_sync_tool(self):
        result = select_tools_for_message("Sync my data", ALL_SCHEMAS)
        names = _names(result)
        assert "sync_data" in names

    def test_sync_excludes_most_tools(self):
        result = select_tools_for_message("Sync my data", ALL_SCHEMAS)
        assert len(result) < 10


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

class TestKnowledgeIntent:

    def test_book_search_includes_rag(self):
        result = select_tools_for_message(
            "What does the research say about periodization?", ALL_SCHEMAS,
        )
        names = _names(result)
        assert "search_knowledge_base" in names

    def test_knowledge_excludes_most_tools(self):
        result = select_tools_for_message(
            "Search my books for VO2max training", ALL_SCHEMAS,
        )
        assert len(result) < 15


# ---------------------------------------------------------------------------
# Action items
# ---------------------------------------------------------------------------

class TestActionItemsIntent:

    def test_action_items_included(self):
        result = select_tools_for_message("Show my action items", ALL_SCHEMAS)
        names = _names(result)
        assert "get_action_items" in names
        assert "add_action_item" in names
        assert "update_action_item" in names
        assert "complete_action_item" in names


# ---------------------------------------------------------------------------
# Multi-intent (union of groups)
# ---------------------------------------------------------------------------

class TestMultiIntent:

    def test_sleep_and_training_unions(self):
        result = select_tools_for_message(
            "How did I sleep and what did I do yesterday?", ALL_SCHEMAS,
        )
        names = _names(result)
        assert "get_vitals" in names
        assert "get_activities" in names

    def test_strength_and_recommend(self):
        result = select_tools_for_message(
            "Recommend me a strength workout", ALL_SCHEMAS,
        )
        names = _names(result)
        assert "recommend_strength_workout" in names
        assert "get_strength_sessions" in names
        assert "get_routine_weight_recommendations" in names


# ---------------------------------------------------------------------------
# Significant reduction
# ---------------------------------------------------------------------------

class TestTokenReduction:

    def test_sleep_query_significant_reduction(self):
        result = select_tools_for_message("How did I sleep?", ALL_SCHEMAS)
        assert len(result) < len(ALL_SCHEMAS) * 0.5

    def test_sync_query_significant_reduction(self):
        result = select_tools_for_message("Sync my data please", ALL_SCHEMAS)
        assert len(result) < len(ALL_SCHEMAS) * 0.3

    def test_fitness_query_significant_reduction(self):
        result = select_tools_for_message("What's my CTL?", ALL_SCHEMAS)
        assert len(result) < len(ALL_SCHEMAS) * 0.5

    def test_recommend_still_reduced(self):
        """Even the broadest intent should be meaningfully smaller."""
        result = select_tools_for_message("Recommend me a workout", ALL_SCHEMAS)
        assert len(result) < len(ALL_SCHEMAS) * 0.7


# ---------------------------------------------------------------------------
# Tool groups coverage
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Caller wiring
# ---------------------------------------------------------------------------

class TestCallerWiring:

    def test_chat_app_uses_select_tools_for_message(self):
        import inspect
        from scripts import chat_app
        source = inspect.getsource(chat_app.on_message)
        assert "select_tools_for_message" in source, (
            "chat_app.on_message must use select_tools_for_message "
            "for per-request tool filtering"
        )

    def test_telegram_uses_select_tools_for_message(self):
        import inspect
        from scripts import telegram_bot
        source = inspect.getsource(telegram_bot.handle_message)
        assert "select_tools_for_message" in source, (
            "telegram_bot.handle_message must use select_tools_for_message "
            "for per-request tool filtering"
        )


# ---------------------------------------------------------------------------
# Tool group integrity
# ---------------------------------------------------------------------------

class TestToolGroupIntegrity:

    def test_every_group_is_a_frozenset(self):
        for name, group in TOOL_GROUPS.items():
            assert isinstance(group, frozenset), f"{name} should be frozenset"

    def test_core_group_exists(self):
        assert "core" in TOOL_GROUPS
        assert len(TOOL_GROUPS["core"]) >= 1

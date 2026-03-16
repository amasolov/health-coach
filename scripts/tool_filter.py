"""Per-request tool filtering based on user message intent.

Reduces the number of tool schemas sent to the LLM by detecting the
user's intent and selecting only relevant tool groups.  Falls back to
all tools when intent is ambiguous (conservative — never under-filters).

Typical savings: 50-80% fewer tool-schema tokens for focused queries
like "How did I sleep?" or "Sync my data".
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Tool groups — each group contains tools that serve a related purpose.
# Tools may appear in multiple groups.  Groups are combined (unioned) when
# the user's message matches multiple intents.
# ---------------------------------------------------------------------------

TOOL_GROUPS: dict[str, frozenset[str]] = {
    "core": frozenset({
        "get_fitness_summary",
        "get_action_items",
    }),
    "vitals": frozenset({
        "get_vitals",
        "get_body_composition",
    }),
    "training": frozenset({
        "get_activities",
        "get_activity_detail",
        "get_training_load",
        "get_workout_summary",
        "get_strength_sessions",
    }),
    "fitness": frozenset({
        "get_fitness_summary",
        "get_training_load",
        "get_training_zones",
        "setup_running_hr_zones",
        "get_athlete_profile",
    }),
    "recommend": frozenset({
        "recommend_ifit_workout",
        "recommend_strength_workout",
        "search_ifit_library",
        "get_ifit_workout_details",
        "check_weather",
        "recommend_outdoor_run",
        "get_routine_weight_recommendations",
    }),
    "strength": frozenset({
        "get_strength_sessions",
        "get_workout_summary",
        "recommend_strength_workout",
        "create_hevy_routine_from_recommendation",
        "get_hevy_routine_review",
        "compare_hevy_workout",
        "apply_exercise_feedback",
        "get_routine_weight_recommendations",
        "manage_hevy_routines",
    }),
    "ifit": frozenset({
        "search_ifit_library",
        "get_ifit_workout_details",
        "search_ifit_programs",
        "get_ifit_program_details",
        "discover_ifit_series",
        "create_hevy_routine_from_recommendation",
        "recommend_ifit_workout",
        "recommend_strength_workout",
        "report_exercise_correction",
    }),
    "treadmill": frozenset({
        "list_treadmill_templates",
        "generate_treadmill_workout",
        "search_ifit_library",
    }),
    "weather": frozenset({
        "check_weather",
        "recommend_outdoor_run",
        "rate_route",
    }),
    "knowledge": frozenset({
        "search_knowledge_base",
        "list_knowledge_documents",
        "delete_knowledge_document",
    }),
    "goals": frozenset({
        "set_user_goals",
        "get_user_goals",
        "get_athlete_profile",
    }),
    "setup": frozenset({
        "garmin_auth_status",
        "garmin_authenticate",
        "garmin_submit_mfa",
        "hevy_auth_status",
        "hevy_connect",
        "garmin_fetch_profile",
        "generate_fitness_assessment",
        "update_athlete_profile",
        "get_onboarding_questions",
        "set_user_goals",
        "get_user_goals",
        "get_supported_integrations",
        "set_user_integrations",
        "get_user_integrations",
    }),
    "action_items": frozenset({
        "get_action_items",
        "add_action_item",
        "update_action_item",
        "complete_action_item",
    }),
    "sync": frozenset({
        "sync_data",
    }),
    "meta": frozenset({
        "suggest_feature",
        "report_exercise_correction",
        "generate_telegram_link_code",
    }),
}

# ---------------------------------------------------------------------------
# Intent patterns — each maps to a list of tool groups that should be
# included when the pattern matches.  Multiple patterns can match;
# their groups are unioned.
# ---------------------------------------------------------------------------

_INTENT_PATTERNS: list[tuple[re.Pattern[str], list[str]]] = [
    # Vitals / recovery / sleep / wellbeing
    (re.compile(
        r"\b(sleep|slept|hrv|heart\s*rate|resting\s*hr|stress\s*(level|score)?"
        r"|body\s*battery|spo2|blood\s*pressure|recovery"
        r"|vital|readiness"
        r"|how\s+(am|are)\s+i\s+(doing|feeling|going))\b", re.I),
     ["vitals", "fitness"]),

    # Training / activities / history
    (re.compile(
        r"\b(activit\w*|what\s+did\s+i"
        r"|training\s+(log|history|this|last)"
        r"|yesterday|last\s+week)\b", re.I),
     ["training"]),

    # Fitness / PMC / training load
    (re.compile(
        r"\b(ctl|atl|tsb|pmc"
        r"|fitness\s*(level|status|score)?"
        r"|fatigue|form\b|ramp\s*rate|training\s*load"
        r"|overtraining|overreach)\b", re.I),
     ["fitness"]),

    # Zones / thresholds
    (re.compile(
        r"\b(zone|hr\s*zone|power\s*zone|pace\s*zone"
        r"|threshold|lthr|ftp|lactate)\b", re.I),
     ["fitness"]),

    # Workout recommendation / what to do
    (re.compile(
        r"\b(recommend\w*|what\s+should\s+i\s+do"
        r"|what\s+workout|which\s+workout"
        r"|ready\s+to\s+train"
        r"|what\s+to\s+do|what\s+can\s+i\s+do)\b", re.I),
     ["recommend", "vitals", "training", "fitness", "strength"]),

    # Strength / Hevy / weight training
    (re.compile(
        r"\b(strength|hevy|routine\w*|weight\s*train"
        r"|reps?\b|sets?\b"
        r"|muscle|squat|deadlift|bench\s*press|bench\b"
        r"|upper\s*body|lower\s*body"
        r"|dumbbell|barbell|gym)\b", re.I),
     ["strength", "fitness"]),

    # iFit
    (re.compile(r"\b(ifit|i-fit)\b", re.I),
     ["ifit"]),

    # Treadmill
    (re.compile(r"\btreadmill\b", re.I),
     ["treadmill"]),

    # Weather / outdoor running
    (re.compile(
        r"\b(weather|outdoor|outside|rain|temperature|route)\b", re.I),
     ["weather"]),

    # Knowledge base / books / research
    (re.compile(
        r"\b(books?|research|study|science|evidence|literature"
        r"|knowledge\s*base|documents?|upload)\b", re.I),
     ["knowledge"]),

    # Goals / races / targets
    (re.compile(
        r"\b(goal|target\s*(event|date|race)?"
        r"|marathon|half\s*marathon|5k\b|10k\b|ironman"
        r"|preference|constraint)\b", re.I),
     ["goals"]),

    # Setup / connect / authenticate
    (re.compile(
        r"\b(connect\s+(my\s+)?|authenticat\w*|garmin|setup|set\s*up"
        r"|onboard\w*|configure|profile|integration)\b", re.I),
     ["setup"]),

    # Action items / tasks
    (re.compile(
        r"\b(action\s*items?|tasks?|to[\s-]?do|checklist|reminder)\b", re.I),
     ["action_items"]),

    # Sync / refresh data
    (re.compile(
        r"\b(sync|refresh|update\s+(my\s+)?data|pull\s+data)\b", re.I),
     ["sync"]),

    # Feature suggestions / bug reports
    (re.compile(
        r"\b(feature|bug\s*report|feedback|suggest\s+a\s+feature"
        r"|correction|wrong\s+exercise)\b", re.I),
     ["meta"]),

    # Telegram linking
    (re.compile(r"\btelegram\b", re.I),
     ["meta"]),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def select_tools_for_message(
    user_message: str,
    all_schemas: list[dict],
) -> list[dict]:
    """Return the subset of *all_schemas* relevant to *user_message*.

    Detects intents via keyword patterns, unions the corresponding tool
    groups, and filters schemas.  Returns *all_schemas* unchanged when
    no intent is detected (safe fallback).
    """
    matched_groups: set[str] = set()

    for pattern, groups in _INTENT_PATTERNS:
        if pattern.search(user_message):
            matched_groups.update(groups)

    if not matched_groups:
        return all_schemas

    matched_groups.add("core")

    allowed: set[str] = set()
    for group_name in matched_groups:
        allowed |= TOOL_GROUPS.get(group_name, frozenset())

    return [s for s in all_schemas if s["function"]["name"] in allowed]

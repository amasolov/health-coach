"""
OpenAI function-calling tool schemas for the health tracker.

Each entry maps to a function in scripts.health_tools.  The Chainlit
chat app sends these schemas to the LLM and dispatches tool_calls back
to the corresponding health_tools function.

Tools are split into two categories based on the user identifier they need:
  - uid tools:  require the numeric user_id (DB queries)
  - slug tools: require the string user_slug (YAML config lookups)

Some tools also need credentials (garmin_email, garmin_password, hevy_api_key)
which are injected by the caller.
"""

from __future__ import annotations

from scripts import health_tools

# ---------------------------------------------------------------------------
# Schema definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    # ===== FITNESS / PMC =====
    {
        "type": "function",
        "function": {
            "name": "get_fitness_summary",
            "description": "Get current fitness status: CTL (fitness), ATL (fatigue), TSB (form), ramp rate, plain-language interpretation, and 8-week CTL projection.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_training_load",
            "description": "Get daily TSS, CTL, ATL, TSB for a date range. Defaults to last 90 days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Start date (YYYY-MM-DD). Omit for automatic."},
                    "end_date": {"type": "string", "description": "End date (YYYY-MM-DD). Omit for automatic."},
                    "days": {"type": "integer", "description": "Lookback days if start_date omitted. Default 90."},
                },
                "required": [],
            },
        },
    },
    # ===== ACTIVITIES =====
    {
        "type": "function",
        "function": {
            "name": "get_activities",
            "description": "List activities with metrics. Filter by date range and/or sport type (running, cycling, strength_training, etc.). Defaults to last 30 days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "days": {"type": "integer", "description": "Lookback days. Default 30."},
                    "sport": {"type": "string", "description": "Filter by sport type (partial match)."},
                    "limit": {"type": "integer", "description": "Max results. Default 50."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_activity_detail",
            "description": "Get full detail for a single activity by its timestamp (ISO format).",
            "parameters": {
                "type": "object",
                "properties": {
                    "activity_time": {"type": "string", "description": "Activity timestamp in ISO format."},
                },
                "required": ["activity_time"],
            },
        },
    },
    # ===== BODY COMPOSITION =====
    {
        "type": "function",
        "function": {
            "name": "get_body_composition",
            "description": "Get body composition trend (weight, body fat %, muscle mass, BMI) over a date range. Defaults to last 90 days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "days": {"type": "integer", "description": "Lookback days. Default 90."},
                },
                "required": [],
            },
        },
    },
    # ===== VITALS =====
    {
        "type": "function",
        "function": {
            "name": "get_vitals",
            "description": "Get daily vitals (resting HR, HRV, blood pressure, sleep, stress, body battery, SpO2). Defaults to last 30 days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "days": {"type": "integer", "description": "Lookback days. Default 30."},
                },
                "required": [],
            },
        },
    },
    # ===== ZONES & PROFILE =====
    {
        "type": "function",
        "function": {
            "name": "get_training_zones",
            "description": "Get current training zones: heart rate, running power, cycling power, and running pace with absolute lower/upper bounds.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_athlete_profile",
            "description": "Get the athlete's profile: goals, thresholds, body composition, training status, and treadmill zone-to-speed mapping.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ===== STRENGTH =====
    {
        "type": "function",
        "function": {
            "name": "get_strength_sessions",
            "description": "Get strength training sets from Hevy. Filter by date range and/or exercise name (partial match). Defaults to last 30 days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "days": {"type": "integer", "description": "Lookback days. Default 30."},
                    "exercise": {"type": "string", "description": "Filter by exercise name (partial match)."},
                },
                "required": [],
            },
        },
    },
    # ===== TREADMILL =====
    {
        "type": "function",
        "function": {
            "name": "list_treadmill_templates",
            "description": "List available treadmill workout templates with name, duration, and step count.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_treadmill_workout",
            "description": "Generate a structured treadmill workout from a template. Returns step-by-step table with speed, incline, duration, and distance for iFit Workout Creator.",
            "parameters": {
                "type": "object",
                "properties": {
                    "template_key": {"type": "string", "description": "Template key from list_treadmill_templates."},
                },
                "required": ["template_key"],
            },
        },
    },
    # ===== GARMIN AUTH =====
    {
        "type": "function",
        "function": {
            "name": "garmin_auth_status",
            "description": "Check whether Garmin Connect authentication is set up and tokens are valid.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "garmin_authenticate",
            "description": "Start Garmin Connect authentication. If MFA is required, returns a prompt to call garmin_submit_mfa.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "garmin_submit_mfa",
            "description": "Complete Garmin Connect MFA authentication with a code received via email or authenticator app.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mfa_code": {"type": "string", "description": "The MFA code."},
                },
                "required": ["mfa_code"],
            },
        },
    },
    # ===== PROFILE SETUP =====
    {
        "type": "function",
        "function": {
            "name": "garmin_fetch_profile",
            "description": "Fetch athlete profile from Garmin Connect and merge into config. Auto-populates DOB, sex, height, body composition, resting HR, max HR, VO2max, lactate threshold, FTP. Only fills null fields.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_fitness_assessment",
            "description": "Generate a comprehensive 6-month fitness assessment from Garmin Connect (and optionally Hevy). Returns training overview, endurance metrics, intensity analysis, body composition, vitals, and recommendations. Recommended FIRST tool for new users.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lookback_days": {"type": "integer", "description": "Days of history to analyze. Default 180."},
                    "include_hevy": {"type": "boolean", "description": "Include Hevy strength data. Default true."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_athlete_profile",
            "description": "Update a single field in the athlete profile. field_path is dot-separated (e.g. thresholds.heart_rate.max_hr, body.weight_kg).",
            "parameters": {
                "type": "object",
                "properties": {
                    "field_path": {"type": "string", "description": "Dot-separated path relative to user (e.g. thresholds.cycling.ftp)."},
                    "value": {"description": "The value to set (number or string)."},
                },
                "required": ["field_path", "value"],
            },
        },
    },
    # ===== GOALS & ONBOARDING =====
    {
        "type": "function",
        "function": {
            "name": "get_onboarding_questions",
            "description": "Get onboarding questions for a new user about goals, preferences, and constraints. Returns answered and unanswered questions.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_user_goals",
            "description": "Store goals, preferences, and constraints. Keys: primary_goal, target_event, target_date, secondary_goals, available_hours_per_week, preferred_sports, constraints, experience_level, training_preferences.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goals": {
                        "type": "object",
                        "description": "Dict of goal keys to values. Only provided keys are updated.",
                    },
                },
                "required": ["goals"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_goals",
            "description": "Get the user's current goals, preferences, and constraints.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ===== ACTION ITEMS =====
    {
        "type": "function",
        "function": {
            "name": "get_action_items",
            "description": "Get action items grouped by priority. Call at the START of every conversation to review outstanding tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status_filter": {"type": "string", "description": "Filter: pending, in_progress, completed, or blank for all."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_action_item",
            "description": "Add a new action item. Categories: testing, habit, equipment, training, setup, nutrition. Priority: high, medium, low.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {"type": "string", "description": "testing, habit, equipment, training, setup, nutrition. Default: training."},
                    "priority": {"type": "string", "description": "high, medium, low. Default: medium."},
                    "due": {"type": "string", "description": "Optional YYYY-MM-DD deadline."},
                },
                "required": ["title", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_action_item",
            "description": "Update an action item: change status, priority, title, description, due date, or add a note.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "status": {"type": "string", "description": "pending, in_progress, completed, skipped."},
                    "priority": {"type": "string", "description": "high, medium, low."},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "due": {"type": "string", "description": "YYYY-MM-DD"},
                    "note": {"type": "string", "description": "Note to append."},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_action_item",
            "description": "Mark an action item as completed. Optionally add a completion note (e.g. 'LTHR measured at 168 bpm').",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "note": {"type": "string", "description": "Optional completion note."},
                },
                "required": ["item_id"],
            },
        },
    },
    # ===== INTEGRATIONS =====
    {
        "type": "function",
        "function": {
            "name": "get_supported_integrations",
            "description": "List all supported hardware and software integrations. Used during onboarding. Optional category filter: wearable, cycling, treadmill, health, gym, software.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Filter by category."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_user_integrations",
            "description": "Store which integrations/hardware the user has.",
            "parameters": {
                "type": "object",
                "properties": {
                    "integrations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of integration IDs.",
                    },
                    "equipment_notes": {
                        "type": "object",
                        "description": "Optional dict of integration_id -> note (e.g. 'Wahoo Kickr v5').",
                    },
                },
                "required": ["integrations"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_integrations",
            "description": "Get the user's configured integrations and hardware.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ===== iFit STRENGTH RECOMMENDATIONS =====
    {
        "type": "function",
        "function": {
            "name": "recommend_strength_workout",
            "description": (
                "Run the iFit strength workout recommendation engine. "
                "Analyses athlete's current TSB, vitals, muscle load, goals, "
                "and iFit preferences to suggest 3 optimal strength workouts "
                "from the iFit library with full exercise breakdowns. "
                "Uses VTT caption analysis via LLM for deep scoring."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_hevy_routine_from_recommendation",
            "description": (
                "Create a Hevy routine from a previously generated iFit "
                "strength recommendation. Run recommend_strength_workout "
                "first, then use the recommendation_index (0-based) to "
                "pick which one to create as a Hevy routine."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recommendation_index": {
                        "type": "integer",
                        "description": "0-based index of the recommendation to use (0, 1, or 2).",
                    },
                },
                "required": ["recommendation_index"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatch: tool_name -> (function, param_kind)
#
# param_kind indicates what user identifier the function needs:
#   "uid"   -> pass user_id (int) as first arg
#   "slug"  -> pass user_slug (str) as first arg
#   "none"  -> no user context needed
#   "creds" -> special handling (garmin/hevy credentials)
# ---------------------------------------------------------------------------

TOOL_DISPATCH: dict[str, tuple] = {
    # (function, param_kind)
    "get_fitness_summary":          (health_tools.get_fitness_summary, "uid"),
    "get_training_load":            (health_tools.get_training_load, "uid"),
    "get_activities":               (health_tools.get_activities, "uid"),
    "get_activity_detail":          (health_tools.get_activity_detail, "uid"),
    "get_body_composition":         (health_tools.get_body_composition, "uid"),
    "get_vitals":                   (health_tools.get_vitals, "uid"),
    "get_training_zones":           (health_tools.get_training_zones, "slug"),
    "get_athlete_profile":          (health_tools.get_athlete_profile, "slug"),
    "get_strength_sessions":        (health_tools.get_strength_sessions, "uid"),
    "list_treadmill_templates":     (health_tools.list_treadmill_templates, "none"),
    "generate_treadmill_workout":   (health_tools.generate_treadmill_workout, "slug"),
    "garmin_auth_status":           (health_tools.garmin_auth_status, "creds"),
    "garmin_authenticate":          (health_tools.garmin_authenticate, "creds"),
    "garmin_submit_mfa":            (health_tools.garmin_submit_mfa, "slug"),
    "garmin_fetch_profile":         (health_tools.garmin_fetch_profile, "slug"),
    "generate_fitness_assessment":  (health_tools.generate_fitness_assessment, "creds"),
    "update_athlete_profile":       (health_tools.update_athlete_profile, "slug"),
    "get_onboarding_questions":     (health_tools.get_onboarding_questions, "slug"),
    "set_user_goals":               (health_tools.set_user_goals, "slug"),
    "get_user_goals":               (health_tools.get_user_goals, "slug"),
    "get_action_items":             (health_tools.get_action_items, "slug"),
    "add_action_item":              (health_tools.add_action_item, "slug"),
    "update_action_item":           (health_tools.update_action_item, "slug"),
    "complete_action_item":         (health_tools.complete_action_item, "slug"),
    "get_supported_integrations":   (health_tools.get_supported_integrations, "none"),
    "set_user_integrations":        (health_tools.set_user_integrations, "slug"),
    "get_user_integrations":        (health_tools.get_user_integrations, "slug"),
    # iFit strength recommendations
    "recommend_strength_workout":   (health_tools.recommend_strength_workout, "slug"),
    "create_hevy_routine_from_recommendation": (health_tools.create_hevy_routine_from_recommendation, "creds"),
}

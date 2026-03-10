"""
OpenAI function-calling tool schemas for the Health Coach.

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
            "name": "setup_running_hr_zones",
            "description": (
                "Analyse available data (LTHR, max HR, resting HR, recent hard runs, age) "
                "and recommend running heart rate zones using the best possible estimation method. "
                "Returns zone boundaries in BPM, Garmin watch setup instructions, and recommendations "
                "to improve accuracy. Call this when the user wants to set up, review, or update their "
                "running HR zones, or asks about configuring their Garmin watch HR zones."
            ),
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
    # ===== UNIFIED WORKOUT VIEW =====
    {
        "type": "function",
        "function": {
            "name": "get_workout_summary",
            "description": (
                "Get strength workouts merged across Garmin (HR, duration, calories) "
                "and Hevy (exercises, sets, reps, weight). A single workout tracked on "
                "both platforms appears as one record with combined data from both sources. "
                "Defaults to last 7 days."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "days": {"type": "integer", "description": "Lookback days. Default 7."},
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
    # ===== FEATURE SUGGESTIONS =====
    {
        "type": "function",
        "function": {
            "name": "suggest_feature",
            "description": (
                "Open a GitHub issue to suggest a new feature, report a bug, or ask a question. "
                "Use when the user says they want to suggest something, request a feature, report a problem, or give feedback."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short, clear issue title.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Full description of the suggestion or problem.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Issue type: 'enhancement' (default), 'bug', or 'question'.",
                    },
                },
                "required": ["title", "description"],
            },
        },
    },
    # ===== EXERCISE DATA CORRECTION =====
    {
        "type": "function",
        "function": {
            "name": "report_exercise_correction",
            "description": (
                "Report incorrect exercise data for an iFit workout. "
                "Use when the user says the extracted exercises for a workout "
                "are wrong, incomplete, or inaccurate. Opens a GitHub issue "
                "with the current data and user feedback for review."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workout_id": {
                        "type": "string",
                        "description": "The iFit workout ID with incorrect exercises.",
                    },
                    "feedback": {
                        "type": "string",
                        "description": (
                            "User's description of what's wrong and what the "
                            "correct exercises should be."
                        ),
                    },
                },
                "required": ["workout_id", "feedback"],
            },
        },
    },
    # ===== SYNC =====
    {
        "type": "function",
        "function": {
            "name": "sync_data",
            "description": (
                "Trigger an immediate data sync from Garmin Connect and Hevy. "
                "Use when the user asks to sync, refresh, or update their data. "
                "Set full_sync=true when the user says data is missing, sync returned 0 items, "
                "or they want to pull their full history — this ignores the incremental start "
                "date and fetches everything from the beginning of time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "full_sync": {
                        "type": "boolean",
                        "description": "If true, fetch all historical data instead of just new records since last sync. Use when normal sync returns 0 but data is expected.",
                    },
                },
                "required": [],
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
    # ===== iFit INTEGRATION =====
    {
        "type": "function",
        "function": {
            "name": "recommend_ifit_workout",
            "description": (
                "Recommend today's iFit workout based on recent 14-day activity "
                "history, muscle group fatigue, and variety. Returns top 5 "
                "ranked workouts from the user's up-next queue, favorites, and "
                "iFit recommendations. Covers ALL workout types: running, "
                "strength, cycling, yoga, recovery, etc."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_ifit_library",
            "description": (
                "Search the iFit workout library (12,000+ workouts) by title, "
                "trainer name, category, or keyword. Use this when the user "
                "asks about specific iFit programs, series, trainers, or "
                "workout types. Returns workout details, ratings, and metadata."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query: workout name, trainer name, series title, or keywords.",
                    },
                    "workout_type": {
                        "type": "string",
                        "description": "Optional type filter: 'run', 'strength', 'cycling', 'yoga', etc.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ifit_workout_details",
            "description": (
                "Get detailed info about a specific iFit workout by its ID. "
                "Returns description, trainer info, muscle groups, difficulty, "
                "duration, equipment needed, and ratings. Use after "
                "search_ifit_library or recommend_ifit_workout to get more "
                "details about a specific workout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workout_id": {
                        "type": "string",
                        "description": "The iFit workout ID.",
                    },
                },
                "required": ["workout_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_strength_workout",
            "description": (
                "Run the iFit strength workout recommendation engine (deep "
                "analysis). Analyses athlete's current TSB, vitals, muscle "
                "load, goals, and iFit preferences to suggest 3 optimal "
                "strength workouts from the iFit library with full exercise "
                "breakdowns. Uses VTT caption analysis via LLM for deep "
                "scoring. Use this for strength-specific recommendations; "
                "for general workout recommendations use recommend_ifit_workout."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_ifit_programs",
            "description": (
                "Search the iFit program/series index by name, trainer, or "
                "keyword. Returns matching programs with their workout lists. "
                "Use when the user asks about iFit series, programs, or "
                "training plans."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query: program name, trainer name, or keywords.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ifit_program_details",
            "description": (
                "Get detailed info about a specific iFit program/series by "
                "its series ID. Returns the program overview, trainers, and "
                "full workout list. Use after search_ifit_programs to get "
                "more details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "series_id": {
                        "type": "string",
                        "description": "The iFit series/program ID.",
                    },
                },
                "required": ["series_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_ifit_series",
            "description": (
                "Discover all series/programs a workout belongs to and map "
                "every workout in those series. Use this when a user asks "
                "about an iFit series or program and you have a workout ID "
                "from that series. Returns full workout lists for each "
                "discovered series."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workout_id": {
                        "type": "string",
                        "description": "An iFit workout ID from the series to discover.",
                    },
                },
                "required": ["workout_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_hevy_routine_from_recommendation",
            "description": (
                "Create a Hevy routine from an iFit workout. Pass "
                "ifit_workout_id ONLY if you obtained it from a tool call "
                "in THIS conversation (search_ifit_library, "
                "get_ifit_workout_details, recommend_strength_workout, etc). "
                "NEVER guess or recall IDs from memory — they will 404. "
                "If you don't have a confirmed ID, pass workout_title "
                "instead and the tool will search for the workout. "
                "ALWAYS pass workout_title as a fallback."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ifit_workout_id": {
                        "type": "string",
                        "description": (
                            "The iFit workout ID — ONLY from a tool response "
                            "in this conversation. Never guess."
                        ),
                    },
                    "workout_title": {
                        "type": "string",
                        "description": (
                            "The workout title (e.g. 'Week 2 - Upper Body "
                            "Pull'). ALWAYS pass this. Used as fallback when "
                            "ID is missing or invalid."
                        ),
                    },
                },
                "required": ["workout_title"],
            },
        },
    },
    # ===== iFit ↔ Hevy FEEDBACK LOOP =====
    {
        "type": "function",
        "function": {
            "name": "get_hevy_routine_review",
            "description": (
                "Review an iFit-to-Hevy routine conversion. Shows the predicted "
                "exercises that were sent to Hevy alongside the current stored "
                "exercise data from the iFit transcript. Use when a user wants "
                "to review or verify what was converted, or before/after doing "
                "the workout in Hevy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ifit_workout_id": {
                        "type": "string",
                        "description": "The iFit workout ID to review. Provide this OR hevy_routine_id.",
                    },
                    "hevy_routine_id": {
                        "type": "string",
                        "description": "The Hevy routine ID to review. Provide this OR ifit_workout_id.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_hevy_workout",
            "description": (
                "Compare a completed Hevy workout with the iFit-predicted "
                "exercises. Detects which Hevy workouts came from iFit routine "
                "conversions and shows differences between predicted and actual "
                "exercises, sets, reps, and weights. Use when a user says they "
                "completed an iFit-based workout in Hevy and wants to check "
                "accuracy, or to proactively find discrepancies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hevy_workout_id": {
                        "type": "string",
                        "description": "Specific Hevy workout ID to compare. If omitted, scans recent workouts.",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Look back N days for iFit-sourced workouts. Default 7.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_exercise_feedback",
            "description": (
                "Apply user corrections to stored iFit exercise data. Updates "
                "the exercise extraction for a workout so future Hevy routine "
                "conversions use the corrected data. Use when a user says an "
                "exercise name is wrong, sets/reps are incorrect, an exercise "
                "is missing, or an exercise should be removed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ifit_workout_id": {
                        "type": "string",
                        "description": "The iFit workout ID to correct.",
                    },
                    "corrections": {
                        "type": "array",
                        "description": "List of corrections to apply.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "description": "Type of correction: 'update', 'add', or 'remove'.",
                                },
                                "exercise_name": {
                                    "type": "string",
                                    "description": "Name of the exercise to update or remove.",
                                },
                                "new_name": {
                                    "type": "string",
                                    "description": "Corrected exercise name (for update/add).",
                                },
                                "sets": {
                                    "type": "integer",
                                    "description": "Corrected number of sets.",
                                },
                                "reps": {
                                    "type": "string",
                                    "description": "Corrected reps (e.g. '12' or '30s').",
                                },
                                "weight": {
                                    "type": "string",
                                    "description": "Corrected weight hint (e.g. 'dumbbell 15lb').",
                                },
                                "muscle_group": {
                                    "type": "string",
                                    "description": "Corrected primary muscle group.",
                                },
                            },
                            "required": ["action", "exercise_name"],
                        },
                    },
                },
                "required": ["ifit_workout_id", "corrections"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_routine_weight_recommendations",
            "description": (
                "Recommend specific weights, reps, and sets for each exercise in "
                "a user's Hevy routine based on their training history, progression "
                "trends, current fatigue (TSB, HRV, cardio leg stress), and "
                "progressive overload principles. If no routine is specified, "
                "lists all available routines so the user can pick one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "routine_id": {
                        "type": "string",
                        "description": "Hevy routine ID. If omitted, lists available routines.",
                    },
                    "routine_name": {
                        "type": "string",
                        "description": "Partial name match for the routine (e.g. 'upper body').",
                    },
                },
                "required": [],
            },
        },
    },
    # ===== TELEGRAM LINKING =====
    {
        "type": "function",
        "function": {
            "name": "generate_telegram_link_code",
            "description": (
                "Generate a one-time code for linking the user's Telegram account "
                "to Health Coach. The user sends /start <CODE> to the Telegram bot "
                "within 10 minutes to complete the link. Use when the user asks to "
                "connect or link their Telegram."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ===== KNOWLEDGE BASE (RAG) =====
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": (
                "Search uploaded fitness books and documents for relevant passages. "
                "Use when the user asks about training methodologies, periodisation, "
                "exercise science, nutrition principles, or references a specific book. "
                "Returns the most relevant text passages with source and page number."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query — describe what information you need.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of passages to return (default 5, max 10).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_knowledge_documents",
            "description": "List all fitness books and documents in the knowledge base.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_knowledge_document",
            "description": "Remove a document from the knowledge base by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "integer",
                        "description": "The document ID to delete (from list_knowledge_documents).",
                    },
                },
                "required": ["document_id"],
            },
        },
    },
    # ===== HEVY ROUTINE MANAGEMENT =====
    {
        "type": "function",
        "function": {
            "name": "manage_hevy_routines",
            "description": (
                "List, rename, or clean up duplicate Hevy routines. "
                "The public Hevy API does not support deletion, so duplicates "
                "are marked with a '[DELETE] ' prefix for manual removal. "
                "Use action='list' to see all routines. "
                "Use action='rename' with routine_id and new_title to rename one. "
                "Use action='mark_duplicates' to prefix duplicate routines "
                "with '[DELETE] ' so the user can easily remove them in the Hevy app."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "rename", "mark_duplicates"],
                        "description": "The action to perform.",
                    },
                    "routine_id": {
                        "type": "string",
                        "description": "The routine ID (required for action='rename').",
                    },
                    "new_title": {
                        "type": "string",
                        "description": "New title for the routine (required for action='rename').",
                    },
                },
                "required": ["action"],
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
    "setup_running_hr_zones":       (health_tools.setup_running_hr_zones, "slug"),
    "get_athlete_profile":          (health_tools.get_athlete_profile, "slug"),
    "get_strength_sessions":        (health_tools.get_strength_sessions, "uid"),
    "get_workout_summary":          (health_tools.get_workout_summary, "uid"),
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
    "suggest_feature":              (health_tools.suggest_feature, "slug"),
    "report_exercise_correction":   (health_tools.report_exercise_correction, "slug"),
    "sync_data":                    (health_tools.sync_data, "creds"),
    # iFit integration
    "recommend_ifit_workout":       (health_tools.recommend_ifit_workout, "slug"),
    "search_ifit_library":          (health_tools.search_ifit_library, "none"),
    "get_ifit_workout_details":     (health_tools.get_ifit_workout_details, "none"),
    "search_ifit_programs":         (health_tools.search_ifit_programs, "none"),
    "get_ifit_program_details":     (health_tools.get_ifit_program_details, "none"),
    "discover_ifit_series":         (health_tools.discover_ifit_series, "none"),
    "recommend_strength_workout":   (health_tools.recommend_strength_workout, "slug"),
    "create_hevy_routine_from_recommendation": (health_tools.create_hevy_routine_from_recommendation, "creds"),
    "manage_hevy_routines":         (health_tools.manage_hevy_routines, "creds"),
    # iFit ↔ Hevy feedback loop
    "get_hevy_routine_review":      (health_tools.get_hevy_routine_review, "slug"),
    "compare_hevy_workout":         (health_tools.compare_hevy_workout, "uid"),
    "apply_exercise_feedback":      (health_tools.apply_exercise_feedback, "slug"),
    # Weight recommendations
    "get_routine_weight_recommendations": (health_tools.get_routine_weight_recommendations, "creds"),
    # Telegram linking
    "generate_telegram_link_code": (health_tools.generate_telegram_link_code, "uid"),
    # Knowledge base (RAG)
    "search_knowledge_base":       (health_tools.search_knowledge_base, "uid"),
    "list_knowledge_documents":    (health_tools.list_knowledge_documents, "uid"),
    "delete_knowledge_document":   (health_tools.delete_knowledge_document, "uid"),
}

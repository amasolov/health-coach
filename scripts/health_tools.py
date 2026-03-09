"""
Shared health-tracker tool logic.

Pure Python functions with no MCP dependency.  Both the MCP server and
the Chainlit chat app import from here so the business logic lives in
exactly one place.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg2
import yaml

from scripts.tz import DEFAULT_TZ_NAME, load_user_tz, user_today

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
ATHLETE_PATH = ROOT / "config" / "athlete.yaml"
ZONES_PATH = ROOT / "config" / "zones.yaml"
TEMPLATES_PATH = ROOT / "config" / "treadmill_templates.yaml"
EQUIPMENT_PATH = ROOT / "config" / "equipment.yaml"

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "health"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def resolve_user_id(slug: str) -> int | None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE slug = %s", (slug,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def query(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            rows.append({c: _serialise(v) for c, v in zip(cols, row)})
        return rows
    finally:
        conn.close()


def _serialise(v: Any) -> Any:
    """Make values JSON-friendly."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (float, int, str, bool, type(None))):
        return v
    return str(v)


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _save_yaml(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# Fitness / PMC
# ---------------------------------------------------------------------------


def get_fitness_summary(user_id: int) -> dict:
    """Get current fitness status: CTL (fitness), ATL (fatigue), TSB (form),
    ramp rate, and a plain-language interpretation.  Also includes 8-week
    projection of CTL."""
    rows = query(
        """SELECT time, tss, ctl, atl, tsb, ramp, source
           FROM training_load
           WHERE user_id = %s AND source = 'calculated'
           ORDER BY time DESC LIMIT 1""",
        (user_id,),
    )
    if not rows:
        return {"status": "No training load data yet. Sync activities first."}

    current = rows[0]
    tsb = float(current["tsb"] or 0)
    ramp = float(current["ramp"] or 0)

    if tsb > 25:
        form = "Transitioning / Detraining"
    elif tsb > 5:
        form = "Fresh — good for racing or key sessions"
    elif tsb > -10:
        form = "Neutral — maintaining"
    elif tsb > -30:
        form = "Fatigued — absorbing load"
    else:
        form = "Very fatigued — recovery needed"

    if ramp > 8:
        ramp_note = "Ramp rate high — injury risk, consider recovery"
    elif ramp > 5:
        ramp_note = "Solid fitness building"
    elif ramp > 0:
        ramp_note = "Gradual build"
    else:
        ramp_note = "Fitness declining or stable"

    proj = query(
        """SELECT time, ctl, atl, tsb FROM training_load
           WHERE user_id = %s AND source = 'projected'
           ORDER BY time""",
        (user_id,),
    )

    return {
        "date": current["time"],
        "ctl_fitness": current["ctl"],
        "atl_fatigue": current["atl"],
        "tsb_form": current["tsb"],
        "ramp_rate": current["ramp"],
        "form_status": form,
        "ramp_note": ramp_note,
        "projection_8_weeks": proj[-1] if proj else None,
    }


def get_training_load(
    user_id: int,
    start_date: str = "",
    end_date: str = "",
    days: int = 90,
    tz_name: str = "",
) -> list[dict]:
    """Get daily TSS, CTL, ATL, TSB for a date range.  Defaults to last 90 days.
    Dates in YYYY-MM-DD format."""
    today = user_today(ZoneInfo(tz_name) if tz_name else None)
    if not start_date:
        start_date = (today - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (today + timedelta(days=1)).isoformat()

    return query(
        """SELECT time, tss, ctl, atl, tsb, ramp, source
           FROM training_load
           WHERE user_id = %s AND time >= %s AND time < %s
           ORDER BY time""",
        (user_id, start_date, end_date),
    )


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


def get_activities(
    user_id: int,
    start_date: str = "",
    end_date: str = "",
    days: int = 30,
    sport: str = "",
    limit: int = 50,
    tz_name: str = "",
) -> list[dict]:
    """List activities with metrics.  Filter by date range and/or sport type
    (running, cycling, strength_training, etc.).  Defaults to last 30 days."""
    today = user_today(ZoneInfo(tz_name) if tz_name else None)
    if not start_date:
        start_date = (today - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (today + timedelta(days=1)).isoformat()

    sql = """SELECT time, activity_type, title, duration_s, distance_m,
                    elevation_gain_m, avg_hr, max_hr, avg_power, max_power,
                    normalized_power, tss, intensity_factor, avg_cadence,
                    avg_pace_sec_km, calories,
                    training_effect_ae, training_effect_an
             FROM activities
             WHERE user_id = %s AND time >= %s AND time < %s"""
    params: list = [user_id, start_date, end_date]

    if sport:
        sql += " AND LOWER(activity_type) LIKE %s"
        params.append(f"%{sport.lower()}%")

    sql += " ORDER BY time DESC LIMIT %s"
    params.append(limit)

    return query(sql, tuple(params))


def get_activity_detail(user_id: int, activity_time: str) -> dict:
    """Get full detail for a single activity by its timestamp (ISO format)."""
    rows = query(
        """SELECT * FROM activities
           WHERE user_id = %s AND time = %s LIMIT 1""",
        (user_id, activity_time),
    )
    if not rows:
        raise ValueError("Activity not found")
    return rows[0]


# ---------------------------------------------------------------------------
# Body composition
# ---------------------------------------------------------------------------


def get_body_composition(
    user_id: int,
    start_date: str = "",
    end_date: str = "",
    days: int = 90,
    tz_name: str = "",
) -> list[dict]:
    """Get body composition trend (weight, body fat %, muscle mass, BMI)
    over a date range.  Defaults to last 90 days."""
    today = user_today(ZoneInfo(tz_name) if tz_name else None)
    if not start_date:
        start_date = (today - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (today + timedelta(days=1)).isoformat()

    return query(
        """SELECT time, weight_kg, body_fat_pct, muscle_mass_kg,
                  bone_mass_kg, bmi, body_water_pct
           FROM body_composition
           WHERE user_id = %s AND time >= %s AND time < %s
           ORDER BY time""",
        (user_id, start_date, end_date),
    )


# ---------------------------------------------------------------------------
# Vitals
# ---------------------------------------------------------------------------


def get_vitals(
    user_id: int,
    start_date: str = "",
    end_date: str = "",
    days: int = 30,
    tz_name: str = "",
) -> list[dict]:
    """Get daily vitals (resting HR, HRV, blood pressure, sleep, stress,
    body battery, SpO2).  Defaults to last 30 days."""
    today = user_today(ZoneInfo(tz_name) if tz_name else None)
    if not start_date:
        start_date = (today - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (today + timedelta(days=1)).isoformat()

    return query(
        """SELECT time, resting_hr, hrv_ms, bp_systolic, bp_diastolic,
                  bp_pulse, sleep_score, sleep_duration_min, stress_avg,
                  body_battery_high, body_battery_low, spo2_avg,
                  respiration_avg
           FROM vitals
           WHERE user_id = %s AND time >= %s AND time < %s
           ORDER BY time""",
        (user_id, start_date, end_date),
    )


# ---------------------------------------------------------------------------
# Zones & profile
# ---------------------------------------------------------------------------


def get_training_zones(user_slug: str) -> dict:
    """Get current training zones: heart rate, running power, cycling power,
    and running pace with absolute lower/upper bounds."""
    zones = load_yaml(ZONES_PATH)
    user_zones = zones.get("users", {}).get(user_slug)
    if not user_zones:
        return {"error": f"No zones configured for user '{user_slug}'"}

    result = {}
    for section in ("heart_rate", "running_power", "cycling_power", "running_pace"):
        data = user_zones.get(section)
        if not data:
            continue
        result[section] = {
            "model": data.get("model"),
            "anchor_value": data.get("anchor_value"),
            "anchor_unit": data.get("anchor_unit"),
            "zones": [
                {
                    "name": z["name"],
                    "lower": z.get("lower"),
                    "upper": z.get("upper"),
                    "description": z.get("description", ""),
                }
                for z in data.get("zones", [])
            ],
        }
    return result


def get_athlete_profile(user_slug: str) -> dict:
    """Get the athlete's profile: goals, thresholds, body composition,
    training status, and treadmill zone-to-speed mapping."""
    athlete = load_yaml(ATHLETE_PATH)
    user_data = athlete.get("users", {}).get(user_slug)
    if not user_data:
        return {"error": f"No profile configured for user '{user_slug}'"}

    return {
        "profile": user_data.get("profile"),
        "goals": user_data.get("goals"),
        "thresholds": user_data.get("thresholds"),
        "body": user_data.get("body"),
        "training_status": user_data.get("training_status"),
    }


# ---------------------------------------------------------------------------
# Strength
# ---------------------------------------------------------------------------


def get_strength_sessions(
    user_id: int,
    start_date: str = "",
    end_date: str = "",
    days: int = 30,
    exercise: str = "",
    tz_name: str = "",
) -> list[dict]:
    """Get strength training sets from Hevy.  Filter by date range and/or
    exercise name (partial match).  Defaults to last 30 days."""
    today = user_today(ZoneInfo(tz_name) if tz_name else None)
    if not start_date:
        start_date = (today - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (today + timedelta(days=1)).isoformat()

    sql = """SELECT time, workout_id, exercise_name, exercise_type,
                    muscle_group, set_number, set_type,
                    weight_kg, reps, rpe, duration_s, distance_m
             FROM strength_sets
             WHERE user_id = %s AND time >= %s AND time < %s"""
    params: list = [user_id, start_date, end_date]

    if exercise:
        sql += " AND LOWER(exercise_name) LIKE %s"
        params.append(f"%{exercise.lower()}%")

    sql += " ORDER BY time DESC, workout_id, set_number LIMIT 200"
    return query(sql, tuple(params))


# ---------------------------------------------------------------------------
# Treadmill workouts
# ---------------------------------------------------------------------------


def list_treadmill_templates() -> list[dict]:
    """List available treadmill workout templates with name, duration, and
    step count."""
    templates = load_yaml(TEMPLATES_PATH).get("templates", {})
    result = []
    for key, tmpl in templates.items():
        steps = tmpl.get("steps", [])
        total_min = sum(s.get("duration_min", 0) for s in steps)
        result.append({
            "key": key,
            "name": tmpl["name"],
            "description": tmpl.get("description", ""),
            "total_minutes": total_min,
            "steps": len(steps),
        })
    return result


def generate_treadmill_workout(user_slug: str, template_key: str) -> dict:
    """Generate a structured treadmill workout from a template.  Returns a
    step-by-step table with speed, incline, duration, and distance for
    entry into iFit Workout Creator."""
    templates = load_yaml(TEMPLATES_PATH).get("templates", {})
    if template_key not in templates:
        available = ", ".join(templates.keys())
        raise ValueError(f"Template '{template_key}' not found. Available: {available}")

    athlete = load_yaml(ATHLETE_PATH)
    user_data = athlete.get("users", {}).get(user_slug, {})
    treadmill = user_data.get("treadmill", {})
    zone_map = {**treadmill.get("zone_speed_map", {}), **treadmill.get("hill_map", {})}

    template = templates[template_key]
    steps = template["steps"]
    total_distance = 0.0
    rows = []

    for i, step in enumerate(steps, 1):
        zone_key = step["zone"]
        settings = zone_map.get(zone_key, {"speed_kph": 0.0, "incline_pct": 0.0})
        speed = settings["speed_kph"]
        incline = settings["incline_pct"]
        duration = step["duration_min"]
        distance = speed * (duration / 60)
        total_distance += distance

        rows.append({
            "step": i,
            "phase": step["phase"],
            "zone": zone_key,
            "speed_kph": speed,
            "incline_pct": incline,
            "duration_min": duration,
            "distance_km": round(distance, 2),
        })

    return {
        "name": template["name"],
        "description": template.get("description", ""),
        "total_minutes": sum(s["duration_min"] for s in steps),
        "total_distance_km": round(total_distance, 1),
        "steps": rows,
        "instructions": "Enter these steps in iFit: ifit.com -> Create -> Distance Based Workout",
    }


# ---------------------------------------------------------------------------
# Garmin authentication
# ---------------------------------------------------------------------------


def garmin_auth_status(user_slug: str, garmin_email: str = "") -> dict:
    """Check whether Garmin Connect authentication is set up and tokens are
    valid for the current user."""
    from scripts.garmin_auth import get_auth_status

    status = get_auth_status(user_slug)
    status["garmin_email"] = garmin_email or "(not configured)"
    return status


def garmin_authenticate(
    user_slug: str, garmin_email: str, garmin_password: str
) -> dict:
    """Start Garmin Connect authentication.  If MFA is required, returns a
    prompt -- the user should then call garmin_submit_mfa with the code."""
    from scripts.garmin_auth import try_cached_login, start_login

    client = try_cached_login(user_slug)
    if client:
        return {"status": "ok", "message": "Already authenticated with cached tokens."}

    if not garmin_email or not garmin_password:
        raise ValueError(
            "Garmin credentials not configured. Set garmin_email and "
            "garmin_password in the addon config (or secrets for local dev)."
        )

    result, _client = start_login(user_slug, garmin_email, garmin_password)

    if result == "ok":
        return {"status": "ok", "message": "Authenticated successfully. Tokens cached."}
    elif result == "needs_mfa":
        return {
            "status": "needs_mfa",
            "message": (
                "MFA code required. Garmin has sent a code to your email. "
                "Ask the user for the code, then call garmin_submit_mfa with it."
            ),
        }
    else:
        raise ValueError(result)


def garmin_submit_mfa(user_slug: str, mfa_code: str) -> dict:
    """Complete Garmin Connect MFA authentication with the code the user
    received (via email or authenticator app)."""
    from scripts.garmin_auth import finish_mfa_login

    result, _client = finish_mfa_login(user_slug, mfa_code)

    if result == "ok":
        return {"status": "ok", "message": "MFA verified. Tokens cached for future use."}
    else:
        raise ValueError(result)


# ---------------------------------------------------------------------------
# Athlete profile setup
# ---------------------------------------------------------------------------


def garmin_fetch_profile(user_slug: str) -> dict:
    """Fetch athlete profile data from Garmin Connect and merge into the
    config.  Auto-populates user profile, body composition, resting HR,
    max HR, VO2max, lactate threshold, and cycling FTP.

    Only fills fields that are currently null -- never overwrites."""
    from scripts.garmin_auth import try_cached_login
    from scripts.garmin_fetch import (
        fetch_garmin_profile,
        merge_into_athlete_yaml,
    )

    client = try_cached_login(user_slug)
    if not client:
        raise ValueError("Garmin not authenticated. Call garmin_authenticate first.")

    result = fetch_garmin_profile(user_slug, client)

    written = merge_into_athlete_yaml(
        str(ATHLETE_PATH), user_slug, result["fetched"]
    )

    return {
        "fetched": result["fetched"],
        "sources": result["sources"],
        "written_to_config": written,
        "still_missing": result["missing"],
    }


def generate_fitness_assessment(
    user_slug: str,
    hevy_api_key: str | None = None,
    lookback_days: int = 180,
    include_hevy: bool = True,
) -> dict:
    """Generate a comprehensive fitness assessment by pulling 6 months of
    historical data from Garmin Connect (and optionally Hevy)."""
    from scripts.garmin_auth import try_cached_login
    from scripts.garmin_fetch import merge_into_athlete_yaml
    from scripts.fitness_assessment import assess_fitness

    client = try_cached_login(user_slug)
    if not client:
        raise ValueError("Garmin not authenticated. Call garmin_authenticate first.")

    key = hevy_api_key if include_hevy else None

    result = assess_fitness(
        slug=user_slug,
        garmin_client=client,
        hevy_api_key=key,
        lookback_days=lookback_days,
    )

    profile_data = result.get("auto_profile", {})
    if profile_data.get("fetched"):
        written = merge_into_athlete_yaml(
            str(ATHLETE_PATH), user_slug, profile_data["fetched"]
        )
        result["written_to_config"] = written

    suggested = result.get("suggested_action_items", [])
    if suggested:
        existing = load_action_items(user_slug)
        existing_ids = {i.get("id") for i in existing}
        added = []
        for item in suggested:
            if item.get("id") not in existing_ids:
                existing.append(item)
                added.append(item["id"])
        if added:
            save_action_items(user_slug, existing)
            result["action_items_added"] = added

    return result


def update_athlete_profile(
    user_slug: str, field_path: str, value: float | int | str
) -> dict:
    """Update a single field in the athlete profile config.

    field_path is dot-separated relative to the user, for example:
      thresholds.heart_rate.max_hr, body.weight_kg, profile.date_of_birth"""
    from scripts.garmin_fetch import update_athlete_field

    update_athlete_field(str(ATHLETE_PATH), user_slug, field_path, value)

    athlete = load_yaml(ATHLETE_PATH)
    user = athlete.get("users", {}).get(user_slug, {})
    if "ftp" in field_path or "weight" in field_path:
        ftp = (user.get("thresholds", {}).get("cycling", {}).get("ftp"))
        weight = user.get("body", {}).get("weight_kg")
        if ftp and weight and weight > 0:
            wkg = round(ftp / weight, 2)
            update_athlete_field(str(ATHLETE_PATH), user_slug, "thresholds.cycling.ftp_wkg", wkg)
            return {
                "updated": field_path,
                "value": value,
                "also_computed": {"thresholds.cycling.ftp_wkg": wkg},
            }

    return {"updated": field_path, "value": value}


# ---------------------------------------------------------------------------
# Goals & onboarding
# ---------------------------------------------------------------------------

ONBOARDING_QUESTIONS = [
    {
        "id": "primary_goal",
        "question": "What is your primary fitness or racing goal?",
        "examples": [
            "Run a marathon", "Complete UTMB", "General fitness",
            "Lose weight", "Build muscle", "First triathlon",
            "Improve 5K time", "Stay healthy and active",
        ],
        "field": "goals.primary_goal",
    },
    {
        "id": "target_event",
        "question": "Do you have a specific target event? If so, what and when?",
        "examples": ["London Marathon April 2027", "No specific event yet"],
        "field": "goals.target_event",
        "optional": True,
    },
    {
        "id": "secondary_goals",
        "question": "Any secondary goals? (list as many as you like)",
        "examples": [
            "Improve body composition", "Build running endurance",
            "Get stronger", "Better sleep", "Reduce stress",
        ],
        "field": "goals.secondary_goals",
    },
    {
        "id": "available_hours",
        "question": "How many hours per week can you realistically train?",
        "examples": ["3-4 hours", "5-7 hours", "8-10 hours", "It varies a lot"],
        "field": "goals.available_hours_per_week",
    },
    {
        "id": "preferred_sports",
        "question": "What sports/activities do you enjoy or want to focus on?",
        "examples": [
            "Running", "Trail running", "Cycling", "Swimming",
            "Strength training", "Yoga", "Hiking",
        ],
        "field": "goals.preferred_sports",
    },
    {
        "id": "constraints",
        "question": "Any constraints or limitations? (injuries, schedule, equipment)",
        "examples": [
            "Bad knee -- can't do high impact daily",
            "Only free mornings before 7am",
            "No pool access", "Travel frequently",
        ],
        "field": "goals.constraints",
        "optional": True,
    },
    {
        "id": "experience_level",
        "question": "How would you describe your training experience?",
        "examples": [
            "beginner (less than 1 year)",
            "intermediate (1-3 years consistent)",
            "advanced (3+ years structured training)",
        ],
        "field": "goals.experience_level",
    },
    {
        "id": "likes_dislikes",
        "question": "What do you enjoy about training, and what do you dislike?",
        "examples": [
            "Love outdoor runs, hate indoor cycling",
            "Enjoy iFit workouts but find them unstructured",
            "Like data and tracking, dislike monotony",
        ],
        "field": "goals.training_preferences",
        "optional": True,
    },
]


def get_onboarding_questions(user_slug: str) -> dict:
    """Get the list of onboarding questions to ask a new user about their
    goals, preferences, and constraints.  Also returns any goals already
    on file so the AI can skip answered questions."""
    athlete = load_yaml(ATHLETE_PATH)
    user_data = athlete.get("users", {}).get(user_slug, {})
    existing_goals = user_data.get("goals", {})

    answered = []
    unanswered = []
    for q in ONBOARDING_QUESTIONS:
        field_key = q["field"].removeprefix("goals.")
        parts = field_key.split(".")
        val = existing_goals
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p)
            else:
                val = None
                break

        if val is not None:
            answered.append({**q, "current_value": val})
        else:
            unanswered.append(q)

    return {
        "existing_goals": existing_goals,
        "answered": answered,
        "unanswered": unanswered,
        "instructions": (
            "Ask the unanswered questions conversationally. You don't have to "
            "follow the exact wording -- adapt to the conversation. Store each "
            "answer with set_user_goals. After collecting goals, run "
            "generate_fitness_assessment for a data-driven overview."
        ),
    }


def set_user_goals(user_slug: str, goals: dict) -> dict:
    """Store the user's goals, preferences, and constraints in their
    athlete profile.  Only provided keys are updated; existing values
    are preserved."""
    athlete = load_yaml(ATHLETE_PATH)
    user = athlete.setdefault("users", {}).setdefault(user_slug, {})
    existing = user.setdefault("goals", {})

    for key, value in goals.items():
        if value is not None:
            existing[key] = value

    _save_yaml(ATHLETE_PATH, athlete)
    return {"updated_goals": existing}


def get_user_goals(user_slug: str) -> dict:
    """Get the user's current goals, preferences, and constraints."""
    athlete = load_yaml(ATHLETE_PATH)
    user_data = athlete.get("users", {}).get(user_slug, {})
    goals = user_data.get("goals", {})

    if not goals:
        return {
            "goals": None,
            "suggestion": (
                "No goals set yet. Call get_onboarding_questions to get "
                "the questions to ask this user."
            ),
        }

    return {"goals": goals}


# ---------------------------------------------------------------------------
# Action items
# ---------------------------------------------------------------------------


def load_action_items(user_slug: str) -> list[dict]:
    athlete = load_yaml(ATHLETE_PATH)
    return athlete.get("users", {}).get(user_slug, {}).get("action_items", [])


def save_action_items(user_slug: str, items: list[dict]) -> None:
    athlete = load_yaml(ATHLETE_PATH)
    athlete.setdefault("users", {}).setdefault(user_slug, {})["action_items"] = items
    _save_yaml(ATHLETE_PATH, athlete)


def get_action_items(user_slug: str, status_filter: str = "") -> dict:
    """Get the user's action items grouped by priority.  Optional
    status_filter: 'pending', 'in_progress', 'completed', or blank for all."""
    items = load_action_items(user_slug)

    if status_filter:
        items = [i for i in items if i.get("status") == status_filter]

    high = [i for i in items if i.get("priority") == "high"]
    medium = [i for i in items if i.get("priority") == "medium"]
    low = [i for i in items if i.get("priority") == "low"]

    all_items = load_action_items(user_slug)
    pending = sum(1 for i in all_items if i.get("status") == "pending")
    in_progress = sum(1 for i in all_items if i.get("status") == "in_progress")

    return {
        "high_priority": high,
        "medium_priority": medium,
        "low_priority": low,
        "summary": {
            "total": len(items),
            "pending": pending,
            "in_progress": in_progress,
        },
        "instructions": (
            "Review these with the user. Ask about progress on in_progress "
            "and high-priority pending items. Mark completed items and "
            "add new ones based on the conversation."
        ),
    }


def add_action_item(
    user_slug: str,
    title: str,
    description: str,
    category: str = "training",
    priority: str = "medium",
    due: str = "",
) -> dict:
    """Add a new action item for the user.

    category: testing, habit, equipment, training, setup, nutrition
    priority: high, medium, low
    due: optional YYYY-MM-DD deadline"""
    items = load_action_items(user_slug)

    item_id = title.lower().replace(" ", "-")[:40]
    existing_ids = {i.get("id") for i in items}
    if item_id in existing_ids:
        base = item_id
        n = 2
        while item_id in existing_ids:
            item_id = f"{base}-{n}"
            n += 1

    new_item = {
        "id": item_id,
        "title": title,
        "description": description,
        "category": category,
        "priority": priority,
        "status": "pending",
        "created": user_today(load_user_tz(user_slug)).isoformat(),
        "due": due or None,
        "completed": None,
    }
    items.append(new_item)
    save_action_items(user_slug, items)
    return {"added": new_item}


def update_action_item(
    user_slug: str,
    item_id: str,
    status: str = "",
    priority: str = "",
    title: str = "",
    description: str = "",
    due: str = "",
    note: str = "",
) -> dict:
    """Update an existing action item.  Use this to mark items as completed,
    change priority, add notes, or update details."""
    items = load_action_items(user_slug)

    target = None
    for item in items:
        if item.get("id") == item_id:
            target = item
            break

    if not target:
        raise ValueError(f"Action item '{item_id}' not found")

    today_str = user_today(load_user_tz(user_slug)).isoformat()
    if status:
        target["status"] = status
        if status == "completed":
            target["completed"] = today_str
    if priority:
        target["priority"] = priority
    if title:
        target["title"] = title
    if description:
        target["description"] = description
    if due:
        target["due"] = due
    if note:
        existing_notes = target.get("notes", [])
        existing_notes.append({"date": today_str, "text": note})
        target["notes"] = existing_notes

    save_action_items(user_slug, items)
    return {"updated": target}


def complete_action_item(user_slug: str, item_id: str, note: str = "") -> dict:
    """Mark an action item as completed.  Optionally add a completion note
    (e.g. 'LTHR measured at 168 bpm')."""
    items = load_action_items(user_slug)

    target = None
    for item in items:
        if item.get("id") == item_id:
            target = item
            break

    if not target:
        raise ValueError(f"Action item '{item_id}' not found")

    today_str = user_today(load_user_tz(user_slug)).isoformat()
    target["status"] = "completed"
    target["completed"] = today_str
    if note:
        existing_notes = target.get("notes", [])
        existing_notes.append({"date": today_str, "text": note})
        target["notes"] = existing_notes

    save_action_items(user_slug, items)
    return {"completed": target}


# ---------------------------------------------------------------------------
# Integrations & hardware registry
# ---------------------------------------------------------------------------

SUPPORTED_INTEGRATIONS: list[dict] = [
    {
        "id": "garmin_fenix",
        "name": "Garmin Fenix / Forerunner / Enduro",
        "category": "wearable",
        "description": "GPS watch with optical HR, SpO2, barometric altitude, temperature",
        "data_provided": ["activities", "heart_rate", "hrv", "sleep", "stress", "body_battery", "spo2", "vo2max"],
        "setup": "Pair with Garmin Connect; data syncs automatically",
        "required": False,
        "recommended": True,
    },
    {
        "id": "garmin_hrm_pro",
        "name": "Garmin HRM Pro / HRM Pro Plus",
        "category": "wearable",
        "description": "Chest strap HR monitor with running dynamics and native running power",
        "data_provided": ["heart_rate", "running_dynamics", "running_power"],
        "setup": "Pair with Garmin watch via ANT+/BLE; provides running power without extra foot pod",
        "required": False,
        "recommended": True,
        "notes": "Provides Garmin native running power -- no Stryd needed",
    },
    {
        "id": "stryd",
        "name": "Stryd Running Power Meter",
        "category": "wearable",
        "description": "Foot pod for running power, cadence, ground contact time, leg spring stiffness",
        "data_provided": ["running_power", "cadence", "ground_contact", "leg_spring_stiffness", "critical_power"],
        "setup": "Pair with Garmin watch; power data appears in Garmin Connect activities",
        "required": False,
        "recommended": False,
        "notes": "Optional if you have HRM Pro -- Garmin native power is sufficient. Stryd adds wind adjustment and its own Critical Power model.",
    },
    {
        "id": "garmin_edge",
        "name": "Garmin Edge (1030/540/etc.)",
        "category": "cycling",
        "description": "Bike computer with GPS and sensor pairing",
        "data_provided": ["cycling_activities", "power", "cadence", "speed"],
        "setup": "Pair with Garmin Connect; pair HRM and power meter via ANT+",
        "required": False,
        "recommended": False,
    },
    {
        "id": "smart_trainer",
        "name": "Smart Trainer / Smart Bike (Wahoo Kickr, Tacx, etc.)",
        "category": "cycling",
        "description": "Indoor cycling with direct power measurement and controllable resistance",
        "data_provided": ["cycling_power", "cadence", "speed"],
        "setup": "Connect to Zwift/Garmin via ANT+ or Bluetooth",
        "required": False,
        "recommended": False,
    },
    {
        "id": "nordictrack_treadmill",
        "name": "NordicTrack / ProForm Treadmill (iFit-compatible)",
        "category": "treadmill",
        "description": "Treadmill with iFit integration for guided workouts, auto speed/incline control",
        "data_provided": ["treadmill_activities"],
        "setup": "iFit subscription required; we generate zone-based workouts for the iFit Workout Creator",
        "required": False,
        "recommended": False,
        "notes": "Supports up to 40% incline and -6% decline on x22i",
    },
    {
        "id": "garmin_scale",
        "name": "Garmin Smart Scale (Index S2/Index 2)",
        "category": "health",
        "description": "Smart scale measuring weight, body fat, muscle mass, bone mass, BMI, body water",
        "data_provided": ["weight", "body_fat", "muscle_mass", "bone_mass", "bmi", "body_water"],
        "setup": "Pair with Garmin Connect; weigh daily at the same time for best trends",
        "required": False,
        "recommended": True,
    },
    {
        "id": "garmin_bp_monitor",
        "name": "Garmin Blood Pressure Monitor",
        "category": "health",
        "description": "Blood pressure monitor syncing to Garmin Connect",
        "data_provided": ["blood_pressure_systolic", "blood_pressure_diastolic", "resting_pulse"],
        "setup": "Pair with Garmin Connect; measure morning and evening during baseline",
        "required": False,
        "recommended": False,
    },
    {
        "id": "home_gym",
        "name": "Home Gym Equipment",
        "category": "gym",
        "description": "Dumbbells, kettlebells, barbells, squat rack, resistance bands, etc.",
        "data_provided": [],
        "setup": "Log workouts in Hevy; tell the system what you have so workouts use available equipment",
        "required": False,
        "recommended": False,
    },
    {
        "id": "garmin_connect",
        "name": "Garmin Connect",
        "category": "software",
        "description": "Central data hub -- all Garmin device data flows here. Primary data source for activities, HR, sleep, body composition, vitals.",
        "data_provided": [
            "activities", "heart_rate", "hrv", "sleep", "stress", "body_battery",
            "spo2", "vo2max", "body_composition", "blood_pressure", "running_power",
            "cycling_power", "training_effect", "lactate_threshold",
        ],
        "setup": "Create account at connect.garmin.com; provide credentials in addon config",
        "required": True,
        "credentials": ["garmin_email", "garmin_password"],
    },
    {
        "id": "hevy",
        "name": "Hevy",
        "category": "software",
        "description": "Strength training logger -- tracks sets, reps, weight, RPE per exercise",
        "data_provided": ["strength_sets", "exercise_history", "personal_records"],
        "setup": "Create account; get API key from Hevy app settings",
        "required": False,
        "recommended": True,
        "credentials": ["hevy_api_key"],
    },
    {
        "id": "strava",
        "name": "Strava",
        "category": "software",
        "description": "Social fitness platform with route planning and segment tracking",
        "data_provided": ["activities_social", "segments", "routes"],
        "setup": "Connect Garmin Connect to Strava for auto-sync; no direct API integration needed",
        "required": False,
        "recommended": False,
        "notes": "Data flows from Garmin -- no credentials needed in the addon",
    },
    {
        "id": "ifit",
        "name": "iFit",
        "category": "software",
        "description": "Guided treadmill workouts with auto speed/incline. We generate structured workouts for the iFit Workout Creator.",
        "data_provided": ["treadmill_workouts"],
        "setup": "iFit subscription; use ifit.com Workout Creator to enter generated workouts",
        "required": False,
        "recommended": False,
        "notes": "No API -- workouts are generated as step tables for manual entry",
    },
    {
        "id": "zwift",
        "name": "Zwift",
        "category": "software",
        "description": "Virtual cycling and running platform with structured workouts",
        "data_provided": ["indoor_cycling", "indoor_running"],
        "setup": "Connect smart trainer via ANT+/BLE; activities auto-sync to Garmin Connect",
        "required": False,
        "recommended": False,
        "notes": "Data flows to Garmin Connect -- no direct integration needed",
    },
]


def get_supported_integrations(category: str = "") -> dict:
    """Get the list of all supported hardware and software integrations.

    Optional category filter: wearable, cycling, treadmill, health, gym, software"""
    items = SUPPORTED_INTEGRATIONS
    if category:
        items = [i for i in items if i["category"] == category.lower()]

    required = [i for i in items if i.get("required")]
    recommended = [i for i in items if i.get("recommended") and not i.get("required")]
    optional = [i for i in items if not i.get("required") and not i.get("recommended")]

    categories = sorted({i["category"] for i in SUPPORTED_INTEGRATIONS})

    return {
        "required": required,
        "recommended": recommended,
        "optional": optional,
        "categories": categories,
        "total": len(items),
        "instructions": (
            "Present these to the user, starting with required integrations, "
            "then recommended, then optional. Ask which ones they have. "
            "Store their selections with set_user_integrations."
        ),
    }


def set_user_integrations(
    user_slug: str,
    integrations: list[str],
    equipment_notes: dict | None = None,
) -> dict:
    """Store which integrations and hardware the user has.

    integrations: list of integration IDs from get_supported_integrations
    equipment_notes: optional dict of integration_id -> note string"""
    valid_ids = {i["id"] for i in SUPPORTED_INTEGRATIONS}
    unknown = [i for i in integrations if i not in valid_ids]
    if unknown:
        raise ValueError(f"Unknown integration IDs: {unknown}. Use get_supported_integrations to see valid IDs.")

    athlete = load_yaml(ATHLETE_PATH)
    user = athlete.setdefault("users", {}).setdefault(user_slug, {})

    user_integrations: list[dict[str, Any]] = []
    for int_id in integrations:
        entry: dict[str, Any] = {"id": int_id}
        ref = next((i for i in SUPPORTED_INTEGRATIONS if i["id"] == int_id), None)
        if ref:
            entry["name"] = ref["name"]
            entry["category"] = ref["category"]
        if equipment_notes and int_id in equipment_notes:
            entry["notes"] = equipment_notes[int_id]
        user_integrations.append(entry)

    user["integrations"] = user_integrations
    _save_yaml(ATHLETE_PATH, athlete)

    cred_needed = []
    for int_id in integrations:
        ref = next((i for i in SUPPORTED_INTEGRATIONS if i["id"] == int_id), None)
        if ref and ref.get("credentials"):
            cred_needed.append({"integration": int_id, "credentials": ref["credentials"]})

    return {
        "stored": len(user_integrations),
        "integrations": [i["id"] for i in user_integrations],
        "credentials_needed": cred_needed,
        "next_steps": (
            "If credentials_needed is not empty, ensure those are configured in "
            "the addon config or secrets. Then proceed with garmin_authenticate "
            "and generate_fitness_assessment."
        ),
    }


def get_user_integrations(user_slug: str) -> dict:
    """Get the user's configured integrations and hardware."""
    athlete = load_yaml(ATHLETE_PATH)
    user_data = athlete.get("users", {}).get(user_slug, {})
    integrations = user_data.get("integrations", [])

    if not integrations:
        return {
            "integrations": None,
            "suggestion": (
                "No integrations configured yet. Call get_supported_integrations "
                "to show the user what's available, then set_user_integrations "
                "to store their selections."
            ),
        }

    return {"integrations": integrations, "count": len(integrations)}


# ---------------------------------------------------------------------------
# iFit integration — general recommendations, library search, strength recs
# ---------------------------------------------------------------------------


def recommend_ifit_workout(user_slug: str) -> dict:
    """Run the general iFit workout recommendation engine.

    Analyses recent 14-day activity history, muscle group fatigue, and variety
    to score up-next, favorites, and iFit-recommended workouts.  Returns top 5
    ranked candidates with muscle focus, scoring rationale, and recent activity
    summary."""
    from scripts.ifit_recommend import (
        fetch_recent_history,
        analyze_fatigue,
        fetch_candidates,
        score_candidates,
        RECOVERY_DAYS,
    )
    from scripts.ifit_auth import get_auth_headers

    try:
        headers = get_auth_headers()
    except RuntimeError as exc:
        return {"error": str(exc)}

    history = fetch_recent_history(headers, days=14)
    fatigue = analyze_fatigue(history)
    candidates = fetch_candidates(headers)
    ranked = score_candidates(candidates, fatigue, history, headers)

    recent_summary = []
    for entry in history[:7]:
        day_label = "today" if entry["days_ago"] == 0 else f"{entry['days_ago']}d ago"
        recent_summary.append({
            "when": day_label,
            "title": entry.get("title", "?"),
            "type": entry.get("log_type", "?"),
            "muscle_groups": sorted(entry.get("muscle_groups", set())),
            "styles": sorted(entry.get("styles", set())),
        })

    muscle_status = {}
    for mg in ["upper", "lower", "core", "total"]:
        days = fatigue["days_since"].get(mg)
        needed = RECOVERY_DAYS.get(mg, 2)
        if days is None:
            muscle_status[mg] = "not trained recently — READY"
        elif days >= needed:
            muscle_status[mg] = f"rested ({days}d ago) — READY"
        else:
            muscle_status[mg] = f"recovering ({days}d ago, need {needed}d)"

    top = []
    for cand in ranked[:5]:
        top.append({
            "title": cand.get("title", "?"),
            "source": cand.get("source", "?"),
            "series_progress": cand.get("series_progress", ""),
            "score": cand.get("score", 0),
            "type": cand.get("type", "?"),
            "muscle_groups": sorted(cand.get("muscle_groups", set())),
            "styles": sorted(cand.get("styles", set())),
            "difficulty": cand.get("difficulty", "?"),
            "required_equipment": cand.get("required_equipment", []),
            "reasons": cand.get("reasons", []),
        })

    return {
        "recent_activity": recent_summary,
        "muscle_status": muscle_status,
        "workouts_last_3d": fatigue["total_3d"],
        "workouts_last_7d": fatigue["total_7d"],
        "last_run_days_ago": fatigue["last_run_day"],
        "recommendations": top,
        "count": len(top),
    }


def search_ifit_library(query: str, workout_type: str = "", limit: int = 10) -> dict:
    """Search the iFit workout library by title, trainer, category, description, or keyword.

    Uses the cached library (12K+ workouts).  Results are ranked by relevance
    (title match > trainer match > description match > category match) and rating.
    Also enriches results with program/series info from R2 when available."""
    import json as _json
    from pathlib import Path as _P

    cache_path = _P(__file__).resolve().parent.parent / ".ifit_capture" / "library_workouts.json"
    trainers_path = _P(__file__).resolve().parent.parent / ".ifit_capture" / "trainers.json"

    if not cache_path.exists():
        try:
            import asyncio
            from scripts.ifit_auth import get_auth_headers
            from scripts.ifit_list_series import fetch_all_trainers, fetch_all_workouts
            headers = get_auth_headers()
            fetch_all_trainers(headers)
            asyncio.run(fetch_all_workouts(headers))
        except Exception as exc:
            return {"error": f"iFit library cache not available and auto-build failed: {exc}"}
        if not cache_path.exists():
            return {"error": "Failed to build iFit library cache."}

    with open(cache_path) as f:
        workouts = _json.load(f)
    trainers = {}
    if trainers_path.exists():
        with open(trainers_path) as f:
            trainers = _json.load(f)

    # Load program index for enrichment
    program_index: dict = {}
    try:
        from scripts.ifit_r2_sync import load_program_index
        program_index = load_program_index()
    except Exception:
        pass

    q_lower = query.lower()
    terms = q_lower.split()

    if workout_type:
        wt = workout_type.lower()
        workouts = [
            w for w in workouts
            if wt in w.get("type", "").lower()
            or any(wt in c.lower() for c in w.get("categories", []))
            or any(wt in c.lower() for c in w.get("subcategories", []))
        ]

    scored = []
    for w in workouts:
        title = (w.get("title") or "").lower()
        trainer_id = w.get("trainer_id", "")
        trainer_name = (trainers.get(trainer_id, {}).get("name") or "").lower()
        cats = " ".join(w.get("categories", []) + w.get("subcategories", [])).lower()
        desc = (w.get("description") or "").lower()

        prog = program_index.get(w.get("id", ""))
        prog_title = (prog["title"].lower() if prog else "")

        score = 0
        for term in terms:
            if term in title:
                score += 10
            if term in trainer_name:
                score += 8
            if term in desc:
                score += 5
            if term in prog_title:
                score += 7
            if term in cats:
                score += 3

        if score > 0:
            rating = w.get("rating_avg", 0) or 0
            score += rating * 0.5
            scored.append((score, w, trainers.get(trainer_id, {}).get("name", "")))

    scored.sort(key=lambda x: -x[0])

    results = []
    for _score, w, trainer_name in scored[:limit]:
        wid = w.get("id", "")
        entry = {
            "title": w.get("title", ""),
            "trainer": trainer_name,
            "type": w.get("type", ""),
            "difficulty": w.get("difficulty", ""),
            "duration_min": round(w.get("time_sec", 0) / 60) if w.get("time_sec") else None,
            "rating": w.get("rating_avg", 0),
            "categories": w.get("categories", []),
            "subcategories": w.get("subcategories", []),
            "equipment": w.get("required_equipment", []),
            "workout_id": wid,
        }
        desc = w.get("description", "")
        if desc:
            entry["description"] = desc[:200] + ("..." if len(desc) > 200 else "")

        prog = program_index.get(wid)
        if prog:
            entry["program"] = prog["title"]
            entry["program_position"] = f"{prog['position']} of {prog['total']}"

        results.append(entry)

    return {
        "query": query,
        "type_filter": workout_type or None,
        "results": results,
        "count": len(results),
        "total_library_size": len(workouts),
    }


def search_ifit_programs(query: str, limit: int = 10) -> dict:
    """Search the iFit program/series index by name, trainer, or keyword."""
    try:
        from scripts.r2_store import is_configured as r2_configured, list_keys, download_json
    except ImportError:
        return {"error": "R2 module not available"}

    if not r2_configured():
        return {"error": "R2 not configured — program index unavailable"}

    keys = list_keys("programs/")
    if not keys:
        return {"query": query, "results": [], "count": 0, "note": "No programs indexed yet"}

    terms = query.lower().split()
    scored = []

    for key in keys:
        program = download_json(key)
        if not program:
            continue

        title = (program.get("title") or "").lower()
        overview = (program.get("overview") or "").lower()
        trainer_names = " ".join(t.get("name", "") for t in program.get("trainers", [])).lower()

        score = 0
        for term in terms:
            if term in title:
                score += 10
            if term in trainer_names:
                score += 8
            if term in overview:
                score += 5

        if score > 0:
            rating = program.get("rating", {})
            avg = rating.get("average", 0) if isinstance(rating, dict) else 0
            score += avg * 0.5
            scored.append((score, program))

    scored.sort(key=lambda x: -x[0])

    results = []
    for _score, p in scored[:limit]:
        results.append({
            "title": p.get("title", ""),
            "series_id": p.get("series_id", ""),
            "type": p.get("type", ""),
            "overview": (p.get("overview") or "")[:200],
            "trainers": [t.get("name", "") for t in p.get("trainers", [])],
            "workout_count": p.get("workout_count", 0),
            "workouts": p.get("workout_titles", [])[:10],
            "rating": p.get("rating", {}).get("average") if isinstance(p.get("rating"), dict) else None,
        })

    return {
        "query": query,
        "results": results,
        "count": len(results),
        "total_programs_indexed": len(keys),
    }


def get_ifit_program_details(series_id: str) -> dict:
    """Get details for an iFit program/series by ID.

    First checks R2 cache, falls back to live iFit API."""
    try:
        from scripts.r2_store import (
            is_configured as r2_configured, download_json, upload_json,
        )
    except ImportError:
        r2_configured = lambda: False

    if r2_configured():
        cached = download_json(f"programs/{series_id}.json")
        if cached:
            return cached

    import httpx as _httpx
    try:
        from scripts.ifit_auth import get_auth_headers
        headers = get_auth_headers()
    except RuntimeError as exc:
        return {"error": str(exc)}

    try:
        r = _httpx.get(
            f"https://gateway.ifit.com/wolf-workouts-service/v1/program/{series_id}"
            f"?softwareNumber=424992",
            headers=headers, timeout=15,
        )
        if r.status_code != 200:
            return {"error": f"iFit API returned {r.status_code}"}
        data = r.json()
    except Exception as exc:
        return {"error": f"Failed to fetch program: {exc}"}

    result = {
        "series_id": series_id,
        "title": data.get("title", ""),
        "overview": data.get("overview", ""),
        "type": data.get("type", ""),
        "rating": data.get("rating", {}),
        "trainers": [
            {"name": t.get("name", ""), "id": t.get("itemId", "")}
            for t in data.get("trainers", [])
        ],
        "workout_ids": [w.get("itemId", "") for w in data.get("workouts", [])],
        "workout_titles": [w.get("title", "") for w in data.get("workouts", [])],
        "workout_count": len(data.get("workouts", [])),
    }

    if r2_configured():
        upload_json(f"programs/{series_id}.json", result)

    return result


def get_ifit_workout_details(workout_id: str) -> dict:
    """Get detailed info about a specific iFit workout by ID.

    Returns metadata, exercise breakdown (if VTT captions are available),
    structure, and trainer info."""
    import httpx as _httpx
    from scripts.ifit_auth import get_auth_headers

    try:
        headers = get_auth_headers()
    except RuntimeError as exc:
        return {"error": str(exc)}

    try:
        r = _httpx.get(
            f"https://gateway.ifit.com/lycan/v1/workouts/{workout_id}",
            headers=headers,
            timeout=15,
        )
        if r.status_code != 200:
            return {"error": f"iFit API returned {r.status_code}"}
        meta = r.json()
    except Exception as exc:
        return {"error": f"Failed to fetch workout: {exc}"}

    from scripts.ifit_recommend import classify_workout
    classification = classify_workout(meta)

    difficulty = meta.get("difficulty", {})
    estimates = meta.get("estimates", {})
    ratings = meta.get("ratings", {})
    trainer_meta = meta.get("metadata", {})

    result = {
        "title": meta.get("title", ""),
        "description": meta.get("description", ""),
        "type": meta.get("type", ""),
        "difficulty": difficulty.get("rating", "") if isinstance(difficulty, dict) else str(difficulty),
        "duration_min": round(estimates.get("time", 0) / 60) if estimates.get("time") else None,
        "calories_est": estimates.get("calories"),
        "rating_avg": ratings.get("average", 0),
        "rating_count": ratings.get("count", 0),
        "muscle_groups": sorted(classification.get("muscle_groups", set())),
        "styles": sorted(classification.get("styles", set())),
        "categories": sorted(classification.get("categories", set())),
        "subcategories": sorted(classification.get("subcategories", set())),
        "required_equipment": meta.get("required_equipment", []),
        "workout_group_id": meta.get("workout_group_id"),
    }

    if trainer_meta.get("trainer"):
        try:
            tr = _httpx.get(
                f"https://api.ifit.com/v1/trainers/{trainer_meta['trainer']}",
                headers=headers,
                timeout=10,
            )
            if tr.status_code == 200:
                td = tr.json()
                result["trainer"] = {
                    "name": f"{td.get('first_name', '')} {td.get('last_name', '')}".strip(),
                    "bio": td.get("short_bio", ""),
                }
        except Exception:
            pass

    return result


def recommend_strength_workout(user_slug: str) -> dict:
    """Run the two-stage iFit strength workout recommendation pipeline.

    Returns top 3 workout recommendations with exercise breakdowns,
    muscle focus analysis, and scoring rationale.  Uses the athlete's
    current TSB, vitals, muscle load, goals, and iFit preferences."""
    from scripts.ifit_strength_recommend import recommend, AthleteState
    from dataclasses import asdict

    recs = recommend(user_slug)
    if not recs:
        return {"error": "No recommendations generated. Ensure library cache exists (run ifit_list_series.py)."}

    return {
        "recommendations": [asdict(r) for r in recs],
        "count": len(recs),
        "instructions": (
            "Present these 3 workouts to the user with their exercise breakdowns. "
            "Explain why each was chosen based on the reasoning field. "
            "If the user picks one, offer to create a Hevy routine for it "
            "using create_hevy_routine."
        ),
    }


def suggest_feature(
    user_slug: str,
    title: str,
    description: str,
    category: str = "enhancement",
) -> dict:
    """Open a GitHub issue for a user-suggested feature.

    Requires GITHUB_TOKEN and GITHUB_REPO env vars."""
    import httpx

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "amasolov/health-coach")

    if not token:
        return {
            "error": "GitHub integration is not configured. "
            "Ask the administrator to set GITHUB_TOKEN in the addon options."
        }

    label_map = {
        "enhancement": "enhancement",
        "bug": "bug",
        "question": "question",
    }
    label = label_map.get(category.lower(), "enhancement")

    body = (
        f"**Suggested by:** `{user_slug}`\n\n"
        f"### Description\n\n{description}"
    )

    try:
        resp = httpx.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "body": body, "labels": [label]},
            timeout=15,
        )
        resp.raise_for_status()
        issue = resp.json()
        return {
            "status": "created",
            "issue_number": issue["number"],
            "url": issue["html_url"],
            "title": issue["title"],
        }
    except httpx.HTTPStatusError as exc:
        return {"error": f"GitHub API error {exc.response.status_code}: {exc.response.text}"}
    except Exception as exc:
        return {"error": str(exc)}


def report_exercise_correction(
    user_slug: str,
    workout_id: str,
    feedback: str,
) -> dict:
    """Report incorrect exercise data for an iFit workout.

    Gathers the current extracted exercises, transcript, and workout metadata,
    then opens a GitHub issue so the data can be reviewed and corrected."""
    import json as _json
    import httpx
    from pathlib import Path as _P

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "amasolov/health-coach")

    if not token:
        return {
            "error": "GitHub integration is not configured. "
            "Ask the administrator to set GITHUB_TOKEN in the addon options."
        }

    # Gather workout metadata from library cache
    cache_dir = _P(__file__).resolve().parent.parent / ".ifit_capture"
    library_path = cache_dir / "library_workouts.json"
    trainers_path = cache_dir / "trainers.json"

    workout_title = workout_id
    trainer_name = ""
    if library_path.exists():
        with open(library_path) as f:
            for w in _json.load(f):
                if w.get("id") == workout_id:
                    workout_title = w.get("title", workout_id)
                    tid = w.get("trainer_id", "")
                    if tid and trainers_path.exists():
                        with open(trainers_path) as tf:
                            trainers = _json.load(tf)
                        trainer_name = trainers.get(tid, {}).get("name", "")
                    break

    # Gather current exercises from R2 or local cache
    exercises_json = ""
    try:
        from scripts.r2_store import is_configured as r2_ok, download_json, download_text
        if r2_ok():
            exercises = download_json(f"exercises/{workout_id}.json")
            if exercises:
                exercises_json = _json.dumps(exercises, indent=2)
    except Exception:
        pass

    if not exercises_json:
        exercise_cache_path = cache_dir / "exercise_cache.json"
        if exercise_cache_path.exists():
            with open(exercise_cache_path) as f:
                cache = _json.load(f)
            if workout_id in cache:
                exercises_json = _json.dumps(cache[workout_id], indent=2)

    # Gather transcript from R2
    transcript_snippet = ""
    try:
        from scripts.r2_store import is_configured as r2_ok, download_text
        if r2_ok():
            transcript = download_text(f"transcripts/{workout_id}.txt")
            if transcript:
                transcript_snippet = transcript[:500]
                if len(transcript) > 500:
                    transcript_snippet += "..."
    except Exception:
        pass

    # Build issue body
    parts = [
        f"**Reported by:** `{user_slug}`",
        f"**Workout:** {workout_title} (`{workout_id}`)",
    ]
    if trainer_name:
        parts.append(f"**Trainer:** {trainer_name}")

    parts.append(f"\n### User Feedback\n\n{feedback}")

    if exercises_json:
        parts.append(f"\n### Current Extracted Exercises\n\n```json\n{exercises_json}\n```")
    else:
        parts.append("\n### Current Extracted Exercises\n\n_No exercises extracted yet._")

    if transcript_snippet:
        parts.append(f"\n### Transcript (first 500 chars)\n\n> {transcript_snippet}")

    body = "\n".join(parts)
    issue_title = f"Exercise data correction: {workout_title}"

    try:
        resp = httpx.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "title": issue_title,
                "body": body,
                "labels": ["exercise-data"],
            },
            timeout=15,
        )
        resp.raise_for_status()
        issue = resp.json()
        return {
            "status": "created",
            "issue_number": issue["number"],
            "url": issue["html_url"],
            "title": issue["title"],
            "workout": workout_title,
        }
    except httpx.HTTPStatusError as exc:
        return {"error": f"GitHub API error {exc.response.status_code}: {exc.response.text}"}
    except Exception as exc:
        return {"error": str(exc)}


def sync_data(user_slug: str, user_id: int, hevy_api_key: str = "", full_sync: bool = False) -> dict:
    """Trigger an immediate data sync for the user (Garmin + Hevy).

    full_sync=True ignores the incremental start date and pulls all historical
    data from the beginning of time (Garmin: back to 2000-01-01; Hevy: all pages).
    Use this when the normal sync shows 0 new items but data is missing.

    Returns a summary of what was found and inserted."""
    from scripts.sync_garmin import sync_user as _sync_garmin
    from scripts.sync_hevy import sync_user as _sync_hevy

    results: dict[str, Any] = {}

    garmin_result = _sync_garmin(user_slug, user_id, full_sync=full_sync)
    if "error" in garmin_result:
        results["garmin"] = {"status": "skipped", "reason": garmin_result["error"]}
    else:
        results["garmin"] = {
            "status": "ok",
            "activities_found": garmin_result.get("activities_found", 0),
            "activities_new": garmin_result.get("activities_inserted", 0),
            "body_comp_found": garmin_result.get("body_comp_found", 0),
            "body_comp_new": garmin_result.get("body_comp_inserted", 0),
            "vitals_found": garmin_result.get("vitals_found", 0),
            "vitals_new": garmin_result.get("vitals_inserted", 0),
        }

    if hevy_api_key:
        hevy_result = _sync_hevy(user_slug, user_id, hevy_api_key, full_sync=full_sync)
        if "error" in hevy_result:
            results["hevy"] = {"status": "skipped", "reason": hevy_result["error"]}
        else:
            results["hevy"] = {
                "status": "ok",
                "workouts_found": hevy_result.get("workouts_found", 0),
                "workouts_new": hevy_result.get("workouts_inserted", 0),
                "sets_new": hevy_result.get("sets_inserted", 0),
            }
    else:
        results["hevy"] = {"status": "skipped", "reason": "No Hevy API key configured"}

    return {"synced": results, "full_sync": full_sync}


def create_hevy_routine_from_recommendation(
    user_slug: str,
    recommendation_index: int = 0,
    hevy_api_key: str = "",
) -> dict:
    """Create a Hevy routine from a previously generated recommendation.

    recommendation_index: 0-based index into the last recommendations
    (saved in .ifit_capture/recommendations.json).
    hevy_api_key: the user's Hevy API key."""
    import json as _json
    from scripts.ifit_strength_recommend import (
        create_hevy_routine,
        Recommendation,
    )

    cache_path = ROOT / ".ifit_capture" / "recommendations.json"
    if not cache_path.exists():
        return {"error": "No recommendations cached. Run recommend_strength_workout first."}

    with open(cache_path) as f:
        recs_data = _json.load(f)

    if recommendation_index < 0 or recommendation_index >= len(recs_data):
        return {"error": f"Invalid index {recommendation_index}. {len(recs_data)} recommendations available."}

    rec_dict = recs_data[recommendation_index]
    rec = Recommendation(**rec_dict)

    if not hevy_api_key:
        return {"error": "hevy_api_key required to create a routine."}

    return create_hevy_routine(rec, hevy_api_key)

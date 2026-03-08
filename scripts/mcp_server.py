#!/usr/bin/env python3
"""
MCP server for the Health Tracker addon.

Exposes fitness data, training zones, body composition, vitals, and
workout generation as MCP tools over Streamable HTTP. Each user
authenticates with a bearer API key; all queries are scoped to that
user's data automatically.

Usage (standalone):
    python scripts/mcp_server.py          # reads MCP_PORT, USERS_JSON from env
    # or via addon run.sh (background)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import yaml
from fastmcp import FastMCP, Context
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
ATHLETE_PATH = ROOT / "config" / "athlete.yaml"
ZONES_PATH = ROOT / "config" / "zones.yaml"
TEMPLATES_PATH = ROOT / "config" / "treadmill_templates.yaml"

MCP_PORT = int(os.environ.get("MCP_PORT", "8765"))
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# User registry (built from USERS_JSON or .env)
# ---------------------------------------------------------------------------

_TOKEN_TO_USER: dict[str, dict] = {}


def _build_user_registry() -> None:
    """Populate token->user map from USERS_JSON or fallback env vars."""
    _TOKEN_TO_USER.clear()

    users_json = os.environ.get("USERS_JSON")
    if users_json:
        for u in json.loads(users_json):
            key = u.get("mcp_api_key", "")
            if key:
                _TOKEN_TO_USER[key] = u
        return

    # Local dev fallback: single-user with a dev key
    dev_key = os.environ.get("MCP_API_KEY", "dev")
    slug = os.environ.get("USER_SLUG", "alexey")
    _TOKEN_TO_USER[dev_key] = {"slug": slug, "name": slug, "mcp_api_key": dev_key}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "health"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def _resolve_user_id(slug: str) -> int | None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE slug = %s", (slug,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _query(sql: str, params: tuple = ()) -> list[dict]:
    conn = _get_conn()
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
# Auth middleware
# ---------------------------------------------------------------------------

class BearerAuthMiddleware(Middleware):
    """Verify Bearer token and inject user context."""

    async def _authenticate(self, context: MiddlewareContext) -> None:
        headers = get_http_headers()
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise ToolError("Missing or invalid Authorization header")

        token = auth.removeprefix("Bearer ").strip()
        user = _TOKEN_TO_USER.get(token)
        if not user:
            raise ToolError("Invalid API key")

        user_id = _resolve_user_id(user["slug"])
        if user_id is None:
            raise ToolError(f"User '{user['slug']}' not found in database")

        context.fastmcp_context.set_state("user_id", user_id)
        context.fastmcp_context.set_state("user_slug", user["slug"])
        context.fastmcp_context.set_state("user_name", user.get("name", user["slug"]))

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        await self._authenticate(context)
        return await call_next(context)


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="Health Tracker",
    instructions=(
        "Health and fitness coaching server. All data is scoped to the "
        "authenticated user. Use the available tools to query training data, "
        "body composition, vitals, zones, and generate workouts."
    ),
)
mcp.add_middleware(BearerAuthMiddleware())


def _uid(ctx: Context) -> int:
    return ctx.get_state("user_id")


def _uslug(ctx: Context) -> str:
    return ctx.get_state("user_slug")


# ===== FITNESS / PMC =====

@mcp.tool
def get_fitness_summary(ctx: Context) -> dict:
    """Get current fitness status: CTL (fitness), ATL (fatigue), TSB (form),
    ramp rate, and a plain-language interpretation. Also includes 8-week
    projection of CTL."""
    uid = _uid(ctx)
    rows = _query(
        """SELECT time, tss, ctl, atl, tsb, ramp, source
           FROM training_load
           WHERE user_id = %s AND source = 'calculated'
           ORDER BY time DESC LIMIT 1""",
        (uid,),
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

    proj = _query(
        """SELECT time, ctl, atl, tsb FROM training_load
           WHERE user_id = %s AND source = 'projected'
           ORDER BY time""",
        (uid,),
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


@mcp.tool
def get_training_load(
    ctx: Context,
    start_date: str = "",
    end_date: str = "",
    days: int = 90,
) -> list[dict]:
    """Get daily TSS, CTL, ATL, TSB for a date range. Defaults to last 90 days.
    Dates in YYYY-MM-DD format."""
    uid = _uid(ctx)
    if not start_date:
        start_date = (date.today() - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (date.today() + timedelta(days=1)).isoformat()

    return _query(
        """SELECT time, tss, ctl, atl, tsb, ramp, source
           FROM training_load
           WHERE user_id = %s AND time >= %s AND time < %s
           ORDER BY time""",
        (uid, start_date, end_date),
    )


# ===== ACTIVITIES =====

@mcp.tool
def get_activities(
    ctx: Context,
    start_date: str = "",
    end_date: str = "",
    days: int = 30,
    sport: str = "",
    limit: int = 50,
) -> list[dict]:
    """List activities with metrics. Filter by date range and/or sport type
    (running, cycling, strength_training, etc.). Defaults to last 30 days."""
    uid = _uid(ctx)
    if not start_date:
        start_date = (date.today() - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (date.today() + timedelta(days=1)).isoformat()

    sql = """SELECT time, activity_type, title, duration_s, distance_m,
                    elevation_gain_m, avg_hr, max_hr, avg_power, max_power,
                    normalized_power, tss, intensity_factor, avg_cadence,
                    avg_pace_sec_km, calories,
                    training_effect_ae, training_effect_an
             FROM activities
             WHERE user_id = %s AND time >= %s AND time < %s"""
    params: list = [uid, start_date, end_date]

    if sport:
        sql += " AND LOWER(activity_type) LIKE %s"
        params.append(f"%{sport.lower()}%")

    sql += " ORDER BY time DESC LIMIT %s"
    params.append(limit)

    return _query(sql, tuple(params))


@mcp.tool
def get_activity_detail(ctx: Context, activity_time: str) -> dict:
    """Get full detail for a single activity by its timestamp (ISO format)."""
    uid = _uid(ctx)
    rows = _query(
        """SELECT * FROM activities
           WHERE user_id = %s AND time = %s LIMIT 1""",
        (uid, activity_time),
    )
    if not rows:
        raise ToolError("Activity not found")
    return rows[0]


# ===== BODY COMPOSITION =====

@mcp.tool
def get_body_composition(
    ctx: Context,
    start_date: str = "",
    end_date: str = "",
    days: int = 90,
) -> list[dict]:
    """Get body composition trend (weight, body fat %, muscle mass, BMI)
    over a date range. Defaults to last 90 days."""
    uid = _uid(ctx)
    if not start_date:
        start_date = (date.today() - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (date.today() + timedelta(days=1)).isoformat()

    return _query(
        """SELECT time, weight_kg, body_fat_pct, muscle_mass_kg,
                  bone_mass_kg, bmi, body_water_pct
           FROM body_composition
           WHERE user_id = %s AND time >= %s AND time < %s
           ORDER BY time""",
        (uid, start_date, end_date),
    )


# ===== VITALS =====

@mcp.tool
def get_vitals(
    ctx: Context,
    start_date: str = "",
    end_date: str = "",
    days: int = 30,
) -> list[dict]:
    """Get daily vitals (resting HR, HRV, blood pressure, sleep, stress,
    body battery, SpO2). Defaults to last 30 days."""
    uid = _uid(ctx)
    if not start_date:
        start_date = (date.today() - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (date.today() + timedelta(days=1)).isoformat()

    return _query(
        """SELECT time, resting_hr, hrv_ms, bp_systolic, bp_diastolic,
                  bp_pulse, sleep_score, sleep_duration_min, stress_avg,
                  body_battery_high, body_battery_low, spo2_avg,
                  respiration_avg
           FROM vitals
           WHERE user_id = %s AND time >= %s AND time < %s
           ORDER BY time""",
        (uid, start_date, end_date),
    )


# ===== ZONES & PROFILE =====

@mcp.tool
def get_training_zones(ctx: Context) -> dict:
    """Get current training zones: heart rate, running power, cycling power,
    and running pace with absolute lower/upper bounds."""
    slug = _uslug(ctx)
    zones = _load_yaml(ZONES_PATH)
    user_zones = zones.get("users", {}).get(slug)
    if not user_zones:
        return {"error": f"No zones configured for user '{slug}'"}

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


@mcp.tool
def get_athlete_profile(ctx: Context) -> dict:
    """Get the athlete's profile: goals, thresholds, body composition,
    training status, and treadmill zone-to-speed mapping."""
    slug = _uslug(ctx)
    athlete = _load_yaml(ATHLETE_PATH)
    user_data = athlete.get("users", {}).get(slug)
    if not user_data:
        return {"error": f"No profile configured for user '{slug}'"}

    return {
        "profile": user_data.get("profile"),
        "goals": user_data.get("goals"),
        "thresholds": user_data.get("thresholds"),
        "body": user_data.get("body"),
        "training_status": user_data.get("training_status"),
    }


# ===== STRENGTH =====

@mcp.tool
def get_strength_sessions(
    ctx: Context,
    start_date: str = "",
    end_date: str = "",
    days: int = 30,
    exercise: str = "",
) -> list[dict]:
    """Get strength training sets from Hevy. Filter by date range and/or
    exercise name (partial match). Defaults to last 30 days."""
    uid = _uid(ctx)
    if not start_date:
        start_date = (date.today() - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (date.today() + timedelta(days=1)).isoformat()

    sql = """SELECT time, workout_id, exercise_name, exercise_type,
                    muscle_group, set_number, set_type,
                    weight_kg, reps, rpe, duration_s, distance_m
             FROM strength_sets
             WHERE user_id = %s AND time >= %s AND time < %s"""
    params: list = [uid, start_date, end_date]

    if exercise:
        sql += " AND LOWER(exercise_name) LIKE %s"
        params.append(f"%{exercise.lower()}%")

    sql += " ORDER BY time DESC, workout_id, set_number LIMIT 200"
    return _query(sql, tuple(params))


# ===== TREADMILL WORKOUTS =====

@mcp.tool
def list_treadmill_templates() -> list[dict]:
    """List available treadmill workout templates with name, duration, and
    step count."""
    templates = _load_yaml(TEMPLATES_PATH).get("templates", {})
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


@mcp.tool
def generate_treadmill_workout(ctx: Context, template_key: str) -> dict:
    """Generate a structured treadmill workout from a template. Returns a
    step-by-step table with speed, incline, duration, and distance for
    entry into iFit Workout Creator."""
    slug = _uslug(ctx)
    templates = _load_yaml(TEMPLATES_PATH).get("templates", {})
    if template_key not in templates:
        available = ", ".join(templates.keys())
        raise ToolError(f"Template '{template_key}' not found. Available: {available}")

    athlete = _load_yaml(ATHLETE_PATH)
    user_data = athlete.get("users", {}).get(slug, {})
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


# ===== GARMIN AUTHENTICATION =====

from scripts.garmin_auth import (
    try_cached_login,
    start_login,
    finish_mfa_login,
    get_auth_status,
)


def _get_garmin_creds(ctx: Context) -> tuple[str, str]:
    """Look up Garmin email/password for the authenticated user."""
    slug = _uslug(ctx)
    for user in _TOKEN_TO_USER.values():
        if user.get("slug") == slug:
            email = user.get("garmin_email", "")
            password = user.get("garmin_password", "")
            return email, password
    return "", ""


@mcp.tool
def garmin_auth_status(ctx: Context) -> dict:
    """Check whether Garmin Connect authentication is set up and tokens are
    valid for the current user."""
    slug = _uslug(ctx)
    status = get_auth_status(slug)
    email, _ = _get_garmin_creds(ctx)
    status["garmin_email"] = email or "(not configured)"
    return status


@mcp.tool
def garmin_authenticate(ctx: Context) -> dict:
    """Start Garmin Connect authentication. Uses the email/password from the
    addon config. If MFA is required, returns a prompt -- the user should
    then call garmin_submit_mfa with the code they received."""
    slug = _uslug(ctx)

    # First try cached tokens
    client = try_cached_login(slug)
    if client:
        return {"status": "ok", "message": "Already authenticated with cached tokens."}

    email, password = _get_garmin_creds(ctx)
    if not email or not password:
        raise ToolError(
            "Garmin credentials not configured. Set garmin_email and "
            "garmin_password in the addon config (or secrets for local dev)."
        )

    result, client = start_login(slug, email, password)

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
        raise ToolError(result)


@mcp.tool
def garmin_submit_mfa(ctx: Context, mfa_code: str) -> dict:
    """Complete Garmin Connect MFA authentication with the code the user
    received (via email or authenticator app)."""
    slug = _uslug(ctx)

    result, client = finish_mfa_login(slug, mfa_code)

    if result == "ok":
        return {"status": "ok", "message": "MFA verified. Tokens cached for future use."}
    else:
        raise ToolError(result)


# ===== ATHLETE PROFILE SETUP =====

from scripts.garmin_fetch import (
    fetch_garmin_profile,
    merge_into_athlete_yaml,
    update_athlete_field,
)
from scripts.fitness_assessment import assess_fitness


@mcp.tool
def garmin_fetch_profile(ctx: Context) -> dict:
    """Fetch athlete profile data from Garmin Connect and merge into the
    config. Auto-populates: user profile (DOB, sex, height), body
    composition, resting HR, max HR (from recent activities), VO2max,
    lactate threshold (HR + pace + power), and cycling FTP.

    Only fills fields that are currently null -- never overwrites.
    Returns what was fetched, what was written, and what's still missing
    with hints on how to obtain each value."""
    slug = _uslug(ctx)

    client = try_cached_login(slug)
    if not client:
        raise ToolError(
            "Garmin not authenticated. Call garmin_authenticate first."
        )

    result = fetch_garmin_profile(slug, client)

    written = merge_into_athlete_yaml(
        str(ATHLETE_PATH), slug, result["fetched"]
    )

    return {
        "fetched": result["fetched"],
        "sources": result["sources"],
        "written_to_config": written,
        "still_missing": result["missing"],
    }


@mcp.tool
def generate_fitness_assessment(
    ctx: Context,
    lookback_days: int = 180,
    include_hevy: bool = True,
) -> dict:
    """Generate a comprehensive fitness assessment for the user by pulling
    6 months of historical data from Garmin Connect (and optionally Hevy).

    This is the recommended FIRST tool to call for a new user. It returns:
    - training_overview: volume, frequency, consistency, sport distribution
    - endurance_metrics: VO2max, running/cycling averages, estimated CTL
    - intensity_analysis: HR zone distribution and polarization assessment
    - body_composition: weight and body fat trends
    - vitals: resting HR trend
    - strength_summary: Hevy workout analysis (if available)
    - auto_profile: values auto-populated into the athlete config
    - missing_data: fields still needed, with hints and importance levels
    - recommendations: data-driven observations and suggestions

    After reviewing the assessment with the user, use update_athlete_profile
    to fill in any remaining fields they can provide."""
    slug = _uslug(ctx)

    client = try_cached_login(slug)
    if not client:
        raise ToolError(
            "Garmin not authenticated. Call garmin_authenticate first."
        )

    hevy_key = None
    if include_hevy:
        for user in _TOKEN_TO_USER.values():
            if user.get("slug") == slug:
                hevy_key = user.get("hevy_api_key")
                break

    result = assess_fitness(
        slug=slug,
        garmin_client=client,
        hevy_api_key=hevy_key,
        lookback_days=lookback_days,
    )

    profile_data = result.get("auto_profile", {})
    if profile_data.get("fetched"):
        written = merge_into_athlete_yaml(
            str(ATHLETE_PATH), slug, profile_data["fetched"]
        )
        result["written_to_config"] = written

    # Merge suggested action items (only add new ones)
    suggested = result.get("suggested_action_items", [])
    if suggested:
        existing = _load_action_items(slug)
        existing_ids = {i.get("id") for i in existing}
        added = []
        for item in suggested:
            if item.get("id") not in existing_ids:
                existing.append(item)
                added.append(item["id"])
        if added:
            _save_action_items(slug, existing)
            result["action_items_added"] = added

    return result


@mcp.tool
def update_athlete_profile(
    ctx: Context, field_path: str, value: float | int | str
) -> dict:
    """Update a single field in the athlete profile config.

    field_path is dot-separated relative to the user, for example:
      - thresholds.heart_rate.max_hr
      - thresholds.cycling.ftp
      - body.weight_kg
      - profile.date_of_birth
      - training_status.weekly_volume_hrs

    The value type should match the field (number for metrics,
    string for dates/text)."""
    slug = _uslug(ctx)
    update_athlete_field(str(ATHLETE_PATH), slug, field_path, value)

    # Auto-compute ftp_wkg when both FTP and weight are available
    athlete = _load_yaml(ATHLETE_PATH)
    user = athlete.get("users", {}).get(slug, {})
    if "ftp" in field_path or "weight" in field_path:
        ftp = (user.get("thresholds", {}).get("cycling", {}).get("ftp"))
        weight = user.get("body", {}).get("weight_kg")
        if ftp and weight and weight > 0:
            wkg = round(ftp / weight, 2)
            update_athlete_field(str(ATHLETE_PATH), slug, "thresholds.cycling.ftp_wkg", wkg)
            return {
                "updated": field_path,
                "value": value,
                "also_computed": {"thresholds.cycling.ftp_wkg": wkg},
            }

    return {"updated": field_path, "value": value}


# ===== GOALS & ONBOARDING =====

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


@mcp.tool
def get_onboarding_questions(ctx: Context) -> dict:
    """Get the list of onboarding questions to ask a new user about their
    goals, preferences, and constraints. The AI should ask these
    conversationally, one or a few at a time, then store the answers
    using set_user_goals.

    Also returns any goals already on file so the AI can skip questions
    that have been answered."""
    slug = _uslug(ctx)
    athlete = _load_yaml(ATHLETE_PATH)
    user_data = athlete.get("users", {}).get(slug, {})
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


@mcp.tool
def set_user_goals(ctx: Context, goals: dict) -> dict:
    """Store the user's goals, preferences, and constraints in their
    athlete profile.

    The goals dict can contain any of these keys:
      - primary_goal: str
      - target_event: str
      - target_date: str (YYYY-MM-DD)
      - secondary_goals: list of strings
      - available_hours_per_week: number or str
      - preferred_sports: list of strings
      - constraints: list of strings
      - experience_level: str (beginner/intermediate/advanced)
      - training_preferences: dict with 'likes' and 'dislikes' keys

    Only provided keys are updated; existing values are preserved."""
    slug = _uslug(ctx)
    athlete = _load_yaml(ATHLETE_PATH)
    user = athlete.setdefault("users", {}).setdefault(slug, {})
    existing = user.setdefault("goals", {})

    for key, value in goals.items():
        if value is not None:
            existing[key] = value

    import yaml as _yaml
    with open(ATHLETE_PATH, "w") as f:
        _yaml.dump(athlete, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return {"updated_goals": existing}


@mcp.tool
def get_user_goals(ctx: Context) -> dict:
    """Get the user's current goals, preferences, and constraints."""
    slug = _uslug(ctx)
    athlete = _load_yaml(ATHLETE_PATH)
    user_data = athlete.get("users", {}).get(slug, {})
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


# ===== ACTION ITEMS =====

def _load_action_items(slug: str) -> list[dict]:
    athlete = _load_yaml(ATHLETE_PATH)
    return athlete.get("users", {}).get(slug, {}).get("action_items", [])


def _save_action_items(slug: str, items: list[dict]) -> None:
    import yaml as _yaml
    athlete = _load_yaml(ATHLETE_PATH)
    athlete.setdefault("users", {}).setdefault(slug, {})["action_items"] = items
    with open(ATHLETE_PATH, "w") as f:
        _yaml.dump(athlete, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


@mcp.tool
def get_action_items(
    ctx: Context, status_filter: str = ""
) -> dict:
    """Get the user's action items. This should be called at the START of
    every conversation to review outstanding tasks with the user.

    Optional status_filter: 'pending', 'in_progress', 'completed', or
    blank for all items. Returns items grouped by priority."""
    slug = _uslug(ctx)
    items = _load_action_items(slug)

    if status_filter:
        items = [i for i in items if i.get("status") == status_filter]

    high = [i for i in items if i.get("priority") == "high"]
    medium = [i for i in items if i.get("priority") == "medium"]
    low = [i for i in items if i.get("priority") == "low"]

    pending = sum(1 for i in _load_action_items(slug) if i.get("status") == "pending")
    in_progress = sum(1 for i in _load_action_items(slug) if i.get("status") == "in_progress")

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


@mcp.tool
def add_action_item(
    ctx: Context,
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
    slug = _uslug(ctx)
    items = _load_action_items(slug)

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
        "created": date.today().isoformat(),
        "due": due or None,
        "completed": None,
    }
    items.append(new_item)
    _save_action_items(slug, items)
    return {"added": new_item}


@mcp.tool
def update_action_item(
    ctx: Context,
    item_id: str,
    status: str = "",
    priority: str = "",
    title: str = "",
    description: str = "",
    due: str = "",
    note: str = "",
) -> dict:
    """Update an existing action item. Use this to mark items as
    completed, change priority, add notes, or update details.

    status: pending, in_progress, completed, skipped
    priority: high, medium, low"""
    slug = _uslug(ctx)
    items = _load_action_items(slug)

    target = None
    for item in items:
        if item.get("id") == item_id:
            target = item
            break

    if not target:
        raise ToolError(f"Action item '{item_id}' not found")

    if status:
        target["status"] = status
        if status == "completed":
            target["completed"] = date.today().isoformat()
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
        existing_notes.append({"date": date.today().isoformat(), "text": note})
        target["notes"] = existing_notes

    _save_action_items(slug, items)
    return {"updated": target}


@mcp.tool
def complete_action_item(ctx: Context, item_id: str, note: str = "") -> dict:
    """Mark an action item as completed. Optionally add a completion note
    (e.g. 'LTHR measured at 168 bpm')."""
    slug = _uslug(ctx)
    items = _load_action_items(slug)

    target = None
    for item in items:
        if item.get("id") == item_id:
            target = item
            break

    if not target:
        raise ToolError(f"Action item '{item_id}' not found")

    target["status"] = "completed"
    target["completed"] = date.today().isoformat()
    if note:
        existing_notes = target.get("notes", [])
        existing_notes.append({"date": date.today().isoformat(), "text": note})
        target["notes"] = existing_notes

    _save_action_items(slug, items)
    return {"completed": target}


# ===== INTEGRATIONS & HARDWARE REGISTRY =====

EQUIPMENT_PATH = ROOT / "config" / "equipment.yaml"

SUPPORTED_INTEGRATIONS: list[dict] = [
    # --- Wearables ---
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
    # --- Cycling ---
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
    # --- Treadmill ---
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
    # --- Health Monitoring ---
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
    # --- Gym Equipment ---
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
    # --- Software Integrations ---
    {
        "id": "garmin_connect",
        "name": "Garmin Connect",
        "category": "software",
        "description": "Central data hub -- all Garmin device data flows here. Primary data source for activities, HR, sleep, body composition, vitals.",
        "data_provided": ["activities", "heart_rate", "hrv", "sleep", "stress", "body_battery",
                          "spo2", "vo2max", "body_composition", "blood_pressure", "running_power",
                          "cycling_power", "training_effect", "lactate_threshold"],
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


@mcp.tool
def get_supported_integrations(ctx: Context, category: str = "") -> dict:
    """Get the list of all supported hardware and software integrations.

    This is used during onboarding to show new users what equipment and
    software we can work with, so they can indicate what they have.

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


@mcp.tool
def set_user_integrations(ctx: Context, integrations: list[str], equipment_notes: dict | None = None) -> dict:
    """Store which integrations and hardware the user has.

    integrations: list of integration IDs from get_supported_integrations
        (e.g. ['garmin_connect', 'garmin_fenix', 'garmin_hrm_pro', 'hevy', 'smart_trainer'])
    equipment_notes: optional dict of integration_id -> note string
        (e.g. {'smart_trainer': 'Wahoo Kickr v5', 'home_gym': 'Dumbbells, 20kg kettlebell, squat rack'})
    """
    slug = _uslug(ctx)
    valid_ids = {i["id"] for i in SUPPORTED_INTEGRATIONS}
    unknown = [i for i in integrations if i not in valid_ids]
    if unknown:
        raise ToolError(f"Unknown integration IDs: {unknown}. Use get_supported_integrations to see valid IDs.")

    import yaml as _yaml
    athlete = _load_yaml(ATHLETE_PATH)
    user = athlete.setdefault("users", {}).setdefault(slug, {})

    user_integrations = []
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

    with open(ATHLETE_PATH, "w") as f:
        _yaml.dump(athlete, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

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


@mcp.tool
def get_user_integrations(ctx: Context) -> dict:
    """Get the user's configured integrations and hardware."""
    slug = _uslug(ctx)
    athlete = _load_yaml(ATHLETE_PATH)
    user_data = athlete.get("users", {}).get(slug, {})
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
# Health check endpoint
# ---------------------------------------------------------------------------

from starlette.requests import Request
from starlette.responses import JSONResponse


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "users": len(_TOKEN_TO_USER)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _build_user_registry()

    user_count = len(_TOKEN_TO_USER)
    print(f"Health Tracker MCP server starting on {MCP_HOST}:{MCP_PORT}")
    print(f"  Registered users: {user_count}")
    print(f"  Endpoint: http://{MCP_HOST}:{MCP_PORT}/mcp")

    mcp.run(transport="streamable-http", host=MCP_HOST, port=MCP_PORT, path="/mcp")


if __name__ == "__main__":
    main()

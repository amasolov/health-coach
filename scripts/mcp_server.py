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
    """Get the athlete's profile: thresholds, body composition, training
    status, and treadmill zone-to-speed mapping."""
    slug = _uslug(ctx)
    athlete = _load_yaml(ATHLETE_PATH)
    user_data = athlete.get("users", {}).get(slug)
    if not user_data:
        return {"error": f"No profile configured for user '{slug}'"}

    return {
        "profile": user_data.get("profile"),
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

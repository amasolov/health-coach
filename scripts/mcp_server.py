#!/usr/bin/env python3
"""
MCP server for the Health Coach addon.

Exposes fitness data, training zones, body composition, vitals, and
workout generation as MCP tools over Streamable HTTP. Each user
authenticates with a bearer API key; all queries are scoped to that
user's data automatically.

Tool logic lives in scripts.health_tools; this module is a thin wrapper
that adds MCP transport, auth middleware, and Context extraction.

Usage (standalone):
    python scripts/mcp_server.py          # reads MCP_PORT, USERS_JSON from env
    # or via addon run.sh (background)
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastmcp import FastMCP, Context
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext

from scripts import health_tools

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MCP_PORT = int(os.environ.get("MCP_PORT", "8765"))
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")

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

    dev_key = os.environ.get("MCP_API_KEY", "dev")
    slug = os.environ.get("USER_SLUG", "alexey")
    _TOKEN_TO_USER[dev_key] = {"slug": slug, "name": slug, "mcp_api_key": dev_key}


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

        user_id = health_tools.resolve_user_id(user["slug"])
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
    name="Health Coach",
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


def _get_garmin_creds(ctx: Context) -> tuple[str, str]:
    """Look up Garmin email/password for the authenticated user."""
    slug = _uslug(ctx)
    for user in _TOKEN_TO_USER.values():
        if user.get("slug") == slug:
            return user.get("garmin_email", ""), user.get("garmin_password", "")
    return "", ""


def _get_hevy_key(ctx: Context) -> str | None:
    slug = _uslug(ctx)
    for user in _TOKEN_TO_USER.values():
        if user.get("slug") == slug:
            return user.get("hevy_api_key") or None
    return None


def _wrap(fn, *args, **kwargs):
    """Call a health_tools function, converting ValueError to ToolError."""
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


# ===== FITNESS / PMC =====

@mcp.tool
def get_fitness_summary(ctx: Context) -> dict:
    """Get current fitness status: CTL (fitness), ATL (fatigue), TSB (form),
    ramp rate, and a plain-language interpretation. Also includes 8-week
    projection of CTL."""
    return health_tools.get_fitness_summary(_uid(ctx))


@mcp.tool
def get_training_load(
    ctx: Context,
    start_date: str = "",
    end_date: str = "",
    days: int = 90,
) -> list[dict]:
    """Get daily TSS, CTL, ATL, TSB for a date range. Defaults to last 90 days.
    Dates in YYYY-MM-DD format."""
    return health_tools.get_training_load(_uid(ctx), start_date, end_date, days)


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
    return health_tools.get_activities(_uid(ctx), start_date, end_date, days, sport, limit)


@mcp.tool
def get_activity_detail(ctx: Context, activity_time: str) -> dict:
    """Get full detail for a single activity by its timestamp (ISO format)."""
    return _wrap(health_tools.get_activity_detail, _uid(ctx), activity_time)


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
    return health_tools.get_body_composition(_uid(ctx), start_date, end_date, days)


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
    return health_tools.get_vitals(_uid(ctx), start_date, end_date, days)


# ===== ZONES & PROFILE =====

@mcp.tool
def get_training_zones(ctx: Context) -> dict:
    """Get current training zones: heart rate, running power, cycling power,
    and running pace with absolute lower/upper bounds."""
    return health_tools.get_training_zones(_uslug(ctx))


@mcp.tool
def get_athlete_profile(ctx: Context) -> dict:
    """Get the athlete's profile: goals, thresholds, body composition,
    training status, and treadmill zone-to-speed mapping."""
    return health_tools.get_athlete_profile(_uslug(ctx))


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
    return health_tools.get_strength_sessions(
        _uid(ctx), start_date, end_date, days, exercise,
    )


# ===== TREADMILL WORKOUTS =====

@mcp.tool
def list_treadmill_templates() -> list[dict]:
    """List available treadmill workout templates with name, duration, and
    step count."""
    return health_tools.list_treadmill_templates()


@mcp.tool
def generate_treadmill_workout(ctx: Context, template_key: str) -> dict:
    """Generate a structured treadmill workout from a template. Returns a
    step-by-step table with speed, incline, duration, and distance for
    entry into iFit Workout Creator."""
    return _wrap(health_tools.generate_treadmill_workout, _uslug(ctx), template_key)


# ===== GARMIN AUTHENTICATION =====

@mcp.tool
def garmin_auth_status(ctx: Context) -> dict:
    """Check whether Garmin Connect authentication is set up and tokens are
    valid for the current user."""
    email, _ = _get_garmin_creds(ctx)
    return health_tools.garmin_auth_status(_uslug(ctx), email)


@mcp.tool
def garmin_authenticate(ctx: Context) -> dict:
    """Start Garmin Connect authentication. Uses the email/password from the
    addon config. If MFA is required, returns a prompt -- the user should
    then call garmin_submit_mfa with the code they received."""
    email, password = _get_garmin_creds(ctx)
    return _wrap(health_tools.garmin_authenticate, _uslug(ctx), email, password)


@mcp.tool
def garmin_submit_mfa(ctx: Context, mfa_code: str) -> dict:
    """Complete Garmin Connect MFA authentication with the code the user
    received (via email or authenticator app)."""
    return _wrap(health_tools.garmin_submit_mfa, _uslug(ctx), mfa_code)


# ===== ATHLETE PROFILE SETUP =====

@mcp.tool
def garmin_fetch_profile(ctx: Context) -> dict:
    """Fetch athlete profile data from Garmin Connect and merge into the
    config. Auto-populates: user profile (DOB, sex, height), body
    composition, resting HR, max HR (from recent activities), VO2max,
    lactate threshold (HR + pace + power), and cycling FTP.

    Only fills fields that are currently null -- never overwrites.
    Returns what was fetched, what was written, and what's still missing
    with hints on how to obtain each value."""
    return _wrap(health_tools.garmin_fetch_profile, _uslug(ctx))


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
    hevy_key = _get_hevy_key(ctx) if include_hevy else None
    return _wrap(
        health_tools.generate_fitness_assessment,
        _uslug(ctx), hevy_key, lookback_days, include_hevy,
    )


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
    return _wrap(health_tools.update_athlete_profile, _uslug(ctx), field_path, value)


# ===== GOALS & ONBOARDING =====

@mcp.tool
def get_onboarding_questions(ctx: Context) -> dict:
    """Get the list of onboarding questions to ask a new user about their
    goals, preferences, and constraints. The AI should ask these
    conversationally, one or a few at a time, then store the answers
    using set_user_goals.

    Also returns any goals already on file so the AI can skip questions
    that have been answered."""
    return health_tools.get_onboarding_questions(_uslug(ctx))


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
    return health_tools.set_user_goals(_uslug(ctx), goals)


@mcp.tool
def get_user_goals(ctx: Context) -> dict:
    """Get the user's current goals, preferences, and constraints."""
    return health_tools.get_user_goals(_uslug(ctx))


# ===== ACTION ITEMS =====

@mcp.tool
def get_action_items(ctx: Context, status_filter: str = "") -> dict:
    """Get the user's action items. This should be called at the START of
    every conversation to review outstanding tasks with the user.

    Optional status_filter: 'pending', 'in_progress', 'completed', or
    blank for all items. Returns items grouped by priority."""
    return health_tools.get_action_items(_uslug(ctx), status_filter)


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
    return health_tools.add_action_item(_uslug(ctx), title, description, category, priority, due)


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
    return _wrap(
        health_tools.update_action_item,
        _uslug(ctx), item_id, status, priority, title, description, due, note,
    )


@mcp.tool
def complete_action_item(ctx: Context, item_id: str, note: str = "") -> dict:
    """Mark an action item as completed. Optionally add a completion note
    (e.g. 'LTHR measured at 168 bpm')."""
    return _wrap(health_tools.complete_action_item, _uslug(ctx), item_id, note)


# ===== INTEGRATIONS & HARDWARE REGISTRY =====

@mcp.tool
def get_supported_integrations(ctx: Context, category: str = "") -> dict:
    """Get the list of all supported hardware and software integrations.

    This is used during onboarding to show new users what equipment and
    software we can work with, so they can indicate what they have.

    Optional category filter: wearable, cycling, treadmill, health, gym, software"""
    return health_tools.get_supported_integrations(category)


@mcp.tool
def set_user_integrations(ctx: Context, integrations: list[str], equipment_notes: dict | None = None) -> dict:
    """Store which integrations and hardware the user has.

    integrations: list of integration IDs from get_supported_integrations
        (e.g. ['garmin_connect', 'garmin_fenix', 'garmin_hrm_pro', 'hevy', 'smart_trainer'])
    equipment_notes: optional dict of integration_id -> note string
        (e.g. {'smart_trainer': 'Wahoo Kickr v5', 'home_gym': 'Dumbbells, 20kg kettlebell, squat rack'})
    """
    return _wrap(health_tools.set_user_integrations, _uslug(ctx), integrations, equipment_notes)


@mcp.tool
def get_user_integrations(ctx: Context) -> dict:
    """Get the user's configured integrations and hardware."""
    return health_tools.get_user_integrations(_uslug(ctx))


# ===== iFit INTEGRATION =====

@mcp.tool
def recommend_ifit_workout(ctx: Context) -> dict:
    """Recommend today's iFit workout based on recent 14-day activity history,
    muscle group fatigue, and variety.  Returns top 5 ranked workouts from
    the user's up-next queue, favorites, and iFit recommendations.  Covers
    ALL workout types: running, strength, cycling, yoga, recovery, etc."""
    return _wrap(health_tools.recommend_ifit_workout, _uslug(ctx))


@mcp.tool
def search_ifit_library(ctx: Context, query: str, workout_type: str = "", limit: int = 10) -> dict:
    """Search the iFit workout library (12,000+ workouts) by title, trainer
    name, category, or keyword.  Use this when asking about specific iFit
    programs, series, trainers, or workout types."""
    return _wrap(health_tools.search_ifit_library, query, workout_type, limit)


@mcp.tool
def get_ifit_workout_details(ctx: Context, workout_id: str) -> dict:
    """Get detailed info about a specific iFit workout by its ID.

    Returns description, trainer info, muscle groups, difficulty, duration,
    equipment, ratings, program/series context, AND a full exercise breakdown.

    If the workout hasn't been synced yet, the transcript is fetched from iFit
    and exercises are extracted via LLM on the fly (then cached).  No need to
    wait for the library sync — just pass any workout ID."""
    return _wrap(health_tools.get_ifit_workout_details, workout_id)


@mcp.tool
def search_ifit_programs(ctx: Context, query: str, limit: int = 10) -> dict:
    """Search the iFit program/series index by name, trainer, or keyword.
    Returns matching programs with their workout lists.  Use when asking
    about iFit series, programs, or training plans."""
    return _wrap(health_tools.search_ifit_programs, query, limit)


@mcp.tool
def get_ifit_program_details(ctx: Context, series_id: str) -> dict:
    """Get details for an iFit program/series by its series ID.  Returns
    program overview, trainers, and the full list of workouts in order."""
    return _wrap(health_tools.get_ifit_program_details, series_id)


@mcp.tool
def discover_ifit_series(ctx: Context, workout_id: str) -> dict:
    """Discover all series/programs a workout belongs to and map every
    workout in those series.  Use when a user asks about a series and
    you have one workout ID from it.  Returns full workout lists for
    each discovered series."""
    return _wrap(health_tools.discover_ifit_series, workout_id)


@mcp.tool
def report_exercise_correction(ctx: Context, workout_id: str, feedback: str) -> dict:
    """Report incorrect exercise data for an iFit workout.  Gathers the
    current extracted exercises, transcript snippet, and workout metadata,
    then opens a GitHub issue so the data can be reviewed and corrected."""
    return _wrap(health_tools.report_exercise_correction, _uslug(ctx), workout_id, feedback)


@mcp.tool
def recommend_strength_workout(ctx: Context) -> dict:
    """Run the iFit strength workout recommendation engine (deep analysis).

    Analyses the athlete's current TSB, vitals, muscle load, goals, and
    iFit preferences to suggest 3 optimal strength workouts from the iFit
    library. Each recommendation includes a full exercise breakdown with
    Hevy-compatible names, sets, reps, and scoring rationale.

    Requires cached library data (run ifit_list_series.py once first)."""
    return _wrap(health_tools.recommend_strength_workout, _uslug(ctx))


@mcp.tool
def create_hevy_routine_from_recommendation(
    ctx: Context, recommendation_index: int = 0
) -> dict:
    """Create a Hevy routine from a previously generated iFit strength
    recommendation. Run recommend_strength_workout first, then use
    recommendation_index (0-based) to pick which workout to create.

    The routine will be created in Hevy with all exercises, sets, and reps
    pre-populated from the LLM analysis."""
    hevy_key = _get_hevy_key(ctx) or ""
    return _wrap(
        health_tools.create_hevy_routine_from_recommendation,
        _uslug(ctx), recommendation_index, hevy_key,
    )


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
    print(f"Health Coach MCP server starting on {MCP_HOST}:{MCP_PORT}")
    print(f"  Registered users: {user_count}")
    print(f"  Endpoint: http://{MCP_HOST}:{MCP_PORT}/mcp")

    mcp.run(transport="streamable-http", host=MCP_HOST, port=MCP_PORT, path="/mcp")


if __name__ == "__main__":
    main()

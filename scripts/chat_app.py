"""
Chainlit chat application for health coaching.

Provides a conversational AI interface backed by OpenRouter, with access
to the full suite of health tracker tools via scripts.health_tools.

New-user onboarding flow (when ALLOW_REGISTRATION=true):
  - OAuth users whose email is unknown land in a guided setup chat
  - Password-auth users whose slug is unknown do the same
  - Collects: name, username, timezone, Garmin credentials, Hevy API key
  - Creates the DB record, options.json entry, and athlete.yaml stub
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import chainlit as cl
from chainlit.oauth_providers import providers
from openai import AsyncOpenAI

from scripts import health_tools
from scripts import garmin_auth
from scripts import user_manager
from scripts.sync_garmin import sync_user as sync_garmin_user
from scripts.sync_hevy import sync_user as sync_hevy_user
from scripts.run_sync import sync_garmin_profile
from scripts.chat_tools_schema import TOOL_SCHEMAS, TOOL_DISPATCH
from scripts.chat_charts import maybe_chart

if os.environ.get("OAUTH_APPLE_CLIENT_ID"):
    from scripts.oauth_apple import AppleOAuthProvider
    providers.append(AppleOAuthProvider())

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "anthropic/claude-sonnet-4")
ALLOW_REGISTRATION = os.environ.get("ALLOW_REGISTRATION", "").lower() in ("true", "1", "yes")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "30"))
MAX_TOOL_ROUNDS = 10
CHAINLIT_DB_URL = os.environ.get("CHAINLIT_DB_URL", "")

_client: AsyncOpenAI | None = None

# ---------------------------------------------------------------------------
# Persistent data layer (SQLAlchemy + PostgreSQL)
# ---------------------------------------------------------------------------

if CHAINLIT_DB_URL:
    from chainlit.data.sql_alchemy import SQLAlchemyDataLayer

    @cl.data_layer
    def get_data_layer():
        return SQLAlchemyDataLayer(conninfo=CHAINLIT_DB_URL)


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
        )
    return _client


# ---------------------------------------------------------------------------
# User registry
# ---------------------------------------------------------------------------

_USERS_BY_SLUG: dict[str, dict] = {}
_USERS_BY_EMAIL: dict[str, dict] = {}


def _build_user_registry() -> None:
    _USERS_BY_SLUG.clear()
    _USERS_BY_EMAIL.clear()

    users_json = os.environ.get("USERS_JSON")
    if users_json:
        for u in json.loads(users_json):
            slug = u.get("slug", "")
            if slug:
                _USERS_BY_SLUG[slug] = u
                email = u.get("email", "").lower().strip()
                if email:
                    _USERS_BY_EMAIL[email] = u
        return

    slug = os.environ.get("USER_SLUG", "alexey")
    email = os.environ.get("USER_EMAIL", "").lower().strip()
    user = {
        "slug": slug,
        "first_name": os.environ.get("USER_FIRST_NAME", slug),
        "last_name": os.environ.get("USER_LAST_NAME", ""),
        "email": email,
        "garmin_email": os.environ.get("GARMIN_EMAIL", ""),
        "garmin_password": os.environ.get("GARMIN_PASSWORD", ""),
        "hevy_api_key": os.environ.get("HEVY_API_KEY", ""),
    }
    _USERS_BY_SLUG[slug] = user
    if email:
        _USERS_BY_EMAIL[email] = user


_build_user_registry()


def _register_user_in_memory(user_entry: dict) -> None:
    """Add a newly-registered user to the live in-memory registry."""
    slug = user_entry.get("slug", "")
    if not slug:
        return
    _USERS_BY_SLUG[slug] = user_entry
    email = user_entry.get("email", "").lower().strip()
    if email:
        _USERS_BY_EMAIL[email] = user_entry


# ---------------------------------------------------------------------------
# Authentication  —  OAuth (Google / Apple) + password fallback
# ---------------------------------------------------------------------------

_OAUTH_ENABLED = bool(
    os.environ.get("OAUTH_GOOGLE_CLIENT_ID")
    or os.environ.get("OAUTH_APPLE_CLIENT_ID")
)

if _OAUTH_ENABLED:
    @cl.oauth_callback
    def oauth_callback(
        provider_id: str,
        token: str,
        raw_user_data: dict[str, str],
        default_user: cl.User,
    ) -> cl.User | None:
        email = (
            raw_user_data.get("email", "")
            or default_user.identifier
            or ""
        ).lower().strip()

        if not email:
            return None

        user = _USERS_BY_EMAIL.get(email)
        if user:
            return cl.User(
                identifier=email,
                metadata={
                    "slug": user["slug"],
                    "first_name": user.get("first_name", user["slug"]),
                    "last_name": user.get("last_name", ""),
                    "email": email,
                    "provider": provider_id,
                },
            )

        # Unknown email — allow through for onboarding if registration is open
        if ALLOW_REGISTRATION:
            return cl.User(
                identifier=email,
                metadata={
                    "registration_pending": True,
                    "email": email,
                    "display_name": raw_user_data.get("name", ""),
                    "provider": provider_id,
                },
            )

        return None

else:
    @cl.password_auth_callback
    def password_callback(username: str, password: str) -> cl.User | None:
        user = _USERS_BY_SLUG.get(username)
        if user:
            expected = user.get("mcp_api_key") or os.environ.get("MCP_API_KEY", "")
            if not expected or password != expected:
                return None
            return cl.User(
                identifier=user.get("email", username),
                metadata={
                    "slug": user["slug"],
                    "first_name": user.get("first_name", username),
                    "last_name": user.get("last_name", ""),
                },
            )

        # Unknown user — allow through for onboarding if registration is open
        if ALLOW_REGISTRATION:
            return cl.User(
                identifier=username,
                metadata={
                    "registration_pending": True,
                    "email": username if "@" in username else "",
                    "display_name": username,
                    "provider": "password",
                },
            )

        return None


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt(user_slug: str, first_name: str) -> str:
    parts = [
        f"You are a data-driven fitness coach for {first_name}. "
        "You have access to their complete training, health, and body "
        "composition data through specialized tools.\n"
    ]

    try:
        summary = health_tools.get_fitness_summary(
            health_tools.resolve_user_id(user_slug)
        )
        if "status" not in summary:
            parts.append(
                "Current status:\n"
                f"- CTL (fitness): {summary.get('ctl_fitness')} | "
                f"ATL (fatigue): {summary.get('atl_fatigue')} | "
                f"TSB (form): {summary.get('tsb_form')}\n"
                f"- Form: {summary.get('form_status')}\n"
                f"- Ramp rate: {summary.get('ramp_rate')}%/week — "
                f"{summary.get('ramp_note')}\n"
            )
    except Exception:
        pass

    try:
        profile = health_tools.get_athlete_profile(user_slug)
        if "error" not in profile:
            goals = profile.get("goals") or {}
            if goals.get("primary_goal"):
                parts.append(f"Primary goal: {goals['primary_goal']}")
            if goals.get("preferred_sports"):
                sports = goals["preferred_sports"]
                if isinstance(sports, list):
                    sports = ", ".join(sports)
                parts.append(f"Preferred sports: {sports}")

            thresholds = profile.get("thresholds") or {}
            hr = thresholds.get("heart_rate", {})
            if hr.get("max_hr"):
                parts.append(f"Max HR: {hr['max_hr']} bpm")
            cycling = thresholds.get("cycling", {})
            if cycling.get("ftp"):
                parts.append(f"FTP: {cycling['ftp']}W")
    except Exception:
        pass

    parts.append(
        "\nGuidelines:\n"
        "- Always query tools before making recommendations\n"
        "- Consider current form (TSB) when suggesting training intensity\n"
        "- Flag concerning trends (rapid ramp rate >8%/wk, declining HRV, etc.)\n"
        "- Be specific with numbers and dates\n"
        "- When presenting time-series data, keep text concise — "
        "charts will be auto-generated from the data\n"
        "- At the start of a conversation, check action items for pending tasks\n"
        "- Be encouraging but honest about the data\n"
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _execute_tool(
    tool_name: str,
    arguments: dict,
    user_id: int,
    user_slug: str,
    user_data: dict,
) -> Any:
    entry = TOOL_DISPATCH.get(tool_name)
    if not entry:
        return {"error": f"Unknown tool: {tool_name}"}

    fn, param_kind = entry

    try:
        if param_kind == "uid":
            return fn(user_id, **arguments)
        elif param_kind == "slug":
            return fn(user_slug, **arguments)
        elif param_kind == "none":
            return fn(**arguments)
        elif param_kind == "creds":
            if tool_name == "garmin_auth_status":
                return fn(user_slug, user_data.get("garmin_email", ""))
            elif tool_name == "garmin_authenticate":
                return fn(
                    user_slug,
                    user_data.get("garmin_email", ""),
                    user_data.get("garmin_password", ""),
                )
            elif tool_name == "generate_fitness_assessment":
                hevy_key = user_data.get("hevy_api_key") or None
                return fn(user_slug, hevy_key, **arguments)
            elif tool_name == "create_hevy_routine_from_recommendation":
                hevy_key = user_data.get("hevy_api_key", "")
                return fn(user_slug, hevy_api_key=hevy_key, **arguments)
            elif tool_name == "sync_data":
                hevy_key = user_data.get("hevy_api_key", "")
                return fn(user_slug, user_id, hevy_key, **arguments)
            else:
                return fn(user_slug, **arguments)
        else:
            return fn(**arguments)
    except (ValueError, Exception) as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Session initialisation (shared by normal start and post-onboarding)
# ---------------------------------------------------------------------------

async def _init_session(user_slug: str, user_id: int, first_name: str, user_data: dict) -> None:
    cl.user_session.set("user_id", user_id)
    cl.user_session.set("user_slug", user_slug)
    cl.user_session.set("first_name", first_name)
    cl.user_session.set("user_data", user_data)

    system_prompt = _build_system_prompt(user_slug, first_name)
    cl.user_session.set("system_prompt", system_prompt)
    cl.user_session.set("messages", [{"role": "system", "content": system_prompt}])


# ---------------------------------------------------------------------------
# Onboarding flow
# ---------------------------------------------------------------------------

async def _ask(prompt: str, timeout: int = 300) -> str | None:
    """Helper: send an AskUserMessage and return the stripped response, or None on timeout."""
    res = await cl.AskUserMessage(content=prompt, timeout=timeout).send()
    if not res:
        return None
    return res["output"].strip()


def _is_skip(s: str | None) -> bool:
    return s is None or s.lower() in ("skip", "s", "no", "n", "")


async def run_onboarding(user: cl.User) -> None:
    """
    Guide a new user through account setup.
    Collects name, username, timezone, Garmin credentials, and Hevy API key,
    then creates the DB record, options.json entry, and athlete config stub.
    """
    email = user.metadata.get("email", "")
    display_name = user.metadata.get("display_name", "")

    await cl.Message(
        content=(
            "Welcome to Health Coach! I don't have you in the system yet.\n\n"
            "Let's get you set up — this takes about 2 minutes. "
            "You can type `skip` at any step to configure it later."
        )
    ).send()

    # --- Name ---
    first_name_hint = display_name.split()[0] if display_name else ""
    prompt = "What's your first name?"
    if first_name_hint:
        prompt += f" (or type `skip` to use **{first_name_hint}**)"
    raw = await _ask(prompt)
    first_name = (first_name_hint if _is_skip(raw) else (raw or first_name_hint or "User")).title()

    raw = await _ask("And your last name? (or `skip`)")
    last_name = ("" if _is_skip(raw) else (raw or "").title())

    # --- Username ---
    suggested = user_manager.find_available_slug(user_manager.make_slug(first_name))
    raw = await _ask(
        f"Choose a username — lowercase letters and numbers only.\n"
        f"Suggested: `{suggested}` — type `skip` to accept, or type your own:"
    )
    if _is_skip(raw):
        slug = suggested
    else:
        slug = re.sub(r"[^a-z0-9_]", "", (raw or "").lower()) or suggested

    if not user_manager.slug_available(slug):
        slug = user_manager.find_available_slug(slug)
        await cl.Message(content=f"That username was taken — using `{slug}` instead.").send()

    # --- Timezone ---
    raw = await _ask(
        "What's your timezone? Examples: `Australia/Sydney`, `America/New_York`, `Europe/London`\n"
        "Type `skip` to use UTC and update it later via your athlete profile:"
    )
    timezone = "UTC" if _is_skip(raw) else (raw or "UTC")

    # --- Garmin Connect ---
    garmin_email = ""
    garmin_password = ""

    raw = await _ask(
        "Do you use **Garmin Connect** for activity tracking?\n"
        "Enter your Garmin account email, or `skip` to connect later:"
    )
    if not _is_skip(raw):
        garmin_email = raw or ""

        raw = await _ask(
            f"Enter your Garmin password.\n"
            f"⚠️ This will be stored in the HA addon config to keep your sync tokens fresh. "
            f"Type `skip` to add it later via HA Settings → Add-ons → Health Coach → Configuration:"
        )
        if not _is_skip(raw):
            garmin_password = raw or ""

            async with cl.Step(name="Connecting to Garmin Connect", type="run") as step:
                step.input = f"email={garmin_email}"
                status, _ = await asyncio.to_thread(
                    garmin_auth.start_login, slug, garmin_email, garmin_password
                )
                step.output = status

            if status == "ok":
                await cl.Message(content="Garmin Connect: connected.").send()

            elif status == "needs_mfa":
                raw = await _ask(
                    "Garmin requires a verification code. "
                    "Check your email or authenticator app and enter the code:"
                )
                if raw and not _is_skip(raw):
                    async with cl.Step(name="Verifying Garmin MFA", type="run") as step:
                        step.input = "mfa_code=****"
                        status, _ = await asyncio.to_thread(
                            garmin_auth.finish_mfa_login, slug, raw
                        )
                        step.output = status

                    if status == "ok":
                        await cl.Message(content="Garmin Connect: verified.").send()
                    else:
                        await cl.Message(
                            content=f"Garmin MFA failed ({status}). "
                            "You can reconnect later by asking me to re-authenticate Garmin."
                        ).send()
                        garmin_email = ""
                        garmin_password = ""
                else:
                    garmin_email = ""
                    garmin_password = ""

            else:
                await cl.Message(
                    content=f"Garmin connection failed ({status}). "
                    "You can try again later by asking me to reconnect Garmin."
                ).send()
                garmin_email = ""
                garmin_password = ""

    # --- Hevy ---
    hevy_api_key = ""
    raw = await _ask(
        "Do you use **Hevy** for strength training?\n"
        "Enter your API key (find it at hevy.com → Settings → Developer), or `skip`:"
    )
    if not _is_skip(raw):
        hevy_api_key = raw or ""

    # --- Create the user ---
    await cl.Message(content="Creating your account...").send()

    result = await asyncio.to_thread(
        user_manager.register_user,
        email=email,
        first_name=first_name,
        last_name=last_name,
        slug=slug,
        timezone=timezone,
        garmin_email=garmin_email,
        garmin_password=garmin_password,
        hevy_api_key=hevy_api_key,
    )

    if "error" in result:
        await cl.Message(
            content=f"Registration failed: {result['error']}\n\nPlease refresh and try again."
        ).send()
        return

    user_id: int = result["user_id"]
    user_entry: dict = result["user_entry"]

    _register_user_in_memory(user_entry)

    garmin_status = "connected" if garmin_email else "not configured"
    hevy_status = "connected" if hevy_api_key else "not configured"

    # Run initial full sync so all historical data is available right away
    async with cl.Step(name="Syncing your data (full history)", type="run") as step:
        step.input = f"slug={slug}, full_sync=True"
        sync_errors: list[str] = []

        garmin_result = await asyncio.to_thread(sync_garmin_user, slug, user_id, full_sync=True)
        if "error" in garmin_result:
            sync_errors.append(f"Garmin: {garmin_result['error']}")

        if hevy_api_key:
            hevy_result = await asyncio.to_thread(sync_hevy_user, slug, user_id, hevy_api_key, full_sync=True)
            if "error" in hevy_result:
                sync_errors.append(f"Hevy: {hevy_result['error']}")

        step.output = "Done" if not sync_errors else f"Completed with warnings: {'; '.join(sync_errors)}"

    # Auto-populate thresholds from Garmin (FTP, max HR, resting HR, VO2max, etc.)
    # Only runs when Garmin is connected and thresholds are still null.
    if garmin_email:
        async with cl.Step(name="Fetching athlete thresholds from Garmin", type="run") as step:
            step.input = f"slug={slug}"
            profile_result = await asyncio.to_thread(sync_garmin_profile, slug)
            if profile_result.get("skipped"):
                step.output = "All thresholds already set"
            elif "error" in profile_result:
                step.output = f"Skipped: {profile_result['error']}"
            elif profile_result.get("written"):
                populated = list(profile_result["written"].keys())
                step.output = f"Populated {len(populated)} field(s): {', '.join(populated)}"
            else:
                step.output = "No additional values found in Garmin — some fields may need manual entry"

    profile_fields_populated = (
        garmin_email
        and not profile_result.get("skipped")
        and not profile_result.get("error")
        and bool(profile_result.get("written"))
    ) if garmin_email else False

    await cl.Message(
        content=(
            f"All done, {first_name}! Your account is ready and your data has been synced.\n\n"
            f"| | Status |\n"
            f"|---|---|\n"
            f"| Username | `{slug}` |\n"
            f"| Garmin Connect | {garmin_status} |\n"
            f"| Hevy | {hevy_status} |\n"
            + (
                f"| Thresholds | Auto-populated from Garmin ✓ |\n"
                if profile_fields_populated else ""
            )
            + f"\nAsk me anything about your fitness!"
        )
    ).send()

    # Transition into a normal coaching session
    await _init_session(slug, user_id, first_name, user_entry)
    system_prompt = _build_system_prompt(slug, first_name)
    cl.user_session.set("messages", [{"role": "system", "content": system_prompt}])


# ---------------------------------------------------------------------------
# Chat handlers
# ---------------------------------------------------------------------------

@cl.set_starters
async def set_starters() -> list[cl.Starter]:
    return [
        cl.Starter(
            label="How am I doing this week?",
            message="Give me a summary of my training this week — volume, intensity, TSS, and how I'm trending.",
        ),
        cl.Starter(
            label="What should I do today?",
            message="Based on my recent training load and recovery, what should I do today?",
        ),
        cl.Starter(
            label="Show my PMC",
            message="Show me my performance management chart — CTL, ATL, and TSB trend.",
        ),
        cl.Starter(
            label="Recent activities",
            message="List my last 7 days of activities with TSS and key metrics.",
        ),
    ]


@cl.on_chat_start
async def on_chat_start():
    user = cl.user_session.get("user")

    # New user — run onboarding
    if user.metadata.get("registration_pending"):
        if not ALLOW_REGISTRATION:
            await cl.Message(
                content="New user registration is disabled. Please contact the administrator."
            ).send()
            return
        await run_onboarding(user)
        return

    # Existing user — normal session init
    user_slug = user.metadata["slug"]
    first_name = user.metadata.get("first_name", user_slug)

    user_id = health_tools.resolve_user_id(user_slug)
    if user_id is None:
        await cl.Message(
            content=f"User '{user_slug}' not found in the database. "
            "Please run a sync first."
        ).send()
        return

    user_data = _USERS_BY_SLUG.get(user_slug, {})
    await _init_session(user_slug, user_id, first_name, user_data)


@cl.on_chat_resume
async def on_chat_resume(thread: dict) -> None:
    """Restore session state when a user reopens a previous conversation."""
    user = cl.user_session.get("user")
    if not user or user.metadata.get("registration_pending"):
        return

    user_slug = user.metadata.get("slug")
    first_name = user.metadata.get("first_name", user_slug)

    user_id = health_tools.resolve_user_id(user_slug)
    if user_id is None:
        return

    user_data = _USERS_BY_SLUG.get(user_slug, {})
    await _init_session(user_slug, user_id, first_name, user_data)

    # Reconstruct message history from stored thread steps
    messages: list[dict] = cl.user_session.get("messages", [])
    for step in thread.get("steps", []):
        step_type = step.get("type", "")
        content = step.get("output") or ""
        if not content:
            continue
        if step_type == "user_message":
            messages.append({"role": "user", "content": content})
        elif step_type == "assistant_message":
            messages.append({"role": "assistant", "content": content})
    cl.user_session.set("messages", messages)


@cl.on_message
async def on_message(message: cl.Message):
    messages: list[dict] = cl.user_session.get("messages", [])
    user_id: int = cl.user_session.get("user_id")
    user_slug: str = cl.user_session.get("user_slug")
    user_data: dict = cl.user_session.get("user_data", {})

    if user_id is None:
        await cl.Message(content="Session not initialized. Please refresh.").send()
        return

    messages.append({"role": "user", "content": message.content})

    client = _get_client()
    charts: list = []

    for _round in range(MAX_TOOL_ROUNDS):
        response = await client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            tools=TOOL_SCHEMAS,
            stream=False,
        )

        choice = response.choices[0]

        if choice.finish_reason == "tool_calls" or choice.message.tool_calls:
            messages.append(choice.message.model_dump())

            for tool_call in choice.message.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                async with cl.Step(name=fn_name, type="tool") as step:
                    step.input = json.dumps(fn_args, indent=2) if fn_args else "{}"
                    result = await asyncio.to_thread(
                        _execute_tool,
                        fn_name, fn_args, user_id, user_slug, user_data,
                    )
                    result_str = json.dumps(result, indent=2, default=str)
                    step.output = result_str

                    fig = maybe_chart(fn_name, result)
                    if fig:
                        charts.append(fig)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_str,
                })

            continue

        final_text = choice.message.content or ""
        messages.append({"role": "assistant", "content": final_text})

        elements = []
        for i, fig in enumerate(charts):
            elements.append(cl.Plotly(
                name=f"chart_{i}",
                figure=fig,
                display="inline",
                size="large",
            ))

        await cl.Message(content=final_text, elements=elements).send()
        cl.user_session.set("messages", messages)
        return

    await cl.Message(
        content="I hit the tool-calling limit for this turn. "
        "Please try rephrasing or breaking your question into smaller parts."
    ).send()
    cl.user_session.set("messages", messages)

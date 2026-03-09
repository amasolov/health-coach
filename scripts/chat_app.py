"""
Chainlit chat application for health coaching.

Provides a conversational AI interface backed by OpenRouter, with access
to the full suite of health tracker tools via scripts.health_tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure the project root is on sys.path so ``from scripts import ...``
# works when Chainlit loads this file via importlib.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import chainlit as cl
from chainlit.oauth_providers import providers
from openai import AsyncOpenAI

from scripts import health_tools
from scripts.chat_tools_schema import TOOL_SCHEMAS, TOOL_DISPATCH
from scripts.chat_charts import maybe_chart

# Register custom Apple OAuth provider if configured
if os.environ.get("OAUTH_APPLE_CLIENT_ID"):
    from scripts.oauth_apple import AppleOAuthProvider
    providers.append(AppleOAuthProvider())

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "anthropic/claude-sonnet-4")
MAX_TOOL_ROUNDS = 10

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
        )
    return _client


# ---------------------------------------------------------------------------
# User registry (mirrors MCP server pattern)
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


# ---------------------------------------------------------------------------
# Authentication -- OAuth (Google / Apple) + password fallback
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
        """Map a Google/Apple identity to a health-tracker user by email."""
        email = (
            raw_user_data.get("email", "")
            or default_user.identifier
            or ""
        ).lower().strip()

        if not email:
            return None

        user = _USERS_BY_EMAIL.get(email)
        if not user:
            return None

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

else:
    # Dev-only fallback: slug + MCP API key when no OAuth is configured.
    @cl.password_auth_callback
    def password_callback(username: str, password: str) -> cl.User | None:
        user = _USERS_BY_SLUG.get(username)
        if not user:
            return None

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


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt(user_slug: str, first_name: str) -> str:
    """Construct the coaching system prompt with live fitness data."""
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
                f"- Ramp rate: {summary.get('ramp_rate')}%/week -- "
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
        "- When presenting time-series data, keep text concise -- "
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
    """Execute a health_tools function with the right user context."""
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
            else:
                return fn(user_slug, **arguments)
        else:
            return fn(**arguments)
    except (ValueError, Exception) as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Chat handlers
# ---------------------------------------------------------------------------

@cl.on_chat_start
async def on_chat_start():
    """Set up the session with user context and system prompt."""
    user = cl.user_session.get("user")
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

    cl.user_session.set("user_id", user_id)
    cl.user_session.set("user_slug", user_slug)
    cl.user_session.set("first_name", first_name)
    cl.user_session.set("user_data", user_data)

    system_prompt = _build_system_prompt(user_slug, first_name)
    cl.user_session.set("system_prompt", system_prompt)
    cl.user_session.set("messages", [
        {"role": "system", "content": system_prompt},
    ])

    await cl.Message(
        content=f"Hey {first_name}! I'm your fitness coach. "
        "I have access to your training data, vitals, body composition, "
        "and more. How can I help you today?"
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    """Handle user messages with the tool-calling loop."""
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

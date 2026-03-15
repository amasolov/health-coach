"""Dual-mode tool executor for the health-coach layer.

Supports two execution modes:
  - **Direct** (monolith): imports health_tools and calls functions in-process
  - **MCP** (decoupled): calls health-platform's MCP server over HTTP

The mode is selected by the MCP_ENDPOINT env var:
  - Set    -> MCP mode (requires MCP_API_KEY too)
  - Absent -> direct mode (current behavior)

Both chat_app.py and telegram_bot.py use this module instead of importing
TOOL_DISPATCH directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

MCP_ENDPOINT = os.environ.get("MCP_ENDPOINT", "")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")

_USE_MCP = bool(MCP_ENDPOINT and MCP_API_KEY)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_tool_schemas() -> list[dict]:
    """Return OpenAI function-calling schemas for all available tools."""
    if _USE_MCP:
        return _mcp_get_schemas()
    from scripts.chat_tools_schema import TOOL_SCHEMAS
    return TOOL_SCHEMAS


def get_tool_dispatch() -> dict:
    """Return the dispatch map (tool_name -> handler info).

    In direct mode, returns chat_tools_schema.TOOL_DISPATCH.
    In MCP mode, returns a dispatch map built from MCP tool discovery.
    """
    if _USE_MCP:
        return _mcp_get_dispatch()
    from scripts.chat_tools_schema import TOOL_DISPATCH
    return TOOL_DISPATCH


def is_mcp_mode() -> bool:
    return _USE_MCP


def execute_tool(
    tool_name: str,
    arguments: dict,
    user_id: int,
    user_slug: str,
    user_data: dict,
    excluded_tools: set[str] | None = None,
) -> Any:
    """Execute a tool call in either direct or MCP mode.

    In direct mode, the full in-process dispatch logic runs (credential
    injection, tz_name, param_kind routing).

    In MCP mode, arguments are sent as-is to the MCP server, which handles
    user scoping via the Bearer token.
    """
    if excluded_tools and tool_name in excluded_tools:
        return {"error": "This tool is not available via this channel."}

    if _USE_MCP:
        return _mcp_execute(tool_name, arguments)

    return _direct_execute(tool_name, arguments, user_id, user_slug, user_data)


# ---------------------------------------------------------------------------
# Direct mode (in-process)
# ---------------------------------------------------------------------------

def _direct_execute(
    tool_name: str,
    arguments: dict,
    user_id: int,
    user_slug: str,
    user_data: dict,
) -> Any:
    from scripts.chat_tools_schema import TOOL_DISPATCH
    entry = TOOL_DISPATCH.get(tool_name)
    if not entry:
        return {"error": f"Unknown tool: {tool_name}"}

    fn, param_kind = entry

    if param_kind == "uid" and "tz_name" not in arguments:
        import inspect
        sig = inspect.signature(fn)
        if "tz_name" in sig.parameters:
            from scripts.tz import load_user_tz
            arguments = {**arguments, "tz_name": str(load_user_tz(user_slug))}

    try:
        if param_kind == "uid":
            return fn(user_id, **arguments)
        elif param_kind == "slug":
            return fn(user_slug, **arguments)
        elif param_kind == "none":
            return fn(**arguments)
        elif param_kind == "creds":
            return _creds_dispatch(tool_name, fn, arguments, user_id, user_slug, user_data)
        else:
            return fn(**arguments)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return {"error": str(exc)}


def _creds_dispatch(
    tool_name: str,
    fn: Any,
    arguments: dict,
    user_id: int,
    user_slug: str,
    user_data: dict,
) -> Any:
    if tool_name == "garmin_auth_status":
        return fn(user_slug, user_data.get("garmin_email", ""))
    elif tool_name == "garmin_authenticate":
        email = arguments.pop("garmin_email", "") or user_data.get("garmin_email", "")
        password = arguments.pop("garmin_password", "") or user_data.get("garmin_password", "")
        result = fn(user_slug, email, password)
        if result.get("status") in ("ok", "needs_mfa") and email:
            user_data["garmin_email"] = email
            user_data["garmin_password"] = password
        return result
    elif tool_name == "hevy_auth_status":
        hevy_key = user_data.get("hevy_api_key", "")
        return fn(user_slug, hevy_key)
    elif tool_name == "hevy_connect":
        key = arguments.pop("hevy_api_key", "") or user_data.get("hevy_api_key", "")
        result = fn(user_slug, key)
        if result.get("status") == "ok" and key:
            user_data["hevy_api_key"] = key
        return result
    elif tool_name == "generate_fitness_assessment":
        hevy_key = user_data.get("hevy_api_key") or None
        return fn(user_slug, hevy_key, **arguments)
    elif tool_name == "create_hevy_routine_from_recommendation":
        hevy_key = user_data.get("hevy_api_key", "")
        return fn(user_slug, hevy_api_key=hevy_key, **arguments)
    elif tool_name == "manage_hevy_routines":
        hevy_key = user_data.get("hevy_api_key", "")
        return fn(user_slug, hevy_api_key=hevy_key, **arguments)
    elif tool_name == "get_routine_weight_recommendations":
        hevy_key = user_data.get("hevy_api_key", "")
        return fn(user_id, user_slug, hevy_api_key=hevy_key, **arguments)
    elif tool_name == "sync_data":
        hevy_key = user_data.get("hevy_api_key", "")
        return fn(user_slug, user_id, hevy_key, **arguments)
    else:
        return fn(user_slug, **arguments)


# ---------------------------------------------------------------------------
# MCP mode (HTTP client to health-platform)
# ---------------------------------------------------------------------------

_platform_client = None
_mcp_tools_cache: list[dict] | None = None
_mcp_schemas_cache: list[dict] | None = None
_mcp_dispatch_cache: dict | None = None


def _get_client():
    global _platform_client
    if _platform_client is None:
        from scripts.mcp_client_adapter import PlatformClient
        _platform_client = PlatformClient(
            endpoint=MCP_ENDPOINT,
            api_key=MCP_API_KEY,
        )
    return _platform_client


def _ensure_mcp_tools():
    """Fetch and cache tool list from the MCP server (once)."""
    global _mcp_tools_cache, _mcp_schemas_cache, _mcp_dispatch_cache
    if _mcp_tools_cache is not None:
        return

    client = _get_client()
    loop = asyncio.get_event_loop()
    if loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            _mcp_tools_cache = pool.submit(
                lambda: asyncio.run(client.list_tools())
            ).result()
    else:
        _mcp_tools_cache = asyncio.run(client.list_tools())

    from scripts.mcp_client_adapter import (
        mcp_tools_to_openai_schemas,
        build_dispatch_map,
    )
    _mcp_schemas_cache = mcp_tools_to_openai_schemas(_mcp_tools_cache)
    _mcp_dispatch_cache = build_dispatch_map(_mcp_tools_cache)


def _mcp_get_schemas() -> list[dict]:
    _ensure_mcp_tools()
    return _mcp_schemas_cache or []


def _mcp_get_dispatch() -> dict:
    _ensure_mcp_tools()
    return _mcp_dispatch_cache or {}


def _mcp_execute(tool_name: str, arguments: dict) -> Any:
    """Call a tool on the MCP server synchronously (blocking for the caller)."""
    client = _get_client()

    async def _call():
        return await client.call_tool(tool_name, arguments)

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(lambda: asyncio.run(_call())).result()
        else:
            return asyncio.run(_call())
    except Exception as exc:
        log.error("MCP tool call %s failed: %s", tool_name, exc)
        return {"error": str(exc)}

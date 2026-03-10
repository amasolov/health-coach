"""Shared httpx client pool for external API calls.

Reuses TCP connections and TLS sessions across requests to the same host,
eliminating ~200-400ms of handshake overhead per call.  Clients are created
lazily on first use and kept for the process lifetime.

Usage — drop-in replacement for bare ``httpx.get()`` / ``httpx.post()``::

    from scripts.http_clients import hevy_client, ifit_client

    r = hevy_client().get(f"{HEVY_BASE}/v1/routines", headers=..., params=...)
    r = ifit_client().get("https://gateway.ifit.com/...", headers=...)
"""

from __future__ import annotations

import threading

import httpx

_lock = threading.Lock()
_hevy: httpx.Client | None = None
_ifit: httpx.Client | None = None
_openrouter: httpx.Client | None = None


def hevy_client() -> httpx.Client:
    """Shared client for Hevy API calls — keeps connections alive."""
    global _hevy
    if _hevy is None:
        with _lock:
            if _hevy is None:
                _hevy = httpx.Client(
                    timeout=30,
                    limits=httpx.Limits(
                        max_connections=10,
                        max_keepalive_connections=5,
                    ),
                )
    return _hevy


def ifit_client() -> httpx.Client:
    """Shared client for iFit API calls — keeps connections alive."""
    global _ifit
    if _ifit is None:
        with _lock:
            if _ifit is None:
                _ifit = httpx.Client(
                    timeout=20,
                    limits=httpx.Limits(
                        max_connections=10,
                        max_keepalive_connections=5,
                    ),
                )
    return _ifit


def openrouter_client() -> httpx.Client:
    """Shared client for OpenRouter LLM API calls."""
    global _openrouter
    if _openrouter is None:
        with _lock:
            if _openrouter is None:
                _openrouter = httpx.Client(
                    timeout=60,
                    limits=httpx.Limits(
                        max_connections=5,
                        max_keepalive_connections=3,
                    ),
                )
    return _openrouter

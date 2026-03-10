#!/usr/bin/env python3
"""Async task runner for periodic data sync.

Replaces the bash ``while true; sleep N; done`` loop with APScheduler.

Features
--------
- Per-user **parallel** sync (users synced concurrently via asyncio.gather)
- **Retry** with exponential back-off for transient API/DB failures
- Per-user **timeout** so a hung API call cannot stall the entire cycle
- Structured **ops_log** events for every cycle
- PMC calculation runs after all user syncs complete

Environment variables
---------------------
SYNC_INTERVAL        Sync interval in minutes (default 30, set by run.sh)
SYNC_MAX_RETRIES     Per-user retry attempts (default 2)
SYNC_RETRY_BASE      Base delay in seconds for exponential back-off (default 10)
SYNC_USER_TIMEOUT    Per-user timeout in seconds (default 600)

Usage::

    python scripts/task_runner.py     # started by run.sh
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import signal
import sys
import time as _time

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger("task_runner")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "30"))
MAX_RETRIES = int(os.environ.get("SYNC_MAX_RETRIES", "2"))
RETRY_BASE_DELAY = int(os.environ.get("SYNC_RETRY_BASE", "10"))
USER_TIMEOUT = int(os.environ.get("SYNC_USER_TIMEOUT", "600"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _to_thread(fn, *args, **kwargs):
    """Dispatch a sync callable to a worker thread."""
    return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))


async def _retry(fn, *args, retries: int = MAX_RETRIES, label: str = "", **kwargs):
    """Call *fn* in a thread with exponential-back-off retry.

    Only retries on Exception; the final attempt re-raises.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await _to_thread(fn, *args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "[%s] attempt %d/%d failed: %s — retrying in %ds",
                    label, attempt + 1, retries + 1, exc, delay,
                )
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Per-user sync with retry + timeout
# ---------------------------------------------------------------------------

async def _sync_user(user: dict) -> dict:
    """Sync one user with retry and a per-user timeout."""
    from scripts.run_sync import sync_one_user

    slug = user["slug"]
    t0 = _time.monotonic()
    try:
        async with asyncio.timeout(USER_TIMEOUT):
            result = await _retry(
                sync_one_user, user,
                retries=MAX_RETRIES,
                label=f"sync:{slug}",
            )
        elapsed = _time.monotonic() - t0
        logger.info("[%s] completed in %.1fs (errors=%d)", slug, elapsed, result.get("errors", 0))
        return result
    except TimeoutError:
        elapsed = _time.monotonic() - t0
        logger.error("[%s] timed out after %.0fs", slug, elapsed)
        return {"slug": slug, "errors": 1, "timeout": True}
    except Exception as exc:
        elapsed = _time.monotonic() - t0
        logger.error("[%s] failed after %.1fs: %s", slug, elapsed, exc)
        return {"slug": slug, "errors": 1, "error": str(exc)}


# ---------------------------------------------------------------------------
# Full sync cycle
# ---------------------------------------------------------------------------

async def sync_cycle() -> None:
    """Run a full sync cycle: all users in parallel, then global tasks + PMC."""
    from scripts.run_sync import get_users, sync_global
    from scripts.calc_pmc import main as calc_pmc_main
    from scripts import ops_emit

    t0 = _time.monotonic()
    logger.info("=== Sync cycle started ===")

    users = await _to_thread(get_users)
    if not users:
        logger.warning("No users found — skipping cycle")
        return

    # Fan out per-user sync
    results = await asyncio.gather(
        *[_sync_user(u) for u in users],
    )

    total_errors = sum(r.get("errors", 0) for r in results)
    timeouts = sum(1 for r in results if r.get("timeout"))

    # Global tasks (iFit library, R2, system stats)
    logger.info("Running global sync tasks...")
    try:
        await _retry(sync_global, retries=1, label="sync:global")
    except Exception as exc:
        logger.error("Global sync tasks failed: %s", exc)
        total_errors += 1

    # PMC calculation
    logger.info("Calculating PMC...")
    try:
        await _to_thread(calc_pmc_main)
    except Exception as exc:
        logger.error("PMC calculation failed: %s", exc)
        total_errors += 1

    cycle_ms = int((_time.monotonic() - t0) * 1000)
    ops_emit.emit(
        "sync", "sync_cycle",
        status="error" if total_errors else "ok",
        duration_ms=cycle_ms,
        user_count=len(users),
        errors=total_errors,
        timeouts=timeouts,
        parallel=True,
    )
    logger.info(
        "=== Sync cycle complete in %.1fs (%d users, %d errors, %d timeouts) ===",
        cycle_ms / 1000, len(users), total_errors, timeouts,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    logger.info(
        "Task runner starting (interval=%dm, retries=%d, timeout=%ds)",
        SYNC_INTERVAL, MAX_RETRIES, USER_TIMEOUT,
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        sync_cycle,
        "interval",
        minutes=SYNC_INTERVAL,
        id="sync_cycle",
        name="Data Sync + PMC",
        next_run_time=None,  # first run triggered manually below
        max_instances=1,
        misfire_grace_time=120,
        coalesce=True,
    )
    scheduler.start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(sig, _frame):
        logger.info("Received %s — shutting down", signal.Signals(sig).name)
        scheduler.shutdown(wait=False)
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Run the first cycle immediately, then let the scheduler take over
    loop.run_until_complete(sync_cycle())

    try:
        loop.run_forever()
    finally:
        loop.close()
        logger.info("Task runner stopped")


if __name__ == "__main__":
    main()

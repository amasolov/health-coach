#!/usr/bin/env python3
"""
Batched iFit transcript sync to Cloudflare R2.

Fetches VTT captions for library workouts in configurable batches, cleans
them into plain-text transcripts, and uploads to R2.  Designed to be called
each sync cycle (chips away at the backlog) OR standalone for faster catch-up.

Also uploads library metadata and discovers program/series information.

Usage (standalone):
    python scripts/ifit_r2_sync.py                     # one batch (default 100)
    python scripts/ifit_r2_sync.py --batch-size 500     # larger batch
    python scripts/ifit_r2_sync.py --all                # process everything
    python scripts/ifit_r2_sync.py --dry-run             # preview only
    python scripts/ifit_r2_sync.py --reset-state         # clear sync state
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from r2_store import is_configured as r2_configured, upload_text, upload_json, download_json, list_keys

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".ifit_capture"
STATE_KEY = "sync/state.json"
PROGRAMS_PREFIX = "programs/"
IFIT_SW_NUMBER = "424992"


# ── VTT cleaning (shared with ifit_strength_recommend.py) ────────────

def clean_vtt(raw: str) -> str:
    """Strip WEBVTT headers, timestamps, cue numbers, and speaker tags."""
    lines = raw.split("\n")
    text_lines = [
        l for l in lines
        if not l.startswith("WEBVTT") and "-->" not in l
        and l.strip() and not l.strip().isdigit()
    ]
    clean = [re.sub(r"<v [^>]*>", "", l).replace("</v>", "").strip() for l in text_lines]
    return " ".join(c for c in clean if c)


# ── Sync state management ────────────────────────────────────────────

def _load_state() -> dict:
    state = download_json(STATE_KEY)
    if state:
        return state
    local = CACHE_DIR / "r2_sync_state.json"
    if local.exists():
        with open(local) as f:
            return json.load(f)
    return {"attempted": {}, "stats": {}, "migrated": False}


def _save_state(state: dict) -> None:
    upload_json(STATE_KEY, state)
    local = CACHE_DIR / "r2_sync_state.json"
    CACHE_DIR.mkdir(exist_ok=True)
    with open(local, "w") as f:
        json.dump(state, f, indent=2)


# ── Library upload ────────────────────────────────────────────────────

def sync_library() -> dict:
    """Upload library_workouts.json and trainers.json to R2."""
    if not r2_configured():
        return {"skipped": True, "reason": "R2 not configured"}

    t0 = time.time()
    results = {}
    for name, key in [
        ("library_workouts.json", "library/workouts.json"),
        ("trainers.json", "library/trainers.json"),
    ]:
        path = CACHE_DIR / name
        if not path.exists():
            print(f"    [library] {name}: not found locally, skipping")
            results[name] = "not found"
            continue
        with open(path) as f:
            data = json.load(f)
        print(f"    [library] Uploading {name} ({len(data)} items)...", flush=True)
        if upload_json(key, data):
            results[name] = f"uploaded ({len(data)} items)"
            print(f"    [library] {name}: uploaded OK")
        else:
            results[name] = "upload failed"
            print(f"    [library] {name}: UPLOAD FAILED")

    elapsed = time.time() - t0
    print(f"    [library] Done in {elapsed:.1f}s")
    return results


# ── Transcript batch fetch ────────────────────────────────────────────

def _fetch_single_vtt(
    workout_id: str, headers: dict, client: httpx.Client,
) -> tuple[str, str | None]:
    """Fetch and clean VTT for one workout. Returns (workout_id, transcript|None)."""
    try:
        r = client.get(
            f"https://gateway.ifit.com/video-streaming-service/v1/workoutVideo/{workout_id}",
            headers=headers, timeout=15,
        )
        if r.status_code != 200:
            return workout_id, None
        captions = r.json().get("captions", {})
        eng_url = captions.get("eng", "")
        if not eng_url:
            return workout_id, None
        r2 = client.get(eng_url, timeout=15)
        if r2.status_code != 200:
            return workout_id, None
        return workout_id, clean_vtt(r2.text)
    except Exception:
        return workout_id, None


def sync_transcripts(
    batch_size: int = 100,
    dry_run: bool = False,
) -> dict:
    """Fetch the next batch of transcripts and upload to R2.

    Returns stats dict with processed/uploaded/no_captions/remaining counts.
    """
    if not r2_configured():
        return {"skipped": True, "reason": "R2 not configured"}

    library_path = CACHE_DIR / "library_workouts.json"
    if not library_path.exists():
        print("    [transcripts] library_workouts.json not found, skipping")
        return {"error": "library_workouts.json not found"}

    with open(library_path) as f:
        library = json.load(f)
    all_ids = {w["id"] for w in library}

    state = _load_state()
    attempted = state.get("attempted", {})
    total_synced = sum(1 for v in attempted.values() if v == "ok")

    pending = [wid for wid in all_ids if wid not in attempted]
    to_process = pending[:batch_size]

    print(f"    [transcripts] Library: {len(all_ids)} workouts, "
          f"{total_synced} synced, {len(attempted)} attempted, "
          f"{len(pending)} pending", flush=True)

    if not to_process:
        print("    [transcripts] Nothing to process, all caught up")
        return {
            "processed": 0, "uploaded": 0, "no_captions": 0,
            "remaining": 0, "total": len(all_ids),
            "total_synced": total_synced,
        }

    if dry_run:
        print(f"    [transcripts] DRY RUN: would process {len(to_process)}")
        return {
            "dry_run": True, "would_process": len(to_process),
            "remaining": len(pending), "total": len(all_ids),
        }

    from ifit_auth import get_auth_headers
    headers = get_auth_headers()

    print(f"    [transcripts] Starting batch of {len(to_process)} "
          f"(batch_size={batch_size})...", flush=True)

    uploaded = 0
    no_captions = 0
    errors = 0
    t0 = time.time()

    with httpx.Client(timeout=20) as client:
        for i, wid in enumerate(to_process):
            wid, transcript = _fetch_single_vtt(wid, headers, client)

            if transcript:
                if upload_text(f"transcripts/{wid}.txt", transcript):
                    attempted[wid] = "ok"
                    uploaded += 1
                else:
                    attempted[wid] = "upload_failed"
                    errors += 1
            else:
                attempted[wid] = "no_captions"
                no_captions += 1

            if (i + 1) % 10 == 0:
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                eta = (len(to_process) - i - 1) / rate if rate > 0 else 0
                print(f"    [transcripts] {i+1}/{len(to_process)} "
                      f"({uploaded} ok, {no_captions} no-caption, {errors} err) "
                      f"[{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining]", flush=True)

            time.sleep(0.3)

    elapsed = time.time() - t0
    state["attempted"] = attempted
    state["stats"]["last_batch"] = {
        "processed": len(to_process),
        "uploaded": uploaded,
        "no_captions": no_captions,
        "errors": errors,
        "elapsed_seconds": round(elapsed, 1),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_state(state)

    remaining = len(all_ids) - len(attempted)
    total_synced = sum(1 for v in attempted.values() if v == "ok")

    print(f"    [transcripts] Batch complete in {elapsed:.1f}s: "
          f"{uploaded} uploaded, {no_captions} no-captions, {errors} errors", flush=True)
    print(f"    [transcripts] Overall: {total_synced}/{len(all_ids)} synced, "
          f"{remaining} remaining", flush=True)

    return {
        "processed": len(to_process),
        "uploaded": uploaded,
        "no_captions": no_captions,
        "errors": errors,
        "remaining": remaining,
        "total": len(all_ids),
        "total_synced": total_synced,
        "elapsed_seconds": round(elapsed, 1),
    }


# ── Program/series discovery ─────────────────────────────────────────

def sync_programs() -> dict:
    """Discover and fetch program/series metadata from iFit, store in R2."""
    if not r2_configured():
        return {"skipped": True, "reason": "R2 not configured"}

    from ifit_auth import get_auth_headers
    headers = get_auth_headers()
    t0 = time.time()

    series_ids: set[str] = set()

    print("    [programs] Discovering series from recommended-series...", flush=True)
    try:
        r = httpx.get(
            f"https://gateway.ifit.com/wolf-dashboard-service/v1/recommended-series"
            f"?softwareNumber={IFIT_SW_NUMBER}&limit=50",
            headers=headers, timeout=15,
        )
        if r.status_code == 200:
            for item in r.json():
                sid = item.get("seriesId")
                if sid:
                    series_ids.add(str(sid))
            print(f"    [programs] recommended-series: {len(series_ids)} found")
        else:
            print(f"    [programs] recommended-series: HTTP {r.status_code}")
    except Exception as e:
        print(f"    [programs] recommended-series: error ({e})")

    print("    [programs] Discovering series from up-next...", flush=True)
    before = len(series_ids)
    try:
        r = httpx.get(
            f"https://gateway.ifit.com/wolf-dashboard-service/v1/up-next"
            f"?softwareNumber={IFIT_SW_NUMBER}&limit=100"
            f"&challengeStoreEnabled=true&userType=premium",
            headers=headers, timeout=15,
        )
        if r.status_code == 200:
            for item in r.json():
                sid = item.get("seriesId")
                if sid:
                    series_ids.add(str(sid))
            print(f"    [programs] up-next: {len(series_ids) - before} new "
                  f"({len(series_ids)} total)")
        else:
            print(f"    [programs] up-next: HTTP {r.status_code}")
    except Exception as e:
        print(f"    [programs] up-next: error ({e})")

    existing_keys = set(list_keys(PROGRAMS_PREFIX))
    existing_ids = {k.replace(PROGRAMS_PREFIX, "").replace(".json", "") for k in existing_keys}
    new_ids = series_ids - existing_ids
    print(f"    [programs] {len(existing_ids)} already in R2, "
          f"{len(new_ids)} new to fetch", flush=True)

    fetched = 0
    fetch_errors = 0
    for sid in new_ids:
        try:
            r = httpx.get(
                f"https://gateway.ifit.com/wolf-workouts-service/v1/program/{sid}"
                f"?softwareNumber={IFIT_SW_NUMBER}",
                headers=headers, timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                program = {
                    "series_id": sid,
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
                upload_json(f"{PROGRAMS_PREFIX}{sid}.json", program)
                fetched += 1
                print(f"    [programs] Fetched: {data.get('title', sid)} "
                      f"({program['workout_count']} workouts)")
            else:
                fetch_errors += 1
                print(f"    [programs] Failed to fetch {sid}: HTTP {r.status_code}")
            time.sleep(0.2)
        except Exception as e:
            fetch_errors += 1
            print(f"    [programs] Error fetching {sid}: {e}")
            continue

    elapsed = time.time() - t0
    print(f"    [programs] Done in {elapsed:.1f}s: {len(series_ids)} discovered, "
          f"{fetched} newly fetched, {fetch_errors} errors", flush=True)

    return {
        "discovered": len(series_ids),
        "already_stored": len(existing_ids),
        "newly_fetched": fetched,
        "errors": fetch_errors,
    }


def load_program_index() -> dict[str, dict]:
    """Load all programs from R2 and build workout_id -> program lookup."""
    keys = list_keys(PROGRAMS_PREFIX)
    index: dict[str, dict] = {}
    for key in keys:
        program = download_json(key)
        if not program:
            continue
        entry = {
            "series_id": program.get("series_id", ""),
            "title": program.get("title", ""),
            "type": program.get("type", ""),
        }
        for i, wid in enumerate(program.get("workout_ids", [])):
            if wid:
                index[wid] = {
                    **entry,
                    "position": i + 1,
                    "total": program.get("workout_count", 0),
                }
    return index


# ── Exercise cache migration ─────────────────────────────────────────

def migrate_exercise_cache() -> dict:
    """One-time upload of local exercise_cache.json entries to R2."""
    if not r2_configured():
        return {"skipped": True}

    state = _load_state()
    if state.get("migrated"):
        print("    [migrate] Already migrated, skipping")
        return {"already_migrated": True}

    cache_path = CACHE_DIR / "exercise_cache.json"
    if not cache_path.exists():
        print("    [migrate] No local exercise_cache.json found")
        return {"no_cache": True}

    with open(cache_path) as f:
        cache = json.load(f)

    print(f"    [migrate] Uploading {len(cache)} exercise cache entries to R2...",
          flush=True)
    t0 = time.time()
    uploaded = 0
    for i, (wid, exercises) in enumerate(cache.items()):
        if upload_json(f"exercises/{wid}.json", exercises):
            uploaded += 1
        if (i + 1) % 25 == 0:
            print(f"    [migrate] {i+1}/{len(cache)} uploaded", flush=True)

    state["migrated"] = True
    _save_state(state)

    elapsed = time.time() - t0
    print(f"    [migrate] Done in {elapsed:.1f}s: {uploaded}/{len(cache)} uploaded")
    return {"uploaded": uploaded, "total": len(cache)}


# ── CLI ───────────────────────────────────────────────────────────────

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="iFit R2 transcript sync")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--all", action="store_true", help="Process all pending")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--migrate", action="store_true", help="Migrate exercise cache")
    parser.add_argument("--programs", action="store_true", help="Sync programs only")
    args = parser.parse_args()

    if not r2_configured():
        print("R2 not configured (set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY)")
        return 1

    if args.reset_state:
        _save_state({"attempted": {}, "stats": {}, "migrated": False})
        print("Sync state reset.")
        return 0

    if args.migrate:
        result = migrate_exercise_cache()
        print(f"Migration: {json.dumps(result, indent=2)}")
        return 0

    if args.programs:
        result = sync_programs()
        print(f"Programs: {json.dumps(result, indent=2)}")
        return 0

    print("Uploading library to R2...")
    lib_result = sync_library()
    for name, status in lib_result.items():
        print(f"  {name}: {status}")

    batch_size = 999_999 if args.all else args.batch_size

    print(f"\nSyncing transcripts (batch_size={batch_size}, dry_run={args.dry_run})...")
    result = sync_transcripts(batch_size=batch_size, dry_run=args.dry_run)
    for k, v in result.items():
        print(f"  {k}: {v}")

    if not args.dry_run:
        print("\nDiscovering programs...")
        prog_result = sync_programs()
        for k, v in prog_result.items():
            print(f"  {k}: {v}")

        print("\nMigrating exercise cache...")
        mig_result = migrate_exercise_cache()
        for k, v in mig_result.items():
            print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

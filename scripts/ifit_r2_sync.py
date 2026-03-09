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

    results = {}
    for name, key in [
        ("library_workouts.json", "library/workouts.json"),
        ("trainers.json", "library/trainers.json"),
    ]:
        path = CACHE_DIR / name
        if not path.exists():
            results[name] = "not found"
            continue
        with open(path) as f:
            data = json.load(f)
        if upload_json(key, data):
            results[name] = f"uploaded ({len(data)} items)"
        else:
            results[name] = "upload failed"

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
        return {"error": "library_workouts.json not found"}

    with open(library_path) as f:
        library = json.load(f)
    all_ids = {w["id"] for w in library}

    state = _load_state()
    attempted = state.get("attempted", {})

    pending = [wid for wid in all_ids if wid not in attempted]
    to_process = pending[:batch_size]

    if not to_process:
        return {
            "processed": 0, "uploaded": 0, "no_captions": 0,
            "remaining": 0, "total": len(all_ids),
            "total_synced": sum(1 for v in attempted.values() if v == "ok"),
        }

    if dry_run:
        return {
            "dry_run": True, "would_process": len(to_process),
            "remaining": len(pending), "total": len(all_ids),
        }

    from ifit_auth import get_auth_headers
    headers = get_auth_headers()

    uploaded = 0
    no_captions = 0

    with httpx.Client(timeout=20) as client:
        for i, wid in enumerate(to_process):
            wid, transcript = _fetch_single_vtt(wid, headers, client)

            if transcript:
                if upload_text(f"transcripts/{wid}.txt", transcript):
                    attempted[wid] = "ok"
                    uploaded += 1
                else:
                    attempted[wid] = "upload_failed"
            else:
                attempted[wid] = "no_captions"
                no_captions += 1

            if (i + 1) % 25 == 0:
                print(f"    Progress: {i+1}/{len(to_process)} "
                      f"(uploaded={uploaded}, no_captions={no_captions})", flush=True)

            time.sleep(0.3)

    state["attempted"] = attempted
    state["stats"]["last_batch"] = {
        "processed": len(to_process),
        "uploaded": uploaded,
        "no_captions": no_captions,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_state(state)

    remaining = len(all_ids) - len(attempted)
    total_synced = sum(1 for v in attempted.values() if v == "ok")

    return {
        "processed": len(to_process),
        "uploaded": uploaded,
        "no_captions": no_captions,
        "remaining": remaining,
        "total": len(all_ids),
        "total_synced": total_synced,
    }


# ── Program/series discovery ─────────────────────────────────────────

def sync_programs() -> dict:
    """Discover and fetch program/series metadata from iFit, store in R2."""
    if not r2_configured():
        return {"skipped": True, "reason": "R2 not configured"}

    from ifit_auth import get_auth_headers
    headers = get_auth_headers()

    series_ids: set[str] = set()

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
    except Exception:
        pass

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
    except Exception:
        pass

    existing_keys = set(list_keys(PROGRAMS_PREFIX))
    existing_ids = {k.replace(PROGRAMS_PREFIX, "").replace(".json", "") for k in existing_keys}
    new_ids = series_ids - existing_ids

    fetched = 0
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
            time.sleep(0.2)
        except Exception:
            continue

    return {
        "discovered": len(series_ids),
        "already_stored": len(existing_ids),
        "newly_fetched": fetched,
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
        return {"already_migrated": True}

    cache_path = CACHE_DIR / "exercise_cache.json"
    if not cache_path.exists():
        return {"no_cache": True}

    with open(cache_path) as f:
        cache = json.load(f)

    uploaded = 0
    for wid, exercises in cache.items():
        if upload_json(f"exercises/{wid}.json", exercises):
            uploaded += 1

    state["migrated"] = True
    _save_state(state)

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

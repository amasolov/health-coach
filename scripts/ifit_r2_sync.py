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
from scripts.cache_store import (
    get_cache, put_cache,
    KEY_LIBRARY_WORKOUTS, KEY_R2_SYNC_STATE, KEY_EXERCISE_CACHE,
)

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".ifit_capture"
STATE_KEY = "sync/state.json"
SERIES_DISCOVERY_STATE_KEY = "sync/series_discovery_state.json"
WORKOUT_SERIES_MAP_KEY = "library/workout_series_map.json"
PROGRAMS_PREFIX = "programs/"
IFIT_SW_NUMBER = "424992"
PRE_WORKOUT_URL = (
    "https://gateway.ifit.com/wolf-workouts-service/v1/pre-workout/{wid}"
    "?softwareNumber=" + IFIT_SW_NUMBER + "&locale=en&deviceType=mobile&platform=ios"
)
SERIES_REFRESH_DAYS = 30


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
    cached = get_cache(KEY_R2_SYNC_STATE)
    if cached:
        return cached
    state = download_json(STATE_KEY)
    if state:
        put_cache(KEY_R2_SYNC_STATE, state)
        return state
    local = CACHE_DIR / "r2_sync_state.json"
    if local.exists():
        with open(local) as f:
            data = json.load(f)
        put_cache(KEY_R2_SYNC_STATE, data)
        return data
    return {"attempted": {}, "stats": {}, "migrated": False}


def _save_state(state: dict) -> None:
    put_cache(KEY_R2_SYNC_STATE, state)
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
    from scripts.cache_store import KEY_TRAINERS
    cache_key_map = {
        "library_workouts.json": KEY_LIBRARY_WORKOUTS,
        "trainers.json": KEY_TRAINERS,
    }
    results = {}
    for name, key in [
        ("library_workouts.json", "library/workouts.json"),
        ("trainers.json", "library/trainers.json"),
    ]:
        data = get_cache(cache_key_map[name])
        if data is None:
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

    library = get_cache(KEY_LIBRARY_WORKOUTS)
    if library is None:
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

def _is_objectid(s: str) -> bool:
    """Check if a string looks like a MongoDB ObjectId (24-char hex)."""
    return len(s) == 24 and all(c in "0123456789abcdef" for c in s.lower())


def sync_programs() -> dict:
    """Discover and fetch program/series metadata from iFit, store in R2."""
    if not r2_configured():
        return {"skipped": True, "reason": "R2 not configured"}

    from ifit_auth import get_auth_headers
    headers = get_auth_headers()
    t0 = time.time()

    series_ids: set[str] = set()
    skipped_numeric = 0

    print("    [programs] Discovering series from recommended-series...", flush=True)
    try:
        r = httpx.get(
            f"https://gateway.ifit.com/wolf-dashboard-service/v1/recommended-series"
            f"?softwareNumber={IFIT_SW_NUMBER}&limit=50",
            headers=headers, timeout=15,
        )
        if r.status_code == 200:
            for item in r.json():
                sid = str(item.get("seriesId", ""))
                if sid and _is_objectid(sid):
                    series_ids.add(sid)
                elif sid:
                    skipped_numeric += 1
            print(f"    [programs] recommended-series: {len(series_ids)} found"
                  f"{f' ({skipped_numeric} numeric IDs skipped)' if skipped_numeric else ''}")
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
                sid = str(item.get("seriesId", ""))
                if sid and _is_objectid(sid):
                    series_ids.add(sid)
                elif sid:
                    skipped_numeric += 1
            print(f"    [programs] up-next: {len(series_ids) - before} new "
                  f"({len(series_ids)} total)"
                  f"{f', {skipped_numeric} numeric IDs skipped total' if skipped_numeric else ''}")
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


def load_program_index() -> dict[str, list[dict]]:
    """Load all programs from R2 and build workout_id -> list of program entries.

    Returns one-to-many mapping: a workout can belong to multiple series/challenges.
    """
    index: dict[str, list[dict]] = {}

    ws_map = _load_workout_series_map()
    for wid, entries in ws_map.items():
        index[wid] = [
            {
                "series_id": e.get("seriesId", ""),
                "title": e.get("title", ""),
                "position": e.get("position"),
                "week": e.get("week"),
                "is_challenge": e.get("isChallenge", False),
            }
            for e in entries
        ]

    keys = list_keys(PROGRAMS_PREFIX)
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
            if not wid:
                continue
            prog_entry = {
                **entry,
                "position": i + 1,
                "total": program.get("workout_count", 0),
            }
            if wid not in index:
                index[wid] = [prog_entry]
            elif not any(e.get("series_id") == entry["series_id"] for e in index[wid]):
                index[wid].append(prog_entry)

    return index


# ── Series discovery via pre-workout API ──────────────────────────────

def _load_series_state() -> dict:
    state = download_json(SERIES_DISCOVERY_STATE_KEY)
    if state:
        return state
    return {"attempted": {}, "stats": {}}


def _save_series_state(state: dict) -> None:
    upload_json(SERIES_DISCOVERY_STATE_KEY, state)


def _load_workout_series_map() -> dict[str, list]:
    data = download_json(WORKOUT_SERIES_MAP_KEY)
    return data if isinstance(data, dict) else {}


def _save_workout_series_map(mapping: dict[str, list]) -> None:
    upload_json(WORKOUT_SERIES_MAP_KEY, mapping)


def _extract_series_from_pre_workout(data: dict, workout_id: str) -> list[dict]:
    """Extract series entries from a pre-workout response for one workout."""
    entries = []
    program_details = data.get("programDetails") or []
    for pd in program_details:
        series_id = pd.get("id", "")
        if not series_id:
            continue
        title = pd.get("title", "")
        week = None
        position = None
        for section in pd.get("workoutSections", []):
            wids = section.get("workoutIds", [])
            if workout_id in wids:
                week = section.get("title")
                position = wids.index(workout_id) + 1
                break
        entries.append({
            "seriesId": series_id,
            "title": title,
            "week": week,
            "position": position,
            "isChallenge": pd.get("isChallenge", False),
        })

    top_pid = data.get("programId")
    if top_pid and not any(e["seriesId"] == top_pid for e in entries):
        entries.insert(0, {
            "seriesId": top_pid,
            "title": data.get("title", ""),
            "week": None,
            "position": None,
            "isChallenge": False,
        })

    return entries


def _build_weeks_from_api(data: dict) -> list[dict]:
    """Build structured weeks list from the program API response.

    The API has two sources:
      - workoutSections[].workoutIds for the ID ordering within each week
      - workouts[] (top-level) for title lookup by itemId
    """
    title_lookup = {w.get("itemId", ""): w.get("title", "") for w in data.get("workouts", [])}
    sections = data.get("workoutSections", [])
    weeks: list[dict] = []
    for sec in sections:
        wids = sec.get("workoutIds", [])
        weeks.append({
            "name": sec.get("title", ""),
            "workouts": [
                {"id": wid, "title": title_lookup.get(wid, "")}
                for wid in wids
            ],
        })
    return weeks


def _store_program_from_pre_workout(pd: dict, headers: dict, force: bool = False) -> bool:
    """Fetch full program details and store to R2.

    If force=False, skips programs already in R2 that have week structure.
    If force=True, always re-fetches (used to backfill missing week data).
    """
    series_id = pd.get("id", "")
    if not series_id:
        return False
    from r2_store import exists, download_json as r2_dl
    if not force and exists(f"{PROGRAMS_PREFIX}{series_id}.json"):
        existing = r2_dl(f"{PROGRAMS_PREFIX}{series_id}.json")
        if existing and existing.get("weeks"):
            return False
    try:
        r = httpx.get(
            f"https://gateway.ifit.com/wolf-workouts-service/v1/program/{series_id}"
            f"?softwareNumber={IFIT_SW_NUMBER}",
            headers=headers, timeout=15,
        )
        if r.status_code != 200:
            return False
        data = r.json()

        top_workouts = data.get("workouts", [])
        workout_titles = [w.get("title", "") for w in top_workouts]
        workout_ids = [w.get("itemId", "") for w in top_workouts]

        if not workout_ids:
            for s in data.get("workoutSections", []):
                workout_ids.extend(s.get("workoutIds", []))

        weeks = _build_weeks_from_api(data)

        program = {
            "series_id": series_id,
            "title": data.get("title", ""),
            "overview": data.get("overview", ""),
            "type": data.get("type", ""),
            "rating": data.get("rating", {}),
            "trainers": [
                {"name": t.get("name", ""), "id": t.get("itemId", "")}
                for t in data.get("trainers", [])
            ],
            "workout_ids": workout_ids,
            "workout_titles": workout_titles,
            "workout_count": len(workout_ids),
            "weeks": weeks,
        }
        upload_json(f"{PROGRAMS_PREFIX}{series_id}.json", program)
        return True
    except Exception:
        return False


def _week_position_from_program(program: dict, workout_id: str) -> tuple[str | None, int | None]:
    """Find the week name and 1-based position of a workout within a program's weeks."""
    for week in program.get("weeks", []):
        for i, w in enumerate(week.get("workouts", [])):
            if w.get("id") == workout_id:
                return week.get("name"), i + 1
    return None, None


def fetch_workout_series(workout_id: str, headers: dict | None = None) -> list[dict]:
    """On-demand series lookup for a single workout. Caches to R2."""
    ws_map = _load_workout_series_map()
    if workout_id in ws_map:
        return ws_map[workout_id]

    if headers is None:
        from ifit_auth import get_auth_headers
        headers = get_auth_headers()

    try:
        r = httpx.get(
            PRE_WORKOUT_URL.format(wid=workout_id),
            headers=headers, timeout=15,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        entries = _extract_series_from_pre_workout(data, workout_id)

        if entries:
            ws_map[workout_id] = entries
            _save_workout_series_map(ws_map)
            for pd in data.get("programDetails") or []:
                _store_program_from_pre_workout(pd, headers)

        return entries
    except Exception:
        return []


def discover_series_for_workout(workout_id: str) -> dict:
    """Discover all series a workout belongs to and map every workout in those series.

    This is the on-demand entry point: given one workout, it finds its series,
    fetches full program details, and maps ALL sibling workouts so the entire
    series is immediately searchable.

    Returns a summary dict with discovered series and mapped workout counts.
    """
    from ifit_auth import get_auth_headers
    headers = get_auth_headers()

    series_entries = fetch_workout_series(workout_id, headers)
    if not series_entries:
        return {"workout_id": workout_id, "series": [], "mapped": 0}

    ws_map = _load_workout_series_map()
    newly_mapped = 0
    series_summaries = []

    for entry in series_entries:
        series_id = entry.get("seriesId", "")
        if not series_id:
            continue

        program = download_json(f"{PROGRAMS_PREFIX}{series_id}.json")
        if not program:
            continue

        series_title = program.get("title", "")
        is_challenge = entry.get("isChallenge", False)
        weeks = program.get("weeks", [])

        all_wids_ordered: list[tuple[str, str | None, int]] = []
        if weeks:
            for week in weeks:
                week_name = week.get("name")
                for pos_idx, w in enumerate(week.get("workouts", [])):
                    wid = w.get("id", "")
                    if wid:
                        all_wids_ordered.append((wid, week_name, pos_idx + 1))
        else:
            for i, wid in enumerate(program.get("workout_ids", [])):
                if wid:
                    all_wids_ordered.append((wid, None, i + 1))

        for wid, week_name, pos in all_wids_ordered:
            map_entry = {
                "seriesId": series_id,
                "title": series_title,
                "position": pos,
                "week": week_name,
                "isChallenge": is_challenge,
            }
            if wid in ws_map:
                existing_sids = {e.get("seriesId") for e in ws_map[wid]}
                if series_id in existing_sids:
                    for existing in ws_map[wid]:
                        if existing.get("seriesId") == series_id:
                            existing["week"] = week_name
                            existing["position"] = pos
                            break
                    continue
                ws_map[wid].append(map_entry)
            else:
                ws_map[wid] = [map_entry]
            newly_mapped += 1

        program_titles = program.get("workout_titles", [])
        series_workouts = []
        if weeks:
            for week in weeks:
                for w in week.get("workouts", []):
                    series_workouts.append({
                        "id": w.get("id", ""),
                        "title": w.get("title", ""),
                        "week": week.get("name", ""),
                    })
        else:
            for i, wid in enumerate(program.get("workout_ids", [])):
                series_workouts.append({
                    "id": wid,
                    "title": program_titles[i] if i < len(program_titles) else "",
                })

        series_summaries.append({
            "series_id": series_id,
            "title": series_title,
            "workout_count": len(all_wids_ordered),
            "workouts": series_workouts,
        })

    _save_workout_series_map(ws_map)

    return {
        "workout_id": workout_id,
        "series": series_summaries,
        "newly_mapped": newly_mapped,
    }


def backfill_program_weeks(headers: dict | None = None) -> dict:
    """Re-fetch programs that are missing the 'weeks' field.

    Returns stats on how many were backfilled.
    """
    if not r2_configured():
        return {"skipped": True, "reason": "R2 not configured"}
    if headers is None:
        from ifit_auth import get_auth_headers
        headers = get_auth_headers()

    keys = list_keys(PROGRAMS_PREFIX)
    missing = 0
    updated = 0
    errors = 0
    ws_map = _load_workout_series_map()
    ws_map_changed = False

    for key in keys:
        prog = download_json(key)
        if not prog:
            continue
        if prog.get("weeks"):
            continue
        missing += 1
        series_id = prog.get("series_id", "")
        if not series_id:
            continue

        ok = _store_program_from_pre_workout({"id": series_id}, headers, force=True)
        if ok:
            updated += 1
            refreshed = download_json(key)
            if refreshed and refreshed.get("weeks"):
                series_title = refreshed.get("title", "")
                is_challenge = False
                for week in refreshed["weeks"]:
                    week_name = week.get("name")
                    for pos_idx, w in enumerate(week.get("workouts", [])):
                        wid = w.get("id", "")
                        if not wid:
                            continue
                        if wid in ws_map:
                            for entry in ws_map[wid]:
                                if entry.get("seriesId") == series_id:
                                    entry["week"] = week_name
                                    entry["position"] = pos_idx + 1
                                    ws_map_changed = True
                                    break
                        else:
                            ws_map[wid] = [{
                                "seriesId": series_id,
                                "title": series_title,
                                "position": pos_idx + 1,
                                "week": week_name,
                                "isChallenge": is_challenge,
                            }]
                            ws_map_changed = True
            time.sleep(1)
        else:
            errors += 1

    if ws_map_changed:
        _save_workout_series_map(ws_map)

    print(f"    [backfill] {missing} programs missing weeks, "
          f"{updated} updated, {errors} errors")
    return {"missing": missing, "updated": updated, "errors": errors}


def sync_series_discovery(batch_size: int = 50) -> dict:
    """Discover series memberships for library workouts via pre-workout API.

    Processes a batch each cycle, building the workout-to-series mapping
    incrementally. Uses 1s delay between calls to avoid API rate issues.
    """
    if not r2_configured():
        return {"skipped": True, "reason": "R2 not configured"}

    library = get_cache(KEY_LIBRARY_WORKOUTS)
    if library is None:
        library_path = CACHE_DIR / "library_workouts.json"
        if not library_path.exists():
            print("    [series] library_workouts.json not found, skipping")
            return {"error": "library not found"}
        with open(library_path) as f:
            library = json.load(f)
    all_ids = {w["id"] for w in library}

    state = _load_series_state()
    attempted = state.get("attempted", {})

    cutoff = time.time() - SERIES_REFRESH_DAYS * 86400
    stale = [
        wid for wid, ts in attempted.items()
        if isinstance(ts, (int, float)) and ts < cutoff
    ]

    pending = [wid for wid in all_ids if wid not in attempted]
    to_process = (pending + stale)[:batch_size]

    total_attempted = sum(1 for wid in all_ids if wid in attempted)
    print(f"    [series] Library: {len(all_ids)} workouts, "
          f"{total_attempted} attempted, {len(pending)} pending, "
          f"{len(stale)} stale", flush=True)

    if not to_process:
        print("    [series] Nothing to process, all caught up")
        return {
            "processed": 0, "total": len(all_ids),
            "total_attempted": total_attempted,
        }

    from ifit_auth import get_auth_headers
    headers = get_auth_headers()

    ws_map = _load_workout_series_map()

    print(f"    [series] Starting batch of {len(to_process)} "
          f"(batch_size={batch_size})...", flush=True)

    discovered = 0
    no_series = 0
    new_programs = 0
    errors = 0
    t0 = time.time()

    with httpx.Client(timeout=20) as client:
        for i, wid in enumerate(to_process):
            try:
                r = client.get(PRE_WORKOUT_URL.format(wid=wid), headers=headers)
                if r.status_code != 200:
                    errors += 1
                    attempted[wid] = time.time()
                    continue

                data = r.json()
                entries = _extract_series_from_pre_workout(data, wid)

                if entries:
                    ws_map[wid] = entries
                    discovered += 1
                    for pd in data.get("programDetails") or []:
                        if _store_program_from_pre_workout(pd, headers):
                            new_programs += 1
                else:
                    no_series += 1

                attempted[wid] = time.time()

            except Exception:
                errors += 1
                attempted[wid] = time.time()

            if (i + 1) % 10 == 0:
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                eta = (len(to_process) - i - 1) / rate if rate > 0 else 0
                print(f"    [series] {i+1}/{len(to_process)} "
                      f"({discovered} with series, {no_series} none, "
                      f"{new_programs} new programs, {errors} err) "
                      f"[{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining]", flush=True)

            time.sleep(1.0)

    elapsed = time.time() - t0
    _save_workout_series_map(ws_map)

    state["attempted"] = attempted
    state["stats"]["last_batch"] = {
        "processed": len(to_process),
        "discovered": discovered,
        "no_series": no_series,
        "new_programs": new_programs,
        "errors": errors,
        "elapsed_seconds": round(elapsed, 1),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_series_state(state)

    total_in_map = len(ws_map)
    print(f"    [series] Batch complete in {elapsed:.1f}s: "
          f"{discovered} with series, {no_series} none, "
          f"{new_programs} new programs, {errors} errors", flush=True)
    print(f"    [series] Overall: {total_in_map} workouts mapped, "
          f"{len(pending) - len(to_process)} remaining", flush=True)

    return {
        "processed": len(to_process),
        "discovered": discovered,
        "no_series": no_series,
        "new_programs": new_programs,
        "errors": errors,
        "total": len(all_ids),
        "total_mapped": total_in_map,
        "elapsed_seconds": round(elapsed, 1),
    }


# ── Exercise cache migration ─────────────────────────────────────────

def migrate_exercise_cache() -> dict:
    """One-time upload of local exercise_cache.json entries to R2."""
    if not r2_configured():
        return {"skipped": True}

    state = _load_state()
    if state.get("migrated"):
        print("    [migrate] Already migrated, skipping")
        return {"already_migrated": True}

    cache = get_cache(KEY_EXERCISE_CACHE)
    if cache is None:
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
    parser.add_argument("--series", action="store_true", help="Discover series via pre-workout")
    parser.add_argument("--series-batch", type=int, default=50, help="Series batch size")
    parser.add_argument("--backfill-weeks", action="store_true", help="Re-fetch programs missing week structure")
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

    if args.backfill_weeks:
        result = backfill_program_weeks()
        print(f"Backfill weeks: {json.dumps(result, indent=2)}")
        return 0

    if args.series:
        result = sync_series_discovery(batch_size=args.series_batch)
        print(f"Series discovery: {json.dumps(result, indent=2)}")
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
        print("\nDiscovering programs (recommended/up-next)...")
        prog_result = sync_programs()
        for k, v in prog_result.items():
            print(f"  {k}: {v}")

        print(f"\nDiscovering series via pre-workout (batch={args.series_batch})...")
        series_result = sync_series_discovery(batch_size=args.series_batch)
        for k, v in series_result.items():
            print(f"  {k}: {v}")

        print("\nMigrating exercise cache...")
        mig_result = migrate_exercise_cache()
        for k, v in mig_result.items():
            print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

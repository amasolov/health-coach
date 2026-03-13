#!/usr/bin/env python3
"""
Sync Hevy strength training data into TimescaleDB.

Pulls workouts via the Hevy REST API (v1) and stores individual sets.
Uses incremental sync: pages through workouts newest-first and stops
when it encounters a workout already in the database.

Also syncs exercise templates for the iFit-to-Hevy routine pipeline.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg2

from scripts.cache_store import (
    get_cache, put_cache, get_cache_text, put_cache_text,
    KEY_HEVY_EXERCISES, KEY_HEVY_EXERCISE_REF,
)

HEVY_BASE = "https://api.hevyapp.com/v1"
PAGE_SIZE = 10
CACHE_DIR = Path(__file__).resolve().parent.parent / ".ifit_capture"
EXERCISES_JSON = CACHE_DIR / "hevy_exercises.json"
EXERCISES_REF = CACHE_DIR / "hevy_exercise_ref.txt"
TEMPLATES_MAX_AGE = 86400 * 3  # refresh every 3 days


# ---------------------------------------------------------------------------
# Hevy API helpers
# ---------------------------------------------------------------------------

def _hevy_get(
    path: str,
    api_key: str,
    params: dict | None = None,
) -> dict:
    headers = {"api-key": api_key, "accept": "application/json"}
    resp = httpx.get(f"{HEVY_BASE}{path}", headers=headers, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fetch_exercise_templates(api_key: str) -> dict[str, dict]:
    """Build a lookup of exercise_template_id -> {title, type, muscle_group}."""
    templates: dict[str, dict] = {}
    page = 1
    while True:
        data = _hevy_get("/exercise_templates", api_key, {"page": page, "pageSize": 100})
        for t in data.get("exercise_templates", []):
            templates[t["id"]] = {
                "title": t.get("title", ""),
                "type": t.get("type", ""),
                "muscle_group": t.get("primary_muscle_group", ""),
            }
        if page >= data.get("page_count", 1):
            break
        page += 1
    return templates


# ---------------------------------------------------------------------------
# Exercise template sync (for iFit-to-Hevy routine pipeline)
# ---------------------------------------------------------------------------

def sync_exercise_templates(api_key: str, force: bool = False) -> dict:
    """Fetch all Hevy exercise templates and cache them locally.

    Produces two files used by the LLM extraction and Hevy routine creation:
      - hevy_exercises.json: full template data with IDs
      - hevy_exercise_ref.txt: compact reference for LLM prompts (Title | ID | muscle | equipment)

    Returns stats dict.
    """
    cached = get_cache(KEY_HEVY_EXERCISES)
    if not force and cached is not None:
        if EXERCISES_JSON.exists():
            age = time.time() - EXERCISES_JSON.stat().st_mtime
        else:
            age = 0
        if age < TEMPLATES_MAX_AGE:
            print(f"    Exercise templates cache fresh ({len(cached)} templates, {age/3600:.0f}h old)")
            return {"cached": True, "count": len(cached)}
    if not force and cached is None and EXERCISES_JSON.exists():
        age = time.time() - EXERCISES_JSON.stat().st_mtime
        if age < TEMPLATES_MAX_AGE:
            with open(EXERCISES_JSON) as f:
                existing = json.load(f)
            put_cache(KEY_HEVY_EXERCISES, existing)
            print(f"    Exercise templates cache fresh ({len(existing)} templates, {age/3600:.0f}h old)")
            return {"cached": True, "count": len(existing)}

    print("    Fetching exercise templates from Hevy API...", flush=True)
    templates: list[dict] = []
    page = 1
    while True:
        data = _hevy_get("/exercise_templates", api_key, {"page": page, "pageSize": 100})
        batch = data.get("exercise_templates", [])
        for t in batch:
            templates.append({
                "id": t["id"],
                "title": t.get("title", ""),
                "type": t.get("type", ""),
                "primary_muscle_group": t.get("primary_muscle_group", ""),
                "secondary_muscle_groups": t.get("secondary_muscle_groups", []),
                "equipment": t.get("equipment", "none"),
                "is_custom": t.get("is_custom", False),
            })
        if page >= data.get("page_count", 1):
            break
        page += 1

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    with open(EXERCISES_JSON, "w") as f:
        json.dump(templates, f, indent=2)

    ref_lines = []
    for t in sorted(templates, key=lambda x: x["title"]):
        ref_lines.append(f"{t['title']} | {t['id']} | {t['primary_muscle_group']} | {t['equipment']}")
    ref_text = "\n".join(ref_lines) + "\n"
    with open(EXERCISES_REF, "w") as f:
        f.write(ref_text)

    put_cache(KEY_HEVY_EXERCISES, templates)
    put_cache_text(KEY_HEVY_EXERCISE_REF, ref_text)

    print(f"    Cached {len(templates)} exercise templates "
          f"({sum(1 for t in templates if t['is_custom'])} custom)", flush=True)
    return {"cached": False, "count": len(templates)}


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _extract_sets(
    workout: dict,
    templates: dict[str, dict],
) -> list[dict]:
    """Flatten a Hevy workout into individual set rows."""
    workout_id = workout.get("id", "")
    routine_id = workout.get("routine_id") or None
    workout_start = _parse_iso(workout.get("start_time"))
    if not workout_start:
        return []

    rows: list[dict] = []
    for exercise in workout.get("exercises", []):
        tmpl_id = exercise.get("exercise_template_id", "")
        tmpl = templates.get(tmpl_id, {})

        exercise_name = exercise.get("title") or tmpl.get("title", "Unknown")
        exercise_type = tmpl.get("type", "")
        muscle_group = tmpl.get("muscle_group", "")

        for s in exercise.get("sets", []):
            rows.append({
                "time": workout_start,
                "workout_id": workout_id,
                "routine_id": routine_id,
                "exercise_name": exercise_name,
                "exercise_type": exercise_type,
                "muscle_group": muscle_group,
                "set_number": s.get("index", 0) + 1,
                "set_type": s.get("type", "normal"),
                "weight_kg": s.get("weight_kg"),
                "reps": s.get("reps"),
                "rpe": s.get("rpe"),
                "duration_s": s.get("duration_seconds"),
                "distance_m": s.get("distance_meters"),
            })

    return rows


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def _get_conn():
    from scripts.db_pool import dsn_kwargs
    return psycopg2.connect(**dsn_kwargs())


def _workout_exists(cur, user_id: int, workout_id: str) -> bool:
    cur.execute(
        "SELECT 1 FROM strength_sets WHERE user_id = %s AND workout_id = %s LIMIT 1",
        (user_id, workout_id),
    )
    return cur.fetchone() is not None


def _insert_set(cur, user_id: int, data: dict) -> None:
    cur.execute("""
        INSERT INTO strength_sets (
            time, user_id, workout_id, routine_id, exercise_name, exercise_type,
            muscle_group, set_number, set_type,
            weight_kg, reps, rpe, duration_s, distance_m
        ) VALUES (
            %(time)s, %(user_id)s, %(workout_id)s, %(routine_id)s,
            %(exercise_name)s, %(exercise_type)s,
            %(muscle_group)s, %(set_number)s, %(set_type)s,
            %(weight_kg)s, %(reps)s, %(rpe)s, %(duration_s)s, %(distance_m)s
        )
    """, {**data, "user_id": user_id})


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def sync_user(
    slug: str,
    user_id: int,
    api_key: str,
    full_sync: bool = False,
) -> dict:
    """Sync all Hevy workouts for a user. Returns summary.

    full_sync=True fetches every page even if a workout is already in the DB,
    useful for backfilling historical data that was missed on initial sync.
    """
    if not api_key:
        return {"error": f"No Hevy API key for {slug}"}

    conn = _get_conn()
    conn.autocommit = True
    cur = conn.cursor()

    try:
        print(f"    Fetching exercise templates from Hevy...")
        templates = _fetch_exercise_templates(api_key)
        print(f"    Loaded {len(templates)} exercise templates")

        total_workouts_seen = 0
        total_workouts_inserted = 0
        total_sets = 0
        page = 1

        print(f"    Fetching workouts{'  [FULL SYNC]' if full_sync else ''}...")
        while True:
            data = _hevy_get("/workouts", api_key, {"page": page, "pageSize": PAGE_SIZE})
            workouts = data.get("workouts", [])
            page_count = data.get("page_count", 1)

            if not workouts:
                break

            stop = False
            for workout in workouts:
                wid = workout.get("id", "")
                total_workouts_seen += 1

                if _workout_exists(cur, user_id, wid):
                    if not full_sync:
                        stop = True
                        break
                    continue  # already stored; skip but keep paginating

                sets = _extract_sets(workout, templates)
                for s in sets:
                    _insert_set(cur, user_id, s)

                total_workouts_inserted += 1
                total_sets += len(sets)

            if stop or page >= page_count:
                break
            page += 1

        print(f"    Seen {total_workouts_seen} workouts, inserted {total_workouts_inserted} ({total_sets} sets)")
        return {
            "workouts_found": total_workouts_seen,
            "workouts_inserted": total_workouts_inserted,
            "sets_inserted": total_sets,
            "templates_loaded": len(templates),
        }
    finally:
        cur.close()
        conn.close()

#!/usr/bin/env python3
"""
Sync orchestrator.

In the HA addon, iterates over users from USERS_JSON env var.
Locally, uses USER_SLUG from .env.

Data sources:
  - Garmin Connect: activities, vitals, body composition
  - Hevy: strength training (sets, reps, weight)
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

import psycopg2

from scripts.sync_garmin import sync_user as sync_garmin_user
from scripts.sync_hevy import sync_user as sync_hevy_user
from scripts.strength_tss import (
    SetData,
    estimate_hybrid_tss,
    estimate_volume_tss,
)
from scripts.tz import load_user_tz


def get_users() -> list[dict]:
    """Get user list from USERS_JSON (addon) or USER_SLUG (local dev)."""
    users_json = os.environ.get("USERS_JSON")
    if users_json:
        return json.loads(users_json)

    slug = os.environ.get("USER_SLUG", "alexey")
    hevy_key = os.environ.get("HEVY_API_KEY", "")
    return [{"slug": slug, "name": slug, "hevy_api_key": hevy_key}]


def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "health"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def _resolve_user_id(slug: str) -> int | None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE slug = %s", (slug,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _load_exercise_maxes(cur, user_id: int) -> dict[str, float]:
    """Historical max weight per exercise (for relative intensity)."""
    cur.execute("""
        SELECT exercise_name, MAX(weight_kg)
        FROM strength_sets
        WHERE user_id = %s AND weight_kg IS NOT NULL AND weight_kg > 0
        GROUP BY exercise_name
    """, (user_id,))
    return {row[0]: float(row[1]) for row in cur.fetchall()}


def _load_workout_sets(cur, user_id: int, workout_id: str) -> list[SetData]:
    cur.execute("""
        SELECT exercise_name, muscle_group, set_type,
               weight_kg, reps, rpe, duration_s
        FROM strength_sets
        WHERE user_id = %s AND workout_id = %s
        ORDER BY time, set_number
    """, (user_id, workout_id))
    return [
        SetData(
            exercise_name=r[0],
            muscle_group=r[1],
            set_type=r[2],
            weight_kg=float(r[3]) if r[3] else None,
            reps=int(r[4]) if r[4] else None,
            rpe=float(r[5]) if r[5] else None,
            duration_s=int(r[6]) if r[6] else None,
        )
        for r in cur.fetchall()
    ]


def _load_user_thresholds(cur, user_id: int) -> tuple[float | None, float | None]:
    """Load resting_hr and LTHR from latest vitals / athlete config."""
    cur.execute("""
        SELECT resting_hr FROM vitals
        WHERE user_id = %s AND resting_hr IS NOT NULL
        ORDER BY time DESC LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    resting_hr = float(row[0]) if row else None
    lthr = None  # will come from athlete.yaml when available
    return resting_hr, lthr


def backfill_strength_tss(user_id: int, tz_name: str = "Australia/Sydney") -> dict:
    """
    Compute estimated TSS for each Hevy workout and ensure it appears
    in the activities table.

    Three paths per workout:
      1. Garmin activity HAS avg_hr (HRM Pro tracked) → hybrid TSS
         (hrTSS + volume bonus). Updates even if Garmin already has
         a non-NULL TSS from its own estimation, since hybrid is better.
      2. Garmin activity exists but no HR → volume-model TSS, tagged.
      3. No Garmin activity → insert synthetic hevy-source activity.

    Returns counts of updated/inserted/skipped/hybrid rows.
    """
    conn = _get_conn()
    conn.autocommit = True
    cur = conn.cursor()

    exercise_maxes = _load_exercise_maxes(cur, user_id)
    resting_hr, lthr = _load_user_thresholds(cur, user_id)

    cur.execute("""
        SELECT DISTINCT workout_id, MIN(time) AS workout_time
        FROM strength_sets
        WHERE user_id = %s
        GROUP BY workout_id
        ORDER BY MIN(time)
    """, (user_id,))
    hevy_workouts = cur.fetchall()

    updated = 0
    inserted = 0
    skipped = 0
    hybrid_count = 0

    for wid, wtime in hevy_workouts:
        day = wtime.date() if isinstance(wtime, datetime) else wtime
        sets = _load_workout_sets(cur, user_id, wid)

        # --- Already tracked as a hevy-source activity? ---
        cur.execute("""
            SELECT 1 FROM activities
            WHERE user_id = %s AND source = 'hevy' AND source_id = %s
        """, (user_id, wid))
        if cur.fetchone():
            skipped += 1
            continue

        # --- Already tagged on a Garmin activity? ---
        # Re-estimate if the tagged Garmin activity now has HR data
        cur.execute("""
            SELECT source_id, avg_hr, duration_s, tss
            FROM activities
            WHERE user_id = %s AND source = 'garmin'
              AND raw_data->>'hevy_workout_id' = %s
        """, (user_id, wid))
        tagged = cur.fetchone()
        if tagged:
            garmin_sid, avg_hr, dur_s, old_tss = tagged
            if avg_hr and avg_hr > 0 and dur_s and dur_s > 60:
                tss = estimate_hybrid_tss(
                    dur_s, avg_hr, sets, exercise_maxes, resting_hr, lthr,
                )
                cur.execute("""
                    UPDATE activities SET tss = %s,
                        raw_data = raw_data || '{"tss_method":"hybrid"}'::jsonb
                    WHERE user_id = %s AND source_id = %s
                """, (tss, user_id, garmin_sid))
                hybrid_count += 1
            skipped += 1
            continue

        # --- Try to match untagged Garmin strength activity on the same day ---
        date_expr = f"(time AT TIME ZONE '{tz_name}')::date"
        cur.execute(f"""
            SELECT source_id, avg_hr, duration_s FROM activities
            WHERE user_id = %s
              AND activity_type = 'strength_training'
              AND {date_expr} = %s
              AND source = 'garmin'
              AND (raw_data IS NULL OR raw_data->>'hevy_workout_id' IS NULL)
            ORDER BY time
            LIMIT 1
        """, (user_id, day))
        garmin_match = cur.fetchone()

        if garmin_match:
            garmin_sid, avg_hr, dur_s = garmin_match

            if avg_hr and avg_hr > 0 and dur_s and dur_s > 60:
                tss = estimate_hybrid_tss(
                    dur_s, avg_hr, sets, exercise_maxes, resting_hr, lthr,
                )
                method = "hybrid"
                hybrid_count += 1
            else:
                tss = estimate_volume_tss(sets, exercise_maxes)
                method = "volume_enhanced"

            cur.execute("""
                UPDATE activities
                SET tss = %s,
                    raw_data = COALESCE(raw_data, '{}'::jsonb)
                              || jsonb_build_object(
                                   'hevy_workout_id', %s,
                                   'tss_method', %s
                                 )
                WHERE user_id = %s AND source_id = %s
            """, (tss, wid, method, user_id, garmin_sid))
            updated += 1
        else:
            tss = estimate_volume_tss(sets, exercise_maxes)
            working = sum(1 for s in sets if not s.set_type or s.set_type != "warmup")
            cur.execute("""
                INSERT INTO activities
                    (time, user_id, source, source_id, activity_type,
                     title, duration_s, tss, raw_data)
                VALUES (%s, %s, 'hevy', %s, 'strength_training',
                        'Strength Training (Hevy)', %s, %s,
                        jsonb_build_object('tss_method', 'volume_enhanced'))
            """, (wtime, user_id, wid,
                  working * 90,
                  tss))
            inserted += 1

    cur.close()
    conn.close()
    return {
        "updated": updated,
        "inserted": inserted,
        "skipped": skipped,
        "hybrid": hybrid_count,
    }


def main() -> int:
    users = get_users()
    errors = 0

    for user in users:
        slug = user["slug"]
        name = user.get("name", slug)
        print(f"\n{'='*60}")
        print(f"Syncing data for {name} ({slug})")
        print(f"{'='*60}")

        user_id = _resolve_user_id(slug)
        if not user_id:
            print(f"  ERROR: User '{slug}' not found in database. Run migrations first.")
            errors += 1
            continue

        tz = load_user_tz(slug)
        tz_name = str(tz)

        # --- Garmin Connect ---
        print(f"\n  [Garmin Connect]")
        try:
            result = sync_garmin_user(slug, user_id)
            if "error" in result:
                print(f"    SKIP: {result['error']}")
            else:
                print(f"    Activities: {result['activities_inserted']} new  (found {result.get('activities_found', '?')} from Garmin)")
                print(f"    Body comp:  {result['body_comp_inserted']} new  (found {result.get('body_comp_found', '?')} from Garmin)")
                print(f"    Vitals:     {result['vitals_inserted']} new  (found {result.get('vitals_found', '?')} days with data)")
        except Exception as e:
            print(f"    ERROR: Garmin sync failed: {e}")
            traceback.print_exc()
            errors += 1

        # --- Hevy ---
        hevy_key = user.get("hevy_api_key") or os.environ.get("HEVY_API_KEY", "")
        print(f"\n  [Hevy]")
        if hevy_key:
            try:
                result = sync_hevy_user(slug, user_id, hevy_key)
                if "error" in result:
                    print(f"    SKIP: {result['error']}")
                else:
                    print(f"    Workouts: {result['workouts_inserted']} new  (found {result.get('workouts_found', '?')} total, {result['sets_inserted']} sets)")
            except Exception as e:
                print(f"    ERROR: Hevy sync failed: {e}")
                traceback.print_exc()
                errors += 1
        else:
            print(f"    SKIP: No Hevy API key configured")

        # --- Strength TSS backfill ---
        print(f"\n  [Strength TSS]")
        try:
            result = backfill_strength_tss(user_id, tz_name=tz_name)
            print(f"    Garmin updated: {result['updated']}, Hevy-only inserted: {result['inserted']}, hybrid(HR): {result['hybrid']}")
        except Exception as e:
            print(f"    ERROR: Strength TSS backfill failed: {e}")
            traceback.print_exc()
            errors += 1

    if errors:
        print(f"\nSync completed with {errors} error(s)")
        return 1

    print(f"\nSync completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())

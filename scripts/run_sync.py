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

# Key threshold fields that gate profile auto-population.
# If ALL of these are set we skip the Garmin profile fetch to save API calls.
_THRESHOLD_GATE_FIELDS = [
    ("thresholds", "heart_rate", "max_hr"),
    ("thresholds", "heart_rate", "resting_hr"),
    ("thresholds", "cycling", "ftp"),
    ("body", "weight_kg"),
]


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


def _load_athlete_thresholds(slug: str) -> dict:
    """Load threshold values from athlete.yaml for a given slug."""
    from pathlib import Path
    import yaml

    path = Path(__file__).resolve().parent.parent / "config" / "athlete.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    user = data.get("users", {}).get(slug, {})
    thresholds = user.get("thresholds", {})
    hr = thresholds.get("heart_rate", {})
    return {
        "ftp": thresholds.get("cycling", {}).get("ftp"),
        "lthr_run": hr.get("lthr_run"),
        "lthr_bike": hr.get("lthr_bike"),
        "resting_hr": hr.get("resting_hr"),
        "max_hr": hr.get("max_hr"),
    }


def _load_user_thresholds(cur, user_id: int, slug: str = "") -> tuple[float | None, float | None]:
    """Load resting_hr and LTHR from vitals + athlete.yaml."""
    cur.execute("""
        SELECT resting_hr FROM vitals
        WHERE user_id = %s AND resting_hr IS NOT NULL
        ORDER BY time DESC LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    resting_hr_vitals = float(row[0]) if row else None

    athlete = _load_athlete_thresholds(slug) if slug else {}
    resting_hr = resting_hr_vitals or athlete.get("resting_hr")

    # Prefer explicit lthr; fall back to 88% of max_hr as a reasonable estimate
    lthr = athlete.get("lthr_run") or athlete.get("lthr_bike")
    if not lthr and athlete.get("max_hr"):
        lthr = round(athlete["max_hr"] * 0.88)

    return resting_hr, lthr


def _thresholds_incomplete(slug: str) -> bool:
    """Return True if any key threshold is still null in athlete.yaml."""
    data = _load_athlete_thresholds(slug)
    for keys in _THRESHOLD_GATE_FIELDS:
        # keys is a flat tuple like ("thresholds", "heart_rate", "max_hr")
        # _load_athlete_thresholds returns a flat dict so we check the leaf key
        leaf = keys[-1]
        if data.get(leaf) is None:
            return True
    return False


def sync_garmin_profile(slug: str) -> dict:
    """
    Fetch athlete profile data from Garmin Connect and merge any null
    fields into athlete.yaml.

    Covers: date_of_birth, sex, height, weight, body_fat, resting_hr,
    max_hr, VO2max, lactate threshold, cycling FTP, threshold pace.

    Only fills fields that are currently null — never overwrites manually
    set values.  Skips entirely if all key thresholds are already set.

    Returns a dict with keys:
      - "skipped": True when all thresholds were already populated
      - "written": dict of field_path -> value for everything updated
      - "still_missing": list of fields still null with hints
      - "error": str (only present on failure)
    """
    from pathlib import Path
    from scripts.garmin_auth import try_cached_login
    from scripts.garmin_fetch import fetch_garmin_profile, merge_into_athlete_yaml

    athlete_path = Path(__file__).resolve().parent.parent / "config" / "athlete.yaml"

    if not _thresholds_incomplete(slug):
        return {"skipped": True, "written": {}, "still_missing": []}

    client = try_cached_login(slug)
    if not client:
        return {"error": "Garmin not authenticated", "written": {}, "still_missing": []}

    result = fetch_garmin_profile(slug, client)
    written = merge_into_athlete_yaml(str(athlete_path), slug, result["fetched"])

    return {
        "skipped": False,
        "written": written,
        "still_missing": result.get("missing", []),
    }


def backfill_strength_tss(user_id: int, tz_name: str = "Australia/Sydney", slug: str = "") -> dict:
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
    resting_hr, lthr = _load_user_thresholds(cur, user_id, slug)

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


def backfill_missing_tss(user_id: int, slug: str = "") -> dict:
    """
    Retroactively estimate TSS for activities that are missing it.

    Pass 1 – Power-based: activities with avg_power or normalized_power but no TSS.
              Uses FTP from athlete.yaml. Covers Zwift/cycling rides synced before
              FTP was configured.

    Pass 2 – HR-based: activities with avg_hr but no TSS (non-strength).
              Uses LTHR and resting_hr from athlete.yaml / vitals.

    Pass 3 – Duration-based fallback: any remaining activity with no TSS and no
              useful physiological data, using activity-type-specific MET estimates
              to give at least a plausible stress value.
              Applied only to activity types where this makes sense (yoga, strength,
              recovery, hiking, skiing, etc.).

    Only updates activities where the new estimate is meaningfully higher than what
    was there before (NULL or zero).
    """
    athlete = _load_athlete_thresholds(slug) if slug else {}
    ftp = athlete.get("ftp")
    lthr = athlete.get("lthr_run") or athlete.get("lthr_bike")
    if not lthr and athlete.get("max_hr"):
        lthr = round(athlete["max_hr"] * 0.88)
    max_hr = athlete.get("max_hr")

    conn = _get_conn()
    conn.autocommit = True
    cur = conn.cursor()

    # Get resting HR from vitals as well
    cur.execute("""
        SELECT resting_hr FROM vitals
        WHERE user_id = %s AND resting_hr IS NOT NULL
        ORDER BY time DESC LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    resting_hr = float(row[0]) if row else athlete.get("resting_hr")

    power_updated = 0
    hr_updated = 0
    hr_corrected = 0
    duration_updated = 0

    # -------------------------------------------------------------------
    # Pass 0: Recalculate TSS for activities that were sync-estimated with
    # generic defaults (rest=50, lt=160) instead of the user's actual
    # thresholds.  Only touches Garmin-sourced activities without a
    # tss_method tag (i.e. set during initial sync, not by backfill).
    # Skips activities where Garmin provided its own trainingStressScore.
    # -------------------------------------------------------------------
    if lthr and resting_hr and (lthr != 160 or resting_hr != 50):
        cur.execute("""
            SELECT source_id, duration_s, avg_hr, tss, activity_type
            FROM activities
            WHERE user_id = %s AND source = 'garmin'
              AND tss IS NOT NULL
              AND avg_hr IS NOT NULL AND avg_hr > 0
              AND (raw_data IS NULL OR raw_data->>'tss_method' IS NULL)
        """, (user_id,))
        recalc_rows = cur.fetchall()
        for sid, dur_s, avg_hr_val, old_tss, atype in recalc_rows:
            if not dur_s or dur_s <= 0:
                continue
            hours = dur_s / 3600

            # Check if the stored TSS matches HR formula with generic defaults
            hr_if_default = max(0, min((float(avg_hr_val) - 50) / (160 - 50), 2.0))
            tss_default = round(hours * hr_if_default * hr_if_default * 100, 1)
            if abs(float(old_tss) - tss_default) > 1.0:
                continue  # Garmin-provided TSS — don't touch

            # Recalculate with correct thresholds
            hr_if_correct = max(0, min((float(avg_hr_val) - resting_hr) / (float(lthr) - resting_hr), 2.0))
            new_tss = round(hours * hr_if_correct * hr_if_correct * 100, 1)
            if new_tss != float(old_tss) and new_tss > 0:
                cur.execute("""
                    UPDATE activities SET tss = %s,
                        raw_data = COALESCE(raw_data, '{}'::jsonb)
                                  || '{"tss_method":"hr_corrected"}'::jsonb
                    WHERE user_id = %s AND source_id = %s
                """, (new_tss, user_id, sid))
                hr_corrected += 1

    # -------------------------------------------------------------------
    # Pass 1: power-based TSS for CYCLING activities with power but no TSS.
    # FTP is a cycling-specific threshold; running power (HRM Pro) operates
    # on a different scale and must not be divided by cycling FTP.
    # -------------------------------------------------------------------
    from scripts.sync_garmin import _CYCLING_TYPES
    cycling_list = list(_CYCLING_TYPES)
    if ftp and ftp > 0:
        cur.execute("""
            SELECT source_id, duration_s, avg_power, normalized_power
            FROM activities
            WHERE user_id = %s AND tss IS NULL AND source != 'hevy'
              AND (normalized_power IS NOT NULL OR avg_power IS NOT NULL)
              AND activity_type = ANY(%s)
        """, (user_id, cycling_list))
        power_rows = cur.fetchall()
        for sid, dur_s, avg_p, norm_p in power_rows:
            if not dur_s or dur_s <= 0:
                continue
            hours = dur_s / 3600
            p = float(norm_p or avg_p)
            intensity = p / ftp
            tss = round(hours * intensity * intensity * 100, 1)
            if tss > 0:
                cur.execute("""
                    UPDATE activities SET tss = %s,
                        raw_data = COALESCE(raw_data, '{}'::jsonb)
                                  || '{"tss_method":"power_backfill"}'::jsonb
                    WHERE user_id = %s AND source_id = %s AND tss IS NULL
                """, (tss, user_id, sid))
                power_updated += 1

    # -------------------------------------------------------------------
    # Pass 2: HR-based TSS for non-strength activities with HR but no TSS
    # -------------------------------------------------------------------
    if lthr and resting_hr:
        cur.execute("""
            SELECT source_id, duration_s, avg_hr, activity_type
            FROM activities
            WHERE user_id = %s AND tss IS NULL AND source != 'hevy'
              AND avg_hr IS NOT NULL AND avg_hr > 0
              AND activity_type NOT IN ('strength_training', 'weight_training')
        """, (user_id,))
        hr_rows = cur.fetchall()
        for sid, dur_s, avg_hr, atype in hr_rows:
            if not dur_s or dur_s <= 0:
                continue
            hours = dur_s / 3600
            rest = resting_hr or 50
            lt = float(lthr)
            if lt <= rest:
                continue
            hr_if = max(0.0, min((avg_hr - rest) / (lt - rest), 2.0))
            tss = round(hours * hr_if * hr_if * 100, 1)
            if tss > 0:
                cur.execute("""
                    UPDATE activities SET tss = %s,
                        raw_data = COALESCE(raw_data, '{}'::jsonb)
                                  || '{"tss_method":"hr_backfill"}'::jsonb
                    WHERE user_id = %s AND source_id = %s AND tss IS NULL
                """, (tss, user_id, sid))
                hr_updated += 1

    # -------------------------------------------------------------------
    # Pass 3: duration-based fallback for activities with no useful data
    # Applied to activity types where we can make a reasonable estimate.
    # Stress per hour at moderate effort is approximated from literature:
    #   yoga/recovery/stretching ~20 TSS/hr, strength ~30 TSS/hr (untested),
    #   hiking ~40 TSS/hr, skiing ~35 TSS/hr, climbing ~40 TSS/hr.
    # These are conservative lower bounds to avoid over-inflating CTL.
    # -------------------------------------------------------------------
    DURATION_TSS_PER_HOUR: dict[str, float] = {
        "yoga": 20.0,
        "pilates": 20.0,
        "breathwork": 5.0,
        "stretching": 15.0,
        "strength_training": 30.0,
        "weight_training": 30.0,
        "indoor_climbing": 40.0,
        "rock_climbing": 40.0,
        "hiking": 40.0,
        "trail_running": 60.0,
        "running": 65.0,
        "treadmill_running": 60.0,
        "indoor_running": 55.0,
        "virtual_run": 55.0,
        "cycling": 50.0,
        "virtual_ride": 50.0,
        "mountain_biking": 50.0,
        "road_biking": 50.0,
        "gravel_cycling": 50.0,
        "indoor_cycling": 55.0,
        "backcountry_skiing_snowboarding_ws": 35.0,
        "resort_skiing_snowboarding_ws": 30.0,
        "cross_country_skiing_ws": 55.0,
    }

    cur.execute("""
        SELECT source_id, duration_s, activity_type
        FROM activities
        WHERE user_id = %s AND tss IS NULL AND source != 'hevy'
          AND duration_s IS NOT NULL AND duration_s >= 300
    """, (user_id,))
    dur_rows = cur.fetchall()
    for sid, dur_s, atype in dur_rows:
        rate = DURATION_TSS_PER_HOUR.get(atype or "")
        if not rate:
            continue
        # Cap at 5 hours to avoid absurd TSS for a 314-min hiking multi-day
        hours = min(dur_s / 3600, 5.0)
        tss = round(hours * rate, 1)
        if tss > 0:
            cur.execute("""
                UPDATE activities SET tss = %s,
                    raw_data = COALESCE(raw_data, '{}'::jsonb)
                              || '{"tss_method":"duration_estimate"}'::jsonb
                WHERE user_id = %s AND source_id = %s AND tss IS NULL
            """, (tss, user_id, sid))
            duration_updated += 1

    cur.close()
    conn.close()
    return {
        "hr_corrected": hr_corrected,
        "power_updated": power_updated,
        "hr_updated": hr_updated,
        "duration_updated": duration_updated,
    }


def _refresh_ifit_library_cache() -> None:
    """Refresh the iFit workout library cache if it's stale (>7 days) or missing.

    The cache is used by search_ifit_library and recommend_ifit_workout tools."""
    import asyncio
    from pathlib import Path

    cache_path = Path(__file__).resolve().parent.parent / ".ifit_capture" / "library_workouts.json"

    try:
        from scripts.ifit_auth import get_auth_headers
        headers = get_auth_headers()
    except Exception:
        if cache_path.exists():
            print("  iFit token unavailable but cache exists — skipping refresh")
        else:
            print("  iFit token unavailable and no cache — search_ifit_library won't work")
        return

    from scripts.ifit_list_series import (
        fetch_all_trainers,
        fetch_all_workouts,
        _cache_fresh,
        WORKOUTS_CACHE,
    )

    if _cache_fresh(WORKOUTS_CACHE):
        print("  Cache is fresh (< 7 days old) — skipping")
        return

    print("  Fetching trainers...")
    trainers = fetch_all_trainers(headers)
    print(f"  {len(trainers)} trainers loaded")

    print("  Fetching library workouts (this may take a minute)...")
    workouts = asyncio.run(fetch_all_workouts(headers))
    print(f"  {len(workouts)} workouts cached")


def _sync_ifit_r2() -> None:
    """Upload library to R2, fetch a batch of transcripts, discover programs."""
    from scripts.r2_store import is_configured as r2_configured
    if not r2_configured():
        return

    print(f"\n{'='*60}")
    print("iFit R2 sync")
    print(f"{'='*60}")

    from scripts.ifit_r2_sync import (
        sync_library, sync_transcripts, sync_programs, migrate_exercise_cache,
    )

    try:
        result = migrate_exercise_cache()
        if result.get("uploaded"):
            print(f"  Migrated {result['uploaded']} exercise cache entries to R2")
    except Exception as e:
        print(f"  WARN: Exercise cache migration failed: {e}")

    try:
        lib_result = sync_library()
        for name, status in lib_result.items():
            if name != "skipped":
                print(f"  Library {name}: {status}")
    except Exception as e:
        print(f"  WARN: Library upload failed: {e}")

    try:
        result = sync_transcripts(batch_size=100)
        if not result.get("skipped"):
            synced = result.get("total_synced", 0)
            total = result.get("total", 0)
            uploaded = result.get("uploaded", 0)
            remaining = result.get("remaining", 0)
            print(f"  Transcripts: {synced}/{total} synced "
                  f"({uploaded} new this cycle, {remaining} remaining)")
    except Exception as e:
        print(f"  WARN: Transcript sync failed: {e}")

    try:
        result = sync_programs()
        if not result.get("skipped"):
            print(f"  Programs: {result.get('discovered', 0)} discovered, "
                  f"{result.get('newly_fetched', 0)} new")
    except Exception as e:
        print(f"  WARN: Program sync failed: {e}")


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

        # --- Garmin profile auto-populate (runs whenever any threshold is null) ---
        print(f"\n  [Garmin Profile]")
        try:
            result = sync_garmin_profile(slug)
            if result.get("skipped"):
                print(f"    SKIP: all key thresholds already set")
            elif "error" in result:
                print(f"    SKIP: {result['error']}")
            else:
                if result["written"]:
                    print(f"    Populated: {', '.join(f'{k}={v}' for k, v in result['written'].items())}")
                else:
                    print(f"    No new values found in Garmin")
                missing_count = len(result.get("still_missing", []))
                if missing_count:
                    print(f"    Still missing: {missing_count} field(s) (manual entry required)")
        except Exception as e:
            print(f"    ERROR: Garmin profile sync failed: {e}")
            traceback.print_exc()

        # --- Strength TSS backfill ---
        print(f"\n  [Strength TSS]")
        try:
            result = backfill_strength_tss(user_id, tz_name=tz_name, slug=slug)
            print(f"    Garmin updated: {result['updated']}, Hevy-only inserted: {result['inserted']}, hybrid(HR): {result['hybrid']}")
        except Exception as e:
            print(f"    ERROR: Strength TSS backfill failed: {e}")
            traceback.print_exc()
            errors += 1

        # --- General TSS backfill (power / HR / duration estimates) ---
        print(f"\n  [TSS Backfill]")
        try:
            result = backfill_missing_tss(user_id, slug=slug)
            print(f"    HR-corrected: {result['hr_corrected']}, Power-based: {result['power_updated']}, HR-based: {result['hr_updated']}, Duration-estimated: {result['duration_updated']}")
        except Exception as e:
            print(f"    ERROR: TSS backfill failed: {e}")
            traceback.print_exc()
            errors += 1

    # --- iFit library cache (shared across all users, refreshes every 7 days) ---
    print(f"\n{'='*60}")
    print("Refreshing iFit library cache")
    print(f"{'='*60}")
    try:
        _refresh_ifit_library_cache()
    except Exception as e:
        print(f"  WARN: iFit library refresh failed: {e}")

    # --- iFit R2 sync (transcript batches + library upload + programs) ---
    _sync_ifit_r2()

    if errors:
        print(f"\nSync completed with {errors} error(s)")
        return 1

    print(f"\nSync completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())

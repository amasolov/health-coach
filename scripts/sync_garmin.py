#!/usr/bin/env python3
"""
Sync Garmin Connect data into TimescaleDB.

Pulls activities, body composition, and daily vitals for a single user.
Uses incremental sync: only fetches data newer than the last stored record.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg2
from garminconnect import Garmin

from scripts.garmin_auth import try_cached_login
from scripts.tz import load_user_tz, ts_to_utc, user_today

# ---------------------------------------------------------------------------
# TSS estimation when Garmin doesn't provide it
# ---------------------------------------------------------------------------

# hrTSS = (duration_hours * HR_IF^2) * 100
# HR_IF = (avgHR - restHR) / (ltHR - restHR)
# Fallback when no power/LTHR: use training effect as rough proxy

DEFAULT_LTHR = 155  # typical recreational runner LTHR; override via athlete.yaml lthr_run


_CYCLING_TYPES = frozenset({
    "cycling", "virtual_ride", "indoor_cycling", "road_biking",
    "gravel_cycling", "mountain_biking",
})


def _estimate_tss(
    duration_s: float,
    avg_hr: float | None,
    avg_power: float | None,
    norm_power: float | None,
    ftp: float | None,
    lthr: float | None,
    resting_hr: float | None,
    ae_effect: float | None,
    activity_type: str = "",
) -> float | None:
    """Estimate TSS from available data, preferring power > HR > training effect.

    Power-based TSS is restricted to cycling (FTP is cycling-specific).
    Running power from HRM Pro is on a different scale and must not be
    divided by cycling FTP.

    HR-based TSS uses the Training Peaks hrTSS formula:
        IF = avgHR / LTHR
        TSS = hours × IF² × 100
    This matches Training Peaks exactly when the correct LTHR is configured
    in athlete.yaml (lthr_run for running, lthr_bike for cycling).
    """
    if not duration_s or duration_s <= 0:
        return None

    hours = duration_s / 3600

    # Power-based TSS — only valid for cycling where FTP is meaningful
    is_cycling = activity_type in _CYCLING_TYPES
    if is_cycling and ftp and ftp > 0:
        power = norm_power or avg_power
        if power and power > 0:
            intensity = power / ftp
            return round(hours * intensity * intensity * 100, 1)

    # HR-based TSS (Training Peaks hrTSS formula: IF = avgHR / LTHR)
    if avg_hr and avg_hr > 0:
        lt = lthr or DEFAULT_LTHR
        if lt > 0:
            hr_if = avg_hr / lt
            hr_if = max(0, min(hr_if, 1.5))  # clamp at 1.5x LTHR
            return round(hours * hr_if * hr_if * 100, 1)

    # Training Effect proxy (very rough)
    if ae_effect and ae_effect > 0:
        return round(ae_effect * hours * 15, 1)

    return None


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------

def _ts_to_datetime(ts_ms: int | None) -> datetime | None:
    if ts_ms is None:
        return None
    return ts_to_utc(ts_ms)


def _parse_garmin_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _speed_to_pace(speed_mps: float | None) -> float | None:
    """Convert m/s to seconds per km."""
    if not speed_mps or speed_mps <= 0:
        return None
    return round(1000 / speed_mps, 1)


def _extract_activity(act: dict, user_thresholds: dict) -> dict:
    """Extract a flat dict from a Garmin activity summary."""
    sport = act.get("activityType", {}).get("typeKey", "unknown")
    start_gmt = act.get("startTimeGMT", "")
    start_dt = _parse_garmin_datetime(start_gmt)
    if not start_dt:
        start_dt = _ts_to_datetime(act.get("beginTimestamp"))

    duration = act.get("duration") or act.get("elapsedDuration") or 0
    avg_hr = act.get("averageHR")
    max_hr = act.get("maxHR")
    avg_power = act.get("avgPower") or act.get("averagePower")
    max_power = act.get("maxPower")
    norm_power = act.get("normPower") or act.get("normalizedPower")
    avg_speed = act.get("averageSpeed")
    ae = act.get("aerobicTrainingEffect")
    an = act.get("anaerobicTrainingEffect")

    tss_garmin = act.get("trainingStressScore")
    if_garmin = act.get("intensityFactor")

    ftp = user_thresholds.get("ftp")
    lthr = user_thresholds.get("lthr_run") if sport not in _CYCLING_TYPES else user_thresholds.get("lthr_bike")

    tss = tss_garmin
    intensity_factor = if_garmin
    if not tss:
        tss = _estimate_tss(duration, avg_hr, avg_power, norm_power,
                            ftp, lthr, None, ae,
                            activity_type=sport)
    if not intensity_factor and norm_power and ftp and ftp > 0 and sport in _CYCLING_TYPES:
        intensity_factor = round(norm_power / ftp, 3)

    cadence = (act.get("averageRunningCadenceInStepsPerMinute")
               or act.get("averageBikingCadenceInRevPerMinute"))

    is_run = any(k in sport for k in ("running", "trail", "treadmill"))
    pace = _speed_to_pace(avg_speed) if is_run else None

    return {
        "time": start_dt,
        "source": "garmin",
        "source_id": str(act.get("activityId", "")),
        "activity_type": sport,
        "title": act.get("activityName", ""),
        "duration_s": int(duration) if duration else None,
        "distance_m": act.get("distance"),
        "elevation_gain_m": act.get("elevationGain"),
        "avg_hr": int(avg_hr) if avg_hr else None,
        "max_hr": int(max_hr) if max_hr else None,
        "avg_power": int(avg_power) if avg_power else None,
        "max_power": int(max_power) if max_power else None,
        "normalized_power": int(norm_power) if norm_power else None,
        "tss": tss,
        "intensity_factor": intensity_factor,
        "avg_cadence": int(cadence) if cadence else None,
        "avg_pace_sec_km": pace,
        "calories": int(act.get("calories", 0)) if act.get("calories") else None,
        "training_effect_ae": round(ae, 1) if ae else None,
        "training_effect_an": round(an, 1) if an else None,
    }


def _extract_body_comp(entry: dict) -> dict:
    """Extract a flat dict from a Garmin body composition weight entry."""
    cal_date = entry.get("calendarDate")
    ts_gmt = entry.get("timestampGMT")
    dt = _ts_to_datetime(ts_gmt) if ts_gmt else _parse_garmin_datetime(cal_date)

    # Garmin stores weight in grams, muscle/bone mass in grams
    weight = entry.get("weight")
    muscle = entry.get("muscleMass")
    bone = entry.get("boneMass")

    return {
        "time": dt,
        "weight_kg": round(weight / 1000, 2) if weight else None,
        "body_fat_pct": entry.get("bodyFat"),
        "muscle_mass_kg": round(muscle / 1000, 2) if muscle else None,
        "bone_mass_kg": round(bone / 1000, 2) if bone else None,
        "bmi": round(entry.get("bmi"), 1) if entry.get("bmi") else None,
        "body_water_pct": entry.get("bodyWater"),
        "source": "garmin_scale",
    }


def _extract_vitals(
    day: str,
    stats: dict,
    sleep: dict | None,
    hrv: dict | None,
    resp: dict | None,
    bp: dict | None,
) -> dict:
    """Combine daily stats, sleep, HRV, respiration, BP into one vitals row."""
    dt = _parse_garmin_datetime(day)
    if not dt:
        dt = datetime.strptime(day, "%Y-%m-%d")

    sleep_dto = sleep.get("dailySleepDTO", {}) if sleep else {}
    sleep_scores = sleep_dto.get("sleepScores", {})
    overall_sleep = sleep_scores.get("overall", {})

    hrv_summary = hrv.get("hrvSummary", hrv) if hrv else {}

    bp_measurements = bp.get("measurementSummaries", []) if bp else []
    bp_latest = bp_measurements[0] if bp_measurements else {}

    return {
        "time": dt,
        "resting_hr": stats.get("restingHeartRate"),
        "hrv_ms": (
            hrv_summary.get("lastNightAvg")
            or hrv_summary.get("weeklyAvg")
            or (sleep.get("avgOvernightHrv") if sleep else None)
        ),
        "bp_systolic": bp_latest.get("systolic"),
        "bp_diastolic": bp_latest.get("diastolic"),
        "bp_pulse": bp_latest.get("pulse"),
        "sleep_score": overall_sleep.get("value"),
        "sleep_duration_min": (
            round(sleep_dto.get("sleepTimeSeconds", 0) / 60)
            if sleep_dto.get("sleepTimeSeconds") else None
        ),
        "stress_avg": stats.get("averageStressLevel"),
        "body_battery_high": stats.get("bodyBatteryHighestValue"),
        "body_battery_low": stats.get("bodyBatteryLowestValue"),
        "spo2_avg": stats.get("averageSpo2"),
        "respiration_avg": (
            resp.get("avgWakingRespirationValue")
            if resp else None
        ),
        "source": "garmin",
    }


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "health"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def _get_last_activity_time(cur, user_id: int) -> datetime | None:
    cur.execute(
        "SELECT MAX(time) FROM activities WHERE user_id = %s AND source = 'garmin'",
        (user_id,),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def _get_last_body_comp_time(cur, user_id: int) -> datetime | None:
    cur.execute(
        "SELECT MAX(time) FROM body_composition WHERE user_id = %s",
        (user_id,),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def _get_last_vitals_time(cur, user_id: int) -> datetime | None:
    cur.execute(
        "SELECT MAX(time) FROM vitals WHERE user_id = %s AND source = 'garmin'",
        (user_id,),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def _activity_exists(cur, user_id: int, source_id: str) -> bool:
    cur.execute(
        "SELECT 1 FROM activities WHERE user_id = %s AND source = 'garmin' AND source_id = %s LIMIT 1",
        (user_id, source_id),
    )
    return cur.fetchone() is not None


def _update_activity_tss(cur, user_id: int, source_id: str, tss: float | None, intensity_factor: float | None) -> None:
    """Update TSS and IF for an existing activity (used when re-syncing with corrected formula)."""
    cur.execute(
        """UPDATE activities SET tss = %s, intensity_factor = %s
           WHERE user_id = %s AND source = 'garmin' AND source_id = %s""",
        (tss, intensity_factor, user_id, source_id),
    )


def _insert_activity(cur, user_id: int, data: dict) -> None:
    cur.execute("""
        INSERT INTO activities (
            time, user_id, source, source_id, activity_type, title,
            duration_s, distance_m, elevation_gain_m,
            avg_hr, max_hr, avg_power, max_power, normalized_power,
            tss, intensity_factor, avg_cadence, avg_pace_sec_km,
            calories, training_effect_ae, training_effect_an
        ) VALUES (
            %(time)s, %(user_id)s, %(source)s, %(source_id)s, %(activity_type)s, %(title)s,
            %(duration_s)s, %(distance_m)s, %(elevation_gain_m)s,
            %(avg_hr)s, %(max_hr)s, %(avg_power)s, %(max_power)s, %(normalized_power)s,
            %(tss)s, %(intensity_factor)s, %(avg_cadence)s, %(avg_pace_sec_km)s,
            %(calories)s, %(training_effect_ae)s, %(training_effect_an)s
        )
    """, {**data, "user_id": user_id})


def _body_comp_exists(cur, user_id: int, dt: datetime) -> bool:
    cur.execute(
        "SELECT 1 FROM body_composition WHERE user_id = %s AND time = %s LIMIT 1",
        (user_id, dt),
    )
    return cur.fetchone() is not None


def _insert_body_comp(cur, user_id: int, data: dict) -> None:
    cur.execute("""
        INSERT INTO body_composition (
            time, user_id, weight_kg, body_fat_pct, muscle_mass_kg,
            bone_mass_kg, bmi, body_water_pct, source
        ) VALUES (
            %(time)s, %(user_id)s, %(weight_kg)s, %(body_fat_pct)s, %(muscle_mass_kg)s,
            %(bone_mass_kg)s, %(bmi)s, %(body_water_pct)s, %(source)s
        )
    """, {**data, "user_id": user_id})


def _vitals_exist(cur, user_id: int, dt: datetime) -> bool:
    cur.execute(
        "SELECT 1 FROM vitals WHERE user_id = %s AND time = %s LIMIT 1",
        (user_id, dt),
    )
    return cur.fetchone() is not None


def _upsert_vitals(cur, user_id: int, data: dict) -> None:
    cur.execute("""
        INSERT INTO vitals (
            time, user_id, resting_hr, hrv_ms, bp_systolic, bp_diastolic,
            bp_pulse, sleep_score, sleep_duration_min, stress_avg,
            body_battery_high, body_battery_low, spo2_avg,
            respiration_avg, source
        ) VALUES (
            %(time)s, %(user_id)s, %(resting_hr)s, %(hrv_ms)s, %(bp_systolic)s,
            %(bp_diastolic)s, %(bp_pulse)s, %(sleep_score)s, %(sleep_duration_min)s,
            %(stress_avg)s, %(body_battery_high)s, %(body_battery_low)s,
            %(spo2_avg)s, %(respiration_avg)s, %(source)s
        )
    """, {**data, "user_id": user_id})


# ---------------------------------------------------------------------------
# User thresholds from athlete.yaml
# ---------------------------------------------------------------------------

def _load_user_thresholds(slug: str) -> dict:
    """Load threshold values used for TSS estimation."""
    from pathlib import Path
    import yaml

    path = Path(__file__).resolve().parent.parent / "config" / "athlete.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    user = data.get("users", {}).get(slug, {})
    thresholds = user.get("thresholds", {})
    body = user.get("body", {})
    return {
        "ftp": thresholds.get("cycling", {}).get("ftp"),
        "lthr_run": thresholds.get("heart_rate", {}).get("lthr_run"),
        "resting_hr": thresholds.get("heart_rate", {}).get("resting_hr"),
        "weight_kg": body.get("weight_kg"),
    }


# ---------------------------------------------------------------------------
# Main sync functions
# ---------------------------------------------------------------------------

def sync_activities(
    client: Garmin,
    cur,
    user_id: int,
    slug: str,
    lookback_days: int = 180,
    tz=None,
    full_sync: bool = False,
) -> dict:
    """Sync activities from Garmin Connect. Returns dict with found/inserted counts."""
    today = user_today(tz)
    last = _get_last_activity_time(cur, user_id)

    if full_sync:
        start = "2000-01-01"
    elif last:
        start = (last.date() - timedelta(days=1)).isoformat()
    else:
        start = (today - timedelta(days=lookback_days)).isoformat()

    end = (today + timedelta(days=1)).isoformat()
    thresholds = _load_user_thresholds(slug)

    print(f"    Fetching activities from {start} to {end}{'  [FULL SYNC]' if full_sync else ''}...")
    activities = client.get_activities_by_date(start, end)
    found = len(activities)
    print(f"    Found {found} activities from Garmin")

    inserted = 0
    updated = 0
    for act in activities:
        source_id = str(act.get("activityId", ""))
        if not source_id:
            continue

        data = _extract_activity(act, thresholds)
        if not data["time"]:
            continue

        if _activity_exists(cur, user_id, source_id):
            # Re-sync TSS/IF for activities where Garmin didn't provide a native
            # value — this corrects previously estimated values when thresholds
            # or the formula change.
            if act.get("trainingStressScore") is None:
                _update_activity_tss(cur, user_id, source_id, data["tss"], data["intensity_factor"])
                updated += 1
        else:
            _insert_activity(cur, user_id, data)
            inserted += 1

    if updated:
        print(f"    Updated TSS for {updated} existing activities")
    return {"found": found, "inserted": inserted, "updated": updated}


def sync_body_composition(
    client: Garmin,
    cur,
    user_id: int,
    lookback_days: int = 180,
    tz=None,
    full_sync: bool = False,
) -> dict:
    """Sync body composition data. Returns dict with found/inserted counts."""
    today = user_today(tz)
    last = _get_last_body_comp_time(cur, user_id)

    if full_sync:
        start = "2000-01-01"
    elif last:
        start = (last.date() - timedelta(days=1)).isoformat()
    else:
        start = (today - timedelta(days=lookback_days)).isoformat()

    end = today.isoformat()

    print(f"    Fetching body composition from {start} to {end}{'  [FULL SYNC]' if full_sync else ''}...")
    try:
        bc = client.get_body_composition(start, end)
    except Exception as e:
        print(f"    WARN: Body composition fetch failed: {e}")
        return {"found": 0, "inserted": 0}

    entries = bc.get("dateWeightList", [])
    found = len(entries)
    print(f"    Found {found} weight entries from Garmin")

    inserted = 0
    for entry in entries:
        data = _extract_body_comp(entry)
        if not data["time"]:
            continue
        if _body_comp_exists(cur, user_id, data["time"]):
            continue
        _insert_body_comp(cur, user_id, data)
        inserted += 1

    return {"found": found, "inserted": inserted}


def sync_vitals(
    client: Garmin,
    cur,
    user_id: int,
    lookback_days: int = 30,
    tz=None,
    full_sync: bool = False,
) -> dict:
    """Sync daily vitals (stats, sleep, HRV, respiration, BP). Returns dict with found/inserted."""
    today = user_today(tz)
    last = _get_last_vitals_time(cur, user_id)

    if full_sync:
        start_date = date(2000, 1, 1)
    elif last:
        start_date = last.date()
    else:
        start_date = today - timedelta(days=lookback_days)

    end_date = today
    days_checked = 0
    inserted = 0
    current = start_date

    print(f"    Fetching vitals from {start_date} to {end_date}{'  [FULL SYNC]' if full_sync else ''}...")
    while current <= end_date:
        day_str = current.isoformat()

        try:
            stats = client.get_stats(day_str) or {}
        except Exception:
            stats = {}

        # Skip days with no data at all
        if not stats.get("restingHeartRate") and not stats.get("averageStressLevel"):
            current += timedelta(days=1)
            continue

        days_checked += 1
        sleep = None
        hrv = None
        resp = None
        bp = None

        try:
            sleep = client.get_sleep_data(day_str)
        except Exception:
            pass
        try:
            hrv = client.get_hrv_data(day_str)
        except Exception:
            pass
        try:
            resp = client.get_respiration_data(day_str)
        except Exception:
            pass
        try:
            bp_data = client.get_blood_pressure(day_str)
            if bp_data and bp_data.get("measurementSummaries"):
                bp = bp_data
        except Exception:
            pass

        data = _extract_vitals(day_str, stats, sleep, hrv, resp, bp)
        if data["time"]:
            if _vitals_exist(cur, user_id, data["time"]):
                current += timedelta(days=1)
                continue
            _upsert_vitals(cur, user_id, data)
            inserted += 1

        current += timedelta(days=1)

    return {"found": days_checked, "inserted": inserted}


def sync_user(
    slug: str,
    user_id: int,
    initial_lookback_days: int = 180,
    full_sync: bool = False,
) -> dict:
    """Full sync for a single user. Returns summary counts."""
    client = try_cached_login(slug)
    if not client:
        return {"error": f"Garmin not authenticated for {slug}"}

    tz = load_user_tz(slug)
    conn = _get_conn()
    conn.autocommit = True
    cur = conn.cursor()

    try:
        act = sync_activities(client, cur, user_id, slug, initial_lookback_days, tz=tz, full_sync=full_sync)
        bc = sync_body_composition(client, cur, user_id, initial_lookback_days, tz=tz, full_sync=full_sync)
        vit = sync_vitals(client, cur, user_id, tz=tz, full_sync=full_sync)

        return {
            "activities_found": act["found"],
            "activities_inserted": act["inserted"],
            "body_comp_found": bc["found"],
            "body_comp_inserted": bc["inserted"],
            "vitals_found": vit["found"],
            "vitals_inserted": vit["inserted"],
        }
    finally:
        cur.close()
        conn.close()

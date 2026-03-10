"""
Fetch athlete profile data from Garmin Connect.

Pulls user profile, body composition, heart rate, VO2max, lactate
threshold, cycling FTP, and max HR from recent activities. Returns a
structured dict that maps directly to athlete config fields, plus
metadata about where each value came from and hints for missing fields.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from scripts.tz import user_today

from garminconnect import Garmin

logger = logging.getLogger(__name__)

# Hints returned for fields that couldn't be auto-populated
FIELD_HINTS: dict[str, str] = {
    "max_hr": (
        "Use the highest HR you've seen in a hard race or all-out effort. "
        "Fallback formula: 220 minus your age (rough estimate)."
    ),
    "resting_hr": (
        "Wear your watch overnight for a few nights. Garmin will calculate "
        "this automatically once it has enough data."
    ),
    "lthr_run": (
        "Do a 30-minute all-out solo run once warmed up. Your average HR for "
        "the last 20 minutes is a good LTHR estimate."
    ),
    "lthr_bike": (
        "Do a 30-minute solo time trial on the bike. Average HR of the last "
        "20 minutes. Skip if you don't train cycling seriously."
    ),
    "critical_power": (
        "Stryd calculates this automatically from your running data -- check "
        "the Stryd app under the Power tab. Skip if you don't use Stryd yet."
    ),
    "rftp_garmin": (
        "Garmin auto-detects running FTP from your HRM Pro data. Run "
        "outdoors at varied intensities with the chest strap for Garmin "
        "to estimate this. Note: Garmin running power is on a different "
        "scale than Stryd/TrainingPeaks (~32-38%% higher)."
    ),
    "threshold_pace": (
        "The pace you could sustain for roughly 60 minutes all-out. Garmin "
        "race predictions can help estimate this."
    ),
    "vo2max_garmin": (
        "Run outdoors with GPS and chest HR strap a few times. Garmin "
        "estimates VO2max automatically after enough data."
    ),
    "ftp": (
        "Do a 20-minute FTP test on Zwift or your Wahoo Kickr, then multiply "
        "average power by 0.95. Skip if not cycling regularly."
    ),
    "ftp_wkg": "Auto-calculated from FTP and weight once both are available.",
    "weight_kg": (
        "Step on your Garmin smart scale, or enter an estimate. The scale "
        "will sync automatically for future updates."
    ),
    "body_fat_pct": (
        "Your Garmin smart scale measures this. If unavailable, most gyms "
        "offer body composition scans."
    ),
    "lactate": (
        "Requires a lab test or DIY with a portable lactate monitor. Leave "
        "blank if untested -- zones will use LTHR instead."
    ),
    "training_status": (
        "Rough self-assessment: how many hours per week do you train? "
        "What's your longest recent run/ride? What's your current focus?"
    ),
}


def _safe_call(fn, *args, **kwargs) -> Any:
    """Call a Garmin API method, returning None on any error."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.debug("Garmin API call %s failed: %s", fn.__name__, e)
        return None


def _extract_user_profile(client: Garmin) -> dict:
    """Pull basic profile: dob, sex, height."""
    result = {}
    sources = {}

    data = _safe_call(client.get_user_profile)
    if not data:
        return result, sources

    settings = data if isinstance(data, dict) else {}
    user_data = settings.get("userData", settings)

    if user_data.get("birthDate"):
        result["date_of_birth"] = user_data["birthDate"]
        sources["date_of_birth"] = "garmin_profile"

    gender = user_data.get("gender") or settings.get("gender")
    if gender:
        result["sex"] = gender.lower()
        sources["sex"] = "garmin_profile"

    height = user_data.get("height") or settings.get("height")
    if height:
        h = float(height)
        if h > 500:
            h = h / 10  # millimeters -> cm
        elif h < 3:
            h = h * 100  # meters -> cm
        result["height_cm"] = round(h, 1)
        sources["height_cm"] = "garmin_profile"

    return result, sources


def _extract_body_composition(client: Garmin, today: date | None = None) -> tuple[dict, dict]:
    """Pull latest body composition from Garmin scale."""
    result = {}
    sources = {}

    _today = today or user_today()
    today_s = _today.isoformat()
    start = (_today - timedelta(days=30)).isoformat()
    data = _safe_call(client.get_body_composition, start, today_s)
    if not data:
        return result, sources

    daily = data.get("dateWeightList") or data.get("dailyWeightSummaries") or []
    if not daily:
        weight_data = data
    else:
        weight_data = daily[-1] if daily else data

    for garmin_key, our_key in [
        ("weight", "weight_kg"),
        ("bodyFat", "body_fat_pct"),
        ("muscleMass", "muscle_mass_kg"),
        ("boneMass", "bone_mass_kg"),
        ("bmi", "bmi"),
    ]:
        val = weight_data.get(garmin_key)
        if val is not None and val > 0:
            if garmin_key == "weight":
                val = round(val / 1000, 1)
            elif garmin_key in ("muscleMass", "boneMass"):
                val = round(val / 1000, 1)
            else:
                val = round(float(val), 1)
            result[our_key] = val
            sources[our_key] = "garmin_scale"

    if result:
        cal_date = weight_data.get("calendarDate") or weight_data.get("date")
        if cal_date:
            result["measured_date"] = str(cal_date)[:10]

    return result, sources


def _extract_heart_rate(client: Garmin, today: date | None = None) -> tuple[dict, dict]:
    """Pull resting HR from today's heart rate data."""
    result = {}
    sources = {}

    today = (today or user_today()).isoformat()
    data = _safe_call(client.get_heart_rates, today)
    if not data:
        return result, sources

    rhr = data.get("restingHeartRate")
    if rhr and rhr > 0:
        result["resting_hr"] = int(rhr)
        sources["resting_hr"] = "garmin_api"

    return result, sources


def _extract_max_metrics(client: Garmin, today: date | None = None) -> tuple[dict, dict]:
    """Pull VO2max from max metrics endpoint."""
    result = {}
    sources = {}

    today = (today or user_today()).isoformat()
    data = _safe_call(client.get_max_metrics, today)
    if not data:
        return result, sources

    entries = data if isinstance(data, list) else data.get("maxMetricsData", [data])
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        generic = entry.get("generic", entry)
        vo2max = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
        if vo2max and vo2max > 0:
            result["vo2max_garmin"] = round(float(vo2max), 1)
            sources["vo2max_garmin"] = "garmin_api"
            break

    return result, sources


def _extract_lactate_threshold(client: Garmin) -> tuple[dict, dict]:
    """Pull lactate threshold HR, pace, and running power.

    The Garmin API may return a nested structure:
      {"speed_and_heart_rate": {heartRate, speed, ...}, "power": {functionalThresholdPower, ...}}
    or a flat dict with the fields at the top level.
    """
    result = {}
    sources = {}

    data = _safe_call(client.get_lactate_threshold, latest=True)
    if not data:
        return result, sources

    if isinstance(data, dict):
        # HR and speed may be nested under "speed_and_heart_rate"
        hr_data = data.get("speed_and_heart_rate", data)
        if not isinstance(hr_data, dict):
            hr_data = data

        lt_hr = (
            hr_data.get("heartRate")
            or hr_data.get("lactateThresholdHeartRate")
            or data.get("lactateThresholdHeartRate")
            or data.get("heartRate")
        )
        if lt_hr and lt_hr > 0:
            result["lthr_run"] = int(lt_hr)
            sources["lthr_run"] = "garmin_lactate_threshold"

        speed = (
            hr_data.get("speed")
            or hr_data.get("runningLactateThresholdSpeed")
            or data.get("runningLactateThresholdSpeed")
            or data.get("speed")
        )
        if speed and speed > 0:
            pace_min_km = (1000 / float(speed)) / 60
            result["threshold_pace"] = round(pace_min_km, 2)
            sources["threshold_pace"] = "garmin_lactate_threshold"

        # Running power threshold — Garmin uses "functionalThresholdPower"
        power = data.get("power")
        if isinstance(power, dict):
            rftp = (
                power.get("functionalThresholdPower")
                or power.get("criticalPower")
                or power.get("power")
            )
            if rftp and rftp > 0:
                result["rftp_garmin"] = int(rftp)
                sources["rftp_garmin"] = "garmin_lactate_threshold"

    return result, sources


def _extract_cycling_ftp(client: Garmin, weight_kg: float | None) -> tuple[dict, dict]:
    """Pull cycling FTP and compute W/kg if weight is known."""
    result = {}
    sources = {}

    data = _safe_call(client.get_cycling_ftp)
    if not data:
        return result, sources

    ftp_val = None
    if isinstance(data, dict):
        ftp_val = data.get("functionalThresholdPower") or data.get("ftp")
    elif isinstance(data, list) and data:
        ftp_val = data[0].get("functionalThresholdPower") or data[0].get("ftp")

    if ftp_val and ftp_val > 0:
        result["ftp"] = int(ftp_val)
        sources["ftp"] = "garmin_api"
        if weight_kg and weight_kg > 0:
            result["ftp_wkg"] = round(ftp_val / weight_kg, 2)
            sources["ftp_wkg"] = "calculated"

    return result, sources


def _extract_max_hr_from_activities(client: Garmin, today: date | None = None) -> tuple[dict, dict]:
    """Scan recent activities for the highest recorded max HR."""
    result = {}
    sources = {}

    _today = today or user_today()
    end = _today.isoformat()
    start = (_today - timedelta(days=90)).isoformat()
    activities = _safe_call(client.get_activities_by_date, start, end)
    if not activities:
        return result, sources

    highest = 0
    for act in activities:
        mhr = act.get("maxHR") or act.get("maxHeartRate") or 0
        if mhr > highest:
            highest = mhr

    if highest > 100:
        result["max_hr"] = int(highest)
        sources["max_hr"] = "highest_in_90d_activities"

    return result, sources


def _extract_race_predictions(client: Garmin) -> tuple[dict, dict]:
    """Pull race predictions to estimate threshold pace if not available."""
    result = {}
    sources = {}

    data = _safe_call(client.get_race_predictions)
    if not data:
        return result, sources

    predictions = data if isinstance(data, list) else [data]
    for pred in predictions:
        if not isinstance(pred, dict):
            continue
        half = pred.get("halfMarathon") or pred.get("half_marathon")
        ten_k = pred.get("tenK") or pred.get("10k")

        if half and isinstance(half, dict):
            secs = half.get("time") or half.get("predictedTime")
            if secs and secs > 0:
                pace = (float(secs) / 21.0975) / 60
                result["threshold_pace_est"] = round(pace, 2)
                sources["threshold_pace_est"] = "garmin_race_prediction_half"
                break

        if ten_k and isinstance(ten_k, dict):
            secs = ten_k.get("time") or ten_k.get("predictedTime")
            if secs and secs > 0:
                pace = (float(secs) / 10.0) / 60
                result["threshold_pace_est"] = round(pace * 1.05, 2)
                sources["threshold_pace_est"] = "garmin_race_prediction_10k"
                break

    return result, sources


def fetch_garmin_profile(slug: str, client: Garmin) -> dict:
    """
    Fetch all available athlete profile data from Garmin Connect.

    Returns a dict with:
      - "fetched": values organized by athlete config section
      - "sources": where each value came from
      - "missing": fields still null with hints for how to obtain them
    """
    from scripts.tz import load_user_tz
    today = user_today(load_user_tz(slug))
    all_sources = {}

    # Profile
    profile, src = _extract_user_profile(client)
    all_sources.update(src)

    # Body composition
    body, src = _extract_body_composition(client, today)
    all_sources.update(src)

    # Heart rate
    hr, src = _extract_heart_rate(client, today)
    all_sources.update(src)

    # Max HR from activities
    max_hr, src = _extract_max_hr_from_activities(client, today)
    all_sources.update(src)

    # VO2max
    metrics, src = _extract_max_metrics(client, today)
    all_sources.update(src)

    # Lactate threshold
    lt, src = _extract_lactate_threshold(client)
    all_sources.update(src)

    # Cycling FTP
    ftp, src = _extract_cycling_ftp(client, body.get("weight_kg"))
    all_sources.update(src)

    # Race predictions (fallback for threshold pace)
    race, src = _extract_race_predictions(client)
    all_sources.update(src)

    # Assemble the fetched data
    fetched = {
        "profile": profile,
        "thresholds": {
            "heart_rate": {**hr, **max_hr},
            "running": {**metrics, **{k: v for k, v in lt.items() if k in ("threshold_pace", "critical_power", "rftp_garmin", "vo2max_garmin")}},
            "cycling": ftp,
        },
        "body": body,
    }

    if lt.get("lthr_run"):
        fetched["thresholds"]["heart_rate"]["lthr_run"] = lt["lthr_run"]
    if lt.get("threshold_pace"):
        fetched["thresholds"]["running"]["threshold_pace"] = lt["threshold_pace"]
    elif race.get("threshold_pace_est"):
        fetched["thresholds"]["running"]["threshold_pace"] = race["threshold_pace_est"]
        all_sources["threshold_pace"] = all_sources.pop("threshold_pace_est", "estimated")

    # Build missing fields with hints
    expected_fields = {
        "profile": ["date_of_birth", "sex", "height_cm"],
        "thresholds.heart_rate": ["max_hr", "resting_hr", "lthr_run", "lthr_bike"],
        "thresholds.running": ["critical_power", "rftp_garmin", "threshold_pace", "vo2max_garmin"],
        "thresholds.cycling": ["ftp"],
        "body": ["weight_kg", "body_fat_pct"],
    }

    missing = []
    for section, fields in expected_fields.items():
        parts = section.split(".")
        data = fetched
        for p in parts:
            data = data.get(p, {})
        for field in fields:
            val = data.get(field) if isinstance(data, dict) else None
            if val is None:
                hint = FIELD_HINTS.get(field, "No specific guidance available.")
                missing.append({"field": f"{section}.{field}", "hint": hint})

    return {
        "fetched": fetched,
        "sources": all_sources,
        "missing": missing,
    }


def merge_into_athlete_profile(slug: str, fetched: dict) -> dict:
    """Merge fetched values into the athlete config stored in the DB.

    Only fills null fields -- never overwrites existing values.
    Returns a dict of what was actually written.
    """
    from scripts import athlete_store

    user = athlete_store.load(slug) or {}
    written = {}

    prof = user.setdefault("profile", {})
    for k, v in fetched.get("profile", {}).items():
        if prof.get(k) is None and v is not None:
            prof[k] = v
            written[f"profile.{k}"] = v

    thresh = user.setdefault("thresholds", {})
    for sub in ("heart_rate", "running", "cycling"):
        section = thresh.setdefault(sub, {})
        for k, v in fetched.get("thresholds", {}).get(sub, {}).items():
            if section.get(k) is None and v is not None:
                section[k] = v
                written[f"thresholds.{sub}.{k}"] = v

    body = user.setdefault("body", {})
    for k, v in fetched.get("body", {}).items():
        if body.get(k) is None and v is not None:
            body[k] = v
            written[f"body.{k}"] = v

    athlete_store.save(slug, user)
    return written


_THRESHOLD_FIELD_MAP = {
    "lthr_run": ("thresholds", "heart_rate"),
    "lthr_bike": ("thresholds", "heart_rate"),
    "max_hr": ("thresholds", "heart_rate"),
    "resting_hr": ("thresholds", "heart_rate"),
    "critical_power": ("thresholds", "running"),
    "rftp_garmin": ("thresholds", "running"),
    "threshold_pace": ("thresholds", "running"),
    "vo2max_garmin": ("thresholds", "running"),
    "ftp": ("thresholds", "cycling"),
    "ftp_wkg": ("thresholds", "cycling"),
}

# Significance thresholds for logging advisories when Garmin differs from lab
_SIGNIFICANCE_THRESHOLDS = {
    "lthr_run": 5,       # bpm
    "lthr_bike": 5,      # bpm
    "max_hr": 3,         # bpm
    "resting_hr": 5,     # bpm
    "rftp_garmin": 10,   # watts
    "critical_power": 10,  # watts
    "ftp": 10,           # watts
    "threshold_pace": 0.15,  # min/km
    "vo2max_garmin": 1.0,
}


def refresh_thresholds(
    slug: str,
    fetched: dict,
    fetched_sources: dict | None = None,
) -> dict:
    """Source-aware threshold refresh: always compare, auto-update non-lab fields.

    Returns a dict with:
      - "updated": dict of field_path -> new_value (fields that were changed)
      - "advisories": list of dicts describing significant differences from lab values
      - "garmin_latest": dict of field -> value (latest Garmin values stored in _sources)
    """
    from datetime import date as _date
    from scripts import athlete_store

    user = athlete_store.load(slug) or {}
    thresh = user.setdefault("thresholds", {})
    sources = thresh.setdefault("_sources", {})
    today = str(_date.today())

    updated = {}
    advisories = []
    garmin_latest = {}

    fetched_thresholds = fetched.get("thresholds", {})
    flat_fetched = {}
    for sub in ("heart_rate", "running", "cycling"):
        for k, v in fetched_thresholds.get(sub, {}).items():
            if v is not None:
                flat_fetched[k] = v

    for field, new_val in flat_fetched.items():
        field_map = _THRESHOLD_FIELD_MAP.get(field)
        if not field_map:
            continue

        section_key = field_map[1]
        section = thresh.setdefault(section_key, {})
        current_val = section.get(field)

        src = sources.setdefault(field, {})
        origin = src.get("origin", "")

        garmin_latest[field] = new_val
        src["garmin_latest"] = new_val
        src["garmin_date"] = today

        if current_val is None:
            section[field] = new_val
            src.setdefault("origin", "garmin")
            src["date"] = today
            if not src.get("origin"):
                src["origin"] = "garmin"
            updated[f"thresholds.{section_key}.{field}"] = new_val
            continue

        if origin == "lab":
            sig = _SIGNIFICANCE_THRESHOLDS.get(field, 0)
            if sig and abs(new_val - current_val) > sig:
                advisories.append({
                    "field": field,
                    "current": current_val,
                    "garmin": new_val,
                    "source": "lab",
                    "message": (
                        f"{field}: lab value {current_val} differs significantly "
                        f"from Garmin auto-detect {new_val} (Δ{new_val - current_val:+g})"
                    ),
                })
            continue

        if new_val != current_val:
            section[field] = new_val
            src["date"] = today
            if not origin:
                src["origin"] = "garmin"
            updated[f"thresholds.{section_key}.{field}"] = new_val

    body = user.setdefault("body", {})
    for k, v in fetched.get("body", {}).items():
        if body.get(k) is None and v is not None:
            body[k] = v
            updated[f"body.{k}"] = v

    prof = user.setdefault("profile", {})
    for k, v in fetched.get("profile", {}).items():
        if prof.get(k) is None and v is not None:
            prof[k] = v
            updated[f"profile.{k}"] = v

    athlete_store.save(slug, user)

    if updated:
        athlete_store.record_threshold_snapshot(slug, source="garmin")

    return {
        "updated": updated,
        "advisories": advisories,
        "garmin_latest": garmin_latest,
    }


def update_athlete_field(
    athlete_path: str, slug: str, field_path: str, value: Any
) -> None:
    """Set a single nested field in the athlete config.

    field_path is dot-separated, e.g. 'thresholds.heart_rate.max_hr'.
    The ``athlete_path`` parameter is kept for backward compatibility.
    """
    from scripts import athlete_store
    athlete_store.update_field(slug, field_path, value)

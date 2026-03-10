"""
Shared health-tracker tool logic.

Pure Python functions with no MCP dependency.  Both the MCP server and
the Chainlit chat app import from here so the business logic lives in
exactly one place.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg2
import yaml

from scripts.tz import DEFAULT_TZ_NAME, load_user_tz, user_today
from scripts import athlete_store

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
ATHLETE_PATH = ROOT / "config" / "athlete.yaml"
ZONES_PATH = ROOT / "config" / "zones.yaml"

# In-memory caches (survive across tool calls within the same process)
_ifit_library_cache: dict[str, Any] = {}  # {"workouts": [...], "trainers": {...}}
_workout_details_cache: dict[str, dict] = {}  # workout_id -> details dict
TEMPLATES_PATH = ROOT / "config" / "treadmill_templates.yaml"
EQUIPMENT_PATH = ROOT / "config" / "equipment.yaml"

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "health"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def resolve_user_id(slug: str) -> int | None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE slug = %s", (slug,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def query(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            rows.append({c: _serialise(v) for c, v in zip(cols, row)})
        return rows
    finally:
        conn.close()


def _serialise(v: Any) -> Any:
    """Make values JSON-friendly."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (float, int, str, bool, type(None))):
        return v
    return str(v)


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _save_yaml(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# Fitness / PMC
# ---------------------------------------------------------------------------


def get_fitness_summary(user_id: int) -> dict:
    """Get current fitness status: CTL (fitness), ATL (fatigue), TSB (form),
    ramp rate, and a plain-language interpretation.  Also includes 8-week
    projection of CTL."""
    rows = query(
        """SELECT time, tss, ctl, atl, tsb, ramp, source
           FROM training_load
           WHERE user_id = %s AND source = 'calculated'
           ORDER BY time DESC LIMIT 1""",
        (user_id,),
    )
    if not rows:
        return {"status": "No training load data yet. Sync activities first."}

    current = rows[0]
    tsb = float(current["tsb"] or 0)
    ramp = float(current["ramp"] or 0)

    if tsb > 25:
        form = "Transitioning / Detraining"
    elif tsb > 5:
        form = "Fresh — good for racing or key sessions"
    elif tsb > -10:
        form = "Neutral — maintaining"
    elif tsb > -30:
        form = "Fatigued — absorbing load"
    else:
        form = "Very fatigued — recovery needed"

    if ramp > 8:
        ramp_note = "Ramp rate high — injury risk, consider recovery"
    elif ramp > 5:
        ramp_note = "Solid fitness building"
    elif ramp > 0:
        ramp_note = "Gradual build"
    else:
        ramp_note = "Fitness declining or stable"

    proj = query(
        """SELECT time, ctl, atl, tsb FROM training_load
           WHERE user_id = %s AND source = 'projected'
           ORDER BY time""",
        (user_id,),
    )

    return {
        "date": current["time"],
        "ctl_fitness": current["ctl"],
        "atl_fatigue": current["atl"],
        "tsb_form": current["tsb"],
        "ramp_rate": current["ramp"],
        "form_status": form,
        "ramp_note": ramp_note,
        "projection_8_weeks": proj[-1] if proj else None,
    }


def get_training_load(
    user_id: int,
    start_date: str = "",
    end_date: str = "",
    days: int = 90,
    tz_name: str = "",
) -> list[dict]:
    """Get daily TSS, CTL, ATL, TSB for a date range.  Defaults to last 90 days.
    Dates in YYYY-MM-DD format."""
    today = user_today(ZoneInfo(tz_name) if tz_name else None)
    if not start_date:
        start_date = (today - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (today + timedelta(days=1)).isoformat()

    return query(
        """SELECT time, tss, ctl, atl, tsb, ramp, source
           FROM training_load
           WHERE user_id = %s AND time >= %s AND time < %s
           ORDER BY time""",
        (user_id, start_date, end_date),
    )


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


def get_activities(
    user_id: int,
    start_date: str = "",
    end_date: str = "",
    days: int = 30,
    sport: str = "",
    limit: int = 50,
    tz_name: str = "",
) -> list[dict]:
    """List activities with metrics.  Filter by date range and/or sport type
    (running, cycling, strength_training, etc.).  Defaults to last 30 days."""
    today = user_today(ZoneInfo(tz_name) if tz_name else None)
    if not start_date:
        start_date = (today - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (today + timedelta(days=1)).isoformat()

    sql = """SELECT time, activity_type, title, duration_s, distance_m,
                    elevation_gain_m, avg_hr, max_hr, avg_power, max_power,
                    normalized_power, tss, intensity_factor, avg_cadence,
                    avg_pace_sec_km, calories,
                    training_effect_ae, training_effect_an
             FROM activities
             WHERE user_id = %s AND time >= %s AND time < %s"""
    params: list = [user_id, start_date, end_date]

    if sport:
        sql += " AND LOWER(activity_type) LIKE %s"
        params.append(f"%{sport.lower()}%")

    sql += " ORDER BY time DESC LIMIT %s"
    params.append(limit)

    return query(sql, tuple(params))


def get_activity_detail(user_id: int, activity_time: str) -> dict:
    """Get full detail for a single activity by its timestamp (ISO format)."""
    rows = query(
        """SELECT * FROM activities
           WHERE user_id = %s AND time = %s LIMIT 1""",
        (user_id, activity_time),
    )
    if not rows:
        raise ValueError("Activity not found")
    return rows[0]


# ---------------------------------------------------------------------------
# Body composition
# ---------------------------------------------------------------------------


def get_body_composition(
    user_id: int,
    start_date: str = "",
    end_date: str = "",
    days: int = 90,
    tz_name: str = "",
) -> list[dict]:
    """Get body composition trend (weight, body fat %, muscle mass, BMI)
    over a date range.  Defaults to last 90 days."""
    today = user_today(ZoneInfo(tz_name) if tz_name else None)
    if not start_date:
        start_date = (today - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (today + timedelta(days=1)).isoformat()

    return query(
        """SELECT time, weight_kg, body_fat_pct, muscle_mass_kg,
                  bone_mass_kg, bmi, body_water_pct
           FROM body_composition
           WHERE user_id = %s AND time >= %s AND time < %s
           ORDER BY time""",
        (user_id, start_date, end_date),
    )


# ---------------------------------------------------------------------------
# Vitals
# ---------------------------------------------------------------------------


def get_vitals(
    user_id: int,
    start_date: str = "",
    end_date: str = "",
    days: int = 30,
    tz_name: str = "",
) -> list[dict]:
    """Get daily vitals (resting HR, HRV, blood pressure, sleep, stress,
    body battery, SpO2).  Defaults to last 30 days."""
    today = user_today(ZoneInfo(tz_name) if tz_name else None)
    if not start_date:
        start_date = (today - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (today + timedelta(days=1)).isoformat()

    return query(
        """SELECT time, resting_hr, hrv_ms, bp_systolic, bp_diastolic,
                  bp_pulse, sleep_score, sleep_duration_min, stress_avg,
                  body_battery_high, body_battery_low, spo2_avg,
                  respiration_avg
           FROM vitals
           WHERE user_id = %s AND time >= %s AND time < %s
           ORDER BY time""",
        (user_id, start_date, end_date),
    )


# ---------------------------------------------------------------------------
# Zones & profile
# ---------------------------------------------------------------------------


def get_training_zones(user_slug: str) -> dict:
    """Get current training zones: heart rate, running power, cycling power,
    and running pace with absolute lower/upper bounds."""
    zones = load_yaml(ZONES_PATH)
    user_zones = zones.get("users", {}).get(user_slug)
    if not user_zones:
        return {"error": f"No zones configured for user '{user_slug}'"}

    result = {}
    for section in ("heart_rate", "running_power", "cycling_power", "running_pace"):
        data = user_zones.get(section)
        if not data:
            continue
        result[section] = {
            "model": data.get("model"),
            "anchor_value": data.get("anchor_value"),
            "anchor_unit": data.get("anchor_unit"),
            "zones": [
                {
                    "name": z["name"],
                    "lower": z.get("lower"),
                    "upper": z.get("upper"),
                    "description": z.get("description", ""),
                }
                for z in data.get("zones", [])
            ],
        }
    return result


def setup_running_hr_zones(user_slug: str) -> dict:
    """Analyse available data, estimate running HR zones using the best
    method, and provide Garmin watch setup instructions.

    Method hierarchy (best to worst):
      1. Lab lactate test LT2 heart rate
      2. Garmin auto-detected LTHR
      3. LTHR estimated from recent hard running efforts in the DB
      4. Heart Rate Reserve (Karvonen) — needs max_hr + resting_hr
      5. Percentage of observed max HR
      6. Age-predicted max HR (Tanaka formula)
    """
    from datetime import date, timedelta

    user_data = athlete_store.load(user_slug)
    if not user_data:
        return {"error": f"No athlete data for user '{user_slug}'"}

    hr = user_data.get("thresholds", {}).get("heart_rate", {})
    lactate = user_data.get("thresholds", {}).get("lactate", {})
    profile = user_data.get("profile", {})
    sources = user_data.get("thresholds", {}).get("_sources", {})

    max_hr = hr.get("max_hr")
    resting_hr = hr.get("resting_hr")
    lthr_run = hr.get("lthr_run")
    lt2_hr = lactate.get("lt2_hr")
    lt1_hr = lactate.get("lt1_hr")
    dob = profile.get("date_of_birth")

    # Surface Garmin's latest auto-detected values alongside configured values
    garmin_vs_configured = {}
    for field in ("lthr_run", "rftp_garmin", "threshold_pace"):
        src = sources.get(field, {})
        garmin_val = src.get("garmin_latest")
        if garmin_val is not None:
            section_key = "heart_rate" if field == "lthr_run" else "running"
            configured_val = user_data.get("thresholds", {}).get(section_key, {}).get(field)
            garmin_vs_configured[field] = {
                "configured": configured_val,
                "garmin_latest": garmin_val,
                "source": src.get("origin", "unknown"),
                "garmin_date": src.get("garmin_date"),
            }

    data_inventory = {
        "max_hr": max_hr,
        "resting_hr": resting_hr,
        "lthr_run": lthr_run,
        "lt2_hr_lab": lt2_hr,
        "lt1_hr_lab": lt1_hr,
        "date_of_birth": dob,
    }
    if garmin_vs_configured:
        data_inventory["garmin_vs_configured"] = garmin_vs_configured

    # --- Try to find LTHR from recent hard running efforts in the DB ---
    estimated_lthr_from_effort = None
    effort_detail = None
    try:
        user_id = resolve_user_id(user_slug)
    except Exception:
        user_id = None
    if user_id and not lthr_run and not lt2_hr:
        today = date.today()
        start = (today - timedelta(days=180)).isoformat()
        end = (today + timedelta(days=1)).isoformat()
        hard_runs = query(
            """SELECT time, title, duration_s, distance_m, avg_hr, max_hr
               FROM activities
               WHERE user_id = %s
                 AND time >= %s AND time < %s
                 AND LOWER(activity_type) LIKE '%%running%%'
                 AND avg_hr IS NOT NULL
                 AND duration_s BETWEEN 1200 AND 5400
               ORDER BY avg_hr DESC
               LIMIT 5""",
            (user_id, start, end),
        )
        data_inventory["hard_runs_found"] = len(hard_runs)

        if hard_runs:
            best = hard_runs[0]
            dur_min = (best["duration_s"] or 0) / 60
            avg = best["avg_hr"]
            if dur_min >= 20 and avg and avg > 140:
                if dur_min >= 50:
                    estimated_lthr_from_effort = avg
                elif dur_min >= 30:
                    estimated_lthr_from_effort = round(avg * 0.97)
                else:
                    estimated_lthr_from_effort = round(avg * 0.95)
                effort_detail = {
                    "activity": best.get("title") or "Running",
                    "date": str(best["time"])[:10],
                    "duration_min": round(dur_min),
                    "avg_hr": avg,
                    "max_hr_activity": best.get("max_hr"),
                    "scaling_factor": "Direct" if dur_min >= 50 else
                                      "×0.97 (30-50min)" if dur_min >= 30 else
                                      "×0.95 (20-30min)",
                }
                data_inventory["estimated_lthr_from_effort"] = estimated_lthr_from_effort

    # --- Also update max_hr from DB if not in config ---
    observed_max = None
    if user_id:
        row = query(
            """SELECT MAX(max_hr) as peak
               FROM activities
               WHERE user_id = %s AND max_hr IS NOT NULL
                 AND LOWER(activity_type) LIKE '%%running%%'""",
            (user_id,),
        )
        if row and row[0].get("peak"):
            observed_max = int(row[0]["peak"])
            data_inventory["observed_max_hr_all_time"] = observed_max

    # --- Age-predicted max HR ---
    age_predicted_max = None
    age = None
    if dob:
        try:
            birth = date.fromisoformat(str(dob))
            age = (date.today() - birth).days // 365
            age_predicted_max = round(208 - 0.7 * age)  # Tanaka formula
            data_inventory["age"] = age
            data_inventory["age_predicted_max_hr"] = age_predicted_max
        except (ValueError, TypeError):
            pass

    # --- Select best method and compute zones ---
    method = None
    anchor = None
    anchor_type = None
    confidence = None
    zones = []
    notes = []

    effective_max = max_hr or observed_max or age_predicted_max

    if lt2_hr:
        method = "lab_lactate_test"
        anchor = int(lt2_hr)
        anchor_type = "LTHR (lab LT2)"
        confidence = "high"
        notes.append("Using lab-tested LT2 heart rate — gold standard.")
        if lt1_hr:
            notes.append(f"LT1 (aerobic threshold) HR: {lt1_hr} bpm — your Zone 2 ceiling.")

    elif lthr_run:
        method = "garmin_lthr"
        anchor = int(lthr_run)
        anchor_type = "LTHR (Garmin auto-detect)"
        confidence = "high"
        notes.append(
            "Using Garmin auto-detected lactate threshold HR. "
            "Validate with a 30-min time trial for higher confidence."
        )

    elif estimated_lthr_from_effort:
        method = "race_effort_estimation"
        anchor = estimated_lthr_from_effort
        anchor_type = "LTHR (estimated from hard effort)"
        confidence = "medium"
        notes.append(
            f"Estimated LTHR from your hardest recent run: "
            f"{effort_detail['activity']} on {effort_detail['date']} "
            f"({effort_detail['duration_min']}min, avg HR {effort_detail['avg_hr']} bpm). "
            f"Scaling: {effort_detail['scaling_factor']}."
        )
        notes.append(
            "This is a reasonable estimate. For higher confidence, do a "
            "dedicated 30-min all-out run (avg HR of last 20 min = LTHR)."
        )

    elif effective_max and resting_hr:
        method = "heart_rate_reserve_karvonen"
        anchor_type = "HRR (Karvonen)"
        confidence = "medium" if max_hr else "low"
        hrr = effective_max - resting_hr
        max_source = ("observed" if max_hr else
                      "DB peak" if observed_max else "age-predicted (Tanaka)")
        notes.append(
            f"Using Heart Rate Reserve method. Max HR: {effective_max} bpm "
            f"({max_source}), Resting HR: {resting_hr} bpm, HRR: {hrr} bpm."
        )
        if not max_hr:
            notes.append(
                "Max HR is estimated — do a max HR test or hard race effort "
                "to validate. Age formulas have ±10-12 bpm error."
            )
        zones = [
            {"name": "Zone 1 - Recovery",        "lower": resting_hr + round(hrr * 0.50), "upper": resting_hr + round(hrr * 0.60)},
            {"name": "Zone 2 - Aerobic",         "lower": resting_hr + round(hrr * 0.60), "upper": resting_hr + round(hrr * 0.70)},
            {"name": "Zone 3 - Tempo",           "lower": resting_hr + round(hrr * 0.70), "upper": resting_hr + round(hrr * 0.80)},
            {"name": "Zone 4 - Threshold",       "lower": resting_hr + round(hrr * 0.80), "upper": resting_hr + round(hrr * 0.90)},
            {"name": "Zone 5 - VO2max/Anaerobic","lower": resting_hr + round(hrr * 0.90), "upper": effective_max},
        ]

    elif effective_max:
        method = "percent_max_hr"
        anchor_type = "%MaxHR"
        confidence = "low"
        max_source = ("observed" if max_hr else
                      "DB peak" if observed_max else "age-predicted (Tanaka)")
        notes.append(
            f"Using %Max HR method (least accurate). Max HR: {effective_max} bpm "
            f"({max_source})."
        )
        notes.append(
            "Without resting HR, we can't use the more accurate Karvonen method. "
            "Wear your watch overnight for a few nights to get resting HR."
        )
        zones = [
            {"name": "Zone 1 - Recovery",        "lower": round(effective_max * 0.50), "upper": round(effective_max * 0.60)},
            {"name": "Zone 2 - Aerobic",         "lower": round(effective_max * 0.60), "upper": round(effective_max * 0.70)},
            {"name": "Zone 3 - Tempo",           "lower": round(effective_max * 0.70), "upper": round(effective_max * 0.80)},
            {"name": "Zone 4 - Threshold",       "lower": round(effective_max * 0.80), "upper": round(effective_max * 0.90)},
            {"name": "Zone 5 - VO2max/Anaerobic","lower": round(effective_max * 0.90), "upper": effective_max},
        ]
    else:
        return {
            "error": "Insufficient data to estimate HR zones",
            "data_available": data_inventory,
            "recommendations": [
                "Run `garmin_fetch_profile` to pull max HR, resting HR, and LTHR from Garmin",
                "Do a few outdoor runs with your chest strap so Garmin can auto-detect LTHR",
                "As a last resort, we can use age-predicted values if DOB is set in your profile",
            ],
        }

    # LTHR-based zones (Friel 5-zone model) for methods 1-3
    if anchor and not zones:
        zones = [
            {"name": "Zone 1 - Recovery",         "lower": round(anchor * 0.00), "upper": round(anchor * 0.81)},
            {"name": "Zone 2 - Aerobic",           "lower": round(anchor * 0.81), "upper": round(anchor * 0.89)},
            {"name": "Zone 3 - Tempo",             "lower": round(anchor * 0.90), "upper": round(anchor * 0.93)},
            {"name": "Zone 4 - Sub-Threshold",     "lower": round(anchor * 0.94), "upper": round(anchor * 0.99)},
            {"name": "Zone 5a - Super-Threshold",  "lower": round(anchor * 1.00), "upper": round(anchor * 1.02)},
            {"name": "Zone 5b - Aerobic Capacity",  "lower": round(anchor * 1.03), "upper": round(anchor * 1.06)},
            {"name": "Zone 5c - Anaerobic Capacity","lower": round(anchor * 1.06), "upper": effective_max or round(anchor * 1.15)},
        ]

    # --- Garmin watch setup instructions ---
    garmin_zones_5 = []
    if len(zones) > 5:
        garmin_zones_5 = [
            {"name": "Zone 1", "lower": zones[0]["lower"], "upper": zones[0]["upper"]},
            {"name": "Zone 2", "lower": zones[1]["lower"], "upper": zones[1]["upper"]},
            {"name": "Zone 3", "lower": zones[2]["lower"], "upper": zones[2]["upper"]},
            {"name": "Zone 4", "lower": zones[3]["lower"], "upper": zones[4].get("upper", zones[3]["upper"])},
            {"name": "Zone 5", "lower": zones[4].get("upper", zones[3]["upper"]) + 1, "upper": zones[-1]["upper"]},
        ]
    else:
        garmin_zones_5 = zones

    garmin_setup = {
        "method": "Custom BPM (most accurate — avoids Garmin's internal rounding)",
        "max_hr_to_set": effective_max or (anchor + 15 if anchor else None),
        "zones_to_enter": garmin_zones_5,
        "instructions_watch": [
            "From the watch face, hold MENU (left middle button)",
            "Scroll to 'User Profile' > 'Heart Rate and Power Zones' > 'Running'",
            "Select 'Set Custom'",
            f"Set Max HR to {effective_max or (anchor + 15 if anchor else '???')}",
            "Enter each zone's upper boundary as shown above",
        ],
        "instructions_garmin_connect": [
            "Open Garmin Connect app > More (⋯) > Settings > User Settings",
            "Tap 'Heart Rate and Power Zones' > 'Running'",
            "Toggle 'Based On' to 'Custom'",
            f"Set Max HR to {effective_max or (anchor + 15 if anchor else '???')}",
            "Enter each zone's upper limit matching the values above",
            "Sync your watch to push the new zones",
        ],
        "important_notes": [
            "Set zones for RUNNING specifically (not the default/all-sport zones)",
            "Garmin uses 5 zones; our 7-zone Friel model maps to their 5 as shown",
            "After setting, verify on the watch: Menu > User Profile > HR Zones > Running",
            "Re-run this tool after any threshold test to keep zones current",
        ],
    }

    # --- Recommendations to improve accuracy ---
    recommendations = []
    if method in ("heart_rate_reserve_karvonen", "percent_max_hr"):
        recommendations.append({
            "priority": "high",
            "action": "Determine your LTHR",
            "how": (
                "Option A: Check your Garmin Fenix 8 — go to Performance Stats > "
                "Lactate Threshold. If Garmin has detected it, run `garmin_fetch_profile` "
                "to pull it in.\n"
                "Option B: Do a 30-minute all-out solo run after a 15-min warm-up. "
                "Your average HR for the last 20 minutes is your LTHR. Update with:\n"
                "`update_athlete_profile(field_path='thresholds.heart_rate.lthr_run', value=<HR>)`"
            ),
            "impact": "Moves you from low/medium → high confidence zones",
        })
    if method == "race_effort_estimation":
        recommendations.append({
            "priority": "medium",
            "action": "Validate estimated LTHR with a dedicated test",
            "how": (
                "Do a 30-minute all-out solo run after warm-up. Average HR of "
                "the last 20 minutes should be close to the estimated "
                f"{estimated_lthr_from_effort} bpm. Update the profile if it differs."
            ),
            "impact": "Confirms or corrects the estimate → high confidence",
        })
    if not max_hr or (max_hr and age_predicted_max and abs(max_hr - age_predicted_max) > 12):
        if not max_hr:
            recommendations.append({
                "priority": "medium",
                "action": "Establish true max HR",
                "how": (
                    "Do a max HR test: 3×3-min hard uphill intervals with full recovery, "
                    "then a final all-out 1-min sprint. Your peak HR on the last rep is "
                    "your max HR. Alternatively, use the highest HR from a hard 5K race."
                ),
                "impact": "Better zone ceilings and more accurate %HRmax zones",
            })
    if not resting_hr:
        recommendations.append({
            "priority": "medium",
            "action": "Get resting HR",
            "how": "Wear your Garmin watch overnight for 3-5 nights. It will auto-detect resting HR.",
            "impact": "Enables the more accurate Karvonen (HRR) method",
        })
    if not lt2_hr and method != "lab_lactate_test":
        recommendations.append({
            "priority": "low",
            "action": "Lab lactate test (gold standard)",
            "how": (
                "A sports lab test with blood lactate sampling gives you precise "
                "LT1 and LT2 values. This is the definitive way to set zones."
            ),
            "impact": "Highest possible accuracy — identifies both aerobic and anaerobic thresholds",
        })

    return {
        "method_used": method,
        "anchor_type": anchor_type,
        "anchor_value_bpm": anchor,
        "confidence": confidence,
        "data_available": data_inventory,
        "zones": zones,
        "garmin_setup": garmin_setup,
        "recommendations": recommendations if recommendations else None,
        "notes": notes,
    }


def get_athlete_profile(user_slug: str) -> dict:
    """Get the athlete's profile: goals, thresholds, body composition,
    training status, and treadmill zone-to-speed mapping."""
    user_data = athlete_store.load(user_slug)
    if not user_data:
        return {"error": f"No profile configured for user '{user_slug}'"}

    return {
        "profile": user_data.get("profile"),
        "goals": user_data.get("goals"),
        "thresholds": user_data.get("thresholds"),
        "body": user_data.get("body"),
        "training_status": user_data.get("training_status"),
    }


# ---------------------------------------------------------------------------
# Strength
# ---------------------------------------------------------------------------


def get_strength_sessions(
    user_id: int,
    start_date: str = "",
    end_date: str = "",
    days: int = 30,
    exercise: str = "",
    tz_name: str = "",
) -> list[dict]:
    """Get strength training sets from Hevy.  Filter by date range and/or
    exercise name (partial match).  Defaults to last 30 days."""
    today = user_today(ZoneInfo(tz_name) if tz_name else None)
    if not start_date:
        start_date = (today - timedelta(days=days)).isoformat()
    if not end_date:
        end_date = (today + timedelta(days=1)).isoformat()

    sql = """SELECT time, workout_id, exercise_name, exercise_type,
                    muscle_group, set_number, set_type,
                    weight_kg, reps, rpe, duration_s, distance_m
             FROM strength_sets
             WHERE user_id = %s AND time >= %s AND time < %s"""
    params: list = [user_id, start_date, end_date]

    if exercise:
        sql += " AND LOWER(exercise_name) LIKE %s"
        params.append(f"%{exercise.lower()}%")

    sql += " ORDER BY time DESC, workout_id, set_number LIMIT 200"
    return query(sql, tuple(params))


# ---------------------------------------------------------------------------
# Treadmill workouts
# ---------------------------------------------------------------------------


def list_treadmill_templates() -> list[dict]:
    """List available treadmill workout templates with name, duration, and
    step count."""
    templates = load_yaml(TEMPLATES_PATH).get("templates", {})
    result = []
    for key, tmpl in templates.items():
        steps = tmpl.get("steps", [])
        total_min = sum(s.get("duration_min", 0) for s in steps)
        result.append({
            "key": key,
            "name": tmpl["name"],
            "description": tmpl.get("description", ""),
            "total_minutes": total_min,
            "steps": len(steps),
        })
    return result


def generate_treadmill_workout(user_slug: str, template_key: str) -> dict:
    """Generate a structured treadmill workout from a template.  Returns a
    step-by-step table with speed, incline, duration, and distance for
    entry into iFit Workout Creator."""
    templates = load_yaml(TEMPLATES_PATH).get("templates", {})
    if template_key not in templates:
        available = ", ".join(templates.keys())
        raise ValueError(f"Template '{template_key}' not found. Available: {available}")

    user_data = athlete_store.load(user_slug) or {}
    treadmill = user_data.get("treadmill", {})
    zone_map = {**treadmill.get("zone_speed_map", {}), **treadmill.get("hill_map", {})}

    template = templates[template_key]
    steps = template["steps"]
    total_distance = 0.0
    rows = []

    for i, step in enumerate(steps, 1):
        zone_key = step["zone"]
        settings = zone_map.get(zone_key, {"speed_kph": 0.0, "incline_pct": 0.0})
        speed = settings["speed_kph"]
        incline = settings["incline_pct"]
        duration = step["duration_min"]
        distance = speed * (duration / 60)
        total_distance += distance

        rows.append({
            "step": i,
            "phase": step["phase"],
            "zone": zone_key,
            "speed_kph": speed,
            "incline_pct": incline,
            "duration_min": duration,
            "distance_km": round(distance, 2),
        })

    return {
        "name": template["name"],
        "description": template.get("description", ""),
        "total_minutes": sum(s["duration_min"] for s in steps),
        "total_distance_km": round(total_distance, 1),
        "steps": rows,
        "instructions": "Enter these steps in iFit: ifit.com -> Create -> Distance Based Workout",
    }


# ---------------------------------------------------------------------------
# Garmin authentication
# ---------------------------------------------------------------------------


def garmin_auth_status(user_slug: str, garmin_email: str = "") -> dict:
    """Check whether Garmin Connect authentication is set up and tokens are
    valid for the current user."""
    from scripts.garmin_auth import get_auth_status

    status = get_auth_status(user_slug)
    status["garmin_email"] = garmin_email or "(not configured)"
    return status


def garmin_authenticate(
    user_slug: str, garmin_email: str, garmin_password: str
) -> dict:
    """Start Garmin Connect authentication.  If MFA is required, returns a
    prompt -- the user should then call garmin_submit_mfa with the code."""
    from scripts.garmin_auth import try_cached_login, start_login

    client = try_cached_login(user_slug)
    if client:
        return {"status": "ok", "message": "Already authenticated with cached tokens."}

    if not garmin_email or not garmin_password:
        raise ValueError(
            "Garmin credentials not configured. Set garmin_email and "
            "garmin_password in the addon config (or secrets for local dev)."
        )

    result, _client = start_login(user_slug, garmin_email, garmin_password)

    if result == "ok":
        return {"status": "ok", "message": "Authenticated successfully. Tokens cached."}
    elif result == "needs_mfa":
        return {
            "status": "needs_mfa",
            "message": (
                "MFA code required. Garmin has sent a code to your email. "
                "Ask the user for the code, then call garmin_submit_mfa with it."
            ),
        }
    else:
        raise ValueError(result)


def garmin_submit_mfa(user_slug: str, mfa_code: str) -> dict:
    """Complete Garmin Connect MFA authentication with the code the user
    received (via email or authenticator app)."""
    from scripts.garmin_auth import finish_mfa_login

    result, _client = finish_mfa_login(user_slug, mfa_code)

    if result == "ok":
        return {"status": "ok", "message": "MFA verified. Tokens cached for future use."}
    else:
        raise ValueError(result)


# ---------------------------------------------------------------------------
# Athlete profile setup
# ---------------------------------------------------------------------------


def garmin_fetch_profile(user_slug: str) -> dict:
    """Fetch athlete profile data from Garmin Connect and refresh thresholds.

    Uses source-aware merge: auto-updates Garmin/estimated fields but never
    overwrites lab-tested values. Logs advisories for significant differences."""
    from scripts.garmin_auth import try_cached_login
    from scripts.garmin_fetch import (
        fetch_garmin_profile,
        refresh_thresholds as _refresh_thresholds,
    )

    client = try_cached_login(user_slug)
    if not client:
        raise ValueError("Garmin not authenticated. Call garmin_authenticate first.")

    result = fetch_garmin_profile(user_slug, client)

    refresh = _refresh_thresholds(
        user_slug, result["fetched"],
        fetched_sources=result.get("sources"),
    )

    return {
        "fetched": result["fetched"],
        "sources": result["sources"],
        "written_to_config": refresh["updated"],
        "advisories": refresh.get("advisories", []),
        "garmin_latest": refresh.get("garmin_latest", {}),
        "still_missing": result["missing"],
    }


def generate_fitness_assessment(
    user_slug: str,
    hevy_api_key: str | None = None,
    lookback_days: int = 180,
    include_hevy: bool = True,
) -> dict:
    """Generate a comprehensive fitness assessment by pulling 6 months of
    historical data from Garmin Connect (and optionally Hevy)."""
    from scripts.garmin_auth import try_cached_login
    from scripts.garmin_fetch import merge_into_athlete_yaml
    from scripts.fitness_assessment import assess_fitness

    client = try_cached_login(user_slug)
    if not client:
        raise ValueError("Garmin not authenticated. Call garmin_authenticate first.")

    key = hevy_api_key if include_hevy else None

    result = assess_fitness(
        slug=user_slug,
        garmin_client=client,
        hevy_api_key=key,
        lookback_days=lookback_days,
    )

    profile_data = result.get("auto_profile", {})
    if profile_data.get("fetched"):
        written = merge_into_athlete_yaml(
            str(ATHLETE_PATH), user_slug, profile_data["fetched"]
        )
        result["written_to_config"] = written

    suggested = result.get("suggested_action_items", [])
    if suggested:
        existing = load_action_items(user_slug)
        existing_ids = {i.get("id") for i in existing}
        added = []
        for item in suggested:
            if item.get("id") not in existing_ids:
                existing.append(item)
                added.append(item["id"])
        if added:
            save_action_items(user_slug, existing)
            result["action_items_added"] = added

    return result


def update_athlete_profile(
    user_slug: str, field_path: str, value: float | int | str
) -> dict:
    """Update a single field in the athlete profile config.

    field_path is dot-separated relative to the user, for example:
      thresholds.heart_rate.max_hr, body.weight_kg, profile.date_of_birth"""
    athlete_store.update_field(user_slug, field_path, value)

    user = athlete_store.load(user_slug) or {}
    if "ftp" in field_path or "weight" in field_path:
        ftp = (user.get("thresholds", {}).get("cycling", {}).get("ftp"))
        weight = user.get("body", {}).get("weight_kg")
        if ftp and weight and weight > 0:
            wkg = round(ftp / weight, 2)
            athlete_store.update_field(user_slug, "thresholds.cycling.ftp_wkg", wkg)
            return {
                "updated": field_path,
                "value": value,
                "also_computed": {"thresholds.cycling.ftp_wkg": wkg},
            }

    return {"updated": field_path, "value": value}


# ---------------------------------------------------------------------------
# Goals & onboarding
# ---------------------------------------------------------------------------

ONBOARDING_QUESTIONS = [
    {
        "id": "primary_goal",
        "question": "What is your primary fitness or racing goal?",
        "examples": [
            "Run a marathon", "Complete UTMB", "General fitness",
            "Lose weight", "Build muscle", "First triathlon",
            "Improve 5K time", "Stay healthy and active",
        ],
        "field": "goals.primary_goal",
    },
    {
        "id": "target_event",
        "question": "Do you have a specific target event? If so, what and when?",
        "examples": ["London Marathon April 2027", "No specific event yet"],
        "field": "goals.target_event",
        "optional": True,
    },
    {
        "id": "secondary_goals",
        "question": "Any secondary goals? (list as many as you like)",
        "examples": [
            "Improve body composition", "Build running endurance",
            "Get stronger", "Better sleep", "Reduce stress",
        ],
        "field": "goals.secondary_goals",
    },
    {
        "id": "available_hours",
        "question": "How many hours per week can you realistically train?",
        "examples": ["3-4 hours", "5-7 hours", "8-10 hours", "It varies a lot"],
        "field": "goals.available_hours_per_week",
    },
    {
        "id": "preferred_sports",
        "question": "What sports/activities do you enjoy or want to focus on?",
        "examples": [
            "Running", "Trail running", "Cycling", "Swimming",
            "Strength training", "Yoga", "Hiking",
        ],
        "field": "goals.preferred_sports",
    },
    {
        "id": "constraints",
        "question": "Any constraints or limitations? (injuries, schedule, equipment)",
        "examples": [
            "Bad knee -- can't do high impact daily",
            "Only free mornings before 7am",
            "No pool access", "Travel frequently",
        ],
        "field": "goals.constraints",
        "optional": True,
    },
    {
        "id": "experience_level",
        "question": "How would you describe your training experience?",
        "examples": [
            "beginner (less than 1 year)",
            "intermediate (1-3 years consistent)",
            "advanced (3+ years structured training)",
        ],
        "field": "goals.experience_level",
    },
    {
        "id": "likes_dislikes",
        "question": "What do you enjoy about training, and what do you dislike?",
        "examples": [
            "Love outdoor runs, hate indoor cycling",
            "Enjoy iFit workouts but find them unstructured",
            "Like data and tracking, dislike monotony",
        ],
        "field": "goals.training_preferences",
        "optional": True,
    },
]


def get_onboarding_questions(user_slug: str) -> dict:
    """Get the list of onboarding questions to ask a new user about their
    goals, preferences, and constraints.  Also returns any goals already
    on file so the AI can skip answered questions."""
    user_data = athlete_store.load(user_slug) or {}
    existing_goals = user_data.get("goals", {})

    answered = []
    unanswered = []
    for q in ONBOARDING_QUESTIONS:
        field_key = q["field"].removeprefix("goals.")
        parts = field_key.split(".")
        val = existing_goals
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p)
            else:
                val = None
                break

        if val is not None:
            answered.append({**q, "current_value": val})
        else:
            unanswered.append(q)

    return {
        "existing_goals": existing_goals,
        "answered": answered,
        "unanswered": unanswered,
        "instructions": (
            "Ask the unanswered questions conversationally. You don't have to "
            "follow the exact wording -- adapt to the conversation. Store each "
            "answer with set_user_goals. After collecting goals, run "
            "generate_fitness_assessment for a data-driven overview."
        ),
    }


def set_user_goals(user_slug: str, goals: dict) -> dict:
    """Store the user's goals, preferences, and constraints in their
    athlete profile.  Only provided keys are updated; existing values
    are preserved."""
    user = athlete_store.load(user_slug) or {}
    existing = user.setdefault("goals", {})

    for key, value in goals.items():
        if value is not None:
            existing[key] = value

    athlete_store.save(user_slug, user)
    return {"updated_goals": existing}


def get_user_goals(user_slug: str) -> dict:
    """Get the user's current goals, preferences, and constraints."""
    user_data = athlete_store.load(user_slug) or {}
    goals = user_data.get("goals", {})

    if not goals:
        return {
            "goals": None,
            "suggestion": (
                "No goals set yet. Call get_onboarding_questions to get "
                "the questions to ask this user."
            ),
        }

    return {"goals": goals}


# ---------------------------------------------------------------------------
# Action items
# ---------------------------------------------------------------------------


def load_action_items(user_slug: str) -> list[dict]:
    user_data = athlete_store.load(user_slug) or {}
    return user_data.get("action_items", [])


def save_action_items(user_slug: str, items: list[dict]) -> None:
    user_data = athlete_store.load(user_slug) or {}
    user_data["action_items"] = items
    athlete_store.save(user_slug, user_data)


def get_action_items(user_slug: str, status_filter: str = "") -> dict:
    """Get the user's action items grouped by priority.  Optional
    status_filter: 'pending', 'in_progress', 'completed', or blank for all."""
    items = load_action_items(user_slug)

    if status_filter:
        items = [i for i in items if i.get("status") == status_filter]

    high = [i for i in items if i.get("priority") == "high"]
    medium = [i for i in items if i.get("priority") == "medium"]
    low = [i for i in items if i.get("priority") == "low"]

    all_items = load_action_items(user_slug)
    pending = sum(1 for i in all_items if i.get("status") == "pending")
    in_progress = sum(1 for i in all_items if i.get("status") == "in_progress")

    return {
        "high_priority": high,
        "medium_priority": medium,
        "low_priority": low,
        "summary": {
            "total": len(items),
            "pending": pending,
            "in_progress": in_progress,
        },
        "instructions": (
            "Review these with the user. Ask about progress on in_progress "
            "and high-priority pending items. Mark completed items and "
            "add new ones based on the conversation."
        ),
    }


def add_action_item(
    user_slug: str,
    title: str,
    description: str,
    category: str = "training",
    priority: str = "medium",
    due: str = "",
) -> dict:
    """Add a new action item for the user.

    category: testing, habit, equipment, training, setup, nutrition
    priority: high, medium, low
    due: optional YYYY-MM-DD deadline"""
    items = load_action_items(user_slug)

    item_id = title.lower().replace(" ", "-")[:40]
    existing_ids = {i.get("id") for i in items}
    if item_id in existing_ids:
        base = item_id
        n = 2
        while item_id in existing_ids:
            item_id = f"{base}-{n}"
            n += 1

    new_item = {
        "id": item_id,
        "title": title,
        "description": description,
        "category": category,
        "priority": priority,
        "status": "pending",
        "created": user_today(load_user_tz(user_slug)).isoformat(),
        "due": due or None,
        "completed": None,
    }
    items.append(new_item)
    save_action_items(user_slug, items)
    return {"added": new_item}


def update_action_item(
    user_slug: str,
    item_id: str,
    status: str = "",
    priority: str = "",
    title: str = "",
    description: str = "",
    due: str = "",
    note: str = "",
) -> dict:
    """Update an existing action item.  Use this to mark items as completed,
    change priority, add notes, or update details."""
    items = load_action_items(user_slug)

    target = None
    for item in items:
        if item.get("id") == item_id:
            target = item
            break

    if not target:
        raise ValueError(f"Action item '{item_id}' not found")

    today_str = user_today(load_user_tz(user_slug)).isoformat()
    if status:
        target["status"] = status
        if status == "completed":
            target["completed"] = today_str
    if priority:
        target["priority"] = priority
    if title:
        target["title"] = title
    if description:
        target["description"] = description
    if due:
        target["due"] = due
    if note:
        existing_notes = target.get("notes", [])
        existing_notes.append({"date": today_str, "text": note})
        target["notes"] = existing_notes

    save_action_items(user_slug, items)
    return {"updated": target}


def complete_action_item(user_slug: str, item_id: str, note: str = "") -> dict:
    """Mark an action item as completed.  Optionally add a completion note
    (e.g. 'LTHR measured at 168 bpm')."""
    items = load_action_items(user_slug)

    target = None
    for item in items:
        if item.get("id") == item_id:
            target = item
            break

    if not target:
        raise ValueError(f"Action item '{item_id}' not found")

    today_str = user_today(load_user_tz(user_slug)).isoformat()
    target["status"] = "completed"
    target["completed"] = today_str
    if note:
        existing_notes = target.get("notes", [])
        existing_notes.append({"date": today_str, "text": note})
        target["notes"] = existing_notes

    save_action_items(user_slug, items)
    return {"completed": target}


# ---------------------------------------------------------------------------
# Integrations & hardware registry
# ---------------------------------------------------------------------------

SUPPORTED_INTEGRATIONS: list[dict] = [
    {
        "id": "garmin_fenix",
        "name": "Garmin Fenix / Forerunner / Enduro",
        "category": "wearable",
        "description": "GPS watch with optical HR, SpO2, barometric altitude, temperature",
        "data_provided": ["activities", "heart_rate", "hrv", "sleep", "stress", "body_battery", "spo2", "vo2max"],
        "setup": "Pair with Garmin Connect; data syncs automatically",
        "required": False,
        "recommended": True,
    },
    {
        "id": "garmin_hrm_pro",
        "name": "Garmin HRM Pro / HRM Pro Plus",
        "category": "wearable",
        "description": "Chest strap HR monitor with running dynamics and native running power",
        "data_provided": ["heart_rate", "running_dynamics", "running_power"],
        "setup": "Pair with Garmin watch via ANT+/BLE; provides running power without extra foot pod",
        "required": False,
        "recommended": True,
        "notes": "Provides Garmin native running power -- no Stryd needed",
    },
    {
        "id": "stryd",
        "name": "Stryd Running Power Meter",
        "category": "wearable",
        "description": "Foot pod for running power, cadence, ground contact time, leg spring stiffness",
        "data_provided": ["running_power", "cadence", "ground_contact", "leg_spring_stiffness", "critical_power"],
        "setup": "Pair with Garmin watch; power data appears in Garmin Connect activities",
        "required": False,
        "recommended": False,
        "notes": "Optional if you have HRM Pro -- Garmin native power is sufficient. Stryd adds wind adjustment and its own Critical Power model.",
    },
    {
        "id": "garmin_edge",
        "name": "Garmin Edge (1030/540/etc.)",
        "category": "cycling",
        "description": "Bike computer with GPS and sensor pairing",
        "data_provided": ["cycling_activities", "power", "cadence", "speed"],
        "setup": "Pair with Garmin Connect; pair HRM and power meter via ANT+",
        "required": False,
        "recommended": False,
    },
    {
        "id": "smart_trainer",
        "name": "Smart Trainer / Smart Bike (Wahoo Kickr, Tacx, etc.)",
        "category": "cycling",
        "description": "Indoor cycling with direct power measurement and controllable resistance",
        "data_provided": ["cycling_power", "cadence", "speed"],
        "setup": "Connect to Zwift/Garmin via ANT+ or Bluetooth",
        "required": False,
        "recommended": False,
    },
    {
        "id": "nordictrack_treadmill",
        "name": "NordicTrack / ProForm Treadmill (iFit-compatible)",
        "category": "treadmill",
        "description": "Treadmill with iFit integration for guided workouts, auto speed/incline control",
        "data_provided": ["treadmill_activities"],
        "setup": "iFit subscription required; we generate zone-based workouts for the iFit Workout Creator",
        "required": False,
        "recommended": False,
        "notes": "Supports up to 40% incline and -6% decline on x22i",
    },
    {
        "id": "garmin_scale",
        "name": "Garmin Smart Scale (Index S2/Index 2)",
        "category": "health",
        "description": "Smart scale measuring weight, body fat, muscle mass, bone mass, BMI, body water",
        "data_provided": ["weight", "body_fat", "muscle_mass", "bone_mass", "bmi", "body_water"],
        "setup": "Pair with Garmin Connect; weigh daily at the same time for best trends",
        "required": False,
        "recommended": True,
    },
    {
        "id": "garmin_bp_monitor",
        "name": "Garmin Blood Pressure Monitor",
        "category": "health",
        "description": "Blood pressure monitor syncing to Garmin Connect",
        "data_provided": ["blood_pressure_systolic", "blood_pressure_diastolic", "resting_pulse"],
        "setup": "Pair with Garmin Connect; measure morning and evening during baseline",
        "required": False,
        "recommended": False,
    },
    {
        "id": "home_gym",
        "name": "Home Gym Equipment",
        "category": "gym",
        "description": "Dumbbells, kettlebells, barbells, squat rack, resistance bands, etc.",
        "data_provided": [],
        "setup": "Log workouts in Hevy; tell the system what you have so workouts use available equipment",
        "required": False,
        "recommended": False,
    },
    {
        "id": "garmin_connect",
        "name": "Garmin Connect",
        "category": "software",
        "description": "Central data hub -- all Garmin device data flows here. Primary data source for activities, HR, sleep, body composition, vitals.",
        "data_provided": [
            "activities", "heart_rate", "hrv", "sleep", "stress", "body_battery",
            "spo2", "vo2max", "body_composition", "blood_pressure", "running_power",
            "cycling_power", "training_effect", "lactate_threshold",
        ],
        "setup": "Create account at connect.garmin.com; provide credentials in addon config",
        "required": True,
        "credentials": ["garmin_email", "garmin_password"],
    },
    {
        "id": "hevy",
        "name": "Hevy",
        "category": "software",
        "description": "Strength training logger -- tracks sets, reps, weight, RPE per exercise",
        "data_provided": ["strength_sets", "exercise_history", "personal_records"],
        "setup": "Create account; get API key from Hevy app settings",
        "required": False,
        "recommended": True,
        "credentials": ["hevy_api_key"],
    },
    {
        "id": "strava",
        "name": "Strava",
        "category": "software",
        "description": "Social fitness platform with route planning and segment tracking",
        "data_provided": ["activities_social", "segments", "routes"],
        "setup": "Connect Garmin Connect to Strava for auto-sync; no direct API integration needed",
        "required": False,
        "recommended": False,
        "notes": "Data flows from Garmin -- no credentials needed in the addon",
    },
    {
        "id": "ifit",
        "name": "iFit",
        "category": "software",
        "description": "Guided treadmill workouts with auto speed/incline. We generate structured workouts for the iFit Workout Creator.",
        "data_provided": ["treadmill_workouts"],
        "setup": "iFit subscription; use ifit.com Workout Creator to enter generated workouts",
        "required": False,
        "recommended": False,
        "notes": "No API -- workouts are generated as step tables for manual entry",
    },
    {
        "id": "zwift",
        "name": "Zwift",
        "category": "software",
        "description": "Virtual cycling and running platform with structured workouts",
        "data_provided": ["indoor_cycling", "indoor_running"],
        "setup": "Connect smart trainer via ANT+/BLE; activities auto-sync to Garmin Connect",
        "required": False,
        "recommended": False,
        "notes": "Data flows to Garmin Connect -- no direct integration needed",
    },
]


def get_supported_integrations(category: str = "") -> dict:
    """Get the list of all supported hardware and software integrations.

    Optional category filter: wearable, cycling, treadmill, health, gym, software"""
    items = SUPPORTED_INTEGRATIONS
    if category:
        items = [i for i in items if i["category"] == category.lower()]

    required = [i for i in items if i.get("required")]
    recommended = [i for i in items if i.get("recommended") and not i.get("required")]
    optional = [i for i in items if not i.get("required") and not i.get("recommended")]

    categories = sorted({i["category"] for i in SUPPORTED_INTEGRATIONS})

    return {
        "required": required,
        "recommended": recommended,
        "optional": optional,
        "categories": categories,
        "total": len(items),
        "instructions": (
            "Present these to the user, starting with required integrations, "
            "then recommended, then optional. Ask which ones they have. "
            "Store their selections with set_user_integrations."
        ),
    }


def set_user_integrations(
    user_slug: str,
    integrations: list[str],
    equipment_notes: dict | None = None,
) -> dict:
    """Store which integrations and hardware the user has.

    integrations: list of integration IDs from get_supported_integrations
    equipment_notes: optional dict of integration_id -> note string"""
    valid_ids = {i["id"] for i in SUPPORTED_INTEGRATIONS}
    unknown = [i for i in integrations if i not in valid_ids]
    if unknown:
        raise ValueError(f"Unknown integration IDs: {unknown}. Use get_supported_integrations to see valid IDs.")

    user = athlete_store.load(user_slug) or {}

    user_integrations: list[dict[str, Any]] = []
    for int_id in integrations:
        entry: dict[str, Any] = {"id": int_id}
        ref = next((i for i in SUPPORTED_INTEGRATIONS if i["id"] == int_id), None)
        if ref:
            entry["name"] = ref["name"]
            entry["category"] = ref["category"]
        if equipment_notes and int_id in equipment_notes:
            entry["notes"] = equipment_notes[int_id]
        user_integrations.append(entry)

    user["integrations"] = user_integrations
    athlete_store.save(user_slug, user)

    cred_needed = []
    for int_id in integrations:
        ref = next((i for i in SUPPORTED_INTEGRATIONS if i["id"] == int_id), None)
        if ref and ref.get("credentials"):
            cred_needed.append({"integration": int_id, "credentials": ref["credentials"]})

    return {
        "stored": len(user_integrations),
        "integrations": [i["id"] for i in user_integrations],
        "credentials_needed": cred_needed,
        "next_steps": (
            "If credentials_needed is not empty, ensure those are configured in "
            "the addon config or secrets. Then proceed with garmin_authenticate "
            "and generate_fitness_assessment."
        ),
    }


def get_user_integrations(user_slug: str) -> dict:
    """Get the user's configured integrations and hardware."""
    user_data = athlete_store.load(user_slug) or {}
    integrations = user_data.get("integrations", [])

    if not integrations:
        return {
            "integrations": None,
            "suggestion": (
                "No integrations configured yet. Call get_supported_integrations "
                "to show the user what's available, then set_user_integrations "
                "to store their selections."
            ),
        }

    return {"integrations": integrations, "count": len(integrations)}


# ---------------------------------------------------------------------------
# iFit integration — general recommendations, library search, strength recs
# ---------------------------------------------------------------------------


def recommend_ifit_workout(user_slug: str) -> dict:
    """Run the general iFit workout recommendation engine.

    Analyses recent 14-day activity history, muscle group fatigue, and variety
    to score up-next, favorites, and iFit-recommended workouts.  Returns top 5
    ranked candidates with muscle focus, scoring rationale, and recent activity
    summary."""
    from scripts.ifit_recommend import (
        fetch_recent_history,
        analyze_fatigue,
        fetch_candidates,
        score_candidates,
        RECOVERY_DAYS,
    )
    from scripts.ifit_auth import get_auth_headers

    try:
        headers = get_auth_headers()
    except RuntimeError as exc:
        return {"error": str(exc)}

    history = fetch_recent_history(headers, days=14)
    fatigue = analyze_fatigue(history)
    candidates = fetch_candidates(headers)
    ranked = score_candidates(candidates, fatigue, history, headers)

    recent_summary = []
    for entry in history[:7]:
        day_label = "today" if entry["days_ago"] == 0 else f"{entry['days_ago']}d ago"
        recent_summary.append({
            "when": day_label,
            "title": entry.get("title", "?"),
            "type": entry.get("log_type", "?"),
            "muscle_groups": sorted(entry.get("muscle_groups", set())),
            "styles": sorted(entry.get("styles", set())),
        })

    muscle_status = {}
    for mg in ["upper", "lower", "core", "total"]:
        days = fatigue["days_since"].get(mg)
        needed = RECOVERY_DAYS.get(mg, 2)
        if days is None:
            muscle_status[mg] = "not trained recently — READY"
        elif days >= needed:
            muscle_status[mg] = f"rested ({days}d ago) — READY"
        else:
            muscle_status[mg] = f"recovering ({days}d ago, need {needed}d)"

    top = []
    for cand in ranked[:5]:
        top.append({
            "title": cand.get("title", "?"),
            "source": cand.get("source", "?"),
            "series_progress": cand.get("series_progress", ""),
            "score": cand.get("score", 0),
            "type": cand.get("type", "?"),
            "muscle_groups": sorted(cand.get("muscle_groups", set())),
            "styles": sorted(cand.get("styles", set())),
            "difficulty": cand.get("difficulty", "?"),
            "required_equipment": cand.get("required_equipment", []),
            "reasons": cand.get("reasons", []),
        })

    return {
        "recent_activity": recent_summary,
        "muscle_status": muscle_status,
        "workouts_last_3d": fatigue["total_3d"],
        "workouts_last_7d": fatigue["total_7d"],
        "last_run_days_ago": fatigue["last_run_day"],
        "recommendations": top,
        "count": len(top),
    }


def _load_ifit_library() -> tuple[list[dict], dict]:
    """Load the iFit library and trainers into memory (cached after first call)."""
    import json as _json

    if _ifit_library_cache.get("workouts"):
        return _ifit_library_cache["workouts"], _ifit_library_cache.get("trainers", {})

    cache_path = ROOT / ".ifit_capture" / "library_workouts.json"
    trainers_path = ROOT / ".ifit_capture" / "trainers.json"

    if not cache_path.exists():
        try:
            import asyncio
            from scripts.ifit_auth import get_auth_headers
            from scripts.ifit_list_series import fetch_all_trainers, fetch_all_workouts
            headers = get_auth_headers()
            fetch_all_trainers(headers)
            asyncio.run(fetch_all_workouts(headers))
        except Exception:
            return [], {}
        if not cache_path.exists():
            return [], {}

    with open(cache_path) as f:
        workouts = _json.load(f)
    trainers = {}
    if trainers_path.exists():
        with open(trainers_path) as f:
            trainers = _json.load(f)

    _ifit_library_cache["workouts"] = workouts
    _ifit_library_cache["trainers"] = trainers
    return workouts, trainers


def search_ifit_library(query: str, workout_type: str = "", limit: int = 10) -> dict:
    """Search the iFit workout library by title, trainer, category, description, or keyword.

    Uses the cached library (12K+ workouts).  Results are ranked by relevance
    (title match > trainer match > description match > category match) and rating.
    Also enriches results with program/series info from R2 when available."""

    workouts, trainers = _load_ifit_library()
    if not workouts:
        return {"error": "iFit library cache not available."}

    # Load program index for enrichment (one-to-many)
    program_index: dict[str, list[dict]] = {}
    try:
        from scripts.ifit_r2_sync import load_program_index
        program_index = load_program_index()
    except Exception:
        pass

    q_lower = query.lower()
    terms = q_lower.split()
    n_terms = len(terms)
    # Phrase boost scales with query length -- a 5-word exact phrase match should
    # dominate over scattered single-term hits across different fields.
    phrase_boost = max(50, n_terms * 20) if n_terms > 1 else 0

    if workout_type:
        wt = workout_type.lower()
        workouts = [
            w for w in workouts
            if wt in w.get("type", "").lower()
            or any(wt in c.lower() for c in w.get("categories", []))
            or any(wt in c.lower() for c in w.get("subcategories", []))
        ]

    scored = []
    for w in workouts:
        title = (w.get("title") or "").lower()
        trainer_id = w.get("trainer_id", "")
        trainer_name = (trainers.get(trainer_id, {}).get("name") or "").lower()
        cats = " ".join(w.get("categories", []) + w.get("subcategories", [])).lower()
        desc = (w.get("description") or "").lower()

        progs = program_index.get(w.get("id", ""), [])
        prog_titles = " ".join(p.get("title", "").lower() for p in progs)

        score = 0
        for term in terms:
            if term in title:
                score += 10
            if term in trainer_name:
                score += 8
            if term in desc:
                score += 5
            if term in prog_titles:
                score += 7
            if term in cats:
                score += 3

        if phrase_boost:
            if q_lower in title:
                score += phrase_boost
            if q_lower in prog_titles:
                score += phrase_boost
            if q_lower in desc:
                score += phrase_boost
            if q_lower in trainer_name:
                score += phrase_boost

        if score > 0:
            rating = w.get("rating_avg", 0) or 0
            score += rating * 0.5
            scored.append((score, w, trainers.get(trainer_id, {}).get("name", "")))

    scored.sort(key=lambda x: -x[0])

    results = []
    for _score, w, trainer_name in scored[:limit]:
        wid = w.get("id", "")
        entry = {
            "title": w.get("title", ""),
            "trainer": trainer_name,
            "type": w.get("type", ""),
            "difficulty": w.get("difficulty", ""),
            "duration_min": round(w.get("time_sec", 0) / 60) if w.get("time_sec") else None,
            "rating": w.get("rating_avg", 0),
            "categories": w.get("categories", []),
            "subcategories": w.get("subcategories", []),
            "equipment": w.get("required_equipment", []),
            "workout_id": wid,
        }
        desc = w.get("description", "")
        if desc:
            entry["description"] = desc[:200] + ("..." if len(desc) > 200 else "")

        progs = program_index.get(wid, [])
        if not progs:
            try:
                from scripts.ifit_r2_sync import fetch_workout_series
                series = fetch_workout_series(wid)
                if series:
                    progs = [
                        {"title": e.get("title", ""), "series_id": e.get("seriesId", "")}
                        for e in series
                    ]
            except Exception:
                pass
        if progs:
            entry["programs"] = [
                {"title": p.get("title", ""), "series_id": p.get("series_id", p.get("seriesId", ""))}
                for p in progs
            ]

        results.append(entry)

    return {
        "query": query,
        "type_filter": workout_type or None,
        "results": results,
        "count": len(results),
        "total_library_size": len(workouts),
    }


def search_ifit_programs(query: str, limit: int = 10) -> dict:
    """Search the iFit program/series index by name, trainer, or keyword."""
    try:
        from scripts.r2_store import is_configured as r2_configured, list_keys, download_json
    except ImportError:
        return {"error": "R2 module not available"}

    if not r2_configured():
        return {"error": "R2 not configured — program index unavailable"}

    keys = list_keys("programs/")
    if not keys:
        return {"query": query, "results": [], "count": 0, "note": "No programs indexed yet"}

    q_lower = query.lower()
    terms = q_lower.split()
    n_terms = len(terms)
    phrase_boost = max(50, n_terms * 20) if n_terms > 1 else 0
    scored = []

    for key in keys:
        program = download_json(key)
        if not program:
            continue

        title = (program.get("title") or "").lower()
        overview = (program.get("overview") or "").lower()
        trainer_names = " ".join(t.get("name", "") for t in program.get("trainers", [])).lower()

        score = 0
        for term in terms:
            if term in title:
                score += 10
            if term in trainer_names:
                score += 8
            if term in overview:
                score += 5

        if phrase_boost:
            if q_lower in title:
                score += phrase_boost
            if q_lower in overview:
                score += phrase_boost
            if q_lower in trainer_names:
                score += phrase_boost

        if score > 0:
            rating = program.get("rating", {})
            avg = rating.get("average", 0) if isinstance(rating, dict) else 0
            score += avg * 0.5
            scored.append((score, program))

    scored.sort(key=lambda x: -x[0])

    results = []
    for _score, p in scored[:limit]:
        results.append({
            "title": p.get("title", ""),
            "series_id": p.get("series_id", ""),
            "type": p.get("type", ""),
            "overview": (p.get("overview") or "")[:200],
            "trainers": [t.get("name", "") for t in p.get("trainers", [])],
            "workout_count": p.get("workout_count", 0),
            "workouts": p.get("workout_titles", [])[:10],
            "rating": p.get("rating", {}).get("average") if isinstance(p.get("rating"), dict) else None,
        })

    return {
        "query": query,
        "results": results,
        "count": len(results),
        "total_programs_indexed": len(keys),
    }


def get_ifit_program_details(series_id: str) -> dict:
    """Get details for an iFit program/series by ID.

    First checks R2 cache, falls back to live iFit API.
    Returns structured week-by-week view when available."""
    try:
        from scripts.r2_store import (
            is_configured as r2_configured, download_json, upload_json,
        )
    except ImportError:
        r2_configured = lambda: False

    program = None
    if r2_configured():
        program = download_json(f"programs/{series_id}.json")

    if not program:
        import httpx as _httpx
        try:
            from scripts.ifit_auth import get_auth_headers
            headers = get_auth_headers()
        except RuntimeError as exc:
            return {"error": str(exc)}

        try:
            r = _httpx.get(
                f"https://gateway.ifit.com/wolf-workouts-service/v1/program/{series_id}"
                f"?softwareNumber=424992",
                headers=headers, timeout=15,
            )
            if r.status_code != 200:
                return {"error": f"iFit API returned {r.status_code}"}
            data = r.json()
        except Exception as exc:
            return {"error": f"Failed to fetch program: {exc}"}

        from scripts.ifit_r2_sync import _build_weeks_from_api

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
            "workout_ids": [w.get("itemId", "") for w in data.get("workouts", [])],
            "workout_titles": [w.get("title", "") for w in data.get("workouts", [])],
            "workout_count": len(data.get("workouts", [])),
            "weeks": _build_weeks_from_api(data),
        }

        if r2_configured():
            upload_json(f"programs/{series_id}.json", program)

    result = {**program}

    weeks = result.get("weeks", [])
    if weeks:
        schedule: list[dict] = []
        for week in weeks:
            week_name = week.get("name", "")
            workouts = week.get("workouts", [])
            schedule.append({
                "week": week_name,
                "workout_count": len(workouts),
                "workouts": [
                    {"position": i + 1, "id": w.get("id", ""), "title": w.get("title", "")}
                    for i, w in enumerate(workouts)
                ],
            })
        result["schedule"] = schedule

    return result


def discover_ifit_series(workout_id: str) -> dict:
    """Discover all series/programs a workout belongs to and map every sibling workout.

    Given a single workout ID, finds its series via the pre-workout API,
    fetches full program details, and maps ALL workouts in those series
    so the entire series is immediately searchable. Returns series info
    with the complete workout list for each discovered series."""
    try:
        from scripts.ifit_r2_sync import discover_series_for_workout
        return discover_series_for_workout(workout_id)
    except Exception as exc:
        return {"error": str(exc)}


def get_ifit_workout_details(workout_id: str) -> dict:
    """Get detailed info about a specific iFit workout by ID.

    Returns metadata, trainer info, and a full exercise breakdown.  If the
    workout hasn't been processed yet the transcript is fetched from iFit and
    exercises are extracted via LLM on the fly (then cached for next time).

    Results are cached in memory — repeated lookups for the same workout ID
    within the same process are instant."""

    if workout_id in _workout_details_cache:
        return _workout_details_cache[workout_id]

    import httpx as _httpx
    from scripts.ifit_auth import get_auth_headers

    try:
        headers = get_auth_headers()
    except RuntimeError as exc:
        return {"error": str(exc)}

    try:
        r = _httpx.get(
            f"https://gateway.ifit.com/lycan/v1/workouts/{workout_id}",
            headers=headers,
            timeout=15,
        )
        if r.status_code != 200:
            return {"error": f"iFit API returned {r.status_code}"}
        meta = r.json()
    except Exception as exc:
        return {"error": f"Failed to fetch workout: {exc}"}

    from scripts.ifit_recommend import classify_workout
    classification = classify_workout(meta)

    difficulty = meta.get("difficulty", {})
    estimates = meta.get("estimates", {})
    ratings = meta.get("ratings", {})
    trainer_meta = meta.get("metadata", {})

    result: dict = {
        "title": meta.get("title", ""),
        "description": meta.get("description", ""),
        "type": meta.get("type", ""),
        "difficulty": difficulty.get("rating", "") if isinstance(difficulty, dict) else str(difficulty),
        "duration_min": round(estimates.get("time", 0) / 60) if estimates.get("time") else None,
        "calories_est": estimates.get("calories"),
        "rating_avg": ratings.get("average", 0),
        "rating_count": ratings.get("count", 0),
        "muscle_groups": sorted(classification.get("muscle_groups", set())),
        "styles": sorted(classification.get("styles", set())),
        "categories": sorted(classification.get("categories", set())),
        "subcategories": sorted(classification.get("subcategories", set())),
        "required_equipment": meta.get("required_equipment", []),
        "workout_group_id": meta.get("workout_group_id"),
    }

    if trainer_meta.get("trainer"):
        try:
            tr = _httpx.get(
                f"https://api.ifit.com/v1/trainers/{trainer_meta['trainer']}",
                headers=headers,
                timeout=10,
            )
            if tr.status_code == 200:
                td = tr.json()
                result["trainer"] = {
                    "name": f"{td.get('first_name', '')} {td.get('last_name', '')}".strip(),
                    "bio": td.get("short_bio", ""),
                }
        except Exception:
            pass

    # Fetch exercises on-demand (cache → R2 → live VTT → LLM extraction)
    from scripts.ifit_strength_recommend import fetch_workout_exercises
    exercise_info = fetch_workout_exercises(
        workout_id, result["title"], ifit_headers=headers,
    )
    if exercise_info["exercises"]:
        result["exercises"] = exercise_info["exercises"]
        result["exercises_source"] = exercise_info["source"]
    result["transcript_available"] = exercise_info["transcript_available"]

    _workout_details_cache[workout_id] = result

    # Enrich with program/series info (one-to-many: workout can belong to multiple series)
    programs_list = []
    try:
        from scripts.ifit_r2_sync import load_program_index, fetch_workout_series
        program_index = load_program_index()
        progs = program_index.get(workout_id)
        if progs:
            programs_list = progs
        else:
            series_entries = fetch_workout_series(workout_id, headers)
            if series_entries:
                programs_list = [
                    {
                        "series_id": e.get("seriesId", ""),
                        "title": e.get("title", ""),
                        "position": e.get("position"),
                        "week": e.get("week"),
                        "is_challenge": e.get("isChallenge", False),
                    }
                    for e in series_entries
                ]
    except Exception:
        pass

    if programs_list:
        result["programs"] = programs_list

    return result


def recommend_strength_workout(user_slug: str) -> dict:
    """Run the two-stage iFit strength workout recommendation pipeline.

    Returns top 3 workout recommendations with exercise breakdowns,
    muscle focus analysis, and scoring rationale.  Uses the athlete's
    current TSB, vitals, muscle load, goals, and iFit preferences."""
    from scripts.ifit_strength_recommend import recommend, AthleteState
    from dataclasses import asdict

    recs = recommend(user_slug)
    if not recs:
        return {"error": "No recommendations generated. Ensure library cache exists (run ifit_list_series.py)."}

    return {
        "recommendations": [asdict(r) for r in recs],
        "count": len(recs),
        "instructions": (
            "Present these 3 workouts to the user with their exercise breakdowns. "
            "Explain why each was chosen based on the reasoning field. "
            "If the user picks one, offer to create a Hevy routine for it "
            "using create_hevy_routine."
        ),
    }


def suggest_feature(
    user_slug: str,
    title: str,
    description: str,
    category: str = "enhancement",
) -> dict:
    """Open a GitHub issue for a user-suggested feature.

    Requires GITHUB_TOKEN and GITHUB_REPO env vars."""
    import httpx

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "amasolov/health-coach")

    if not token:
        return {
            "error": "GitHub integration is not configured. "
            "Ask the administrator to set GITHUB_TOKEN in the addon options."
        }

    label_map = {
        "enhancement": "enhancement",
        "bug": "bug",
        "question": "question",
    }
    label = label_map.get(category.lower(), "enhancement")

    body = (
        f"**Suggested by:** `{user_slug}`\n\n"
        f"### Description\n\n{description}"
    )

    try:
        resp = httpx.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "body": body, "labels": [label]},
            timeout=15,
        )
        resp.raise_for_status()
        issue = resp.json()
        return {
            "status": "created",
            "issue_number": issue["number"],
            "url": issue["html_url"],
            "title": issue["title"],
        }
    except httpx.HTTPStatusError as exc:
        return {"error": f"GitHub API error {exc.response.status_code}: {exc.response.text}"}
    except Exception as exc:
        return {"error": str(exc)}


def report_exercise_correction(
    user_slug: str,
    workout_id: str,
    feedback: str,
) -> dict:
    """Report incorrect exercise data for an iFit workout.

    Gathers the current extracted exercises, transcript, and workout metadata,
    then opens a GitHub issue so the data can be reviewed and corrected."""
    import json as _json
    import httpx
    from pathlib import Path as _P

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "amasolov/health-coach")

    if not token:
        return {
            "error": "GitHub integration is not configured. "
            "Ask the administrator to set GITHUB_TOKEN in the addon options."
        }

    # Gather workout metadata from library cache
    cache_dir = _P(__file__).resolve().parent.parent / ".ifit_capture"
    library_path = cache_dir / "library_workouts.json"
    trainers_path = cache_dir / "trainers.json"

    workout_title = workout_id
    trainer_name = ""
    if library_path.exists():
        with open(library_path) as f:
            for w in _json.load(f):
                if w.get("id") == workout_id:
                    workout_title = w.get("title", workout_id)
                    tid = w.get("trainer_id", "")
                    if tid and trainers_path.exists():
                        with open(trainers_path) as tf:
                            trainers = _json.load(tf)
                        trainer_name = trainers.get(tid, {}).get("name", "")
                    break

    # Gather current exercises from R2 or local cache
    exercises_json = ""
    try:
        from scripts.r2_store import is_configured as r2_ok, download_json, download_text
        if r2_ok():
            exercises = download_json(f"exercises/{workout_id}.json")
            if exercises:
                exercises_json = _json.dumps(exercises, indent=2)
    except Exception:
        pass

    if not exercises_json:
        exercise_cache_path = cache_dir / "exercise_cache.json"
        if exercise_cache_path.exists():
            with open(exercise_cache_path) as f:
                cache = _json.load(f)
            if workout_id in cache:
                exercises_json = _json.dumps(cache[workout_id], indent=2)

    # Gather transcript from R2
    transcript_snippet = ""
    try:
        from scripts.r2_store import is_configured as r2_ok, download_text
        if r2_ok():
            transcript = download_text(f"transcripts/{workout_id}.txt")
            if transcript:
                transcript_snippet = transcript[:500]
                if len(transcript) > 500:
                    transcript_snippet += "..."
    except Exception:
        pass

    # Build issue body
    parts = [
        f"**Reported by:** `{user_slug}`",
        f"**Workout:** {workout_title} (`{workout_id}`)",
    ]
    if trainer_name:
        parts.append(f"**Trainer:** {trainer_name}")

    parts.append(f"\n### User Feedback\n\n{feedback}")

    if exercises_json:
        parts.append(f"\n### Current Extracted Exercises\n\n```json\n{exercises_json}\n```")
    else:
        parts.append("\n### Current Extracted Exercises\n\n_No exercises extracted yet._")

    if transcript_snippet:
        parts.append(f"\n### Transcript (first 500 chars)\n\n> {transcript_snippet}")

    body = "\n".join(parts)
    issue_title = f"Exercise data correction: {workout_title}"

    try:
        resp = httpx.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "title": issue_title,
                "body": body,
                "labels": ["exercise-data"],
            },
            timeout=15,
        )
        resp.raise_for_status()
        issue = resp.json()
        return {
            "status": "created",
            "issue_number": issue["number"],
            "url": issue["html_url"],
            "title": issue["title"],
            "workout": workout_title,
        }
    except httpx.HTTPStatusError as exc:
        return {"error": f"GitHub API error {exc.response.status_code}: {exc.response.text}"}
    except Exception as exc:
        return {"error": str(exc)}


def sync_data(user_slug: str, user_id: int, hevy_api_key: str = "", full_sync: bool = False) -> dict:
    """Trigger an immediate data sync for the user (Garmin + Hevy).

    full_sync=True ignores the incremental start date and pulls all historical
    data from the beginning of time (Garmin: back to 2000-01-01; Hevy: all pages).
    Use this when the normal sync shows 0 new items but data is missing.

    Returns a summary of what was found and inserted."""
    from scripts.sync_garmin import sync_user as _sync_garmin
    from scripts.sync_hevy import sync_user as _sync_hevy

    results: dict[str, Any] = {}

    garmin_result = _sync_garmin(user_slug, user_id, full_sync=full_sync)
    if "error" in garmin_result:
        results["garmin"] = {"status": "skipped", "reason": garmin_result["error"]}
    else:
        results["garmin"] = {
            "status": "ok",
            "activities_found": garmin_result.get("activities_found", 0),
            "activities_new": garmin_result.get("activities_inserted", 0),
            "body_comp_found": garmin_result.get("body_comp_found", 0),
            "body_comp_new": garmin_result.get("body_comp_inserted", 0),
            "vitals_found": garmin_result.get("vitals_found", 0),
            "vitals_new": garmin_result.get("vitals_inserted", 0),
        }

    if hevy_api_key:
        hevy_result = _sync_hevy(user_slug, user_id, hevy_api_key, full_sync=full_sync)
        if "error" in hevy_result:
            results["hevy"] = {"status": "skipped", "reason": hevy_result["error"]}
        else:
            results["hevy"] = {
                "status": "ok",
                "workouts_found": hevy_result.get("workouts_found", 0),
                "workouts_new": hevy_result.get("workouts_inserted", 0),
                "sets_new": hevy_result.get("sets_inserted", 0),
            }
    else:
        results["hevy"] = {"status": "skipped", "reason": "No Hevy API key configured"}

    return {"synced": results, "full_sync": full_sync}


def _build_rec_from_details(workout_id: str, details: dict) -> dict | None:
    """Build a Recommendation dict from get_ifit_workout_details output."""
    exercises = details.get("exercises", [])
    if not exercises:
        return None
    trainer = details.get("trainer")
    if isinstance(trainer, dict):
        trainer_name = trainer.get("name", "")
    elif isinstance(trainer, str):
        trainer_name = trainer
    else:
        trainer_name = ""
    return {
        "rank": 0,
        "workout_id": workout_id,
        "title": details.get("title", "iFit Workout"),
        "trainer_name": trainer_name,
        "duration_min": details.get("duration_min") or 0,
        "difficulty": details.get("difficulty", ""),
        "rating": details.get("rating_avg", 0),
        "focus": "",
        "subcategories": details.get("subcategories", []),
        "required_equipment": details.get("required_equipment", []),
        "stage1_score": 0,
        "stage2_score": 0,
        "exercises": exercises,
        "reasoning": "User-selected workout",
    }


def create_hevy_routine_from_recommendation(
    user_slug: str,
    recommendation_index: int = 0,
    ifit_workout_id: str = "",
    workout_title: str = "",
    hevy_api_key: str = "",
) -> dict:
    """Create a Hevy routine from an iFit workout.

    Lookup priority:
      1. ifit_workout_id — find in cached recs or fetch on-the-fly.
      2. workout_title — search the iFit library by title if no valid ID.
      3. recommendation_index — positional fallback into recommendations.json.

    Always prefer passing ifit_workout_id when you have a confirmed ID from
    a previous tool call in the same conversation. Pass workout_title as a
    fallback for title-based search."""
    import json as _json
    from scripts.ifit_strength_recommend import (
        create_hevy_routine,
        fetch_workout_exercises,
        Recommendation,
    )

    if not hevy_api_key:
        return {"error": "hevy_api_key required to create a routine."}

    cache_path = ROOT / ".ifit_capture" / "recommendations.json"
    recs_data: list[dict] = []
    if cache_path.exists():
        with open(cache_path) as f:
            recs_data = _json.load(f)

    rec_dict: dict | None = None

    # Primary: look up by workout ID (stable across conversations)
    if ifit_workout_id:
        for rd in recs_data:
            if rd.get("workout_id") == ifit_workout_id:
                rec_dict = rd
                break

        if rec_dict is None:
            print(f"  Workout {ifit_workout_id} not in cached recs, fetching on-the-fly")
            details = get_ifit_workout_details(ifit_workout_id)
            if "error" not in details:
                rec_dict = _build_rec_from_details(ifit_workout_id, details)
            else:
                print(f"  ID lookup failed ({details['error']}), will try title search")

    # Fallback: search by title when ID is missing or returned 404
    if rec_dict is None and workout_title:
        print(f"  Searching iFit library for: {workout_title}")
        search_result = search_ifit_library(workout_title, workout_type="strength", limit=5)
        matches = search_result.get("results", []) if isinstance(search_result, dict) else []
        for m in matches:
            if m.get("title", "").lower().strip() == workout_title.lower().strip():
                found_id = m.get("workout_id", "") or m.get("id", "")
                if found_id:
                    print(f"  Exact title match: {m['title']} -> {found_id}")
                    details = get_ifit_workout_details(found_id)
                    if "error" not in details:
                        rec_dict = _build_rec_from_details(found_id, details)
                        break
        if rec_dict is None and matches:
            found_id = matches[0].get("workout_id", "") or matches[0].get("id", "")
            if found_id:
                print(f"  Best title match: {matches[0].get('title', '')} -> {found_id}")
                details = get_ifit_workout_details(found_id)
                if "error" not in details:
                    rec_dict = _build_rec_from_details(found_id, details)

    # Fallback: positional index
    if rec_dict is None:
        if not recs_data:
            return {"error": "No recommendations cached and no ifit_workout_id provided. Run recommend_strength_workout first."}
        if recommendation_index < 0 or recommendation_index >= len(recs_data):
            return {"error": f"Invalid index {recommendation_index}. {len(recs_data)} recommendations available (0-based)."}
        rec_dict = recs_data[recommendation_index]

    valid_keys = {f.name for f in Recommendation.__dataclass_fields__.values()}
    filtered = {k: v for k, v in rec_dict.items() if k in valid_keys}
    rec = Recommendation(**filtered)
    return create_hevy_routine(rec, hevy_api_key)


# ---------------------------------------------------------------------------
# Hevy routine management (list / delete)
# ---------------------------------------------------------------------------


def manage_hevy_routines(
    user_slug: str,
    action: str = "list",
    routine_id: str = "",
    new_title: str = "",
    hevy_api_key: str = "",
) -> dict:
    """List, rename, or clean up duplicate Hevy routines.

    Actions:
      - list: return all routines in the user's Hevy account
      - rename: rename a routine (the public API has no DELETE)
      - mark_duplicates: find routines with identical titles and prefix
        extras with '[DELETE] ' so the user can easily find and remove
        them in the Hevy app
    """
    from scripts.ifit_strength_recommend import (
        list_hevy_routines,
        rename_hevy_routine,
    )

    if not hevy_api_key:
        return {"error": "hevy_api_key required."}

    if action == "list":
        routines = list_hevy_routines(hevy_api_key)
        return {
            "routines": routines,
            "count": len(routines),
        }

    if action == "rename":
        if not routine_id:
            return {"error": "routine_id required for rename action."}
        if not new_title:
            return {"error": "new_title required for rename action."}
        return rename_hevy_routine(routine_id, new_title, hevy_api_key)

    if action == "mark_duplicates":
        routines = list_hevy_routines(hevy_api_key)
        seen: dict[str, dict] = {}
        duplicates: list[dict] = []
        for rt in routines:
            title = rt["title"].strip().lower()
            if title in seen:
                duplicates.append(rt)
            else:
                seen[title] = rt

        if not duplicates:
            return {"status": "no_duplicates", "routine_count": len(routines)}

        marked = []
        failed = []
        for dup in duplicates:
            if dup["title"].startswith("[DELETE] "):
                continue
            result = rename_hevy_routine(
                dup["id"], f"[DELETE] {dup['title']}", hevy_api_key,
            )
            if result.get("status") == "renamed":
                marked.append({"id": dup["id"], "title": dup["title"]})
            else:
                failed.append({
                    "id": dup["id"],
                    "title": dup["title"],
                    "error": result.get("error", ""),
                })

        return {
            "status": "marked",
            "marked_count": len(marked),
            "marked": marked,
            "failed_count": len(failed),
            "failed": failed,
            "hint": (
                "Duplicates have been prefixed with '[DELETE] '. "
                "Open the Hevy app and delete them manually — "
                "the public API does not support routine deletion."
            ),
        }

    return {"error": f"Unknown action: {action}. Use 'list', 'rename', or 'mark_duplicates'."}


# ---------------------------------------------------------------------------
# iFit ↔ Hevy feedback loop
# ---------------------------------------------------------------------------


def _load_routine_map() -> dict:
    """Load the Hevy routine_id -> iFit workout mapping from R2."""
    try:
        from scripts.r2_store import is_configured, download_json
        if is_configured():
            data = download_json("hevy/routine_map.json")
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def get_hevy_routine_review(
    user_slug: str,
    ifit_workout_id: str = "",
    hevy_routine_id: str = "",
) -> dict:
    """Review an iFit-to-Hevy routine conversion.

    Looks up the routine mapping by either the iFit workout ID or the Hevy
    routine ID and returns the predicted exercises alongside the current
    stored exercise data so the user can compare and suggest corrections.
    """
    mapping = _load_routine_map()
    if not mapping:
        return {"error": "No iFit-to-Hevy routine conversions found."}

    entry = None
    matched_routine_id = ""

    if hevy_routine_id and hevy_routine_id in mapping:
        entry = mapping[hevy_routine_id]
        matched_routine_id = hevy_routine_id
    elif ifit_workout_id:
        for rid, m in mapping.items():
            if m.get("ifit_workout_id") == ifit_workout_id:
                entry = m
                matched_routine_id = rid
                break

    if not entry:
        available = [
            {"routine_id": rid, "title": m.get("title", ""), "ifit_workout_id": m.get("ifit_workout_id", "")}
            for rid, m in list(mapping.items())[-10:]
        ]
        return {
            "error": "No matching routine conversion found.",
            "hint": "Provide either an ifit_workout_id or hevy_routine_id.",
            "recent_conversions": available,
        }

    ifit_wid = entry.get("ifit_workout_id", "")

    current_exercises = None
    try:
        from scripts.r2_store import is_configured, download_json
        if is_configured():
            current_exercises = download_json(f"exercises/{ifit_wid}.json")
    except Exception:
        pass

    return {
        "hevy_routine_id": matched_routine_id,
        "ifit_workout_id": ifit_wid,
        "title": entry.get("title", ""),
        "created_at": entry.get("created_at", ""),
        "predicted_exercises": entry.get("predicted_exercises", []),
        "current_stored_exercises": current_exercises,
        "hint": (
            "Review the predicted_exercises (what was sent to Hevy) and "
            "current_stored_exercises (LLM extraction from iFit transcript). "
            "If something is wrong, use apply_exercise_feedback to correct it."
        ),
    }


def compare_hevy_workout(
    user_id: int,
    hevy_workout_id: str = "",
    days: int = 7,
) -> dict:
    """Compare a completed Hevy workout with its iFit-predicted exercises.

    If hevy_workout_id is not provided, scans recent workouts (last N days)
    for any that originated from an iFit-converted routine and compares
    the first match found.
    """
    mapping = _load_routine_map()
    if not mapping:
        return {"error": "No iFit-to-Hevy routine conversions recorded yet."}

    conn = get_conn()
    cur = conn.cursor()
    try:
        if hevy_workout_id:
            cur.execute("""
                SELECT workout_id, routine_id, exercise_name, set_number,
                       weight_kg, reps, duration_s, set_type
                FROM strength_sets
                WHERE user_id = %s AND workout_id = %s
                ORDER BY set_number
            """, (user_id, hevy_workout_id))
        else:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cur.execute("""
                SELECT workout_id, routine_id, exercise_name, set_number,
                       weight_kg, reps, duration_s, set_type
                FROM strength_sets
                WHERE user_id = %s AND time >= %s AND routine_id IS NOT NULL
                ORDER BY time DESC, set_number
            """, (user_id, cutoff))

        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if not rows:
        if hevy_workout_id:
            return {"error": f"No sets found for workout {hevy_workout_id}."}
        return {
            "error": f"No iFit-sourced workouts found in the last {days} days.",
            "hint": "Complete a Hevy workout that was created from an iFit recommendation, then try again.",
        }

    target_workout_id = rows[0][0]
    target_routine_id = rows[0][1]

    actual_exercises: dict[str, dict] = {}
    for _, _, name, set_num, weight, reps, dur, stype in rows:
        if rows[0][0] != _ and _ != target_workout_id:
            continue
        if name not in actual_exercises:
            actual_exercises[name] = {"sets": 0, "reps": [], "weights": [], "durations": []}
        ex = actual_exercises[name]
        ex["sets"] += 1
        if reps is not None:
            ex["reps"].append(int(reps))
        if weight is not None:
            ex["weights"].append(float(weight))
        if dur is not None:
            ex["durations"].append(int(dur))

    for ex in actual_exercises.values():
        if ex["reps"]:
            ex["typical_reps"] = max(set(ex["reps"]), key=ex["reps"].count)
        if ex["weights"]:
            ex["typical_weight_kg"] = round(max(ex["weights"]), 1)

    predicted_entry = mapping.get(target_routine_id, {}) if target_routine_id else {}
    predicted_exercises = predicted_entry.get("predicted_exercises", [])
    ifit_workout_id = predicted_entry.get("ifit_workout_id", "")

    if not predicted_exercises and target_routine_id:
        return {
            "workout_id": target_workout_id,
            "routine_id": target_routine_id,
            "actual_exercises": actual_exercises,
            "note": (
                "This workout came from a Hevy routine but no iFit mapping was found. "
                "It may have been created before the mapping feature was added."
            ),
        }

    predicted_names = {ex.get("hevy_name", "").lower(): ex for ex in predicted_exercises}
    actual_names = {name.lower(): data for name, data in actual_exercises.items()}

    differences: list[dict] = []

    for pred_ex in predicted_exercises:
        pname = pred_ex.get("hevy_name", "")
        pname_lower = pname.lower()
        if pname_lower in actual_names:
            actual = actual_names[pname_lower]
            diffs = {}
            pred_sets = int(pred_ex.get("sets", 0))
            if pred_sets and pred_sets != actual["sets"]:
                diffs["sets"] = {"predicted": pred_sets, "actual": actual["sets"]}
            pred_reps = pred_ex.get("reps", "")
            if pred_reps and actual.get("typical_reps"):
                try:
                    pr = int(str(pred_reps).rstrip("s"))
                    if pr != actual["typical_reps"]:
                        diffs["reps"] = {"predicted": pr, "actual": actual["typical_reps"]}
                except ValueError:
                    pass
            if diffs:
                differences.append({"exercise": pname, "status": "modified", **diffs})
        else:
            differences.append({"exercise": pname, "status": "predicted_but_not_done"})

    for aname in actual_names:
        if aname not in predicted_names:
            differences.append({"exercise": aname, "status": "done_but_not_predicted"})

    return {
        "workout_id": target_workout_id,
        "routine_id": target_routine_id,
        "ifit_workout_id": ifit_workout_id,
        "title": predicted_entry.get("title", ""),
        "actual_exercises": {
            name: {"sets": d["sets"], "typical_reps": d.get("typical_reps"), "typical_weight_kg": d.get("typical_weight_kg")}
            for name, d in actual_exercises.items()
        },
        "predicted_exercises": [
            {"name": ex.get("hevy_name", ""), "sets": ex.get("sets"), "reps": ex.get("reps"), "weight": ex.get("weight", "")}
            for ex in predicted_exercises
        ],
        "differences": differences,
        "match_score": f"{len(predicted_exercises) - len([d for d in differences if d['status'] != 'modified'])}/{len(predicted_exercises)} exercises matched",
        "hint": (
            "Review the differences above. If the actual workout better reflects "
            "the iFit workout, use apply_exercise_feedback to update the stored data."
        ),
    }


def apply_exercise_feedback(
    user_slug: str,
    ifit_workout_id: str,
    corrections: list[dict],
) -> dict:
    """Apply user corrections to stored iFit exercise data.

    Each correction dict can have:
      - action: "update", "add", or "remove"
      - exercise_name: name of the exercise to update/remove
      - new_name: (optional) corrected exercise name
      - sets: (optional) corrected number of sets
      - reps: (optional) corrected reps
      - weight: (optional) corrected weight hint
      - muscle_group: (optional) corrected muscle group
      - notes: (optional) notes about the correction

    Updates exercises/{workout_id}.json in R2 and clears the Hevy
    resolved cache so the next routine creation uses the corrected data.
    """
    import json as _json

    try:
        from scripts.r2_store import is_configured, download_json, upload_json
        if not is_configured():
            return {"error": "R2 storage not configured."}
    except ImportError:
        return {"error": "R2 storage module not available."}

    exercises = download_json(f"exercises/{ifit_workout_id}.json")
    if not exercises or not isinstance(exercises, list):
        return {"error": f"No stored exercises found for workout {ifit_workout_id}."}

    changes_applied = []
    original_count = len(exercises)

    for correction in corrections:
        action = correction.get("action", "update")
        target_name = correction.get("exercise_name", "").lower().strip()

        if action == "add":
            new_ex = {
                "hevy_name": correction.get("new_name", correction.get("exercise_name", "New Exercise")),
                "hevy_id": "",
                "muscle_group": correction.get("muscle_group", "other"),
                "sets": correction.get("sets", 3),
                "reps": correction.get("reps", 12),
                "weight": correction.get("weight", ""),
                "notes": correction.get("notes", "User-corrected"),
                "user_corrected": True,
            }
            exercises.append(new_ex)
            changes_applied.append({"action": "added", "exercise": new_ex["hevy_name"]})
            continue

        if action == "remove":
            before = len(exercises)
            exercises = [
                ex for ex in exercises
                if ex.get("hevy_name", "").lower().strip() != target_name
            ]
            if len(exercises) < before:
                changes_applied.append({"action": "removed", "exercise": target_name})
            else:
                changes_applied.append({"action": "not_found", "exercise": target_name})
            continue

        matched = False
        for ex in exercises:
            if ex.get("hevy_name", "").lower().strip() == target_name:
                if "new_name" in correction:
                    ex["hevy_name"] = correction["new_name"]
                if "sets" in correction:
                    ex["sets"] = correction["sets"]
                if "reps" in correction:
                    ex["reps"] = correction["reps"]
                if "weight" in correction:
                    ex["weight"] = correction["weight"]
                if "muscle_group" in correction:
                    ex["muscle_group"] = correction["muscle_group"]
                if "notes" in correction:
                    ex["notes"] = correction["notes"]
                ex["hevy_id"] = ""
                ex["user_corrected"] = True
                changes_applied.append({"action": "updated", "exercise": ex["hevy_name"]})
                matched = True
                break

        if not matched:
            changes_applied.append({"action": "not_found", "exercise": target_name})

    ok = upload_json(f"exercises/{ifit_workout_id}.json", exercises)
    if not ok:
        return {"error": "Failed to save corrected exercises to R2."}

    try:
        from scripts.r2_store import delete as r2_delete
        resolved_key = f"hevy/resolved/{ifit_workout_id}.json"
        if r2_delete(resolved_key):
            changes_applied.append({"action": "cleared_hevy_cache", "key": resolved_key})
    except Exception:
        pass

    return {
        "status": "applied",
        "ifit_workout_id": ifit_workout_id,
        "original_exercise_count": original_count,
        "updated_exercise_count": len(exercises),
        "changes": changes_applied,
        "hint": (
            "Exercise data updated. The next time this iFit workout is "
            "converted to a Hevy routine, it will use the corrected exercises."
        ),
    }


# ---------------------------------------------------------------------------
# Routine weight recommendations
# ---------------------------------------------------------------------------

_COMPOUND_EXERCISES = {
    "squat", "deadlift", "bench press", "overhead press", "row",
    "romanian deadlift", "hip thrust", "lunge", "clean", "snatch",
}


def _is_compound(name: str) -> bool:
    lower = name.lower()
    return any(c in lower for c in _COMPOUND_EXERCISES)


def _analyse_exercise_history(
    rows: list[dict],
) -> dict:
    """Analyse sorted (oldest-first) sets for one exercise.

    Returns trend, last session stats, and best working weight."""
    if not rows:
        return {"trend": "new", "sessions": []}

    sessions: list[dict] = []
    current_day = ""
    for r in rows:
        day = str(r["time"])[:10]
        wt = float(r.get("weight_kg") or 0)
        reps = int(r.get("reps") or 0)
        if r.get("set_type", "normal") != "normal":
            continue
        if day != current_day:
            current_day = day
            sessions.append({"date": day, "sets": []})
        sessions[-1]["sets"].append({"weight_kg": wt, "reps": reps})

    if not sessions:
        return {"trend": "new", "sessions": []}

    def _session_best_weight(s: dict) -> float:
        return max((st["weight_kg"] for st in s["sets"] if st["weight_kg"] > 0), default=0)

    def _session_avg_reps(s: dict) -> float:
        working = [st["reps"] for st in s["sets"] if st["weight_kg"] > 0 and st["reps"] > 0]
        return sum(working) / len(working) if working else 0

    # Trend: compare first half average to second half average
    best_weights = [_session_best_weight(s) for s in sessions if _session_best_weight(s) > 0]
    if len(best_weights) >= 3:
        mid = len(best_weights) // 2
        first_half = sum(best_weights[:mid]) / mid
        second_half = sum(best_weights[mid:]) / (len(best_weights) - mid)
        if second_half > first_half * 1.03:
            trend = "progressing"
        elif second_half < first_half * 0.97:
            trend = "declining"
        else:
            trend = "plateau"
    elif len(best_weights) == 2:
        trend = "progressing" if best_weights[1] > best_weights[0] else "plateau"
    else:
        trend = "new"

    last = sessions[-1]
    last_best = _session_best_weight(last)
    last_avg_reps = _session_avg_reps(last)
    last_set_count = len([s for s in last["sets"] if s["weight_kg"] > 0])

    all_best = max(best_weights) if best_weights else 0

    return {
        "trend": trend,
        "session_count": len(sessions),
        "last_date": last["date"],
        "last_weight_kg": last_best,
        "last_avg_reps": round(last_avg_reps, 1),
        "last_set_count": last_set_count,
        "all_time_best_kg": all_best,
        "sessions": sessions,
    }


def _recommend_weight(
    analysis: dict,
    target_intensity: str,
    muscle_group: str,
    cardio_leg_stress: float,
    is_compound: bool,
) -> dict:
    """Given exercise history analysis and athlete state, recommend weight/reps."""
    trend = analysis.get("trend", "new")
    last_wt = float(analysis.get("last_weight_kg", 0))
    last_reps = analysis.get("last_avg_reps", 0)
    last_sets = analysis.get("last_set_count", 3)
    best_wt = float(analysis.get("all_time_best_kg", 0))

    # Fatigue adjustments
    fatigue_factor = 1.0
    fatigue_notes = []

    if target_intensity == "easy":
        fatigue_factor *= 0.85
        fatigue_notes.append("deload day (low readiness)")
    elif target_intensity == "moderate":
        fatigue_factor *= 0.95
        fatigue_notes.append("moderate readiness")

    lower_groups = {"quadriceps", "hamstrings", "glutes", "calves", "lower body",
                    "lower-body", "legs", "lower"}
    if muscle_group and muscle_group.lower() in lower_groups and cardio_leg_stress >= 30:
        if cardio_leg_stress >= 60:
            fatigue_factor *= 0.85
            fatigue_notes.append(f"legs fatigued from cardio (stress {cardio_leg_stress:.0f})")
        else:
            fatigue_factor *= 0.92
            fatigue_notes.append(f"moderate cardio leg load (stress {cardio_leg_stress:.0f})")

    if trend == "new" or last_wt == 0:
        return {
            "weight_kg": None,
            "reps": 10 if not is_compound else 8,
            "sets": 3,
            "strategy": "start_light",
            "reasoning": (
                "No history for this exercise. Start with a comfortable weight "
                "where you can complete all reps with good form. "
                "Track it in Hevy so I can make better recommendations next time."
            ),
        }

    # Progressive overload logic
    increment = 2.5 if is_compound else 1.0  # kg
    rec_reps = round(last_reps) if last_reps else (6 if is_compound else 12)
    rec_sets = last_sets if last_sets > 0 else 3

    if trend == "progressing" and target_intensity != "easy":
        if last_reps >= (8 if is_compound else 14):
            rec_wt = round((last_wt + increment) * fatigue_factor * 2) / 2
            strategy = "increase_weight"
            reasoning = (
                f"You've been progressing well and hit {last_reps:.0f} reps at "
                f"{last_wt}kg last time. Ready for a small weight bump."
            )
        else:
            rec_wt = round(last_wt * fatigue_factor * 2) / 2
            rec_reps = round(last_reps) + 1
            strategy = "increase_reps"
            reasoning = (
                f"Progressing well at {last_wt}kg. Add a rep before increasing "
                f"weight — aim for {rec_reps} reps this time."
            )
    elif trend == "plateau":
        if target_intensity == "easy":
            rec_wt = round(last_wt * 0.85 * 2) / 2
            strategy = "deload"
            reasoning = f"Deload session — drop to ~85% of your {last_wt}kg working weight."
        else:
            rec_wt = round(last_wt * fatigue_factor * 2) / 2
            rec_reps = round(last_reps) + 2 if last_reps < 15 else round(last_reps)
            strategy = "break_plateau"
            reasoning = (
                f"You've been at {last_wt}kg for a few sessions. "
                f"Try adding reps to build volume before bumping weight."
            )
    elif trend == "declining":
        rec_wt = round(last_wt * 0.90 * fatigue_factor * 2) / 2
        strategy = "recovery"
        reasoning = (
            f"Weight has been trending down from your best of {best_wt}kg. "
            f"Drop to {rec_wt}kg, focus on form, and rebuild."
        )
    else:
        rec_wt = round(last_wt * fatigue_factor * 2) / 2
        strategy = "maintain"
        reasoning = f"Maintain current working weight of {last_wt}kg."

    if fatigue_notes:
        reasoning += " Note: " + "; ".join(fatigue_notes) + "."

    return {
        "weight_kg": rec_wt,
        "reps": rec_reps,
        "sets": rec_sets,
        "strategy": strategy,
        "reasoning": reasoning,
    }


def get_routine_weight_recommendations(
    user_id: int,
    user_slug: str,
    hevy_api_key: str = "",
    routine_id: str = "",
    routine_name: str = "",
) -> dict:
    """Recommend weights for each exercise in a Hevy routine.

    Fetches the routine from the Hevy API, analyses the user's history for
    each exercise, and recommends weight/reps based on progression trends,
    current fatigue (TSB, cardio leg stress), and muscle freshness."""
    import httpx

    if not hevy_api_key:
        return {"error": "Hevy API key required. Configure it in your integrations."}

    headers = {"api-key": hevy_api_key, "Accept": "application/json"}

    # Fetch routines from Hevy
    try:
        if routine_id:
            r = httpx.get(
                f"https://api.hevyapp.com/v1/routines/{routine_id}",
                headers=headers, timeout=15,
            )
            r.raise_for_status()
            routine = r.json().get("routine", r.json())
        else:
            routines = []
            page = 1
            while True:
                r = httpx.get(
                    "https://api.hevyapp.com/v1/routines",
                    headers=headers, params={"page": page, "pageSize": 10},
                    timeout=15,
                )
                r.raise_for_status()
                data = r.json()
                routines.extend(data.get("routines", []))
                if page >= data.get("page_count", 1):
                    break
                page += 1
            if not routines:
                return {"error": "No routines found in your Hevy account."}

            if routine_name:
                name_lower = routine_name.lower()
                matched = [rt for rt in routines
                           if name_lower in rt.get("title", "").lower()]
                if not matched:
                    return {
                        "error": f"No routine matching '{routine_name}'.",
                        "available_routines": [
                            {"id": rt["id"], "title": rt.get("title", "")}
                            for rt in routines
                        ],
                    }
                routine = matched[0]
            else:
                return {
                    "available_routines": [
                        {"id": rt["id"], "title": rt.get("title", "")}
                        for rt in routines
                    ],
                    "hint": "Specify a routine_name or routine_id to get weight recommendations.",
                }
    except httpx.HTTPStatusError as exc:
        return {"error": f"Hevy API error: {exc.response.status_code}"}
    except Exception as exc:
        return {"error": f"Failed to fetch routines: {exc}"}

    # Gather athlete state for fatigue context
    from scripts.ifit_strength_recommend import gather_athlete_state, MUSCLE_GROUP_CANONICAL
    state = gather_athlete_state(user_slug)

    # Build exercise list from routine
    exercises = routine.get("exercises", [])
    if not exercises:
        return {"error": "Routine has no exercises.", "routine": routine.get("title", "")}

    recommendations = []
    for ex in exercises:
        ex_id = ex.get("exercise_template_id", "")
        ex_title = ex.get("title") or ex.get("exercise_template_title", "Unknown")
        sets_in_routine = len(ex.get("sets", []))
        muscle_group = ex.get("muscle_group", "")

        # Query exercise history from DB (last 90 days, all normal sets)
        history_rows = query(
            """SELECT time, set_number, set_type, weight_kg, reps
               FROM strength_sets
               WHERE user_id = %s AND LOWER(exercise_name) = LOWER(%s)
                 AND time >= NOW() - INTERVAL '90 days'
               ORDER BY time ASC, set_number""",
            (user_id, ex_title),
        )

        analysis = _analyse_exercise_history(history_rows)
        canonical_mg = MUSCLE_GROUP_CANONICAL.get(
            (muscle_group or "").lower(), muscle_group or ""
        )

        rec = _recommend_weight(
            analysis,
            target_intensity=state.target_intensity,
            muscle_group=canonical_mg,
            cardio_leg_stress=state.cardio_leg_stress,
            is_compound=_is_compound(ex_title),
        )

        recommendations.append({
            "exercise": ex_title,
            "muscle_group": muscle_group,
            "recommended_weight_kg": rec["weight_kg"],
            "recommended_reps": rec["reps"],
            "recommended_sets": rec.get("sets", sets_in_routine or 3),
            "strategy": rec["strategy"],
            "reasoning": rec["reasoning"],
            "history": {
                "trend": analysis["trend"],
                "sessions_tracked": analysis.get("session_count", 0),
                "last_date": analysis.get("last_date"),
                "last_weight_kg": analysis.get("last_weight_kg"),
                "all_time_best_kg": analysis.get("all_time_best_kg"),
            },
        })

    return {
        "routine_title": routine.get("title", ""),
        "routine_id": routine.get("id", ""),
        "athlete_state": {
            "target_intensity": state.target_intensity,
            "tsb": round(state.tsb, 1),
            "form_status": state.form_status,
            "cardio_leg_stress": round(state.cardio_leg_stress, 1),
        },
        "exercise_count": len(recommendations),
        "recommendations": recommendations,
        "instructions": (
            "Present each exercise with its recommended weight, reps, and sets. "
            "Highlight the strategy (increase_weight, increase_reps, deload, etc.) "
            "and explain the reasoning. If an exercise has no history, encourage "
            "the user to start light and track it."
        ),
    }


# =========================================================================
# Telegram linking
# =========================================================================

def generate_telegram_link_code(user_id: int) -> dict:
    """Generate a one-time code for linking a Telegram account.

    The user sends ``/start <CODE>`` to the Telegram bot within 10 minutes
    to complete the link.
    """
    from scripts.telegram_link import generate_link_code

    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "")
    code = generate_link_code(user_id)
    result: dict = {"code": code, "expires_in_minutes": 10}
    if bot_username:
        result["bot_username"] = bot_username
        result["instructions"] = (
            f"Send /start {code} to @{bot_username} on Telegram within 10 minutes."
        )
    else:
        result["instructions"] = (
            f"Send /start {code} to the Health Coach Telegram bot within 10 minutes."
        )
    return result


# =========================================================================
# Knowledge base (RAG)
# =========================================================================

def search_knowledge_base(user_id: int, query: str, top_k: int = 5) -> dict:
    """Search uploaded fitness books and documents for relevant passages."""
    from scripts.knowledge_store import search_knowledge

    results = search_knowledge(query, user_id=user_id, top_k=top_k)
    if not results:
        return {"results": [], "message": "No matching passages found in the knowledge base."}

    passages = []
    for r in results:
        passages.append({
            "content": r["content"],
            "source": r["title"] or r["filename"],
            "page": r["page_number"],
            "similarity": round(r["similarity"], 3),
        })

    return {"results": passages}


def list_knowledge_documents(user_id: int) -> dict:
    """List all fitness books and documents in the knowledge base."""
    from scripts.knowledge_store import list_documents

    docs = list_documents(user_id=user_id)
    if not docs:
        return {"documents": [], "message": "No documents in the knowledge base yet."}

    return {
        "documents": [
            {
                "id": d["id"],
                "filename": d["filename"],
                "title": d["title"],
                "page_count": d["page_count"],
                "chunk_count": d["chunk_count"],
                "scope": "global" if d["user_id"] is None else "personal",
                "created_at": str(d["created_at"]),
            }
            for d in docs
        ]
    }


def delete_knowledge_document(user_id: int, document_id: int) -> dict:
    """Remove a document from the knowledge base."""
    from scripts.knowledge_store import delete_document

    return delete_document(document_id)

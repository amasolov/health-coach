"""
Comprehensive fitness assessment from Garmin Connect and Hevy data.

Pulls 6 months of historical data (activities, body composition, vitals,
strength workouts) and generates a structured assessment covering training
volume, intensity distribution, endurance metrics, body composition trends,
vitals trends, and strength analysis. Also auto-populates the athlete
profile with data from Garmin APIs.

This is designed as the first-contact onboarding tool -- "show the user
what we already know, then ask smart questions."
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import httpx
from garminconnect import Garmin

from scripts.garmin_fetch import (
    FIELD_HINTS,
    _safe_call,
    fetch_garmin_profile,
    merge_into_athlete_yaml,
)

logger = logging.getLogger(__name__)

HEVY_BASE = "https://api.hevyapp.com"


# ---------------------------------------------------------------------------
# Hevy API client
# ---------------------------------------------------------------------------

def _fetch_hevy_workouts(
    api_key: str, since_date: str, page_size: int = 10
) -> list[dict]:
    """Fetch all Hevy workouts since a date. Paginates automatically."""
    workouts: list[dict] = []
    page = 1
    with httpx.Client(
        base_url=HEVY_BASE,
        headers={"api-key": api_key, "Accept": "application/json"},
        timeout=30.0,
    ) as client:
        while True:
            try:
                resp = client.get(
                    "/v1/workouts",
                    params={"page": page, "pageSize": page_size},
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                logger.warning("Hevy API error on page %d: %s", page, e)
                break

            data = resp.json()
            page_workouts = data.get("workouts", data.get("data", []))
            if not page_workouts:
                break

            for w in page_workouts:
                start = w.get("start_time") or w.get("created_at") or ""
                if start[:10] < since_date:
                    return workouts
                workouts.append(w)

            page_count = data.get("page_count", data.get("pageCount"))
            if page_count is not None and page >= page_count:
                break
            if len(page_workouts) < page_size:
                break

            page += 1

    return workouts


# ---------------------------------------------------------------------------
# Garmin data fetching (bulk historical)
# ---------------------------------------------------------------------------

def _fetch_activities(client: Garmin, start: str, end: str) -> list[dict]:
    """Fetch all activities in a date range."""
    return _safe_call(client.get_activities_by_date, start, end) or []


def _fetch_body_comp_series(client: Garmin, start: str, end: str) -> list[dict]:
    """Fetch body composition readings over a date range."""
    data = _safe_call(client.get_body_composition, start, end)
    if not data:
        return []
    daily = (
        data.get("dateWeightList")
        or data.get("dailyWeightSummaries")
        or []
    )
    return daily if isinstance(daily, list) else []


def _fetch_resting_hr_samples(
    client: Garmin, start_date: date, end_date: date, sample_days: int = 12
) -> list[dict]:
    """Sample resting HR at intervals across the period."""
    span = (end_date - start_date).days
    step = max(span // sample_days, 1)
    samples = []
    d = start_date
    while d <= end_date:
        data = _safe_call(client.get_heart_rates, d.isoformat())
        if data:
            rhr = data.get("restingHeartRate")
            if rhr and rhr > 0:
                samples.append({"date": d.isoformat(), "resting_hr": int(rhr)})
        d += timedelta(days=step)
    return samples


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def _analyse_training_overview(activities: list[dict], lookback_days: int) -> dict:
    """Compute volume, frequency, consistency, sport distribution."""
    if not activities:
        return {
            "total_activities": 0,
            "total_hours": 0,
            "total_distance_km": 0,
            "avg_weekly_hours": 0,
            "avg_weekly_sessions": 0,
            "sport_distribution": {},
            "consistency_score": 0,
            "longest_gap_days": 0,
        }

    total_secs = 0
    total_dist = 0.0
    sport_counts: dict[str, int] = defaultdict(int)
    sport_hours: dict[str, float] = defaultdict(float)
    activity_dates: list[str] = []

    for act in activities:
        dur = act.get("duration") or act.get("duration_s") or act.get("movingDuration") or 0
        if isinstance(dur, str):
            dur = 0
        total_secs += dur

        dist = act.get("distance") or act.get("distance_m") or 0
        if isinstance(dist, str):
            dist = 0
        total_dist += dist

        sport = (
            act.get("activityType", {}).get("typeKey", "")
            if isinstance(act.get("activityType"), dict)
            else str(act.get("activityType", act.get("activity_type", "other")))
        ).lower()

        sport_counts[sport] += 1
        sport_hours[sport] += dur / 3600

        ts = act.get("startTimeLocal") or act.get("startTimeGMT") or act.get("time", "")
        if ts:
            activity_dates.append(str(ts)[:10])

    total_hours = round(total_secs / 3600, 1)
    weeks = max(lookback_days / 7, 1)
    total_acts = len(activities)

    # Sport distribution as percentages
    sport_dist = {}
    for sport, count in sorted(sport_counts.items(), key=lambda x: -x[1]):
        sport_dist[sport] = {
            "sessions": count,
            "pct": round(count / total_acts * 100, 1),
            "hours": round(sport_hours.get(sport, 0), 1),
        }

    # Consistency: fraction of weeks with >= 3 sessions
    if activity_dates:
        activity_dates.sort()
        week_counts: dict[str, int] = defaultdict(int)
        for d in activity_dates:
            try:
                dt = date.fromisoformat(d)
                week_key = dt.isocalendar()[:2]
                week_counts[str(week_key)] += 1
            except ValueError:
                pass
        total_weeks_in_range = max(int(weeks), 1)
        active_weeks = sum(1 for c in week_counts.values() if c >= 3)
        consistency = round(active_weeks / total_weeks_in_range * 100, 1)
    else:
        consistency = 0.0

    # Longest gap
    longest_gap = 0
    if len(activity_dates) > 1:
        sorted_dates = sorted(set(activity_dates))
        for i in range(1, len(sorted_dates)):
            try:
                d1 = date.fromisoformat(sorted_dates[i - 1])
                d2 = date.fromisoformat(sorted_dates[i])
                gap = (d2 - d1).days
                if gap > longest_gap:
                    longest_gap = gap
            except ValueError:
                pass

    # Convert distance: Garmin returns meters
    total_km = round(total_dist / 1000, 1) if total_dist > 100 else round(total_dist, 1)

    return {
        "total_activities": total_acts,
        "total_hours": total_hours,
        "total_distance_km": total_km,
        "avg_weekly_hours": round(total_hours / weeks, 1),
        "avg_weekly_sessions": round(total_acts / weeks, 1),
        "sport_distribution": sport_dist,
        "consistency_score": consistency,
        "longest_gap_days": longest_gap,
    }


def _analyse_endurance(activities: list[dict], profile_data: dict) -> dict:
    """Compute endurance-specific metrics from activities."""
    runs = []
    rides = []

    for act in activities:
        sport = (
            act.get("activityType", {}).get("typeKey", "")
            if isinstance(act.get("activityType"), dict)
            else str(act.get("activityType", ""))
        ).lower()
        if "run" in sport or "trail" in sport:
            runs.append(act)
        elif "cycl" in sport or "bik" in sport or "virtual_ride" in sport:
            rides.append(act)

    result: dict[str, Any] = {}

    # VO2max from profile fetch
    vo2 = (
        profile_data.get("fetched", {})
        .get("thresholds", {})
        .get("running", {})
        .get("vo2max_garmin")
    )
    if vo2:
        result["vo2max"] = vo2

    # Running averages
    if runs:
        avg_hr = _avg([r.get("averageHR") or r.get("avg_hr") for r in runs])
        avg_pace = _avg([r.get("averageSpeed") for r in runs])
        avg_power = _avg([r.get("avgPower") or r.get("avg_power") for r in runs])
        max_hr_seen = max((r.get("maxHR") or r.get("max_hr") or 0) for r in runs)

        result["running"] = {
            "total_sessions": len(runs),
            "avg_hr": round(avg_hr) if avg_hr else None,
            "avg_power_w": round(avg_power) if avg_power else None,
            "max_hr_seen": max_hr_seen if max_hr_seen > 100 else None,
        }
        if avg_pace and avg_pace > 0:
            pace_min_km = (1000 / avg_pace) / 60
            result["running"]["avg_pace_min_km"] = round(pace_min_km, 2)

        # Fastest efforts (potential race paces)
        fastest = sorted(
            [r for r in runs if (r.get("distance") or 0) > 3000],
            key=lambda r: (r.get("averageSpeed") or 0),
            reverse=True,
        )[:3]
        if fastest:
            result["running"]["fastest_efforts"] = [
                {
                    "date": str(r.get("startTimeLocal", ""))[:10],
                    "distance_km": round((r.get("distance") or 0) / 1000, 1),
                    "pace_min_km": round((1000 / r["averageSpeed"]) / 60, 2)
                    if r.get("averageSpeed") and r["averageSpeed"] > 0
                    else None,
                    "avg_hr": r.get("averageHR"),
                }
                for r in fastest
            ]

    # Cycling averages
    if rides:
        avg_power_bike = _avg([r.get("avgPower") or r.get("avg_power") for r in rides])
        avg_hr_bike = _avg([r.get("averageHR") or r.get("avg_hr") for r in rides])

        result["cycling"] = {
            "total_sessions": len(rides),
            "avg_power_w": round(avg_power_bike) if avg_power_bike else None,
            "avg_hr": round(avg_hr_bike) if avg_hr_bike else None,
        }

    # Estimated CTL (rough) from TSS or duration-based load
    daily_load = _estimate_daily_tss(activities)
    if daily_load:
        ctl = _compute_ctl(daily_load)
        result["estimated_ctl"] = round(ctl, 1)

    return result


def _analyse_intensity(activities: list[dict], lthr: int | None) -> dict:
    """Estimate time-in-zone distribution from activity average HR."""
    if not lthr or lthr < 100:
        return {"note": "LTHR not available -- cannot compute zone distribution"}

    zone_time: dict[str, float] = {
        "z1_recovery": 0, "z2_aerobic": 0, "z3_tempo": 0,
        "z4_threshold": 0, "z5_vo2max": 0,
    }
    total_time = 0

    for act in activities:
        avg_hr = act.get("averageHR") or act.get("avg_hr")
        dur = act.get("duration") or act.get("duration_s") or act.get("movingDuration") or 0
        if not avg_hr or not dur or isinstance(dur, str):
            continue

        pct = avg_hr / lthr * 100
        total_time += dur

        if pct < 68:
            zone_time["z1_recovery"] += dur
        elif pct < 84:
            zone_time["z2_aerobic"] += dur
        elif pct < 95:
            zone_time["z3_tempo"] += dur
        elif pct < 105:
            zone_time["z4_threshold"] += dur
        else:
            zone_time["z5_vo2max"] += dur

    if total_time == 0:
        return {"note": "No HR data in activities"}

    distribution = {}
    for zone, secs in zone_time.items():
        distribution[zone] = round(secs / total_time * 100, 1)

    easy_pct = distribution.get("z1_recovery", 0) + distribution.get("z2_aerobic", 0)
    hard_pct = distribution.get("z4_threshold", 0) + distribution.get("z5_vo2max", 0)
    moderate_pct = distribution.get("z3_tempo", 0)

    if easy_pct >= 75 and hard_pct >= 10:
        assessment = "Well-polarized (good 80/20 distribution)"
    elif moderate_pct >= 30:
        assessment = "Too much zone 3 -- risk of being stuck in the 'grey zone'"
    elif easy_pct >= 85:
        assessment = "Predominantly easy -- solid aerobic base, could add more intensity"
    elif hard_pct >= 40:
        assessment = "High intensity ratio -- recovery and injury risk concern"
    else:
        assessment = "Mixed intensity distribution"

    return {
        "zone_distribution_pct": distribution,
        "easy_pct": round(easy_pct, 1),
        "moderate_pct": round(moderate_pct, 1),
        "hard_pct": round(hard_pct, 1),
        "polarization_assessment": assessment,
    }


def _analyse_body_comp(readings: list[dict]) -> dict:
    """Compute body composition trends from Garmin scale readings."""
    if not readings:
        return {
            "data_points": 0,
            "note": "No body composition data. Start using your Garmin smart scale to track.",
        }

    weights = []
    fat_pcts = []

    for r in readings:
        w = r.get("weight")
        if w and w > 0:
            weights.append({"date": str(r.get("calendarDate", ""))[:10], "kg": round(w / 1000, 1)})
        bf = r.get("bodyFat")
        if bf and bf > 0:
            fat_pcts.append({"date": str(r.get("calendarDate", ""))[:10], "pct": round(bf, 1)})

    result: dict[str, Any] = {"data_points": len(weights)}

    if weights:
        result["current_weight_kg"] = weights[-1]["kg"]
        result["first_weight_kg"] = weights[0]["kg"]
        diff = weights[-1]["kg"] - weights[0]["kg"]
        if abs(diff) < 0.5:
            result["weight_trend"] = "stable"
        elif diff > 0:
            result["weight_trend"] = f"gaining (+{diff:.1f} kg over period)"
        else:
            result["weight_trend"] = f"losing ({diff:.1f} kg over period)"

    if fat_pcts:
        result["current_body_fat_pct"] = fat_pcts[-1]["pct"]
        result["first_body_fat_pct"] = fat_pcts[0]["pct"]
        diff = fat_pcts[-1]["pct"] - fat_pcts[0]["pct"]
        if abs(diff) < 0.5:
            result["body_fat_trend"] = "stable"
        elif diff > 0:
            result["body_fat_trend"] = f"increasing (+{diff:.1f}%)"
        else:
            result["body_fat_trend"] = f"decreasing ({diff:.1f}%)"

    return result


def _analyse_vitals(hr_samples: list[dict]) -> dict:
    """Compute resting HR trend from sampled data."""
    if not hr_samples:
        return {"note": "No resting HR data available"}

    values = [s["resting_hr"] for s in hr_samples]
    current = values[-1]
    earliest = values[0]

    diff = current - earliest
    if abs(diff) <= 2:
        trend = "stable"
    elif diff < 0:
        trend = f"improving (dropped {abs(diff)} bpm)"
    else:
        trend = f"rising (+{diff} bpm -- may indicate fatigue or detraining)"

    return {
        "current_resting_hr": current,
        "earliest_resting_hr": earliest,
        "resting_hr_trend": trend,
        "samples": len(hr_samples),
    }


def _analyse_strength(workouts: list[dict]) -> dict:
    """Analyse Hevy strength workout history."""
    if not workouts:
        return {"total_sessions": 0, "note": "No Hevy workout data"}

    weeks = set()
    exercise_data: dict[str, dict] = defaultdict(lambda: {"count": 0, "max_weight": 0, "muscle_group": ""})
    muscle_groups: dict[str, int] = defaultdict(int)
    weekly_volume: dict[str, float] = defaultdict(float)

    for w in workouts:
        start = w.get("start_time") or w.get("created_at") or ""
        week_key = start[:10] if start else ""
        if week_key:
            try:
                dt = date.fromisoformat(week_key)
                weeks.add(dt.isocalendar()[:2])
            except ValueError:
                pass

        exercises = w.get("exercises", [])
        for ex in exercises:
            name = ex.get("title") or ex.get("name") or "unknown"
            muscle = ex.get("muscle_group") or ex.get("superset_id") or ""
            exercise_data[name]["count"] += 1
            exercise_data[name]["muscle_group"] = muscle

            for s in ex.get("sets", []):
                weight = s.get("weight_kg") or 0
                reps = s.get("reps") or 0
                if weight > exercise_data[name]["max_weight"]:
                    exercise_data[name]["max_weight"] = weight
                if week_key:
                    weekly_volume[week_key[:7]] += weight * reps

            if muscle:
                muscle_groups[muscle] += 1

    total_sessions = len(workouts)
    total_weeks = len(weeks) if weeks else 1

    top_exercises = sorted(exercise_data.items(), key=lambda x: -x[1]["count"])[:8]
    top_ex_list = [
        {
            "exercise": name,
            "sessions": d["count"],
            "max_weight_kg": d["max_weight"] if d["max_weight"] > 0 else None,
            "muscle_group": d["muscle_group"] or None,
        }
        for name, d in top_exercises
    ]

    volume_values = list(weekly_volume.values())
    if len(volume_values) >= 2:
        first_half = sum(volume_values[: len(volume_values) // 2])
        second_half = sum(volume_values[len(volume_values) // 2 :])
        if first_half > 0:
            change = (second_half - first_half) / first_half * 100
            if abs(change) < 10:
                vol_trend = "stable"
            elif change > 0:
                vol_trend = f"increasing (+{change:.0f}%)"
            else:
                vol_trend = f"decreasing ({change:.0f}%)"
        else:
            vol_trend = "increasing" if second_half > 0 else "stable"
    else:
        vol_trend = "insufficient data"

    return {
        "total_sessions": total_sessions,
        "avg_sessions_per_week": round(total_sessions / total_weeks, 1),
        "top_exercises": top_ex_list,
        "muscle_group_coverage": dict(muscle_groups),
        "volume_trend": vol_trend,
    }


def _generate_recommendations(
    overview: dict,
    endurance: dict,
    intensity: dict,
    body_comp: dict,
    vitals: dict,
    strength: dict,
    goals: dict | None = None,
) -> list[str]:
    """Generate data-driven recommendations based on the assessment and user goals."""
    recs = []
    goals = goals or {}

    primary = goals.get("primary_goal", "")
    avail_hrs = goals.get("available_hours_per_week")
    experience = goals.get("experience_level", "")
    preferred = goals.get("preferred_sports", [])
    constraints = goals.get("constraints", [])
    secondary = goals.get("secondary_goals", [])

    is_ultra = any(
        kw in primary.lower()
        for kw in ("utmb", "ultra", "trail", "100k", "100mi", "50k")
    )
    is_marathon = any(
        kw in primary.lower()
        for kw in ("marathon", "42k")
    ) and not is_ultra
    wants_body_comp = any(
        kw in str(secondary).lower()
        for kw in ("body composition", "lose weight", "lean", "fat")
    )

    # Goal context
    if primary:
        recs.append(f"Goal: {primary}. All recommendations below are shaped by this.")

    # Training volume vs goal
    avg_hrs = overview.get("avg_weekly_hours", 0)
    if avg_hrs == 0:
        recs.append(
            "No training data found in the last 6 months. If you've been "
            "training with a different tracker or took time off, that's fine "
            "-- we'll build from here."
        )
    elif is_ultra:
        if avg_hrs < 6:
            recs.append(
                f"Average weekly volume is {avg_hrs} hours. For ultra/trail "
                f"goals, you'll eventually need 8-12+ hours/week. Build "
                f"gradually -- no more than 10% increase per week."
            )
        elif avg_hrs < 10:
            recs.append(
                f"Average weekly volume is {avg_hrs} hours -- good foundation "
                f"for ultra training. Continue building with a focus on long "
                f"runs and back-to-back sessions."
            )
    elif is_marathon:
        if avg_hrs < 5:
            recs.append(
                f"Average weekly volume is {avg_hrs} hours. Marathon prep "
                f"typically needs 6-8 hours/week. Build gradually."
            )
    else:
        if avg_hrs < 3:
            target = f" (target: {avail_hrs})" if avail_hrs else ""
            recs.append(
                f"Average weekly volume is {avg_hrs} hours{target}. "
                f"Aim to gradually build to 5-6 hours/week for general "
                f"endurance improvement."
            )
        elif avg_hrs > 10:
            recs.append(
                f"Average weekly volume is {avg_hrs} hours -- solid load. "
                f"Focus on quality over quantity and ensure adequate recovery."
            )

    # Sport distribution vs goals
    sports = overview.get("sport_distribution", {})
    sport_types = list(sports.keys())
    if sport_types and len(sport_types) == 1:
        recs.append(
            f"All training is {sport_types[0]}. Consider adding cross-training "
            f"to reduce injury risk and build overall fitness."
        )

    running_pct = sum(
        v.get("pct", 0) for k, v in sports.items()
        if "run" in k or "trail" in k
    )
    cycling_pct = sum(
        v.get("pct", 0) for k, v in sports.items()
        if "cycl" in k or "bik" in k
    )
    strength_pct = sum(
        v.get("pct", 0) for k, v in sports.items()
        if "strength" in k or "weight" in k
    )

    if is_ultra and running_pct < 50 and overview.get("total_activities", 0) > 5:
        recs.append(
            f"Running makes up only {running_pct:.0f}% of training. For "
            f"ultra/trail goals, running should be the primary volume "
            f"contributor (60-70%), supplemented by cycling and strength."
        )

    if (is_ultra or is_marathon) and strength_pct < 10:
        recs.append(
            "Minimal strength training detected. For distance running, 2 "
            "strength sessions per week targeting legs, core, and hips will "
            "improve running economy, hill power, and injury resilience."
        )
    elif not is_ultra and not is_marathon and running_pct > 70 and strength_pct < 10:
        recs.append(
            "Training is heavily running-focused with minimal strength work. "
            "Adding 2 strength sessions per week improves overall fitness "
            "and reduces injury risk."
        )

    # Consistency
    consistency = overview.get("consistency_score", 0)
    if 0 < consistency < 50:
        recs.append(
            f"Training consistency is {consistency}% (weeks with 3+ sessions). "
            f"Consistency matters more than volume -- aim for regular sessions "
            f"even if they're shorter."
        )

    # Intensity distribution
    pol_assessment = intensity.get("polarization_assessment", "")
    if "grey zone" in pol_assessment.lower():
        recs.append(
            "Too much training in zone 3 (tempo). For better adaptations, "
            "keep ~80% of training easy (zones 1-2) and make hard sessions "
            "truly hard (zones 4-5)."
        )
    elif is_ultra and intensity.get("easy_pct", 0) < 70:
        recs.append(
            "For ultra distances, the aerobic base is everything. Aim for "
            "80%+ of training in zones 1-2. Save intensity for specific "
            "threshold and VO2max sessions."
        )

    # Body composition vs goals
    if body_comp.get("data_points", 0) == 0:
        if wants_body_comp:
            recs.append(
                "You want to track body composition but no scale data was "
                "found. Start using your Garmin smart scale daily to "
                "establish a baseline and track progress."
            )
        else:
            recs.append(
                "No body composition data. Use your Garmin smart scale to "
                "track weight and body fat trends over time."
            )

    if wants_body_comp and body_comp.get("weight_trend", "").startswith("gaining"):
        recs.append(
            "Weight is trending up while body composition improvement is a "
            "goal. Focus on a slight caloric deficit with adequate protein "
            "(1.6-2.0g/kg) to support training while losing fat."
        )

    # Vitals
    if vitals.get("resting_hr_trend", "").startswith("rising"):
        recs.append(
            "Resting heart rate is trending up -- this can indicate "
            "accumulated fatigue, poor sleep, or illness. Monitor closely."
        )

    # Strength vs goals
    if strength.get("total_sessions", 0) == 0 and overview.get("total_activities", 0) > 0:
        if is_ultra:
            recs.append(
                "No strength training detected. For ultra/trail running, "
                "strength is essential -- single-leg work, core stability, "
                "and heavy squats/deadlifts build the durability you need "
                "for mountain terrain."
            )
        elif primary:
            recs.append(
                "No strength training detected. Adding 2 sessions per week "
                "will improve performance and injury resilience regardless "
                "of your primary sport."
            )

    # Longest gap
    gap = overview.get("longest_gap_days", 0)
    if gap > 14:
        recs.append(
            f"Longest training gap was {gap} days. Extended breaks lose "
            f"fitness quickly -- even short sessions maintain adaptations."
        )

    # Experience-specific
    if experience == "beginner":
        recs.append(
            "As a beginner, prioritize consistency over intensity. Build "
            "the habit first, then layer in structured training."
        )

    # Constraint awareness
    for c in constraints:
        cl = c.lower()
        if "knee" in cl or "injur" in cl:
            recs.append(
                f"Noted constraint: '{c}'. Prioritize low-impact cross-"
                f"training (cycling, swimming) and strength work to "
                f"support the affected area."
            )
        elif "time" in cl or "busy" in cl or "schedule" in cl:
            recs.append(
                f"Noted constraint: '{c}'. Short, high-quality sessions "
                f"(30-45 min) with clear zone targets are more effective "
                f"than long junk miles when time is limited."
            )

    return recs


def _build_missing_data(profile_result: dict) -> list[dict]:
    """Build missing data list with importance levels."""
    importance_map = {
        "thresholds.heart_rate.max_hr": "critical",
        "thresholds.heart_rate.resting_hr": "recommended",
        "thresholds.heart_rate.lthr_run": "critical",
        "thresholds.heart_rate.lthr_bike": "optional",
        "thresholds.running.critical_power": "recommended",
        "thresholds.running.threshold_pace": "recommended",
        "thresholds.running.vo2max_garmin": "recommended",
        "thresholds.cycling.ftp": "optional",
        "body.weight_kg": "recommended",
        "body.body_fat_pct": "optional",
        "profile.date_of_birth": "recommended",
        "profile.sex": "recommended",
        "profile.height_cm": "optional",
    }

    can_skip_fields = {
        "thresholds.heart_rate.lthr_bike",
        "thresholds.cycling.ftp",
        "body.body_fat_pct",
        "profile.height_cm",
    }

    missing = profile_result.get("missing", [])
    enriched = []
    for item in missing:
        field = item["field"]
        enriched.append({
            "field": field,
            "hint": item["hint"],
            "importance": importance_map.get(field, "optional"),
            "can_skip": field in can_skip_fields,
        })

    return enriched


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _avg(values: list) -> float | None:
    """Average of non-None, positive numeric values."""
    nums = [v for v in values if v is not None and isinstance(v, (int, float)) and v > 0]
    return sum(nums) / len(nums) if nums else None


def _estimate_daily_tss(activities: list[dict]) -> dict[str, float]:
    """Estimate daily training load from activities.
    Uses TSS if available, otherwise hrTSS or duration-based estimate."""
    daily: dict[str, float] = defaultdict(float)
    for act in activities:
        ts = act.get("startTimeLocal") or act.get("startTimeGMT") or act.get("time", "")
        day = str(ts)[:10]
        if not day:
            continue

        tss = act.get("trainingStressScore") or act.get("tss")
        if tss and tss > 0:
            daily[day] += tss
            continue

        # Fallback: estimate from duration (1 hour of moderate training ~ 60 TSS)
        dur = act.get("duration") or act.get("duration_s") or act.get("movingDuration") or 0
        if isinstance(dur, str):
            dur = 0
        te = act.get("aerobicTrainingEffect") or act.get("training_effect_ae") or 3.0
        if isinstance(te, str):
            te = 3.0
        intensity_factor = te / 3.0
        estimated_tss = (dur / 3600) * 60 * intensity_factor
        daily[day] += estimated_tss

    return dict(daily)


def _compute_ctl(daily_tss: dict[str, float]) -> float:
    """Compute current CTL from daily TSS using 42-day exponential average."""
    if not daily_tss:
        return 0.0

    sorted_days = sorted(daily_tss.keys())
    start = date.fromisoformat(sorted_days[0])
    end = date.fromisoformat(sorted_days[-1])
    ctl = 0.0
    d = start
    while d <= end:
        tss = daily_tss.get(d.isoformat(), 0)
        ctl = ctl + (tss - ctl) / 42
        d += timedelta(days=1)
    return ctl


# ---------------------------------------------------------------------------
# Main assessment function
# ---------------------------------------------------------------------------

def _load_user_goals(slug: str) -> dict:
    """Load goals from athlete.yaml for a user."""
    import yaml
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "config" / "athlete.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("users", {}).get(slug, {}).get("goals", {})


def assess_fitness(
    slug: str,
    garmin_client: Garmin,
    hevy_api_key: str | None = None,
    lookback_days: int = 180,
) -> dict:
    """
    Generate a comprehensive fitness assessment.

    Returns a dict with sections: training_overview, endurance_metrics,
    intensity_analysis, body_composition, vitals, strength_summary,
    auto_profile, missing_data, recommendations, and goals.
    """
    today = date.today()
    start = (today - timedelta(days=lookback_days)).isoformat()
    end = today.isoformat()

    # Load user goals for context-aware recommendations
    goals = _load_user_goals(slug)

    # --- Fetch data ---
    logger.info("Fetching %d days of activities...", lookback_days)
    activities = _fetch_activities(garmin_client, start, end)
    logger.info("Found %d activities", len(activities))

    logger.info("Fetching body composition...")
    body_readings = _fetch_body_comp_series(garmin_client, start, end)
    logger.info("Found %d body comp readings", len(body_readings))

    logger.info("Sampling resting HR trend...")
    hr_samples = _fetch_resting_hr_samples(
        garmin_client, today - timedelta(days=lookback_days), today
    )
    logger.info("Got %d resting HR samples", len(hr_samples))

    logger.info("Fetching athlete profile from Garmin...")
    profile_result = fetch_garmin_profile(slug, garmin_client)

    # Hevy strength data
    hevy_workouts: list[dict] = []
    if hevy_api_key:
        logger.info("Fetching Hevy workout history...")
        hevy_workouts = _fetch_hevy_workouts(hevy_api_key, start)
        logger.info("Found %d Hevy workouts", len(hevy_workouts))

    # --- Analyse ---
    lthr = (
        profile_result.get("fetched", {})
        .get("thresholds", {})
        .get("heart_rate", {})
        .get("lthr_run")
    )

    training_overview = _analyse_training_overview(activities, lookback_days)
    endurance_metrics = _analyse_endurance(activities, profile_result)
    intensity_analysis = _analyse_intensity(activities, lthr)
    body_composition = _analyse_body_comp(body_readings)
    vitals = _analyse_vitals(hr_samples)
    strength_summary = _analyse_strength(hevy_workouts)
    missing_data = _build_missing_data(profile_result)

    recommendations = _generate_recommendations(
        training_overview,
        endurance_metrics,
        intensity_analysis,
        body_composition,
        vitals,
        strength_summary,
        goals=goals,
    )

    return {
        "period": f"{start} to {end} ({lookback_days} days)",
        "goals": goals or None,
        "training_overview": training_overview,
        "endurance_metrics": endurance_metrics,
        "intensity_analysis": intensity_analysis,
        "body_composition": body_composition,
        "vitals": vitals,
        "strength_summary": strength_summary,
        "auto_profile": profile_result,
        "missing_data": missing_data,
        "recommendations": recommendations,
    }

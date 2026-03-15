"""Compress tool results before they enter the LLM conversation context.

Large tool outputs (time-series data, activity lists, program schedules)
inflate the prompt token count on every subsequent round in the tool loop.
This module provides per-tool summarizers and a safety-net size limiter.

Usage — called from both chat_app.py and telegram_bot.py after
``_execute_tool`` returns and before the result is serialized into the
message list::

    from scripts.llm_result_summarizer import summarize_for_llm
    result = summarize_for_llm(fn_name, raw_result)
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from statistics import mean
from typing import Any

log = logging.getLogger(__name__)

RESULT_LOG_THRESHOLD = 8_000
RESULT_TRUNCATE_THRESHOLD = 15_000

RECENT_RECORDS_TO_KEEP = 5
RECENT_STRENGTH_SETS = 10
MAX_ACTIVITIES_FOR_LLM = 15
MAX_EXERCISE_DETAILS = 8
MAX_SCHEDULE_WEEKS = 3
MAX_SERIES_WORKOUTS = 5

# ── Per-tool summarizer registry ──────────────────────────────────────────

_SUMMARIZERS: dict[str, Any] = {}


def _register(name: str):
    def deco(fn):
        _SUMMARIZERS[name] = fn
        return fn
    return deco


# ── Public entry point ────────────────────────────────────────────────────

def summarize_for_llm(tool_name: str, result: Any) -> Any:
    """Summarize *result* for LLM context.  Returns the (possibly compressed)
    result.  The original object may be mutated for dicts."""
    summarizer = _SUMMARIZERS.get(tool_name)
    if summarizer is not None:
        result = summarizer(result)
    return _enforce_size_limit(tool_name, result)


# ── #43: Safety-net size limiter ──────────────────────────────────────────

def _enforce_size_limit(tool_name: str, result: Any) -> Any:
    try:
        serialized = json.dumps(result, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return result

    size = len(serialized)
    if size > RESULT_LOG_THRESHOLD:
        log.warning("Tool %s result is %d chars (threshold %d)",
                    tool_name, size, RESULT_LOG_THRESHOLD)

    if size <= RESULT_TRUNCATE_THRESHOLD:
        return result

    truncated = serialized[:RESULT_TRUNCATE_THRESHOLD]
    return {
        "_truncated": True,
        "_original_size": size,
        "_tool": tool_name,
        "data": truncated,
        "note": (
            f"Result truncated from {size:,} to {RESULT_TRUNCATE_THRESHOLD:,} chars. "
            "Use more specific parameters to narrow the query."
        ),
    }


# ── #40: Time-series summarizers ──────────────────────────────────────────

def _compute_numeric_stats(records: list[dict], fields: list[str]) -> dict:
    """Compute min/max/avg for numeric fields across records."""
    stats: dict[str, dict] = {}
    for field in fields:
        values = [r[field] for r in records if r.get(field) is not None
                  and isinstance(r.get(field), (int, float))]
        if not values:
            continue
        stats[field] = {
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "avg": round(mean(values), 2),
        }
    return stats


def _summarize_ts(result: Any, numeric_fields: list[str]) -> Any:
    if not isinstance(result, list) or len(result) <= RECENT_RECORDS_TO_KEEP * 2:
        return result

    recent = result[-RECENT_RECORDS_TO_KEEP:]
    older = result[:-RECENT_RECORDS_TO_KEEP]

    time_key = "time"
    return {
        "record_count": len(result),
        "date_range": {
            "from": result[0].get(time_key, ""),
            "to": result[-1].get(time_key, ""),
        },
        "summary": _compute_numeric_stats(older, numeric_fields),
        "recent": recent,
    }


@_register("get_training_load")
def _summarize_training_load(result: Any) -> Any:
    return _summarize_ts(result, ["tss", "ctl", "atl", "tsb", "ramp"])


@_register("get_body_composition")
def _summarize_body_composition(result: Any) -> Any:
    return _summarize_ts(result, ["weight_kg", "body_fat_pct", "muscle_mass_kg", "bmi"])


@_register("get_vitals")
def _summarize_vitals(result: Any) -> Any:
    return _summarize_ts(result, [
        "resting_hr", "hrv_ms", "sleep_score", "stress_avg",
        "body_battery_high", "body_battery_low", "spo2_avg",
    ])


@_register("get_strength_sessions")
def _summarize_strength_sessions(result: Any) -> Any:
    if not isinstance(result, list) or len(result) <= RECENT_STRENGTH_SETS:
        return result

    by_exercise: dict[str, list[dict]] = defaultdict(list)
    for s in result:
        by_exercise[s.get("exercise_name", "unknown")].append(s)

    exercise_summaries = []
    for name, sets in sorted(by_exercise.items(), key=lambda x: -len(x[1])):
        weights = [s["weight_kg"] for s in sets
                   if s.get("weight_kg") is not None and isinstance(s.get("weight_kg"), (int, float))]
        exercise_summaries.append({
            "exercise": name,
            "total_sets": len(sets),
            "muscle_group": sets[0].get("muscle_group", ""),
            "weight_range": f"{min(weights):.0f}-{max(weights):.0f} kg" if weights else "",
        })

    time_key = "time"
    return {
        "record_count": len(result),
        "date_range": {
            "from": result[0].get(time_key, ""),
            "to": result[-1].get(time_key, ""),
        },
        "by_exercise": exercise_summaries,
        "recent": result[-RECENT_STRENGTH_SETS:],
    }


# ── #41: Activity summarizers ─────────────────────────────────────────────

@_register("get_activities")
def _summarize_activities(result: Any) -> Any:
    if not isinstance(result, list) or len(result) <= MAX_ACTIVITIES_FOR_LLM:
        return result
    return {
        "total_count": len(result),
        "note": f"Showing most recent {MAX_ACTIVITIES_FOR_LLM} of {len(result)} activities.",
        "activities": result[-MAX_ACTIVITIES_FOR_LLM:],
    }


@_register("get_activity_detail")
def _summarize_activity_detail(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    if "raw_data" in result:
        result = {k: v for k, v in result.items() if k != "raw_data"}
    return result


@_register("get_workout_summary")
def _summarize_workout_summary(result: Any) -> Any:
    if not isinstance(result, list):
        return result
    for workout in result:
        hevy = workout.get("hevy")
        if isinstance(hevy, dict):
            details = hevy.get("exercise_details")
            if isinstance(details, list) and len(details) > MAX_EXERCISE_DETAILS:
                hevy["exercise_details"] = details[:MAX_EXERCISE_DETAILS]
                hevy["exercises_trimmed_from"] = len(details)
    return result


# ── #42: iFit program/series summarizers ──────────────────────────────────

@_register("get_ifit_program_details")
def _summarize_program_details(result: Any) -> Any:
    if not isinstance(result, dict) or "error" in result:
        return result

    schedule = result.get("schedule")
    if isinstance(schedule, list) and len(schedule) > MAX_SCHEDULE_WEEKS:
        result = dict(result)
        result["total_weeks"] = len(schedule)
        result["schedule"] = schedule[:MAX_SCHEDULE_WEEKS]
        result["schedule_note"] = (
            f"Showing first {MAX_SCHEDULE_WEEKS} of {result['total_weeks']} weeks. "
            "Ask for a specific week if needed."
        )
    elif isinstance(schedule, list):
        result = dict(result)
        result["total_weeks"] = len(schedule)

    for key in ("workout_ids", "workout_titles"):
        if key in result:
            result = dict(result) if not isinstance(result, dict) else result
            del result[key]

    return result


@_register("discover_ifit_series")
def _summarize_series_discovery(result: Any) -> Any:
    if not isinstance(result, dict) or "error" in result:
        return result

    series_list = result.get("series")
    if not isinstance(series_list, list):
        return result

    result = dict(result)
    trimmed = []
    for s in series_list:
        s = dict(s)
        workouts = s.get("workouts", [])
        if isinstance(workouts, list) and len(workouts) > MAX_SERIES_WORKOUTS:
            s["workouts"] = workouts[:MAX_SERIES_WORKOUTS]
            s["workouts_note"] = (
                f"Showing first {MAX_SERIES_WORKOUTS} of {s.get('workout_count', len(workouts))} workouts."
            )
        trimmed.append(s)
    result["series"] = trimmed
    return result

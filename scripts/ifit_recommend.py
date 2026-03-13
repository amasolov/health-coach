#!/usr/bin/env python3
"""
Recommend today's iFit workout based on recent activity.

Logic:
  1. Fetch last 14 days of activity logs
  2. Classify each workout by muscle group and type
  3. Score candidate workouts from up-next, favorites, and iFit recs
  4. Recommend what to do today, avoiding muscle overlap and promoting variety

Usage:
    python scripts/ifit_recommend.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

try:
    from scripts.ifit_auth import get_auth_headers, get_valid_token
except ImportError:
    from ifit_auth import get_auth_headers, get_valid_token
from scripts.tz import user_now, DEFAULT_TZ

GATEWAY = "https://gateway.ifit.com"
GATEWAY_CACHE = "https://gateway-cache.ifit.com"
API = "https://api.ifit.com"
SOFTWARE_NUMBER = "424992"

# Muscle group taxonomy -- maps iFit subcategories to our canonical groups
MUSCLE_GROUP_MAP = {
    "upper body": "upper",
    "arms": "upper",
    "biceps": "upper",
    "triceps": "upper",
    "shoulders": "upper",
    "chest": "upper",
    "back": "upper",
    "abs": "core",
    "core": "core",
    "lower body": "lower",
    "legs": "lower",
    "glutes": "lower",
    "total body": "total",
    "total-body": "total",
    "full body": "total",
}

WORKOUT_STYLE_MAP = {
    "endurance": "endurance",
    "tempo": "tempo",
    "hills": "hills",
    "speed": "speed",
    "intervals": "intervals",
    "hiit": "hiit",
    "weight loss": "endurance",
    "mobility and stretching": "recovery",
    "active recovery": "recovery",
    "active-recovery": "recovery",
    "stretching": "recovery",
    "recovery": "recovery",
    "flow": "recovery",
    "restore": "recovery",
    "yoga": "recovery",
    "pilates": "pilates",
    "strength": "strength",
    "time crunch": None,
    "beginner": None,
    "intermediate": None,
    "advanced": None,
}

# Recovery time in days per muscle group before training again
RECOVERY_DAYS = {
    "upper": 2,
    "lower": 2,
    "core": 1,
    "total": 2,
}


def _api_get(url: str, headers: dict) -> dict | list | None:
    try:
        r = httpx.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


_trainer_name_cache: dict[str, str] = {}


def _resolve_trainer_name(trainer_id: str, headers: dict) -> str:
    """Resolve a trainer ID to a human-readable name, with in-memory cache."""
    if not trainer_id:
        return ""
    if trainer_id in _trainer_name_cache:
        return _trainer_name_cache[trainer_id]
    data = _api_get(f"{API}/v1/trainers/{trainer_id}", headers)
    if data:
        name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
    else:
        name = ""
    _trainer_name_cache[trainer_id] = name
    return name


def classify_workout(workout_data: dict) -> dict:
    """Extract muscle groups and workout style from lycan workout data."""
    lf = workout_data.get("library_filters", [])
    categories = set()
    subcategories = set()
    for entry in lf:
        if isinstance(entry, dict):
            for cat in entry.get("categories", []):
                categories.add(cat.get("name", "").strip())
                for sc in cat.get("subcategories", []):
                    subcategories.add(sc.strip())

    muscle_groups = set()
    styles = set()

    for sc in subcategories:
        sc_lower = sc.lower()
        mg = MUSCLE_GROUP_MAP.get(sc_lower)
        if mg:
            muscle_groups.add(mg)
        style = WORKOUT_STYLE_MAP.get(sc_lower)
        if style:
            styles.add(style)

    for cat in categories:
        cat_lower = cat.lower()
        if "running" in cat_lower:
            styles.add("running")
        if "active recovery" in cat_lower:
            styles.add("recovery")
        if "yoga" in cat_lower:
            styles.add("recovery")

    wtype = workout_data.get("type", "")
    if wtype == "run" and not styles - {"strength"}:
        styles.add("running")

    difficulty = workout_data.get("difficulty", {})
    diff_rating = difficulty.get("rating", "moderate") if isinstance(difficulty, dict) else "moderate"

    meta = workout_data.get("metadata") or {}
    estimates = workout_data.get("estimates") or {}
    ratings = workout_data.get("ratings") or {}

    return {
        "muscle_groups": muscle_groups,
        "styles": styles,
        "categories": categories,
        "subcategories": subcategories,
        "difficulty": diff_rating,
        "type": wtype,
        "title": workout_data.get("title", "?"),
        "required_equipment": workout_data.get("required_equipment", []),
        "trainer_id": meta.get("trainer", ""),
        "duration_min": int(estimates.get("time", 0)) // 60,
        "rating_avg": ratings.get("average", 0),
    }


def fetch_recent_history(headers: dict, days: int = 14,
                         tz: "ZoneInfo | None" = None) -> list[dict]:
    """Fetch activity logs and enrich with workout metadata."""
    logs = _api_get(f"{API}/v1/activity_logs?perPage=30", headers) or []

    tz = tz or DEFAULT_TZ
    now = user_now(tz)
    today = now.date()
    cutoff = now - timedelta(days=days)
    recent = []

    for log in logs:
        start_ts = log.get("start", 0) / 1000
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc).astimezone(DEFAULT_TZ)
        if dt < cutoff:
            continue

        wid = log.get("workout_id", "")
        workout_meta = _api_get(f"{GATEWAY}/lycan/v1/workouts/{wid}", headers)
        classification = classify_workout(workout_meta) if workout_meta else {
            "muscle_groups": set(),
            "styles": set(),
            "categories": set(),
            "subcategories": set(),
            "difficulty": "?",
            "type": log.get("type", "?"),
            "title": "?",
            "required_equipment": [],
        }

        recent.append({
            "date": dt,
            "days_ago": (today - dt.date()).days,
            "duration_min": log.get("duration", 0) / 60000,
            "calories": log.get("summary", {}).get("total_calories", 0),
            "workout_id": wid,
            "log_type": log.get("type", "?"),
            **classification,
        })

    return sorted(recent, key=lambda x: x["date"], reverse=True)


def analyze_fatigue(history: list[dict]) -> dict:
    """Analyze what muscle groups are fatigued and when they were last hit."""
    last_trained: dict[str, float] = {}
    days_since: dict[str, float] = {}
    activity_by_day: dict[int, list] = defaultdict(list)

    for entry in history:
        d_ago = entry["days_ago"]
        activity_by_day[d_ago].append(entry)
        for mg in entry["muscle_groups"]:
            if mg not in last_trained or d_ago < last_trained[mg]:
                last_trained[mg] = d_ago

    for mg, d_ago in last_trained.items():
        days_since[mg] = d_ago

    total_workouts_3d = sum(len(v) for k, v in activity_by_day.items() if k <= 2)
    total_workouts_7d = sum(len(v) for k, v in activity_by_day.items() if k <= 6)

    ran_recently = any(
        "running" in e.get("styles", set()) for e in history if e["days_ago"] <= 2
    )
    last_run_day = None
    for e in history:
        if "running" in e.get("styles", set()):
            last_run_day = e["days_ago"]
            break

    return {
        "days_since": days_since,
        "total_3d": total_workouts_3d,
        "total_7d": total_workouts_7d,
        "ran_recently": ran_recently,
        "last_run_day": last_run_day,
        "activity_by_day": dict(activity_by_day),
    }


def fetch_candidates(headers: dict) -> list[dict]:
    """Gather workout candidates from multiple sources."""
    candidates = []

    # Source 1: Up-next (in-progress series)
    up_next = _api_get(
        f"{GATEWAY}/wolf-dashboard-service/v1/up-next"
        f"?softwareNumber={SOFTWARE_NUMBER}&limit=15"
        f"&challengeStoreEnabled=true&userType=premium",
        headers,
    ) or []
    for item in up_next:
        wid = item.get("workoutId", "")
        if not wid:
            continue
        candidates.append({
            "source": "up-next",
            "source_title": item.get("subtitle", ""),
            "workout_id": wid,
            "title": item.get("title", "?"),
            "series_progress": item.get("subtitle", ""),
        })

    # Source 2: Favorites
    favs = _api_get(
        f"{GATEWAY}/wolf-dashboard-service/v1/favorites"
        f"?challengeStoreEnabled=true&softwareNumber={SOFTWARE_NUMBER}"
        f"&page=1&pageSize=30",
        headers,
    ) or []
    for fav in favs:
        if fav.get("favoriteType") != "workout":
            continue
        candidates.append({
            "source": "favorite",
            "source_title": "",
            "workout_id": fav["id"],
            "title": fav.get("title", "?"),
        })

    # Source 3: iFit recommended
    recs = _api_get(
        f"{GATEWAY}/wolf-dashboard-service/v1/recommended-workouts"
        f"?softwareNumber={SOFTWARE_NUMBER}&limit=10",
        headers,
    ) or []
    for rec in recs:
        candidates.append({
            "source": "recommended",
            "source_title": "",
            "workout_id": rec.get("id", ""),
            "title": rec.get("title", "?"),
        })

    return candidates


def score_candidates(
    candidates: list[dict],
    fatigue: dict,
    history: list[dict],
    headers: dict,
) -> list[dict]:
    """Score each candidate based on muscle group freshness and variety."""
    seen_ids = set()
    scored = []
    recent_workout_ids = {e["workout_id"] for e in history}

    for cand in candidates:
        wid = cand["workout_id"]
        if wid in seen_ids or not wid:
            continue
        seen_ids.add(wid)

        # Skip workouts already done recently
        if wid in recent_workout_ids:
            continue

        meta = _api_get(f"{GATEWAY}/lycan/v1/workouts/{wid}", headers)
        if not meta:
            continue

        info = classify_workout(meta)
        cand.update(info)

        score = 50.0
        reasons = []

        # Muscle group freshness scoring
        for mg in info["muscle_groups"]:
            days = fatigue["days_since"].get(mg)
            recovery_needed = RECOVERY_DAYS.get(mg, 2)
            if days is None:
                score += 15
                reasons.append(f"{mg} not trained recently (+15)")
            elif days >= recovery_needed:
                bonus = min((days - recovery_needed + 1) * 5, 20)
                score += bonus
                reasons.append(f"{mg} rested {days}d (+{bonus})")
            elif days == 0:
                score -= 30
                reasons.append(f"{mg} trained today (-30)")
            else:
                penalty = (recovery_needed - days) * 15
                score -= penalty
                reasons.append(f"{mg} only {days}d ago (-{penalty})")

        # Variety bonus: if been doing lots of strength, prefer a run or recovery
        if fatigue["total_3d"] >= 3 and "recovery" in info["styles"]:
            score += 20
            reasons.append("recovery after busy 3 days (+20)")

        if fatigue["total_3d"] >= 2 and "running" in info["styles"] and not fatigue["ran_recently"]:
            score += 15
            reasons.append("run for variety (+15)")

        # Running type variety
        if "running" in info["styles"] and fatigue.get("last_run_day") is not None:
            if fatigue["last_run_day"] == 0:
                score -= 20
                reasons.append("already ran today (-20)")
            elif fatigue["last_run_day"] <= 1:
                score -= 5
                reasons.append("ran yesterday (-5)")

        # Source bonuses
        if cand["source"] == "up-next":
            score += 10
            reasons.append("in-progress series (+10)")
        elif cand["source"] == "favorite":
            score += 5
            reasons.append("favorite (+5)")

        # Easy difficulty bonus when fatigued
        if fatigue["total_3d"] >= 4 and info["difficulty"] == "easy":
            score += 10
            reasons.append("easy workout on tired week (+10)")

        cand["score"] = score
        cand["reasons"] = reasons
        tid = info.get("trainer_id", "")
        cand["trainer_name"] = _resolve_trainer_name(tid, headers)
        scored.append(cand)

    return sorted(scored, key=lambda x: x["score"], reverse=True)


def format_recommendation(ranked: list[dict], history: list[dict], fatigue: dict) -> str:
    """Format a human-readable recommendation."""
    lines = []
    lines.append("=" * 60)
    lines.append("  iFit Workout Recommendation for Today")
    lines.append("=" * 60)

    # Recent activity summary
    lines.append("\nRecent activity:")
    for entry in history[:7]:
        day_label = "today" if entry["days_ago"] == 0 else f"{entry['days_ago']}d ago"
        mgs = ", ".join(sorted(entry["muscle_groups"])) or entry["log_type"]
        styles = ", ".join(sorted(entry["styles"])) if entry["styles"] else ""
        extra = f" ({styles})" if styles else ""
        lines.append(
            f"  {day_label:8s} | {entry['title']:42s} | {mgs}{extra}"
        )

    lines.append(f"\nMuscle group status:")
    for mg in ["upper", "lower", "core", "total"]:
        days = fatigue["days_since"].get(mg)
        needed = RECOVERY_DAYS.get(mg, 2)
        if days is None:
            status = "not trained recently"
        elif days >= needed:
            status = f"rested ({days}d ago) - READY"
        else:
            status = f"recovering ({days}d ago, need {needed}d)"
        lines.append(f"  {mg:8s}: {status}")

    if fatigue["last_run_day"] is not None:
        lines.append(f"  {'running':8s}: last run {fatigue['last_run_day']}d ago")
    else:
        lines.append(f"  {'running':8s}: no recent runs")

    lines.append(f"\n  Workouts last 3 days: {fatigue['total_3d']}")
    lines.append(f"  Workouts last 7 days: {fatigue['total_7d']}")

    # Top recommendations
    lines.append(f"\n{'='*60}")
    lines.append("  Top recommendations:")
    lines.append(f"{'='*60}")

    for i, cand in enumerate(ranked[:5], 1):
        mgs = ", ".join(sorted(cand.get("muscle_groups", set()))) or "general"
        styles = ", ".join(sorted(cand.get("styles", set()))) or cand.get("type", "?")
        src = cand["source"]
        series_info = f" [{cand['series_progress']}]" if cand.get("series_progress") else ""
        equip = ", ".join(cand.get("required_equipment", [])) or "none"

        lines.append(f"\n  #{i} (score: {cand['score']:.0f}) {cand['title']}")
        lines.append(f"     Source: {src}{series_info}")
        lines.append(f"     Focus: {mgs} | Style: {styles} | Difficulty: {cand.get('difficulty', '?')}")
        lines.append(f"     Equipment: {equip}")
        for reason in cand.get("reasons", []):
            lines.append(f"       - {reason}")

    if not ranked:
        lines.append("\n  No suitable workouts found. Consider a rest day!")

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    print("Fetching iFit data...\n")

    headers = get_auth_headers()

    print("  Loading recent history...")
    history = fetch_recent_history(headers, days=14)
    print(f"  Found {len(history)} workouts in last 14 days")

    fatigue = analyze_fatigue(history)

    print("  Gathering workout candidates...")
    candidates = fetch_candidates(headers)
    print(f"  Found {len(candidates)} candidates")

    print("  Scoring candidates (fetching metadata)...")
    ranked = score_candidates(candidates, fatigue, history, headers)
    print(f"  Scored {len(ranked)} workouts\n")

    output = format_recommendation(ranked, history, fatigue)
    print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())

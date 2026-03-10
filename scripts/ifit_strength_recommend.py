#!/usr/bin/env python3
"""
iFit strength workout recommendation engine.

Two-stage pipeline:
  Stage 1 — Filter 12K+ library workouts to ~15 candidates using metadata
            (muscle freshness, intensity match, trainer preference, goal alignment)
  Stage 2 — Fetch VTT captions for candidates, LLM-extract exact exercises,
            re-score by detailed muscle overlap, return top 3.

Optionally creates Hevy routines from recommendations.

Usage:
    python scripts/ifit_strength_recommend.py
    python scripts/ifit_strength_recommend.py --create-hevy 1
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ifit_auth import get_auth_headers
from r2_store import (
    is_configured as r2_configured,
    download_json as r2_download_json,
    download_text as r2_download_text,
    upload_json as r2_upload_json,
    upload_text as r2_upload_text,
)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".ifit_capture"
ATHLETE_PATH = ROOT / "config" / "athlete.yaml"
EXERCISE_CACHE_PATH = CACHE_DIR / "exercise_cache.json"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_MODEL = "google/gemini-2.0-flash-001"

HEVY_BASE = "https://api.hevyapp.com"

MUSCLE_GROUP_CANONICAL = {
    "upper body": "upper",
    "upper-body": "upper",
    "arms": "upper",
    "biceps": "upper",
    "triceps": "upper",
    "shoulders": "upper",
    "chest": "upper",
    "back": "upper",
    "upper_back": "upper",
    "lats": "upper",
    "abs": "core",
    "core": "core",
    "abdominals": "core",
    "lower body": "lower",
    "lower-body": "lower",
    "legs": "lower",
    "glutes": "lower",
    "quadriceps": "lower",
    "hamstrings": "lower",
    "calves": "lower",
    "total body": "total",
    "total-body": "total",
    "full body": "total",
    "full_body": "total",
}

RECOVERY_DAYS = {
    "upper": 2,
    "lower": 2,
    "core": 1,
    "total": 2,
}

# Cardio activities stress specific muscle groups. Each entry maps an activity
# type keyword to the canonical groups it loads and a weight factor (0-1) that
# scales TSS into an equivalent "virtual volume" for that muscle group.
# A factor of 1.0 means the full TSS is treated as leg stress; 0.3 means 30%.
CARDIO_MUSCLE_STRESS: dict[str, dict[str, float]] = {
    "running":  {"lower": 1.0, "core": 0.2},
    "cycling":  {"lower": 0.7, "core": 0.15},
    "walking":  {"lower": 0.3},
    "hiking":   {"lower": 0.5, "core": 0.15},
    "climbing": {"upper": 0.6, "lower": 0.3, "core": 0.3},
}

# TSS-per-minute thresholds for classifying cardio intensity.
_CARDIO_INTENSITY = {"light": 0.5, "moderate": 1.0}  # >= moderate is "hard"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AthleteState:
    tsb: float = 0.0
    form_status: str = "unknown"
    hrv_trend: str = "stable"
    sleep_score: float | None = None
    body_battery: int | None = None
    muscle_load: dict[str, dict] = field(default_factory=dict)
    recent_cardio_legs: bool = False  # kept for backward compat
    cardio_leg_stress: float = 0.0    # 0-100 weighted leg fatigue from cardio
    goals: dict = field(default_factory=dict)
    ifit_prefs: dict = field(default_factory=dict)
    target_intensity: str = "moderate"


@dataclass
class Recommendation:
    rank: int
    workout_id: str
    title: str
    trainer_name: str
    duration_min: int
    difficulty: str
    rating: float
    focus: str
    subcategories: list[str]
    required_equipment: list[str]
    stage1_score: float
    stage2_score: float
    exercises: list[dict]
    reasoning: str


# ---------------------------------------------------------------------------
# YAML helper (avoid importing full health_tools to keep standalone)
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    import yaml
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Stage 0: Gather athlete state
# ---------------------------------------------------------------------------


def gather_athlete_state(user_slug: str) -> AthleteState:
    """Build a snapshot of the athlete's current readiness and muscle load."""
    from health_tools import (
        get_fitness_summary,
        get_vitals,
        get_strength_sessions,
        get_activities,
        resolve_user_id,
    )
    from scripts.athlete_store import load as _load_athlete

    user_cfg = _load_athlete(user_slug) or {}
    goals = user_cfg.get("goals", {})
    ifit_prefs = user_cfg.get("ifit", {})

    state = AthleteState(goals=goals, ifit_prefs=ifit_prefs)

    user_id = resolve_user_id(user_slug)
    if not user_id:
        print(f"  Warning: user '{user_slug}' not in DB, using defaults")
        return state

    # TSB / form
    try:
        summary = get_fitness_summary(user_id)
        state.tsb = float(summary.get("tsb_form") or 0)
        state.form_status = summary.get("form_status", "unknown")
    except Exception:
        pass

    # Vitals (last 3 days for trend)
    try:
        vitals = get_vitals(user_id, days=3)
        if vitals:
            latest = vitals[-1]
            state.sleep_score = latest.get("sleep_score")
            state.body_battery = latest.get("body_battery_high")
            hrvs = [v["hrv_ms"] for v in vitals if v.get("hrv_ms")]
            if len(hrvs) >= 2:
                state.hrv_trend = "rising" if hrvs[-1] > hrvs[0] else "falling"
    except Exception:
        pass

    # Combined muscle load: strength sets (7 days) + cardio stress (4 days)
    load_by_group: dict[str, dict] = defaultdict(
        lambda: {"volume": 0.0, "sets": 0, "last_date": ""}
    )

    # Strength sessions
    try:
        sets = get_strength_sessions(user_id, days=7)
        for s in sets:
            mg_raw = (s.get("muscle_group") or "").lower()
            mg = MUSCLE_GROUP_CANONICAL.get(mg_raw, mg_raw)
            if not mg:
                continue
            weight = float(s.get("weight_kg") or 0)
            reps = int(s.get("reps") or 0)
            load_by_group[mg]["volume"] += weight * reps
            load_by_group[mg]["sets"] += 1
            dt = s.get("time", "")
            if dt > load_by_group[mg]["last_date"]:
                load_by_group[mg]["last_date"] = dt
    except Exception:
        pass

    # Cardio muscle stress — running/cycling/hiking inject fatigue into
    # muscle_load so freshness scoring naturally penalises leg workouts
    # after strenuous cardio.
    try:
        activities = get_activities(user_id, days=4)
        for a in activities:
            atype = (a.get("activity_type") or "").lower()
            matched_key = ""
            for key in CARDIO_MUSCLE_STRESS:
                if key in atype:
                    matched_key = key
                    break
            if not matched_key:
                continue

            state.recent_cardio_legs = True

            tss = float(a.get("tss") or 0)
            dur_min = (int(a.get("duration_s") or 0)) / 60
            if tss <= 0 and dur_min > 0:
                tss = dur_min * 0.8  # conservative estimate when TSS absent

            # Recency decay: yesterday = 1.0, 2 days ago = 0.6, 3+ days = 0.3
            days_ago = _days_since(str(a.get("time", "")))
            if days_ago < 1.5:
                decay = 1.0
            elif days_ago < 2.5:
                decay = 0.6
            else:
                decay = 0.3

            stress_map = CARDIO_MUSCLE_STRESS[matched_key]
            for mg, factor in stress_map.items():
                # A TSS-100 run ≈ 5000 virtual volume for lower body,
                # comparable to a heavy leg session in weight*reps terms.
                virtual_vol = tss * factor * decay * 50
                entry = load_by_group[mg]
                entry["volume"] += virtual_vol
                entry["sets"] += 1
                dt = str(a.get("time", ""))
                if dt > entry["last_date"]:
                    entry["last_date"] = dt

            # Scalar leg stress (capped at 100) for explicit penalty scoring
            leg_factor = stress_map.get("lower", 0)
            state.cardio_leg_stress = min(
                100.0, state.cardio_leg_stress + tss * leg_factor * decay
            )
    except Exception:
        pass

    state.muscle_load = dict(load_by_group)

    # Determine target intensity
    if state.tsb < -20 or state.body_battery and state.body_battery < 25:
        state.target_intensity = "easy"
    elif state.tsb < -5 or (state.body_battery and state.body_battery < 50):
        state.target_intensity = "moderate"
    else:
        state.target_intensity = "hard"

    if state.hrv_trend == "falling" and state.target_intensity == "hard":
        state.target_intensity = "moderate"

    return state


# ---------------------------------------------------------------------------
# Stage 1: Metadata filter and score
# ---------------------------------------------------------------------------


def _days_since(iso_date: str) -> float:
    if not iso_date:
        return 999
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return (datetime.now(dt.tzinfo) - dt).total_seconds() / 86400
    except Exception:
        return 999


def _classify_workout_muscles(workout: dict) -> set[str]:
    """Map a library workout's subcategories to canonical muscle groups."""
    groups = set()
    for sc in workout.get("subcategories", []):
        canonical = MUSCLE_GROUP_CANONICAL.get(sc.lower(), "")
        if canonical:
            groups.add(canonical)
    return groups or {"total"}


def stage1_filter(
    state: AthleteState,
    library: list[dict],
    trainers: dict[str, dict],
) -> list[dict]:
    """Filter and score library workouts, return top ~18 candidates."""
    prefs = state.ifit_prefs
    avail_equip = set(e.lower() for e in prefs.get("available_equipment", []))
    dur_range = prefs.get("preferred_duration_min", [15, 50])
    min_rating = prefs.get("min_rating", 3.5)
    fav_trainer_names = set(n.lower() for n in prefs.get("favourite_trainers", []))

    # Build trainer name lookup
    trainer_names = {}
    for tid, t in trainers.items():
        trainer_names[tid] = t.get("name", "")

    # Identify favourite trainer IDs
    fav_trainer_ids = set()
    for tid, t in trainers.items():
        if t.get("name", "").lower() in fav_trainer_names:
            fav_trainer_ids.add(tid)

    is_runner = any(
        "run" in s.lower()
        for s in state.goals.get("preferred_sports", [])
    )

    candidates = []

    for w in library:
        if w.get("type") != "strength":
            continue

        # Equipment filter: user must own all required items
        req_equip = set(e.lower() for e in w.get("required_equipment", []))
        if req_equip and not req_equip.issubset(avail_equip):
            continue

        # Duration filter
        dur_sec = w.get("time_sec", 0)
        dur_min = dur_sec / 60 if dur_sec else 0
        if dur_min < dur_range[0] or dur_min > dur_range[1]:
            continue

        # Rating filter
        rating = w.get("rating_avg", 0)
        if rating and rating < min_rating:
            continue

        # --- Scoring ---
        score = 0.0
        muscles = _classify_workout_muscles(w)

        # Muscle freshness (0-30 pts)
        freshness_scores = []
        for mg in muscles:
            load = state.muscle_load.get(mg, {})
            days = _days_since(load.get("last_date", ""))
            recovery = RECOVERY_DAYS.get(mg, 2)
            if days >= recovery * 2:
                freshness_scores.append(30)
            elif days >= recovery:
                freshness_scores.append(20)
            elif days >= 1:
                freshness_scores.append(5)
            else:
                freshness_scores.append(-10)
        score += sum(freshness_scores) / max(len(freshness_scores), 1)

        # Intensity match (0-20 pts)
        difficulty = w.get("difficulty", "").lower()
        if state.target_intensity == "easy":
            score += 20 if difficulty == "easy" else (10 if difficulty == "moderate" else 0)
        elif state.target_intensity == "moderate":
            score += 20 if difficulty == "moderate" else 10
        else:
            score += 20 if difficulty in ("moderate", "strenuous") else 5

        # Trainer preference (0-15 pts)
        tid = w.get("trainer_id", "")
        if tid in fav_trainer_ids:
            score += 15
        elif tid and trainer_names.get(tid):
            score += 3  # known trainer bonus

        # Rating quality (0-10 pts)
        rating_count = w.get("rating_count", 0)
        if rating >= 4.8 and rating_count >= 20:
            score += 10
        elif rating >= 4.5 and rating_count >= 10:
            score += 7
        elif rating >= 4.0:
            score += 4

        # Goal alignment (-15 to +20 pts)
        # Uses cardio_leg_stress (0-100) for continuous penalty/boost.
        # High leg stress from recent running/cycling steers toward upper/core.
        leg_stress = state.cardio_leg_stress
        if "lower" in muscles or "total" in muscles:
            if leg_stress >= 60:
                score -= 15
            elif leg_stress >= 30:
                score -= 5
            elif leg_stress > 0:
                score += 2
            else:
                score += 8
        elif "upper" in muscles:
            if leg_stress >= 30:
                score += 20
            elif leg_stress > 0:
                score += 12
            else:
                score += 8
        elif "core" in muscles:
            if leg_stress >= 30:
                score += 15
            else:
                score += 10
        else:
            score += 8

        # Runners get an extra nudge toward complementary work
        if is_runner and "upper" in muscles:
            score += 5

        # Duration sweet spot bonus
        if 25 <= dur_min <= 40:
            score += 5

        candidates.append({
            **w,
            "trainer_name": trainer_names.get(tid, "(unknown)"),
            "duration_min": int(dur_min),
            "muscle_groups": sorted(muscles),
            "stage1_score": round(score, 1),
        })

    candidates.sort(key=lambda x: -x["stage1_score"])
    return candidates[:18]


# ---------------------------------------------------------------------------
# Exercise cache (keyed by workout ID — content never changes)
# ---------------------------------------------------------------------------


def _load_exercise_cache() -> dict[str, list[dict]]:
    if EXERCISE_CACHE_PATH.exists():
        with open(EXERCISE_CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_exercise_cache(cache: dict[str, list[dict]]) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    with open(EXERCISE_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


# ---------------------------------------------------------------------------
# Stage 2: VTT + LLM analysis
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = """\
You are a fitness expert who extracts structured exercise data from workout transcript text.

You will receive:
1. A transcript of an iFit strength training workout (trainer speaking during the video)
2. A reference list of Hevy exercises in the format: Title | ID | muscle_group | equipment

Your task: Extract ONLY the main working exercises (skip warm-up and cool-down/stretching).
For each exercise, output a JSON array of objects with these fields:

- "hevy_name": The closest matching exercise from the Hevy reference list. Use an EXACT name from the list when possible. If no good match exists, use a clear descriptive name for the exercise.
- "hevy_id": The ID from the Hevy reference list (the second column). If you used an exact name match, include the corresponding ID. If no match, use empty string.
- "muscle_group": Primary muscle group (e.g. quadriceps, chest, biceps, shoulders, lats, etc.)
- "equipment": Equipment used (e.g. "dumbbell", "barbell", "kettlebell", "bodyweight", "resistance_band", "none")
- "sets": Number of sets (integer). If the trainer says "3 rounds" or "3 sets", use that.
- "reps": Reps per set (integer or string like "30s" for timed sets).
- "weight": Weight suggestion based on what the trainer says ("heavy dumbbells", "medium dumbbells", "light dumbbells", or "bodyweight")
- "notes": Brief note about form cues or variations the trainer mentions

Rules:
- Only include exercises from the main workout, NOT warm-up or cool-down stretches
- If the trainer repeats the same exercise in multiple rounds, list it ONCE with total sets
- Prefer EXACT names and IDs from the Hevy reference list
- If the exercise has no good match in the list, still include it with a descriptive name and empty hevy_id
- For compound movements, pick the primary exercise and note the combo in notes
- Output ONLY valid JSON array, no markdown, no explanation
"""


def _fetch_vtt(workout_id: str, headers: dict) -> str | None:
    """Fetch English VTT caption text for a workout."""
    try:
        r = httpx.get(
            f"https://gateway.ifit.com/video-streaming-service/v1/workoutVideo/{workout_id}",
            headers=headers, timeout=15,
        )
        if r.status_code != 200:
            return None
        captions = r.json().get("captions", {})
        eng_url = captions.get("eng", "")
        if not eng_url:
            return None
        r2 = httpx.get(eng_url, timeout=15)
        if r2.status_code != 200:
            return None
        lines = r2.text.split("\n")
        text_lines = [
            l for l in lines
            if not l.startswith("WEBVTT") and "-->" not in l
            and l.strip() and not l.strip().isdigit()
        ]
        clean = [re.sub(r"<v [^>]*>", "", l).replace("</v>", "").strip() for l in text_lines]
        return " ".join(c for c in clean if c)
    except Exception as e:
        print(f"  VTT fetch error for {workout_id}: {e}")
        return None


def _llm_extract(
    transcript: str,
    hevy_ref: str,
    workout_title: str,
) -> list[dict]:
    """Send transcript to LLM, return structured exercise list."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return []

    user_msg = f"""## Hevy Exercise Reference List
{hevy_ref}

## Workout Transcript ({workout_title})
{transcript}

Extract all main working exercises as a JSON array. Skip warm-up and stretching/cool-down."""

    try:
        resp = httpx.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": EXTRACT_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 4000,
                "temperature": 0.1,
            },
            timeout=120,
        )
        if resp.status_code != 200:
            print(f"  LLM error: {resp.status_code}")
            return []

        content = resp.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]
        return json.loads(content.strip())
    except Exception as e:
        print(f"  LLM extract error: {e}")
        return []


def _score_exercises_vs_state(
    exercises: list[dict],
    state: AthleteState,
) -> tuple[float, str]:
    """Score an exercise list against athlete's current muscle load.
    Returns (score_adjustment, reasoning)."""
    if not exercises:
        return 0.0, "no exercises extracted"

    muscle_counts: dict[str, int] = defaultdict(int)
    for ex in exercises:
        mg = MUSCLE_GROUP_CANONICAL.get(
            (ex.get("muscle_group") or "").lower(), ""
        )
        if mg:
            muscle_counts[mg] += 1

    total_ex = sum(muscle_counts.values()) or 1
    adjustment = 0.0
    reasons = []

    for mg, count in muscle_counts.items():
        load = state.muscle_load.get(mg, {})
        days = _days_since(load.get("last_date", ""))
        proportion = count / total_ex
        recovery = RECOVERY_DAYS.get(mg, 2)

        if days < recovery and proportion > 0.4:
            penalty = -20 * proportion
            adjustment += penalty
            reasons.append(f"{mg} overloaded ({days:.0f}d ago, {count} exercises)")
        elif days >= recovery * 2:
            bonus = 15 * proportion
            adjustment += bonus
            reasons.append(f"{mg} well-rested ({days:.0f}d)")
        elif days >= recovery:
            bonus = 8 * proportion
            adjustment += bonus
            reasons.append(f"{mg} recovered ({days:.0f}d)")

    # Penalise lower body proportionally to recent cardio leg stress
    lower_proportion = muscle_counts.get("lower", 0) / total_ex
    if state.cardio_leg_stress > 0 and lower_proportion > 0.3:
        # Scale: stress 30 → -5, stress 60 → -10, stress 100 → -20
        cardio_penalty = -(state.cardio_leg_stress / 100) * 20 * lower_proportion
        adjustment += cardio_penalty
        if state.cardio_leg_stress >= 60:
            reasons.append(f"heavy lower body after intense cardio (stress {state.cardio_leg_stress:.0f})")
        elif state.cardio_leg_stress >= 30:
            reasons.append(f"lower body caution — moderate cardio leg load (stress {state.cardio_leg_stress:.0f})")

    # Boost upper/core when legs are fatigued from cardio
    upper_proportion = muscle_counts.get("upper", 0) / total_ex
    core_proportion = muscle_counts.get("core", 0) / total_ex
    if state.cardio_leg_stress >= 30 and (upper_proportion + core_proportion) > 0.5:
        boost = (state.cardio_leg_stress / 100) * 10
        adjustment += boost
        reasons.append("good complement to recent cardio — focuses on upper/core")

    return round(adjustment, 1), "; ".join(reasons) if reasons else "balanced"


def fetch_workout_exercises(
    workout_id: str,
    workout_title: str,
    ifit_headers: dict | None = None,
    hevy_ref: str | None = None,
    verbose: bool = False,
) -> dict:
    """Fetch exercises for a workout using cache layers with on-demand fallback.

    Pipeline: R2 exercise cache → local cache → R2 transcript → live VTT
    → LLM extraction → write-through to all caches.

    Returns dict with:
      exercises  – list of exercise dicts (may be empty)
      source     – 'r2_cache', 'local_cache', 'extracted', or 'none'
      transcript_available – whether a transcript exists for this workout
    """
    exercises: list[dict] | None = None
    source = "none"
    transcript_available = False

    # 1. R2 exercise cache
    if r2_configured():
        exercises = r2_download_json(f"exercises/{workout_id}.json")
        if exercises:
            source = "r2_cache"
            transcript_available = True
            if verbose:
                print(f"    {len(exercises)} exercises (R2 cached)")

    # 2. Local exercise cache
    if exercises is None:
        cache = _load_exercise_cache()
        if workout_id in cache:
            exercises = cache[workout_id]
            source = "local_cache"
            transcript_available = True
            if verbose:
                print(f"    {len(exercises)} exercises (local cached)")

    # 3. Fetch transcript and run LLM extraction
    if exercises is None:
        transcript = None
        if r2_configured():
            transcript = r2_download_text(f"transcripts/{workout_id}.txt")
            if transcript and verbose:
                print(f"    Transcript from R2 ({len(transcript)} chars)")

        if not transcript and ifit_headers:
            transcript = _fetch_vtt(workout_id, ifit_headers)
            if transcript and r2_configured():
                r2_upload_text(f"transcripts/{workout_id}.txt", transcript)

        transcript_available = bool(transcript)

        if transcript:
            if hevy_ref is None:
                hevy_ref_path = CACHE_DIR / "hevy_exercise_ref.txt"
                if hevy_ref_path.exists():
                    with open(hevy_ref_path) as f:
                        hevy_ref = f.read()

            if hevy_ref:
                exercises = _llm_extract(transcript, hevy_ref, workout_title)
                if exercises:
                    source = "extracted"
                    cache = _load_exercise_cache()
                    cache[workout_id] = exercises
                    _save_exercise_cache(cache)
                    if r2_configured():
                        r2_upload_json(f"exercises/{workout_id}.json", exercises)
                    if verbose:
                        print(f"    {len(exercises)} exercises (new — saved to R2 + local)")

    return {
        "exercises": exercises or [],
        "source": source,
        "transcript_available": transcript_available,
    }


def stage2_analyse(
    candidates: list[dict],
    state: AthleteState,
    hevy_ref: str,
    ifit_headers: dict,
) -> list[Recommendation]:
    """Deep-analyse candidates via VTT + LLM, return top 3 diverse picks."""
    cache_hits = 0
    cache_new = 0
    scored = []

    for i, c in enumerate(candidates):
        wid = c["id"]
        title = c["title"]
        print(f"  [{i+1}/{len(candidates)}] Analysing: {title}...", flush=True)

        result = fetch_workout_exercises(
            wid, title, ifit_headers, hevy_ref=hevy_ref, verbose=True,
        )
        exercises = result["exercises"]

        if not exercises:
            if result["transcript_available"]:
                print(f"    Skipping (no exercises extracted)")
            else:
                print(f"    Skipping (no captions)")
            continue

        if result["source"] == "extracted":
            cache_new += 1
            time.sleep(0.3)
        else:
            cache_hits += 1

        adj, reasoning = _score_exercises_vs_state(exercises, state)
        final_score = c["stage1_score"] + adj

        # Build focus summary from extracted exercises
        muscle_counts: dict[str, int] = defaultdict(int)
        for ex in exercises:
            mg = MUSCLE_GROUP_CANONICAL.get(
                (ex.get("muscle_group") or "").lower(), ""
            )
            if mg:
                muscle_counts[mg] += 1
        focus = ", ".join(
            f"{mg}({n})" for mg, n in
            sorted(muscle_counts.items(), key=lambda x: -x[1])[:3]
        )

        scored.append(Recommendation(
            rank=0,
            workout_id=wid,
            title=title,
            trainer_name=c.get("trainer_name", ""),
            duration_min=c.get("duration_min", 0),
            difficulty=c.get("difficulty", ""),
            rating=c.get("rating_avg", 0),
            focus=focus,
            subcategories=c.get("subcategories", []),
            required_equipment=c.get("required_equipment", []),
            stage1_score=c["stage1_score"],
            stage2_score=round(final_score, 1),
            exercises=exercises,
            reasoning=reasoning,
        ))

        print(f"    adj={adj:+.1f} | final={final_score:.1f} | {reasoning}")

    print(f"  Cache stats: {cache_hits} hits, {cache_new} new extractions")

    # Pick top 3 with diversity (different primary muscle focus)
    scored.sort(key=lambda x: -x.stage2_score)
    picks: list[Recommendation] = []
    seen_focus: set[str] = set()

    for rec in scored:
        primary_focus = rec.focus.split("(")[0].strip() if rec.focus else ""
        if primary_focus in seen_focus and len(picks) < 3:
            # Allow duplicates only if we don't have 3 yet and score is high
            if len(picks) >= 2:
                continue
        picks.append(rec)
        if primary_focus:
            seen_focus.add(primary_focus)
        if len(picks) >= 3:
            break

    for i, p in enumerate(picks):
        p.rank = i + 1

    return picks


# ---------------------------------------------------------------------------
# Hevy routine creation
# ---------------------------------------------------------------------------

R2_ROUTINE_MAP_KEY = "hevy/routine_map.json"


def _load_routine_map() -> dict:
    """Load the hevy_routine_id -> ifit mapping from R2."""
    try:
        from scripts.r2_store import is_configured, download_json
        if is_configured():
            data = download_json(R2_ROUTINE_MAP_KEY)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_routine_mapping(
    routine_id: str,
    ifit_workout_id: str,
    title: str,
    predicted_exercises: list[dict],
) -> None:
    """Persist the Hevy routine -> iFit workout mapping to R2."""
    if not routine_id:
        return
    try:
        from scripts.r2_store import is_configured, upload_json
        if not is_configured():
            return
        mapping = _load_routine_map()
        mapping[routine_id] = {
            "ifit_workout_id": ifit_workout_id,
            "title": title,
            "predicted_exercises": [
                {
                    "hevy_name": ex.get("hevy_name", ""),
                    "hevy_id": ex.get("hevy_id", ""),
                    "muscle_group": ex.get("muscle_group", ""),
                    "sets": ex.get("sets", 0),
                    "reps": ex.get("reps", 0),
                    "weight": ex.get("weight", ""),
                    "resolution": ex.get("resolution", ""),
                }
                for ex in predicted_exercises
            ],
            "created_at": datetime.now().isoformat(),
        }
        upload_json(R2_ROUTINE_MAP_KEY, mapping)
        print(f"  Saved routine mapping: {routine_id} -> iFit {ifit_workout_id}")
    except Exception as e:
        print(f"  Warning: failed to save routine mapping: {e}")


def _parse_reps_for_hevy(reps_val) -> dict:
    """Convert reps value to Hevy set fields.

    Returns dict with either {"reps": int} or {"duration_seconds": int}.
    """
    s = str(reps_val).strip().lower()

    m = re.match(r"(\d+)\s*s(?:ec)?", s)
    if m:
        return {"duration_seconds": int(m.group(1))}

    m = re.match(r"(\d+)\s*min", s)
    if m:
        return {"duration_seconds": int(m.group(1)) * 60}

    try:
        return {"reps": int(s)}
    except (ValueError, TypeError):
        return {"reps": 12}


def _find_existing_routine(
    ifit_workout_id: str, title: str, hevy_api_key: str,
) -> dict | None:
    """Check if a Hevy routine already exists for this iFit workout.

    Returns the existing routine info dict or None.
    Checks two sources: the R2 routine map (by iFit workout ID) and
    the Hevy API (by title prefix match).
    """
    expected_title = f"iFit: {title}"

    # 1) Check R2 mapping for this iFit workout ID
    mapping = _load_routine_map()
    for routine_id, entry in mapping.items():
        if entry.get("ifit_workout_id") == ifit_workout_id:
            try:
                r = httpx.get(
                    f"{HEVY_BASE}/v1/routines/{routine_id}",
                    headers={"api-key": hevy_api_key, "accept": "application/json"},
                    timeout=15,
                )
                if r.status_code == 200:
                    routine = r.json().get("routine", r.json())
                    print(f"  Found existing routine by iFit mapping: {routine_id}")
                    return {
                        "routine_id": str(routine.get("id", routine_id)),
                        "title": routine.get("title", entry.get("title", "")),
                        "exercise_count": len(routine.get("exercises", [])),
                        "source": "ifit_mapping",
                    }
            except Exception:
                pass

    # 2) Query Hevy routines list for a title match
    try:
        r = httpx.get(
            f"{HEVY_BASE}/v1/routines",
            headers={"api-key": hevy_api_key, "accept": "application/json"},
            params={"page": 1, "pageSize": 50},
            timeout=15,
        )
        if r.status_code == 200:
            for rt in r.json().get("routines", []):
                if rt.get("title", "").strip().lower() == expected_title.strip().lower():
                    rid = str(rt["id"])
                    print(f"  Found existing routine by title match: {rid}")
                    return {
                        "routine_id": rid,
                        "title": rt["title"],
                        "exercise_count": len(rt.get("exercises", [])),
                        "source": "title_match",
                    }
        else:
            print(f"  Warning: Hevy routines list returned {r.status_code} — skipping duplicate check")
    except Exception:
        pass

    return None


def _clear_resolution_cache(workout_id: str) -> None:
    """Delete the R2 resolution cache for a workout so it re-resolves fresh."""
    if not workout_id:
        return
    try:
        from scripts.r2_store import is_configured, delete as r2_delete
        if is_configured():
            r2_delete(f"hevy/resolved/{workout_id}.json")
            print(f"  Cleared stale resolution cache for {workout_id}")
    except Exception:
        pass


def _resolve_and_build_payload(
    rec: Recommendation, hevy_api_key: str,
    *, force_revalidate: bool = False,
) -> tuple[list[dict], list[str], list[dict], dict]:
    """Resolve exercises and build the Hevy routine payload.

    Returns (exercises_payload, skipped_names, resolved_exercises, resolution_summary).
    """
    from scripts.hevy_exercise_resolver import resolve_hevy_exercises

    print(f"  Resolving {len(rec.exercises)} exercises for Hevy...")
    resolved = resolve_hevy_exercises(
        rec.exercises, hevy_api_key,
        workout_id=rec.workout_id, force_revalidate=force_revalidate,
    )

    resolution_summary = {}
    for ex in resolved:
        r_type = ex.get("resolution", "unknown")
        resolution_summary[r_type] = resolution_summary.get(r_type, 0) + 1
    print(f"  Resolution: {resolution_summary}")

    exercises_payload = []
    skipped = []
    for ex in resolved:
        hevy_id = ex.get("hevy_id", "")
        if not hevy_id:
            skipped.append(ex.get("hevy_name", "unknown"))
            continue

        sets_count = int(ex.get("sets", 3))
        reps_fields = _parse_reps_for_hevy(ex.get("reps", 12))

        sets_payload = []
        for _ in range(sets_count):
            set_entry = {"type": "normal", **reps_fields}
            sets_payload.append(set_entry)

        notes = ex.get("notes", "")
        weight_hint = ex.get("weight", "")
        if weight_hint and weight_hint != "bodyweight":
            notes = f"{weight_hint}. {notes}" if notes else weight_hint

        exercises_payload.append({
            "exercise_template_id": hevy_id,
            "superset_id": None,
            "rest_seconds": 90,
            "notes": notes,
            "sets": sets_payload,
        })

    return exercises_payload, skipped, resolved, resolution_summary


def create_hevy_routine(rec: Recommendation, hevy_api_key: str) -> dict:
    """Create a Hevy routine from a recommendation.

    Checks for duplicates first (by iFit workout ID mapping and Hevy title),
    then resolves exercises to Hevy template IDs and creates the routine.
    On "invalid exercise template id" errors, clears the stale cache and retries once.
    """
    existing = _find_existing_routine(rec.workout_id, rec.title, hevy_api_key)
    if existing:
        return {
            "status": "already_exists",
            "routine_id": existing["routine_id"],
            "title": existing["title"],
            "exercise_count": existing["exercise_count"],
            "message": (
                f"A Hevy routine for this workout already exists: "
                f"\"{existing['title']}\" ({existing['exercise_count']} exercises). "
                f"No duplicate was created. Tell the user it's already in their "
                f"Hevy app and ready to use."
            ),
        }

    exercises_payload, skipped, resolved, resolution_summary = _resolve_and_build_payload(rec, hevy_api_key)

    if not exercises_payload:
        return {
            "error": "No exercises could be resolved to Hevy IDs",
            "skipped": skipped,
            "resolution": resolution_summary,
        }

    body = {
        "routine": {
            "title": f"iFit: {rec.title}",
            "folder_id": None,
            "notes": f"From iFit workout. Trainer: {rec.trainer_name}",
            "exercises": exercises_payload,
        }
    }

    r = httpx.post(
        f"{HEVY_BASE}/v1/routines",
        headers={
            "api-key": hevy_api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=body,
        timeout=30,
    )

    # Retry once on stale exercise template IDs
    if r.status_code == 400 and "invalid exercise template id" in r.text.lower():
        print(f"  Hevy rejected routine — stale exercise IDs. Clearing cache and retrying...")
        _clear_resolution_cache(rec.workout_id)
        exercises_payload, skipped, resolved, resolution_summary = _resolve_and_build_payload(
            rec, hevy_api_key, force_revalidate=True,
        )
        if not exercises_payload:
            return {
                "error": "No exercises could be resolved after cache refresh",
                "skipped": skipped,
                "resolution": resolution_summary,
            }
        body["routine"]["exercises"] = exercises_payload
        r = httpx.post(
            f"{HEVY_BASE}/v1/routines",
            headers={
                "api-key": hevy_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=body,
            timeout=30,
        )

    if r.status_code in (200, 201):
        try:
            data = r.json()
        except Exception:
            data = {}
        routine = data.get("routine", data) if data else {}
        routine_id = str(routine.get("id", ""))

        if routine_id:
            _save_routine_mapping(
                routine_id=routine_id,
                ifit_workout_id=rec.workout_id,
                title=body["routine"]["title"],
                predicted_exercises=resolved,
            )

        result = {
            "status": "created",
            "routine_id": routine_id,
            "title": body["routine"]["title"],
            "exercises_created": len(exercises_payload),
            "exercises_total": len(rec.exercises),
            "resolution": resolution_summary,
        }
        if skipped:
            result["status"] = "created_incomplete"
            result["skipped_exercises"] = skipped
            result["warning"] = (
                f"{len(skipped)} exercise(s) could not be added to the Hevy routine "
                f"because they failed to resolve: {', '.join(skipped)}. "
                f"The routine was created with {len(exercises_payload)} of "
                f"{len(rec.exercises)} exercises. Tell the user which exercises "
                f"are missing and suggest they add them manually in the Hevy app."
            )
        return result
    else:
        print(f"  Hevy routine creation failed: HTTP {r.status_code} - {r.text[:500]}")
        return {"error": f"Hevy API {r.status_code}: {r.text[:300]}"}


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def recommend(user_slug: str = "alexey") -> list[Recommendation]:
    """Run the full recommendation pipeline."""
    print("=" * 80)
    print("  iFit Strength Workout Recommendation Engine")
    print("=" * 80)

    # Load cached data
    trainers_path = CACHE_DIR / "trainers.json"
    library_path = CACHE_DIR / "library_workouts.json"
    hevy_ref_path = CACHE_DIR / "hevy_exercise_ref.txt"

    for p in (trainers_path, library_path, hevy_ref_path):
        if not p.exists():
            print(f"  ERROR: {p} not found. Run ifit_list_series.py first.")
            return []

    with open(trainers_path) as f:
        trainers = json.load(f)
    with open(library_path) as f:
        library = json.load(f)
    with open(hevy_ref_path) as f:
        hevy_ref = f.read()

    # Stage 0: Athlete state
    print("\nStage 0: Gathering athlete state...", flush=True)
    state = gather_athlete_state(user_slug)
    print(f"  TSB: {state.tsb:.1f} ({state.form_status})")
    print(f"  Target intensity: {state.target_intensity}")
    print(f"  HRV trend: {state.hrv_trend}")
    print(f"  Sleep score: {state.sleep_score}")
    print(f"  Body battery: {state.body_battery}")
    print(f"  Recent cardio (legs): {state.recent_cardio_legs}")
    print(f"  Muscle load:")
    for mg, load in sorted(state.muscle_load.items()):
        days = _days_since(load.get("last_date", ""))
        print(f"    {mg:8s}: {load['sets']:3d} sets, vol={load['volume']:.0f}kg, {days:.1f}d ago")

    # Stage 1: Filter
    print(f"\nStage 1: Filtering {len(library)} library workouts...", flush=True)
    candidates = stage1_filter(state, library, trainers)
    print(f"  {len(candidates)} candidates after filtering")
    for i, c in enumerate(candidates[:8]):
        print(
            f"    {i+1:2d}. [{c['stage1_score']:5.1f}] {c['title'][:45]:45s} "
            f"| {c['trainer_name']:20s} | {c['difficulty']:10s} | {c['duration_min']}min"
        )

    # Stage 2: Deep analysis
    ifit_headers = get_auth_headers()
    print(f"\nStage 2: Deep-analysing {len(candidates)} candidates via VTT + LLM...", flush=True)
    recommendations = stage2_analyse(candidates, state, hevy_ref, ifit_headers)

    # Present results
    print(f"\n{'=' * 80}")
    print(f"  TOP 3 RECOMMENDATIONS")
    print(f"{'=' * 80}")

    for rec in recommendations:
        print(f"\n{'─' * 80}")
        print(f"  #{rec.rank}: {rec.title}")
        print(f"  Trainer: {rec.trainer_name} | {rec.duration_min}min | {rec.difficulty} | ★{rec.rating:.1f}")
        print(f"  Focus: {rec.focus}")
        print(f"  Score: {rec.stage2_score:.1f} (stage1={rec.stage1_score:.1f})")
        print(f"  Reasoning: {rec.reasoning}")
        print(f"  Equipment: {', '.join(rec.required_equipment)}")
        print(f"\n  Exercises:")
        print(f"  {'#':>3s}  {'Exercise':<40s} {'Muscle':<12s} {'Sets':>4s} x {'Reps':<6s} {'Weight'}")
        for i, ex in enumerate(rec.exercises, 1):
            reps = str(ex.get("reps", "?"))
            sets = ex.get("sets", "?")
            weight = ex.get("weight", "")
            muscle = ex.get("muscle_group", "")
            print(f"  {i:3d}  {ex.get('hevy_name', '?'):<40s} {muscle:<12s} {sets:>4} x {reps:<6s} {weight}")

    # Save
    out_path = CACHE_DIR / "recommendations.json"
    with open(out_path, "w") as f:
        json.dump([asdict(r) for r in recommendations], f, indent=2, default=str)
    print(f"\nSaved to {out_path}")

    return recommendations


def main() -> int:
    user_slug = os.environ.get("USER_SLUG", "alexey")

    recs = recommend(user_slug)
    if not recs:
        print("No recommendations generated.")
        return 1

    # Handle --create-hevy flag
    if "--create-hevy" in sys.argv:
        idx = sys.argv.index("--create-hevy")
        if idx + 1 < len(sys.argv):
            pick = int(sys.argv[idx + 1]) - 1
            if 0 <= pick < len(recs):
                hevy_key = os.environ.get("HEVY_API_KEY", "")
                if not hevy_key:
                    print("HEVY_API_KEY not set in .env")
                    return 1
                print(f"\nCreating Hevy routine for #{pick + 1}: {recs[pick].title}...")
                result = create_hevy_routine(recs[pick], hevy_key)
                print(f"  Result: {json.dumps(result, indent=2)}")
            else:
                print(f"Invalid pick {pick + 1}. Choose 1-{len(recs)}.")
                return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

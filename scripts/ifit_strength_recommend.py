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

from ifit_auth import get_auth_headers

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
    recent_cardio_legs: bool = False
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

    athlete = _load_yaml(ATHLETE_PATH)
    user_cfg = athlete.get("users", {}).get(user_slug, {})
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

    # Muscle load from strength_sets (last 7 days)
    try:
        sets = get_strength_sessions(user_id, days=7)
        load_by_group: dict[str, dict] = defaultdict(
            lambda: {"volume": 0.0, "sets": 0, "last_date": ""}
        )
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
        state.muscle_load = dict(load_by_group)
    except Exception:
        pass

    # Recent running/cycling loading legs
    try:
        activities = get_activities(user_id, days=3)
        for a in activities:
            atype = (a.get("activity_type") or "").lower()
            if any(k in atype for k in ("running", "cycling", "walking", "hiking")):
                state.recent_cardio_legs = True
                break
    except Exception:
        pass

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

        # Goal alignment (0-15 pts) -- runner needs upper body too
        if is_runner:
            if "upper" in muscles:
                if state.recent_cardio_legs:
                    score += 15
                else:
                    score += 10
            elif "core" in muscles:
                score += 12
            elif "lower" in muscles:
                if state.recent_cardio_legs:
                    score -= 5
                else:
                    score += 5
        else:
            score += 8

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
2. A reference list of Hevy exercise names with their muscle groups and equipment

Your task: Extract ONLY the main working exercises (skip warm-up and cool-down/stretching).
For each exercise, output a JSON array of objects with these fields:

- "hevy_name": The closest matching exercise from the Hevy reference list (MUST be an exact match from the list)
- "hevy_id": The ID from the Hevy reference list if available, otherwise empty string
- "muscle_group": Primary muscle group from the Hevy reference
- "sets": Number of sets (integer). If the trainer says "3 rounds" or "3 sets", use that.
- "reps": Reps per set (integer or string like "30s" for timed sets).
- "weight": Weight suggestion based on what the trainer says ("heavy dumbbells", "medium dumbbells", "light dumbbells", or "bodyweight")
- "notes": Brief note about form cues or variations

Rules:
- Only include exercises from the main workout, NOT warm-up or cool-down stretches
- If the trainer repeats the same exercise in multiple rounds, list it ONCE with total sets
- Use EXACT names from the Hevy reference list
- For compound movements, pick the primary exercise and note the combo
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

    # Penalise lower body if recent cardio loaded legs
    if state.recent_cardio_legs and muscle_counts.get("lower", 0) > total_ex * 0.5:
        adjustment -= 15
        reasons.append("heavy lower body after recent cardio")

    return round(adjustment, 1), "; ".join(reasons) if reasons else "balanced"


def stage2_analyse(
    candidates: list[dict],
    state: AthleteState,
    hevy_ref: str,
    ifit_headers: dict,
) -> list[Recommendation]:
    """Deep-analyse candidates via VTT + LLM, return top 3 diverse picks."""
    exercise_cache = _load_exercise_cache()
    cache_hits = 0
    cache_new = 0
    scored = []

    for i, c in enumerate(candidates):
        wid = c["id"]
        title = c["title"]
        print(f"  [{i+1}/{len(candidates)}] Analysing: {title}...", flush=True)

        if wid in exercise_cache:
            exercises = exercise_cache[wid]
            cache_hits += 1
            print(f"    {len(exercises)} exercises (cached)")
        else:
            transcript = _fetch_vtt(wid, ifit_headers)
            if not transcript:
                print(f"    Skipping (no captions)")
                continue

            exercises = _llm_extract(transcript, hevy_ref, title)
            if not exercises:
                print(f"    Skipping (no exercises extracted)")
                continue

            exercise_cache[wid] = exercises
            cache_new += 1
            print(f"    {len(exercises)} exercises (new — saved to cache)")
            time.sleep(0.3)

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

    if cache_new > 0:
        _save_exercise_cache(exercise_cache)
    print(f"  Cache stats: {cache_hits} hits, {cache_new} new extractions, {len(exercise_cache)} total cached")

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


def create_hevy_routine(rec: Recommendation, hevy_api_key: str) -> dict:
    """Create a Hevy routine from a recommendation."""
    exercises_payload = []
    for ex in rec.exercises:
        hevy_id = ex.get("hevy_id", "")
        if not hevy_id:
            continue
        sets_count = int(ex.get("sets", 3))
        reps_val = ex.get("reps", 12)
        reps = int(reps_val) if str(reps_val).isdigit() else 12

        sets_payload = []
        for _ in range(sets_count):
            sets_payload.append({
                "type": "normal",
                "weight_kg": None,
                "reps": reps,
            })

        exercises_payload.append({
            "exercise_template_id": hevy_id,
            "sets": sets_payload,
            "notes": ex.get("notes", ""),
        })

    if not exercises_payload:
        return {"error": "No exercises with valid Hevy IDs"}

    body = {
        "title": f"iFit: {rec.title}",
        "exercises": exercises_payload,
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

    if r.status_code in (200, 201):
        data = r.json()
        routine = data.get("routine", data)
        return {
            "status": "created",
            "routine_id": routine.get("id", ""),
            "title": body["title"],
            "exercises": len(exercises_payload),
        }
    else:
        return {"error": f"Hevy API {r.status_code}: {r.text[:200]}"}


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

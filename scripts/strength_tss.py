#!/usr/bin/env python3
"""
Enhanced strength training TSS estimation.

Three estimation strategies, used in priority order:
  1. Hybrid (HR + volume) — when Garmin provides avg_hr from HRM Pro
  2. Per-set volume model — uses muscle group size, compound/isolation
     classification, and relative intensity (% of historical max)
  3. Simple volume model — fallback with flat per-set rate

The per-set model scores each working set individually:
  set_tss = base × muscle_mult × compound_mult × intensity_mult
then sums across the workout.
"""

from __future__ import annotations

from dataclasses import dataclass

BASE_TSS_PER_SET = 1.0
MIN_STRENGTH_TSS = 10.0
MAX_STRENGTH_TSS = 100.0
DEFAULT_RESTING_HR = 50
DEFAULT_LTHR = 160

# Larger muscles consume more oxygen and create more systemic fatigue.
MUSCLE_GROUP_MULT: dict[str, float] = {
    "quadriceps": 1.35,
    "hamstrings": 1.30,
    "glutes": 1.30,
    "upper_back": 1.20,
    "lats": 1.20,
    "full_body": 1.40,
    "chest": 1.05,
    "shoulders": 0.95,
    "abdominals": 0.70,
    "biceps": 0.70,
    "triceps": 0.70,
    "calves": 0.65,
    "cardio": 1.10,
}
DEFAULT_MUSCLE_MULT = 1.0

# Keywords that identify compound (multi-joint) movements.
_COMPOUND_KEYWORDS = {
    "squat", "deadlift", "press", "row", "lunge", "clean", "snatch",
    "push up", "push-up", "pushup", "pull up", "pull-up", "pullup",
    "high pull", "thruster", "good morning", "dip",
}
COMPOUND_MULT = 1.20
ISOLATION_MULT = 0.85


@dataclass
class SetData:
    """Single set from strength_sets table."""
    exercise_name: str
    muscle_group: str | None
    set_type: str | None
    weight_kg: float | None
    reps: int | None
    rpe: float | None
    duration_s: int | None


def _is_compound(exercise_name: str) -> bool:
    name_lower = exercise_name.lower()
    return any(kw in name_lower for kw in _COMPOUND_KEYWORDS)


def _muscle_mult(muscle_group: str | None) -> float:
    if not muscle_group:
        return DEFAULT_MUSCLE_MULT
    return MUSCLE_GROUP_MULT.get(muscle_group.lower(), DEFAULT_MUSCLE_MULT)


def _intensity_mult(weight_kg: float | None, max_weight: float | None) -> float:
    """Scale by how close the set is to the user's historical max for this exercise."""
    if not weight_kg or not max_weight or max_weight <= 0:
        return 0.85  # bodyweight / unknown → moderate default
    ratio = weight_kg / max_weight
    if ratio >= 0.85:
        return 1.35  # heavy
    if ratio >= 0.70:
        return 1.15  # moderate-heavy
    if ratio >= 0.50:
        return 1.00  # moderate
    return 0.80  # light / warmup weight on working set


def estimate_volume_tss(
    sets: list[SetData],
    exercise_maxes: dict[str, float] | None = None,
) -> float:
    """
    Per-set volume model.

    Each working set contributes:
      base × muscle_mult × compound_mult × intensity_mult × rpe_mult

    Warmup sets are excluded.
    """
    if not sets:
        return MIN_STRENGTH_TSS

    maxes = exercise_maxes or {}
    total = 0.0
    working_sets = 0

    for s in sets:
        if s.set_type and s.set_type.lower() == "warmup":
            continue

        working_sets += 1
        m_mult = _muscle_mult(s.muscle_group)
        c_mult = COMPOUND_MULT if _is_compound(s.exercise_name) else ISOLATION_MULT
        i_mult = _intensity_mult(s.weight_kg, maxes.get(s.exercise_name))

        rpe_mult = 1.0
        if s.rpe is not None and s.rpe > 0:
            rpe_mult = 0.6 + (s.rpe / 10.0) * 0.6  # RPE 5→0.9, RPE 8→1.08, RPE 10→1.2

        set_tss = BASE_TSS_PER_SET * m_mult * c_mult * i_mult * rpe_mult
        total += set_tss

    if working_sets == 0:
        return MIN_STRENGTH_TSS

    return round(max(MIN_STRENGTH_TSS, min(total, MAX_STRENGTH_TSS)), 1)


def estimate_hr_tss(
    duration_s: float,
    avg_hr: float,
    resting_hr: float | None = None,
    lthr: float | None = None,
) -> float:
    """Standard hrTSS formula."""
    rest = resting_hr or DEFAULT_RESTING_HR
    lt = lthr or DEFAULT_LTHR
    if lt <= rest or duration_s <= 0:
        return 0.0
    hours = duration_s / 3600
    hr_if = max(0, min((avg_hr - rest) / (lt - rest), 2.0))
    return round(hours * hr_if * hr_if * 100, 1)


def estimate_hybrid_tss(
    duration_s: float,
    avg_hr: float,
    sets: list[SetData],
    exercise_maxes: dict[str, float] | None = None,
    resting_hr: float | None = None,
    lthr: float | None = None,
) -> float:
    """
    Hybrid model: hrTSS captures cardiovascular cost, volume bonus
    adds the mechanical/muscular stress that HR doesn't reflect
    (HR drops between sets while muscles accumulate fatigue).

    strength_TSS = hrTSS + 0.35 × volume_TSS
    """
    hr = estimate_hr_tss(duration_s, avg_hr, resting_hr, lthr)
    vol = estimate_volume_tss(sets, exercise_maxes)
    combined = hr + 0.35 * vol
    return round(max(MIN_STRENGTH_TSS, min(combined, MAX_STRENGTH_TSS)), 1)

#!/usr/bin/env python3
"""
Resolve iFit-extracted exercises to Hevy exercise template IDs.

Three-stage resolution:
  1. Exact match on hevy_id (if LLM returned one from the ref list)
  2. Fuzzy name match against the Hevy exercise library (SequenceMatcher > 0.7)
  3. Create a custom exercise via Hevy API (LLM classifies the exercise type/equipment)

The resolver caches custom exercise mappings so the same exercise name
only triggers one API call across all workouts.
"""

from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path

import httpx

HEVY_BASE = "https://api.hevyapp.com/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_MODEL = "google/gemini-2.0-flash-001"

CACHE_DIR = Path(__file__).resolve().parent.parent / ".ifit_capture"
EXERCISES_JSON = CACHE_DIR / "hevy_exercises.json"
CUSTOM_MAP_PATH = CACHE_DIR / "hevy_custom_map.json"

FUZZY_THRESHOLD = 0.70

HEVY_MUSCLE_GROUPS = [
    "abdominals", "shoulders", "biceps", "triceps", "forearms",
    "quadriceps", "hamstrings", "calves", "glutes", "abductors",
    "adductors", "lats", "upper_back", "traps", "lower_back",
    "chest", "cardio", "neck", "full_body", "other",
]

HEVY_EQUIPMENT = [
    "none", "barbell", "dumbbell", "kettlebell", "machine",
    "plate", "resistance_band", "suspension", "other",
]

HEVY_EXERCISE_TYPES = [
    "weight_reps", "reps_only", "bodyweight_reps", "bodyweight_weighted",
    "duration", "weight_duration", "distance_duration",
]

CLASSIFY_PROMPT = """\
You are a fitness expert. Given an exercise name, classify it for the Hevy workout app.

Return a JSON object with these fields:
- "exercise_type": one of {types}
- "equipment_category": one of {equipment}
- "muscle_group": primary muscle group, one of {muscles}
- "other_muscles": array of secondary muscle groups from the same list (can be empty)

Rules:
- "bodyweight_reps" for bodyweight exercises with reps (push-ups, squats, lunges)
- "weight_reps" for exercises using external weight with reps (curls, presses, rows)
- "reps_only" for exercises where you only count reps without weight (jumping jacks)
- "duration" for time-based exercises (planks, wall sits)
- "bodyweight_weighted" for bodyweight exercises with added weight (weighted pull-ups)
- "weight_duration" for weighted holds (farmer's walk, dead hang with weight)
- Pick the MOST SPECIFIC muscle group, not "full_body" unless it truly is
- Output ONLY valid JSON, no markdown, no explanation
"""


def _load_library() -> dict[str, dict]:
    """Load hevy_exercises.json into {title_lower: exercise_dict} lookup."""
    if not EXERCISES_JSON.exists():
        return {}
    with open(EXERCISES_JSON) as f:
        templates = json.load(f)
    return {t["title"].lower(): t for t in templates}


def _load_library_by_id() -> dict[str, dict]:
    """Load hevy_exercises.json into {id: exercise_dict} lookup."""
    if not EXERCISES_JSON.exists():
        return {}
    with open(EXERCISES_JSON) as f:
        templates = json.load(f)
    return {t["id"]: t for t in templates}


def _load_custom_map() -> dict[str, str]:
    """Load the name->template_id mapping for previously created custom exercises."""
    if not CUSTOM_MAP_PATH.exists():
        return {}
    with open(CUSTOM_MAP_PATH) as f:
        return json.load(f)


def _save_custom_map(mapping: dict[str, str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CUSTOM_MAP_PATH, "w") as f:
        json.dump(mapping, f, indent=2)


def _fuzzy_match(name: str, library: dict[str, dict]) -> dict | None:
    """Find best fuzzy match in library by title. Returns template dict or None."""
    lower = name.lower()
    if lower in library:
        return library[lower]

    best_score = 0.0
    best_match = None
    for title, tmpl in library.items():
        score = SequenceMatcher(None, lower, title).ratio()
        if score > best_score:
            best_score = score
            best_match = tmpl

    if best_score >= FUZZY_THRESHOLD:
        return best_match
    return None


def _llm_classify(exercise_name: str, muscle_hint: str = "", weight_hint: str = "") -> dict | None:
    """Ask LLM to classify an exercise for custom creation in Hevy."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return None

    system = CLASSIFY_PROMPT.format(
        types=", ".join(HEVY_EXERCISE_TYPES),
        equipment=", ".join(HEVY_EQUIPMENT),
        muscles=", ".join(HEVY_MUSCLE_GROUPS),
    )

    context_parts = [f'Exercise name: "{exercise_name}"']
    if muscle_hint:
        context_parts.append(f"Muscle group hint: {muscle_hint}")
    if weight_hint:
        context_parts.append(f"Weight/equipment hint: {weight_hint}")

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
                    {"role": "system", "content": system},
                    {"role": "user", "content": "\n".join(context_parts)},
                ],
                "max_tokens": 500,
                "temperature": 0.0,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        content = resp.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]
        result = json.loads(content.strip())

        if result.get("exercise_type") not in HEVY_EXERCISE_TYPES:
            result["exercise_type"] = "weight_reps"
        if result.get("equipment_category") not in HEVY_EQUIPMENT:
            result["equipment_category"] = "none"
        if result.get("muscle_group") not in HEVY_MUSCLE_GROUPS:
            result["muscle_group"] = "other"
        result["other_muscles"] = [
            m for m in result.get("other_muscles", []) if m in HEVY_MUSCLE_GROUPS
        ]
        return result
    except Exception as e:
        print(f"  LLM classify error for '{exercise_name}': {e}")
        return None


def _infer_equipment(weight_hint: str) -> str:
    """Best-effort equipment inference from the weight hint string."""
    if not weight_hint:
        return "none"
    w = weight_hint.lower()
    if "barbell" in w or "bar" in w:
        return "barbell"
    if "dumbbell" in w or "db" in w:
        return "dumbbell"
    if "kettlebell" in w or "kb" in w:
        return "kettlebell"
    if "band" in w or "resistance" in w:
        return "resistance_band"
    if "machine" in w or "cable" in w:
        return "machine"
    if "plate" in w:
        return "plate"
    if "bodyweight" in w or "body" in w:
        return "none"
    return "other"


def _infer_exercise_type(weight_hint: str, reps: str | int) -> str:
    """Infer Hevy exercise type from weight and reps hints."""
    reps_str = str(reps).lower()
    is_timed = "s" in reps_str or "sec" in reps_str or "min" in reps_str

    w = (weight_hint or "").lower()
    is_bodyweight = "bodyweight" in w or "body" in w or not w

    if is_timed and not is_bodyweight:
        return "weight_duration"
    if is_timed:
        return "duration"
    if is_bodyweight:
        return "bodyweight_reps"
    return "weight_reps"


def _create_custom_exercise(
    title: str,
    exercise_type: str,
    equipment_category: str,
    muscle_group: str,
    other_muscles: list[str],
    hevy_api_key: str,
) -> str | None:
    """Create a custom exercise in Hevy via POST /v1/exercise_templates.

    Returns the new exercise_template_id or None on failure.
    """
    body = {
        "exercise": {
            "title": title,
            "exercise_type": exercise_type,
            "equipment_category": equipment_category,
            "muscle_group": muscle_group,
            "other_muscles": other_muscles,
        }
    }
    try:
        r = httpx.post(
            f"{HEVY_BASE}/exercise_templates",
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
            tmpl = data.get("exercise_template", data)
            new_id = tmpl.get("id", "")
            if new_id:
                print(f"    Created custom exercise: {title} -> {new_id}")
                return new_id
        print(f"    Failed to create custom exercise '{title}': HTTP {r.status_code}")
        return None
    except Exception as e:
        print(f"    Error creating custom exercise '{title}': {e}")
        return None


def resolve_hevy_exercises(
    exercises: list[dict],
    hevy_api_key: str,
) -> list[dict]:
    """Resolve a list of LLM-extracted exercises to Hevy template IDs.

    Each input exercise dict should have: hevy_name, hevy_id, muscle_group,
    sets, reps, weight, notes.

    Returns a new list of exercise dicts with guaranteed 'hevy_id' and
    'resolution' field indicating how it was matched.
    """
    library = _load_library()
    library_by_id = _load_library_by_id()
    custom_map = _load_custom_map()
    resolved: list[dict] = []
    custom_map_changed = False

    for ex in exercises:
        result = {**ex}
        hevy_id = ex.get("hevy_id", "")
        hevy_name = ex.get("hevy_name", "")

        # Stage 1: Direct ID match
        if hevy_id and hevy_id in library_by_id:
            result["hevy_id"] = hevy_id
            result["hevy_name"] = library_by_id[hevy_id]["title"]
            result["resolution"] = "id_match"
            resolved.append(result)
            continue

        # Stage 1b: Check custom exercise mapping cache
        name_key = hevy_name.lower().strip()
        if name_key in custom_map:
            result["hevy_id"] = custom_map[name_key]
            result["resolution"] = "custom_cached"
            resolved.append(result)
            continue

        # Stage 2: Fuzzy name match
        match = _fuzzy_match(hevy_name, library)
        if match:
            result["hevy_id"] = match["id"]
            result["hevy_name"] = match["title"]
            result["resolution"] = "fuzzy_match"
            resolved.append(result)
            continue

        # Stage 3: Create custom exercise
        if not hevy_api_key:
            result["hevy_id"] = ""
            result["resolution"] = "unresolved_no_api_key"
            resolved.append(result)
            continue

        classification = _llm_classify(
            hevy_name,
            muscle_hint=ex.get("muscle_group", ""),
            weight_hint=ex.get("weight", ""),
        )

        if classification:
            etype = classification["exercise_type"]
            equip = classification["equipment_category"]
            mgroup = classification["muscle_group"]
            others = classification.get("other_muscles", [])
        else:
            etype = _infer_exercise_type(ex.get("weight", ""), ex.get("reps", ""))
            equip = _infer_equipment(ex.get("weight", ""))
            mgroup = ex.get("muscle_group", "other")
            if mgroup not in HEVY_MUSCLE_GROUPS:
                mgroup = "other"
            others = []

        new_id = _create_custom_exercise(
            title=hevy_name,
            exercise_type=etype,
            equipment_category=equip,
            muscle_group=mgroup,
            other_muscles=others,
            hevy_api_key=hevy_api_key,
        )

        if new_id:
            result["hevy_id"] = new_id
            result["resolution"] = "custom_created"
            custom_map[name_key] = new_id
            custom_map_changed = True
        else:
            result["hevy_id"] = ""
            result["resolution"] = "creation_failed"

        resolved.append(result)

    if custom_map_changed:
        _save_custom_map(custom_map)

    return resolved

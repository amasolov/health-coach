#!/usr/bin/env python3
"""
Extract structured exercise lists from iFit workout transcripts using an LLM.
Maps exercises to Hevy-compatible names.

Usage:
    python scripts/ifit_llm_extract.py
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

from scripts.addon_config import config
from scripts.cache_store import (
    get_cache, put_cache, get_cache_text,
    KEY_HEVY_EXERCISE_REF, KEY_ST101_TRANSCRIPTS, KEY_ST101_EXERCISES_LLM,
)

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".ifit_capture")
API_KEY = config.openrouter_api_key
MODEL = "google/gemini-2.5-flash"
API_URL = "https://openrouter.ai/api/v1/chat/completions"


def load_hevy_ref() -> str:
    ref = get_cache_text(KEY_HEVY_EXERCISE_REF)
    if ref is not None:
        return ref
    with open(os.path.join(CACHE_DIR, "hevy_exercise_ref.txt")) as f:
        return f.read()


SYSTEM_PROMPT = """\
You are a fitness expert who extracts structured exercise data from workout transcript text.

You will receive:
1. A transcript of an iFit strength training workout (trainer speaking during the video)
2. A reference list of Hevy exercise names with their muscle groups and equipment

Your task: Extract ALL exercises performed during the workout.
For each exercise, output a JSON array of objects with these fields:

- "hevy_name": The closest matching exercise from the Hevy reference list (MUST be an exact match from the list)
- "muscle_group": Primary muscle group from the Hevy reference
- "sets": Number of sets (integer). If the trainer says "3 rounds" or "3 sets", use that. If timed, estimate rounds.
- "reps": Reps per set (integer or string like "30s" for timed sets). Use the number the trainer calls out.
- "weight": Weight suggestion based on what the trainer says (e.g. "heavy dumbbells", "medium dumbbells", "light dumbbells", or "bodyweight")
- "notes": Brief note if the trainer gives specific form cues or variations

Rules:
- Include exercises from ALL phases of the workout (warmup, main work, cooldown). Only skip passive stretching, static mobility holds, and rest periods that involve no movement.
- If an active exercise appears during warmup or cooldown (e.g. push-ups, shoulder taps, inchworms), DO include it.
- Use the workout title to cross-check your results. If the title says "upper-body" but you extracted mostly core/lower exercises, re-read the transcript — you likely missed upper-body movements.
- IMPORTANT: If the trainer repeats the same exercise in multiple rounds/circuits/supersets, list it ONCE with the total number of sets across all rounds. For example, if an exercise appears in 3 rounds, sets=3.
- For timed exercises, convert to approximate reps: 30s ≈ 10-12 reps, 45s ≈ 12-15 reps, 60s ≈ 15-20 reps. But if the trainer says a specific rep count, use that.
- Use EXACT names from the Hevy reference list
- If an exercise isn't in the Hevy list, use the closest match and add a note
- For compound movements (e.g. "curl to press"), pick the primary exercise and note the combo
- For "weight" field: use what the trainer says (e.g. "heavy dumbbells ~30-40lb", "medium dumbbells ~15-25lb", "light dumbbells ~8-12lb"). Add approximate lb/kg if the trainer hints at it, otherwise use the relative term.
- Output ONLY valid JSON array, no markdown, no explanation
"""


def extract_exercises(transcript: str, hevy_ref: str, workout_num: int) -> list[dict]:
    user_msg = f"""## Hevy Exercise Reference List
{hevy_ref}

## Workout Transcript (Strength Training 101, Workout {workout_num} by Gideon Akande)
{transcript}

Extract all main working exercises as a JSON array. Skip warm-up and stretching/cool-down."""

    resp = httpx.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": 4000,
            "temperature": 0.1,
        },
        timeout=120,
    )

    if resp.status_code != 200:
        print(f"  API error: {resp.status_code} {resp.text[:200]}")
        return []

    content = resp.json()["choices"][0]["message"]["content"]

    # Strip markdown code fences if present
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
    if content.endswith("```"):
        content = content.rsplit("```", 1)[0]
    content = content.strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        print(f"  JSON parse error. Raw response:\n{content[:500]}")
        return []


def main() -> int:
    hevy_ref = load_hevy_ref()

    transcripts = get_cache(KEY_ST101_TRANSCRIPTS)
    if transcripts is None:
        with open(os.path.join(CACHE_DIR, "st101_transcripts.json")) as f:
            transcripts = json.load(f)

    all_results = {}

    for num_str in sorted(transcripts.keys(), key=int):
        num = int(num_str)
        transcript = transcripts[num_str]
        print(f"Processing Workout {num}...", flush=True)

        exercises = extract_exercises(transcript, hevy_ref, num)
        all_results[num] = exercises

        print(f"  => {len(exercises)} exercises extracted")
        for i, ex in enumerate(exercises, 1):
            name = ex.get("hevy_name", "?")
            sets = ex.get("sets", "?")
            reps = str(ex.get("reps", "?"))
            weight = ex.get("weight", "")
            muscle = ex.get("muscle_group", "")
            print(f"     {i:2d}. {name:<42s} {muscle:<14s} {sets}x{reps:<6s} {weight}")

        time.sleep(0.5)

    put_cache(KEY_ST101_EXERCISES_LLM, all_results)
    out_path = os.path.join(CACHE_DIR, "st101_exercises_llm.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved to {out_path}")

    # Print final formatted summary
    print(f"\n{'='*95}")
    print(f"  STRENGTH TRAINING 101 by Gideon Akande — Full Exercise Breakdown")
    print(f"{'='*95}")

    for num in sorted(all_results.keys()):
        exercises = all_results[num]
        muscles = {}
        for ex in exercises:
            mg = ex.get("muscle_group", "")
            if mg:
                muscles[mg] = muscles.get(mg, 0) + 1
        focus = ", ".join(m for m, _ in sorted(muscles.items(), key=lambda x: -x[1])[:3])

        print(f"\n{'─'*95}")
        print(f"  WORKOUT {num}  |  Focus: {focus}  |  {len(exercises)} exercises")
        print(f"{'─'*95}")
        print(f"  {'#':>2s}  {'Exercise':<42s} {'Muscle':<14s} {'Sets':>4s} x {'Reps':<6s} {'Weight':<20s} {'Notes'}")
        for i, ex in enumerate(exercises, 1):
            name = ex.get("hevy_name", "?")
            sets = ex.get("sets", "?")
            reps = str(ex.get("reps", "?"))
            weight = ex.get("weight", "—")
            muscle = ex.get("muscle_group", "")
            notes = ex.get("notes", "")
            print(f"  {i:2d}  {name:<42s} {muscle:<14s} {sets:>4} x {reps:<6s} {weight:<20s} {notes}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

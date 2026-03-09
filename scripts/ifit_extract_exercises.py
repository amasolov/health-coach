#!/usr/bin/env python3
"""
Extract structured exercise lists from iFit workout transcripts
and map them to Hevy-compatible exercise names.

Reads VTT caption transcripts, identifies exercises, sets, reps,
and maps each to the closest Hevy exercise template.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".ifit_capture")


@dataclass
class Exercise:
    name: str
    hevy_name: str
    hevy_id: str
    muscle_group: str
    secondary_muscles: list[str]
    equipment: str
    sets: int
    reps: str  # can be "12" or "30s" or "1 min"
    weight_suggestion: str
    notes: str


# Canonical exercise names we look for in transcripts, mapped to Hevy search terms
EXERCISE_PATTERNS: list[tuple[str, str, str]] = [
    # (regex pattern for transcript, canonical name, hevy search term)
    # Compound lifts
    (r'\bdeadlift', 'Deadlift', 'Deadlift (Dumbbell)'),
    (r'\bromanian deadlift|rdl\b', 'Romanian Deadlift', 'Romanian Deadlift (Dumbbell)'),
    (r'\bsumo deadlift', 'Sumo Deadlift', 'Sumo Deadlift'),
    (r'\bsingle[- ]leg deadlift|single[- ]leg rdl', 'Single Leg Romanian Deadlift', 'Single Leg Romanian Deadlift (Dumbbell)'),

    # Squats
    (r'\bgoblet squat', 'Goblet Squat', 'Goblet Squat'),
    (r'\bsumo squat', 'Sumo Squat', 'Sumo Squat (Dumbbell)'),
    (r'\bsplit squat', 'Split Squat', 'Split Squat (Dumbbell)'),
    (r'\bbulgarian split squat', 'Bulgarian Split Squat', 'Bulgarian Split Squat'),
    (r'\bfront squat', 'Front Squat', 'Front Squat'),
    (r'\boverhead squat', 'Overhead Squat', 'Overhead Squat'),
    (r'\bsquat(?! jump|s?\b(?:ting|ted))', 'Squat', 'Squat (Dumbbell)'),

    # Lunges
    (r'\breverse lunge', 'Reverse Lunge', 'Reverse Lunge (Dumbbell)'),
    (r'\bwalking lunge', 'Walking Lunge', 'Walking Lunge (Dumbbell)'),
    (r'\bcurtsy lunge|curtsey lunge', 'Curtsy Lunge', 'Curtsy Lunge (Dumbbell)'),
    (r'\blateral lunge', 'Lateral Lunge', 'Lunge (Dumbbell)'),
    (r'\blunge', 'Lunge', 'Lunge (Dumbbell)'),

    # Presses
    (r'\boverhead press|shoulder press|military press', 'Overhead Press', 'Overhead Press (Dumbbell)'),
    (r'\bchest press|bench press|floor press', 'Chest Press', 'Bench Press (Dumbbell)'),
    (r'\barnold press', 'Arnold Press', 'Arnold Press (Dumbbell)'),
    (r'\bpush[- ]?press', 'Push Press', 'Push Press'),

    # Rows
    (r'\bbent[- ]over row', 'Bent Over Row', 'Bent Over Row (Dumbbell)'),
    (r'\brenegade row', 'Renegade Row', 'Renegade Row (Dumbbell)'),
    (r'\bsingle[- ]arm row|one[- ]arm row|dumbbell row', 'Single Arm Dumbbell Row', 'Dumbbell Row'),
    (r'\bupright row', 'Upright Row', 'Upright Row (Dumbbell)'),
    (r'\brow(?:s|ing)?(?:\s|$|,)', 'Row', 'Bent Over Row (Dumbbell)'),

    # Curls
    (r'\bhammer curl', 'Hammer Curl', 'Hammer Curl (Dumbbell)'),
    (r'\bconcentration curl', 'Concentration Curl', 'Concentration Curl'),
    (r'\bcross[- ]body.*curl', 'Cross Body Hammer Curl', 'Cross Body Hammer Curl'),
    (r'\bbicep curl|biceps curl', 'Bicep Curl', 'Bicep Curl (Dumbbell)'),
    (r'\bcurl(?:s)?(?:\s|$|,)', 'Curl', 'Bicep Curl (Dumbbell)'),

    # Triceps
    (r'\bskullcrusher|skull crusher', 'Skullcrusher', 'Skullcrusher (Dumbbell)'),
    (r'\btricep[s]? extension|overhead extension', 'Triceps Extension', 'Triceps Extension (Dumbbell)'),
    (r'\btricep[s]? kickback', 'Triceps Kickback', 'Triceps Kickback (Dumbbell)'),
    (r'\btricep[s]? press|close[- ]grip press', 'Triceps Press', 'Bench Press - Close Grip (Barbell)'),

    # Shoulders / Raises
    (r'\blateral raise|side raise', 'Lateral Raise', 'Lateral Raise (Dumbbell)'),
    (r'\bfront raise', 'Front Raise', 'Front Raise (Dumbbell)'),
    (r'\brear delt fly|reverse fly', 'Rear Delt Fly', 'Rear Delt Reverse Fly (Dumbbell)'),

    # Chest
    (r'\bchest fly|dumbbell fly|fly(?:s|es)?(?:\s|$)', 'Chest Fly', 'Chest Fly (Dumbbell)'),
    (r'\bpullover', 'Pullover', 'Pullover (Dumbbell)'),
    (r'\bpush[- ]?up', 'Push Up', 'Push Up'),

    # Hip / Glute
    (r'\bhip thrust', 'Hip Thrust', 'Hip Thrust (Barbell)'),
    (r'\bglute bridge', 'Glute Bridge', 'Glute Bridge'),
    (r'\bglute kickback', 'Glute Kickback', 'Glute Kickback (Machine)'),
    (r'\bstep[- ]?up', 'Step Up', 'Dumbbell Step Up'),
    (r'\bhip hinge', 'Hip Hinge', 'Romanian Deadlift (Dumbbell)'),

    # Core
    (r'\bplank(?:\s|$|,)', 'Plank', 'Plank'),
    (r'\bside plank', 'Side Plank', 'Side Plank'),
    (r'\bcrunch', 'Crunch', 'Crunch'),
    (r'\bbicycle.*crunch|bicycle', 'Bicycle Crunch', 'Bicycle Crunch'),
    (r'\bmountain climber', 'Mountain Climber', 'Mountain Climbers'),
    (r'\bsit[- ]?up', 'Sit Up', 'Sit Up'),
    (r'\bleg raise|lying leg raise', 'Leg Raise', 'Lying Leg Raise'),
    (r'\brussi?an twist', 'Russian Twist', 'Russian Twist'),
    (r'\bdead bug', 'Dead Bug', 'Dead Bug'),
    (r'\bbird[- ]?dog', 'Bird Dog', 'Bird Dog'),

    # Kettlebell
    (r'\bkettlebell swing|kb swing', 'Kettlebell Swing', 'Kettlebell Swing'),
    (r'\bkettlebell clean|kb clean', 'Kettlebell Clean', 'Kettlebell Clean'),
    (r'\bkettlebell snatch', 'Kettlebell Snatch', 'Kettlebell Snatch'),
    (r'\bkettlebell press', 'Kettlebell Press', 'Kettlebell Shoulder Press'),
    (r'\bturkish get[- ]?up', 'Turkish Get Up', 'Kettlebell Turkish Get Up'),
    (r'\bgoblet.*clean|clean.*goblet', 'Kettlebell Clean', 'Kettlebell Clean'),

    # Bands
    (r'\bband pull[- ]?apart', 'Band Pull Apart', 'Face Pull (Cable)'),
    (r'\bbanded.*squat|band.*squat', 'Banded Squat', 'Squat (Dumbbell)'),

    # Calves
    (r'\bcalf raise', 'Calf Raise', 'Standing Calf Raise (Dumbbell)'),

    # Shrugs
    (r'\bshrug', 'Shrug', 'Shrug (Dumbbell)'),

    # Burpee
    (r'\bburpee', 'Burpee', 'Burpee'),

    # Misc
    (r'\bfarmers? (carry|walk)', "Farmer's Carry", "Farmer's Walk (Dumbbell)"),
    (r'\bshoulder tap', 'Shoulder Tap', 'Plank'),
]


def load_hevy_exercises() -> dict[str, dict]:
    """Load Hevy exercises keyed by title (lowered)."""
    path = os.path.join(CACHE_DIR, "hevy_exercises.json")
    with open(path) as f:
        exercises = json.load(f)
    return {e["title"].lower(): e for e in exercises}


def find_hevy_match(name: str, hevy_db: dict[str, dict]) -> dict | None:
    """Find closest Hevy exercise match by name."""
    lower = name.lower()
    if lower in hevy_db:
        return hevy_db[lower]

    best_score = 0
    best_match = None
    for title, ex in hevy_db.items():
        score = SequenceMatcher(None, lower, title).ratio()
        if score > best_score:
            best_score = score
            best_match = ex
    if best_score > 0.5:
        return best_match
    return None


def extract_reps_context(transcript: str, exercise_pos: int) -> tuple[str, int, str]:
    """Look near an exercise mention for reps/sets/time info."""
    window = transcript[max(0, exercise_pos - 300):exercise_pos + 500].lower()

    reps = ""
    sets = 0
    weight_hint = ""

    # Time-based
    time_match = re.search(r'(\d+)\s*(?:second|sec)(?:s)?', window)
    if time_match:
        reps = f"{time_match.group(1)}s"

    min_match = re.search(r'(\d+)\s*minute', window)
    if min_match:
        reps = f"{min_match.group(1)} min"

    # Rep-based
    rep_match = re.search(r'(\d+)\s*(?:rep|repetition)s?', window)
    if rep_match:
        reps = rep_match.group(1)

    # Specific counts like "give me 15", "we do 12"
    count_match = re.search(r'(?:give me|do|doing|for)\s+(\d+)\b', window)
    if count_match and not reps:
        reps = count_match.group(1)

    # Sets
    set_match = re.search(r'(\d+)\s*(?:round|set|time)s?', window)
    if set_match:
        sets = int(set_match.group(1))

    # Weight hints
    if 'heavy' in window or 'heavier' in window:
        weight_hint = "heavy dumbbells"
    elif 'medium' in window or 'moderate' in window:
        weight_hint = "medium dumbbells"
    elif 'light' in window or 'lighter' in window:
        weight_hint = "light dumbbells"

    return reps, sets, weight_hint


def extract_exercises_from_transcript(
    transcript: str,
    hevy_db: dict[str, dict],
) -> list[Exercise]:
    """Extract exercises from a workout transcript."""
    lower = transcript.lower()
    found: dict[str, Exercise] = {}

    for pattern, canonical, hevy_hint in EXERCISE_PATTERNS:
        matches = list(re.finditer(pattern, lower))
        if not matches:
            continue

        if canonical in found:
            continue

        first_pos = matches[0].start()

        # Skip if only mentioned in warm-up context
        warmup_zone = lower[:min(len(lower) // 6, 2000)]
        main_mentions = [m for m in matches if m.start() > len(warmup_zone)]
        if not main_mentions and len(matches) <= 1:
            if 'warm' in lower[max(0, first_pos - 100):first_pos + 100]:
                continue

        best_pos = main_mentions[0].start() if main_mentions else first_pos
        reps, sets, weight_hint = extract_reps_context(transcript, best_pos)

        hevy_match = find_hevy_match(hevy_hint, hevy_db) or find_hevy_match(canonical, hevy_db)
        if hevy_match:
            hevy_name = hevy_match["title"]
            hevy_id = hevy_match["id"]
            muscle = hevy_match.get("primary_muscle_group", "")
            secondary = hevy_match.get("secondary_muscle_groups", [])
            equipment = hevy_match.get("equipment", "")
        else:
            hevy_name = canonical
            hevy_id = ""
            muscle = ""
            secondary = []
            equipment = "dumbbell"

        found[canonical] = Exercise(
            name=canonical,
            hevy_name=hevy_name,
            hevy_id=hevy_id,
            muscle_group=muscle,
            secondary_muscles=secondary,
            equipment=equipment,
            sets=sets if sets > 0 else 3,
            reps=reps if reps else "12",
            weight_suggestion=weight_hint,
            notes="",
        )

    return list(found.values())


def main():
    hevy_db = load_hevy_exercises()
    print(f"Loaded {len(hevy_db)} Hevy exercises")

    with open(os.path.join(CACHE_DIR, "st101_transcripts.json")) as f:
        transcripts = json.load(f)

    all_results = {}
    for num_str in sorted(transcripts.keys(), key=int):
        num = int(num_str)
        transcript = transcripts[num_str]
        exercises = extract_exercises_from_transcript(transcript, hevy_db)

        all_results[num] = [asdict(e) for e in exercises]

        print(f"\n{'='*90}")
        print(f"  WORKOUT {num}: Strength Training 101")
        print(f"{'='*90}")
        print(f"{'#':>3s}  {'Hevy Exercise':<42s} {'Muscle':<14s} {'Sets':>4s} {'Reps':>6s}  {'Weight'}")
        print(f"{'─'*3}  {'─'*42} {'─'*14} {'─'*4} {'─'*6}  {'─'*20}")
        for i, ex in enumerate(exercises, 1):
            print(
                f"{i:3d}  {ex.hevy_name:<42s} {ex.muscle_group:<14s} "
                f"{ex.sets:4d} {ex.reps:>6s}  {ex.weight_suggestion}"
            )

    out_path = os.path.join(CACHE_DIR, "st101_exercises.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\n\nSaved to {out_path}")


if __name__ == "__main__":
    main()

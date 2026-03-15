"""Tests for LLM result summarization (#40–#43).

Verifies that tool results are compressed before entering the LLM
conversation context, reducing token usage without losing essential data.
"""

from __future__ import annotations

import json
import string

import pytest

from scripts.llm_result_summarizer import (
    summarize_for_llm,
    RESULT_LOG_THRESHOLD,
    RESULT_TRUNCATE_THRESHOLD,
    RECENT_RECORDS_TO_KEEP,
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_training_load(n: int) -> list[dict]:
    return [
        {"time": f"2026-01-{i+1:02d}", "tss": 40 + i, "ctl": 30 + i * 0.5,
         "atl": 35 + i * 0.3, "tsb": -5 + i * 0.2, "ramp": 3.0, "source": "garmin"}
        for i in range(n)
    ]


def _make_vitals(n: int) -> list[dict]:
    return [
        {"time": f"2026-02-{i+1:02d}", "resting_hr": 55 + i % 5,
         "hrv_ms": 45 + i % 10, "sleep_score": 70 + i % 15,
         "stress_avg": 30 + i % 8, "body_battery_high": 80 + i % 10,
         "body_battery_low": 20 + i % 10, "spo2_avg": 96 + i % 3}
        for i in range(n)
    ]


def _make_body_comp(n: int) -> list[dict]:
    return [
        {"time": f"2026-01-{i+1:02d}", "weight_kg": 80 + i * 0.1,
         "body_fat_pct": 18.0 - i * 0.05, "muscle_mass_kg": 35 + i * 0.02,
         "bmi": 25.0}
        for i in range(n)
    ]


def _make_activities(n: int) -> list[dict]:
    return [
        {"time": f"2026-03-{i+1:02d}", "activity_type": "running",
         "title": f"Run {i+1}", "duration_s": 1800 + i * 60,
         "distance_m": 5000 + i * 100, "avg_hr": 145,
         "max_hr": 170, "tss": 50 + i, "calories": 400,
         "avg_cadence": 170, "training_effect_ae": 3.2,
         "training_effect_an": 1.1, "avg_pace_sec_km": 330}
        for i in range(n)
    ]


def _make_strength_sessions(n: int) -> list[dict]:
    exercises = ["Bench Press", "Squat", "Deadlift", "Row"]
    return [
        {"time": f"2026-03-{(i % 28) + 1:02d}", "workout_id": f"w{i // 10}",
         "exercise_name": exercises[i % len(exercises)],
         "muscle_group": "chest" if i % 2 == 0 else "legs",
         "set_number": (i % 4) + 1, "weight_kg": 60 + i,
         "reps": 8 + i % 4, "rpe": 7 + i % 3}
        for i in range(n)
    ]


# ── #43: Safety-net size limits ───────────────────────────────────────────

class TestSafetyNetTruncation:

    def test_small_result_unchanged(self):
        result = {"status": "ok", "value": 42}
        assert summarize_for_llm("unknown_tool", result) == result

    def test_oversized_result_truncated(self):
        huge = {"data": "x" * (RESULT_TRUNCATE_THRESHOLD + 1000)}
        out = summarize_for_llm("unknown_tool", huge)
        serialized = json.dumps(out, default=str)
        assert len(serialized) <= RESULT_TRUNCATE_THRESHOLD + 500

    def test_truncated_result_has_marker(self):
        huge = {"data": "x" * (RESULT_TRUNCATE_THRESHOLD + 1000)}
        out = summarize_for_llm("unknown_tool", huge)
        serialized = json.dumps(out, default=str)
        assert "truncated" in serialized.lower()

    def test_thresholds_are_sensible(self):
        assert RESULT_LOG_THRESHOLD < RESULT_TRUNCATE_THRESHOLD
        assert RESULT_LOG_THRESHOLD >= 4000
        assert RESULT_TRUNCATE_THRESHOLD >= 10000


# ── #40: Time-series summarization ────────────────────────────────────────

class TestTimeSeriesSummarization:

    def test_short_training_load_unchanged(self):
        data = _make_training_load(3)
        out = summarize_for_llm("get_training_load", data)
        assert isinstance(out, list)
        assert len(out) == 3

    def test_long_training_load_summarized(self):
        data = _make_training_load(90)
        out = summarize_for_llm("get_training_load", data)
        assert isinstance(out, dict)
        assert "record_count" in out
        assert out["record_count"] == 90
        assert "recent" in out
        assert len(out["recent"]) == RECENT_RECORDS_TO_KEEP
        assert out["recent"][-1] == data[-1]

    def test_training_load_summary_has_stats(self):
        data = _make_training_load(30)
        out = summarize_for_llm("get_training_load", data)
        assert "summary" in out
        assert "tss" in out["summary"]
        stats = out["summary"]["tss"]
        assert "min" in stats and "max" in stats and "avg" in stats

    def test_vitals_summarized(self):
        data = _make_vitals(30)
        out = summarize_for_llm("get_vitals", data)
        assert isinstance(out, dict)
        assert out["record_count"] == 30
        assert len(out["recent"]) == RECENT_RECORDS_TO_KEEP
        assert "resting_hr" in out["summary"]

    def test_body_composition_summarized(self):
        data = _make_body_comp(60)
        out = summarize_for_llm("get_body_composition", data)
        assert isinstance(out, dict)
        assert out["record_count"] == 60
        assert "weight_kg" in out["summary"]

    def test_strength_sessions_summarized(self):
        data = _make_strength_sessions(100)
        out = summarize_for_llm("get_strength_sessions", data)
        assert isinstance(out, dict)
        assert out["record_count"] == 100
        assert "by_exercise" in out
        assert len(out["recent"]) <= 10
        for entry in out["by_exercise"]:
            assert "exercise" in entry
            assert "total_sets" in entry

    def test_summary_date_range(self):
        data = _make_training_load(30)
        out = summarize_for_llm("get_training_load", data)
        assert "date_range" in out
        assert out["date_range"]["from"] == data[0]["time"]
        assert out["date_range"]["to"] == data[-1]["time"]


# ── #41: Activity result summarization ────────────────────────────────────

class TestActivitySummarization:

    def test_short_activity_list_unchanged(self):
        data = _make_activities(5)
        out = summarize_for_llm("get_activities", data)
        assert isinstance(out, list)
        assert len(out) == 5

    def test_long_activity_list_trimmed(self):
        data = _make_activities(50)
        out = summarize_for_llm("get_activities", data)
        assert isinstance(out, dict)
        assert "activities" in out
        assert len(out["activities"]) <= 15
        assert out["total_count"] == 50

    def test_activity_detail_strips_raw_data(self):
        detail = {
            "time": "2026-03-01", "activity_type": "running",
            "title": "Morning Run", "tss": 50,
            "raw_data": {"huge": "x" * 5000, "nested": {"a": 1}},
        }
        out = summarize_for_llm("get_activity_detail", detail)
        assert "raw_data" not in out
        assert out["title"] == "Morning Run"
        assert out["tss"] == 50

    def test_activity_detail_without_raw_data_unchanged(self):
        detail = {"time": "2026-03-01", "title": "Run", "tss": 50}
        out = summarize_for_llm("get_activity_detail", detail)
        assert out == detail

    def test_workout_summary_caps_exercise_details(self):
        exercises = [
            {"name": f"Exercise {i}", "muscle_group": "chest",
             "sets": 3, "best_set": {"weight_kg": 60, "reps": 10, "volume": 600}}
            for i in range(15)
        ]
        data = [
            {"time": "2026-03-01", "title": "Workout 1",
             "hevy": {"exercises": 15, "total_sets": 45,
                      "exercise_details": exercises}},
        ]
        out = summarize_for_llm("get_workout_summary", data)
        details = out[0]["hevy"]["exercise_details"]
        assert len(details) <= 8


# ── #42: iFit program/series summarization ────────────────────────────────

class TestIfitProgramSummarization:

    def test_program_details_trims_schedule(self):
        schedule = [
            {"week": i + 1, "workout_count": 5,
             "workouts": [{"position": j, "id": f"w{i}_{j}", "title": f"Workout {j}"}
                          for j in range(5)]}
            for i in range(12)
        ]
        data = {
            "series_id": "s1", "title": "Strength 101",
            "workout_count": 60, "schedule": schedule,
            "workout_ids": [f"w{i}" for i in range(60)],
            "workout_titles": [f"Workout {i}" for i in range(60)],
        }
        out = summarize_for_llm("get_ifit_program_details", data)
        assert out["workout_count"] == 60
        assert len(out["schedule"]) <= 3
        assert "workout_ids" not in out
        assert "workout_titles" not in out
        assert out["total_weeks"] == 12

    def test_short_program_unchanged(self):
        data = {
            "series_id": "s1", "title": "Quick",
            "workout_count": 3, "schedule": [
                {"week": 1, "workout_count": 3, "workouts": []}
            ],
        }
        out = summarize_for_llm("get_ifit_program_details", data)
        assert len(out["schedule"]) == 1

    def test_program_error_passthrough(self):
        data = {"error": "Not found"}
        out = summarize_for_llm("get_ifit_program_details", data)
        assert out == data

    def test_series_discovery_trims_workouts(self):
        data = {
            "workout_id": "w1",
            "series": [
                {"series_id": "s1", "title": "Series A", "workout_count": 30,
                 "workouts": [{"id": f"w{i}", "title": f"W{i}", "week": i // 5 + 1}
                              for i in range(30)]},
            ],
        }
        out = summarize_for_llm("discover_ifit_series", data)
        series = out["series"][0]
        assert len(series["workouts"]) <= 5
        assert series["workout_count"] == 30

    def test_short_series_unchanged(self):
        data = {
            "workout_id": "w1",
            "series": [
                {"series_id": "s1", "title": "Short", "workout_count": 3,
                 "workouts": [{"id": f"w{i}", "title": f"W{i}"} for i in range(3)]},
            ],
        }
        out = summarize_for_llm("discover_ifit_series", data)
        assert len(out["series"][0]["workouts"]) == 3


# ── Wiring into frontends ─────────────────────────────────────────────────

class TestFrontendWiring:

    def test_chat_app_uses_summarize(self):
        import inspect
        from scripts import chat_app
        source = inspect.getsource(chat_app.on_message)
        assert "summarize_for_llm" in source

    def test_telegram_uses_summarize(self):
        import inspect
        from scripts import telegram_bot
        source = inspect.getsource(telegram_bot.handle_message)
        assert "summarize_for_llm" in source

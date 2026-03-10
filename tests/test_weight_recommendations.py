"""Tests for routine weight recommendation logic.

Hevy API calls are mocked — no real routines are fetched.
"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from scripts.health_tools import (
    _analyse_exercise_history,
    _recommend_weight,
    _is_compound,
    get_routine_weight_recommendations,
)
from tests.conftest import MockResponse


# ---------------------------------------------------------------------------
# Exercise classification
# ---------------------------------------------------------------------------

class TestIsCompound:

    def test_squat_is_compound(self):
        assert _is_compound("Squat (Barbell)") is True

    def test_deadlift_is_compound(self):
        assert _is_compound("Romanian Deadlift (Dumbbell)") is True

    def test_bench_press_is_compound(self):
        assert _is_compound("Bench Press (Barbell)") is True

    def test_curl_is_not_compound(self):
        assert _is_compound("Bicep Curl (Dumbbell)") is False

    def test_lateral_raise_is_not_compound(self):
        assert _is_compound("Lateral Raise (Dumbbell)") is False

    def test_row_is_compound(self):
        assert _is_compound("Bent Over Row (Barbell)") is True


# ---------------------------------------------------------------------------
# History analysis
# ---------------------------------------------------------------------------

def _make_rows(sessions: list[tuple[str, list[tuple[float, int]]]]) -> list[dict]:
    """Build fake DB rows: [(date, [(weight, reps), ...]), ...]"""
    rows = []
    for day, sets in sessions:
        for i, (wt, reps) in enumerate(sets, 1):
            rows.append({
                "time": f"{day}T12:00:00+00:00",
                "set_number": i,
                "set_type": "normal",
                "weight_kg": wt,
                "reps": reps,
            })
    return rows


class TestAnalyseExerciseHistory:

    def test_no_history(self):
        result = _analyse_exercise_history([])
        assert result["trend"] == "new"

    def test_single_session(self):
        rows = _make_rows([("2026-03-01", [(20, 10), (20, 10), (20, 10)])])
        result = _analyse_exercise_history(rows)
        assert result["trend"] == "new"  # only 1 session, can't determine trend
        assert result["last_weight_kg"] == 20
        assert result["last_set_count"] == 3

    def test_progressing(self):
        rows = _make_rows([
            ("2026-01-01", [(30, 6), (30, 6), (30, 6)]),
            ("2026-01-08", [(35, 6), (35, 6), (35, 6)]),
            ("2026-01-15", [(40, 6), (40, 6), (40, 6)]),
            ("2026-01-22", [(45, 6), (45, 6), (45, 6)]),
        ])
        result = _analyse_exercise_history(rows)
        assert result["trend"] == "progressing"
        assert result["last_weight_kg"] == 45
        assert result["all_time_best_kg"] == 45
        assert result["session_count"] == 4

    def test_plateau(self):
        rows = _make_rows([
            ("2026-01-01", [(50, 8), (50, 8), (50, 8)]),
            ("2026-01-08", [(50, 8), (50, 8), (50, 8)]),
            ("2026-01-15", [(50, 8), (50, 8), (50, 8)]),
            ("2026-01-22", [(50, 8), (50, 8), (50, 8)]),
        ])
        result = _analyse_exercise_history(rows)
        assert result["trend"] == "plateau"

    def test_declining(self):
        rows = _make_rows([
            ("2026-01-01", [(50, 6), (50, 6), (50, 6)]),
            ("2026-01-08", [(45, 6), (45, 6), (45, 6)]),
            ("2026-01-15", [(42, 6), (42, 6), (42, 6)]),
            ("2026-01-22", [(40, 6), (40, 6), (40, 6)]),
        ])
        result = _analyse_exercise_history(rows)
        assert result["trend"] == "declining"
        assert result["all_time_best_kg"] == 50

    def test_warmup_sets_excluded(self):
        rows = [
            {"time": "2026-03-01T12:00:00+00:00", "set_number": 1,
             "set_type": "warmup", "weight_kg": 20, "reps": 10},
            {"time": "2026-03-01T12:00:00+00:00", "set_number": 2,
             "set_type": "normal", "weight_kg": 50, "reps": 6},
            {"time": "2026-03-01T12:00:00+00:00", "set_number": 3,
             "set_type": "normal", "weight_kg": 50, "reps": 6},
        ]
        result = _analyse_exercise_history(rows)
        assert result["last_weight_kg"] == 50
        assert result["last_set_count"] == 2


# ---------------------------------------------------------------------------
# Weight recommendation logic
# ---------------------------------------------------------------------------

class TestRecommendWeight:

    def test_new_exercise(self):
        analysis = {"trend": "new"}
        rec = _recommend_weight(analysis, "moderate", "biceps", 0, False)
        assert rec["weight_kg"] is None
        assert rec["strategy"] == "start_light"
        assert "No history" in rec["reasoning"]

    def test_progressing_compound_increase_weight(self):
        analysis = {
            "trend": "progressing",
            "last_weight_kg": 50,
            "last_avg_reps": 8,  # >= threshold for compound
            "last_set_count": 3,
            "all_time_best_kg": 50,
        }
        rec = _recommend_weight(analysis, "hard", "lower", 0, True)
        assert rec["strategy"] == "increase_weight"
        assert rec["weight_kg"] > 50

    def test_progressing_isolation_increase_reps(self):
        analysis = {
            "trend": "progressing",
            "last_weight_kg": 15,
            "last_avg_reps": 10,  # < 14 threshold for isolation
            "last_set_count": 3,
            "all_time_best_kg": 15,
        }
        rec = _recommend_weight(analysis, "hard", "biceps", 0, False)
        assert rec["strategy"] == "increase_reps"
        assert rec["reps"] > 10

    def test_plateau_break(self):
        analysis = {
            "trend": "plateau",
            "last_weight_kg": 50,
            "last_avg_reps": 8,
            "last_set_count": 3,
            "all_time_best_kg": 50,
        }
        rec = _recommend_weight(analysis, "hard", "chest", 0, True)
        assert rec["strategy"] == "break_plateau"
        assert rec["reps"] > 8

    def test_declining_recovery(self):
        analysis = {
            "trend": "declining",
            "last_weight_kg": 40,
            "last_avg_reps": 6,
            "last_set_count": 3,
            "all_time_best_kg": 50,
        }
        rec = _recommend_weight(analysis, "hard", "lower", 0, True)
        assert rec["strategy"] == "recovery"
        assert rec["weight_kg"] < 40

    def test_easy_day_deload(self):
        analysis = {
            "trend": "plateau",
            "last_weight_kg": 50,
            "last_avg_reps": 8,
            "last_set_count": 3,
            "all_time_best_kg": 50,
        }
        rec = _recommend_weight(analysis, "easy", "lower", 0, True)
        assert rec["strategy"] == "deload"
        assert rec["weight_kg"] < 50

    def test_cardio_leg_stress_reduces_lower_body_weight(self):
        analysis = {
            "trend": "progressing",
            "last_weight_kg": 60,
            "last_avg_reps": 8,
            "last_set_count": 3,
            "all_time_best_kg": 60,
        }
        rec_fresh = _recommend_weight(analysis, "hard", "lower", 0, True)
        rec_tired = _recommend_weight(analysis, "hard", "lower", 70, True)
        assert rec_tired["weight_kg"] < rec_fresh["weight_kg"], \
            "Leg weight should be lower after cardio leg stress"
        assert "cardio" in rec_tired["reasoning"].lower()

    def test_cardio_stress_doesnt_affect_upper(self):
        analysis = {
            "trend": "progressing",
            "last_weight_kg": 20,
            "last_avg_reps": 10,
            "last_set_count": 3,
            "all_time_best_kg": 20,
        }
        rec_fresh = _recommend_weight(analysis, "hard", "biceps", 0, False)
        rec_tired = _recommend_weight(analysis, "hard", "biceps", 70, False)
        assert rec_fresh["weight_kg"] == rec_tired["weight_kg"], \
            "Upper body weight should not change with leg stress"


# ---------------------------------------------------------------------------
# Full tool (mocked Hevy API)
# ---------------------------------------------------------------------------

class TestGetRoutineWeightRecommendations:

    def _mock_routine_response(self):
        return MockResponse(200, {
            "routine": {
                "id": "routine-001",
                "title": "Upper Body Day",
                "exercises": [
                    {
                        "exercise_template_id": "ex1",
                        "title": "Bench Press (Barbell)",
                        "muscle_group": "chest",
                        "sets": [{"type": "normal"}, {"type": "normal"}, {"type": "normal"}],
                    },
                    {
                        "exercise_template_id": "ex2",
                        "title": "Bicep Curl (Dumbbell)",
                        "muscle_group": "biceps",
                        "sets": [{"type": "normal"}, {"type": "normal"}, {"type": "normal"}],
                    },
                ],
            }
        })

    @patch("httpx.get")
    def test_returns_recommendations(self, mock_get, user_id, user_slug):
        mock_get.return_value = self._mock_routine_response()
        result = get_routine_weight_recommendations(
            user_id, user_slug,
            hevy_api_key="test-key",
            routine_id="routine-001",
        )
        assert "recommendations" in result
        assert result["exercise_count"] == 2
        assert result["routine_title"] == "Upper Body Day"
        for rec in result["recommendations"]:
            assert "exercise" in rec
            assert "recommended_weight_kg" in rec
            assert "strategy" in rec
            assert "reasoning" in rec

    @patch("httpx.get")
    def test_lists_routines_when_no_id(self, mock_get, user_id, user_slug):
        mock_get.return_value = MockResponse(200, {
            "routines": [
                {"id": "r1", "title": "Upper Body"},
                {"id": "r2", "title": "Lower Body"},
            ]
        })
        result = get_routine_weight_recommendations(
            user_id, user_slug,
            hevy_api_key="test-key",
        )
        assert "available_routines" in result
        assert len(result["available_routines"]) == 2

    @patch("httpx.get")
    def test_name_filter(self, mock_get, user_id, user_slug):
        mock_get.side_effect = [
            MockResponse(200, {
                "routines": [
                    {"id": "r1", "title": "Upper Body", "exercises": [
                        {"exercise_template_id": "e1", "title": "Curl", "sets": [{}]},
                    ]},
                    {"id": "r2", "title": "Lower Body", "exercises": []},
                ]
            }),
        ]
        result = get_routine_weight_recommendations(
            user_id, user_slug,
            hevy_api_key="test-key",
            routine_name="upper",
        )
        assert result["routine_title"] == "Upper Body"

    def test_no_api_key_error(self, user_id, user_slug):
        result = get_routine_weight_recommendations(user_id, user_slug)
        assert "error" in result

    @patch("httpx.get")
    def test_athlete_state_included(self, mock_get, user_id, user_slug):
        mock_get.return_value = self._mock_routine_response()
        result = get_routine_weight_recommendations(
            user_id, user_slug,
            hevy_api_key="test-key",
            routine_id="routine-001",
        )
        assert "athlete_state" in result
        state = result["athlete_state"]
        assert "target_intensity" in state
        assert "tsb" in state
        assert "cardio_leg_stress" in state

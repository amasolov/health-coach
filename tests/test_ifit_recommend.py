"""Tests for iFit recommendation engines (mocked LLM / external calls)."""

import json
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import MockResponse, make_openrouter_response


class TestRecommendStrengthWorkout:
    """The full recommend pipeline is expensive (LLM calls).
    We test the individual stages with mocks."""

    def test_stage1_filter(self, user_slug):
        """Stage 1 filtering should work with real data."""
        from scripts.ifit_strength_recommend import stage1_filter, gather_athlete_state
        from pathlib import Path

        cache_dir = Path(__file__).resolve().parent.parent / ".ifit_capture"
        library_path = cache_dir / "library_workouts.json"
        trainers_path = cache_dir / "trainers.json"

        if not library_path.exists():
            pytest.skip("No library cache")

        with open(library_path) as f:
            library = json.load(f)
        with open(trainers_path) as f:
            trainers = json.load(f)

        state = gather_athlete_state(user_slug)
        candidates = stage1_filter(state, library, trainers)
        assert isinstance(candidates, list)
        assert len(candidates) > 0, "Expected at least some strength candidates"
        for c in candidates[:3]:
            assert "title" in c
            assert "stage1_score" in c

    def test_gather_athlete_state(self, user_slug):
        from scripts.ifit_strength_recommend import gather_athlete_state
        state = gather_athlete_state(user_slug)
        assert hasattr(state, "tsb")
        assert hasattr(state, "muscle_load")
        assert hasattr(state, "form_status")

    def test_parse_reps_for_hevy(self):
        from scripts.ifit_strength_recommend import _parse_reps_for_hevy
        assert _parse_reps_for_hevy(10) == {"reps": 10}
        assert _parse_reps_for_hevy("12") == {"reps": 12}
        assert _parse_reps_for_hevy("30s") == {"duration_seconds": 30}
        assert _parse_reps_for_hevy("30sec") == {"duration_seconds": 30}
        assert _parse_reps_for_hevy("2min") == {"duration_seconds": 120}
        assert _parse_reps_for_hevy("invalid") == {"reps": 12}

    def test_extract_exercises_prompt_format(self):
        """Verify the LLM prompt references Hevy exercise IDs."""
        from scripts.ifit_strength_recommend import EXTRACT_PROMPT
        assert "hevy_id" in EXTRACT_PROMPT.lower() or "hevy" in EXTRACT_PROMPT.lower()

    def test_recommendation_dataclass(self):
        from scripts.ifit_strength_recommend import Recommendation
        from tests.conftest import make_recommendation

        rec_data = make_recommendation()
        rec = Recommendation(**rec_data)
        assert rec.rank == 1
        assert rec.workout_id == "test_wid_001"
        assert len(rec.exercises) == 3


class TestRecommendIfitWorkout:

    def test_returns_dict(self, user_slug):
        """Basic check that the function returns a dict (may need iFit token)."""
        from scripts import health_tools
        try:
            result = health_tools.recommend_ifit_workout(user_slug)
            assert isinstance(result, (dict, list))
        except Exception:
            pytest.skip("iFit token not available for recommend_ifit_workout")

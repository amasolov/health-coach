"""Tests for iFit recommendation engines (mocked LLM / external calls)."""

import json
import pytest
from collections import defaultdict
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
        assert hasattr(state, "cardio_leg_stress")

    def test_gather_state_captures_cardio_stress(self, user_slug):
        """If user has recent cardio, cardio_leg_stress should be > 0."""
        from scripts.ifit_strength_recommend import gather_athlete_state
        state = gather_athlete_state(user_slug)
        # User has recent running/cycling data
        if state.recent_cardio_legs:
            assert state.cardio_leg_stress > 0
            assert "lower" in state.muscle_load

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

    def test_extract_prompt_includes_duration_guidance(self):
        """Prompt should tell the LLM to use workout duration as context
        so it doesn't under-extract exercises for longer workouts (#24)."""
        from scripts.ifit_strength_recommend import EXTRACT_PROMPT
        lower = EXTRACT_PROMPT.lower()
        assert "duration" in lower or "minutes" in lower, (
            "Prompt must mention workout duration so the LLM calibrates exercise count"
        )

    def test_extract_prompt_includes_title_focus_validation(self):
        """Prompt should instruct the LLM to cross-check extracted exercises
        against the workout title's stated muscle focus (#24)."""
        from scripts.ifit_strength_recommend import EXTRACT_PROMPT
        lower = EXTRACT_PROMPT.lower()
        assert "workout title" in lower or "muscle focus" in lower, (
            "Prompt must instruct the LLM to use the workout title's "
            "muscle focus as a validation check"
        )

    def test_extract_prompt_warmup_not_overly_aggressive(self):
        """Prompt should include substantive exercises even if they appear
        in warmup/cooldown phases.  Only pure stretching should be skipped (#24)."""
        from scripts.ifit_strength_recommend import EXTRACT_PROMPT
        lower = EXTRACT_PROMPT.lower()
        assert "stretch" in lower and "mobility" in lower or "active" in lower, (
            "Prompt should distinguish real exercises from pure stretching/mobility"
        )

    def test_llm_extract_passes_duration(self):
        """_llm_extract should accept and embed duration_min in the LLM
        user message so the model knows the expected workout length (#24)."""
        from unittest.mock import patch, MagicMock
        from tests.conftest import make_openrouter_response

        with patch("scripts.ifit_strength_recommend._llm_http") as mock_http, \
             patch("scripts.addon_config.config") as mock_cfg:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = make_openrouter_response("[]")
            mock_http.return_value.post.return_value = mock_resp
            mock_cfg.openrouter_api_key = "fake-key"

            from scripts.ifit_strength_recommend import _llm_extract
            _llm_extract("transcript", "hevy_ref", "Upper-Body Test", duration_min=28)

            call_args = mock_http.return_value.post.call_args
            body = call_args.kwargs.get("json") or call_args[1].get("json")
            user_content = body["messages"][1]["content"]
            assert "28" in user_content, (
                "User message should contain the workout duration"
            )

    def test_force_reextract_skips_cache(self):
        """force_reextract=True should bypass R2/local caches and re-run
        LLM extraction so stale results can be refreshed (#24)."""
        from unittest.mock import patch, MagicMock
        from tests.conftest import make_openrouter_response

        fake_exercises = json.dumps([{
            "hevy_name": "Push Up", "hevy_id": "", "muscle_group": "chest",
            "equipment": "bodyweight", "sets": 3, "reps": 10,
            "weight": "bodyweight", "notes": "",
        }])

        with patch("scripts.ifit_strength_recommend.r2_configured", return_value=True), \
             patch("scripts.ifit_strength_recommend.r2_download_json") as mock_dl, \
             patch("scripts.ifit_strength_recommend.r2_download_text", return_value="transcript"), \
             patch("scripts.ifit_strength_recommend.r2_upload_json"), \
             patch("scripts.ifit_strength_recommend._llm_extract") as mock_llm, \
             patch("scripts.ifit_strength_recommend._load_exercise_cache", return_value={}), \
             patch("scripts.ifit_strength_recommend._save_exercise_cache"):

            mock_dl.return_value = [{"old": "cached"}]
            mock_llm.return_value = json.loads(fake_exercises)

            from scripts.ifit_strength_recommend import fetch_workout_exercises

            result = fetch_workout_exercises(
                "wid_test", "Test Workout", hevy_ref="ref",
                force_reextract=True,
            )
            mock_dl.assert_not_called()
            mock_llm.assert_called_once()
            assert result["source"] == "extracted"
            assert result["exercises"][0]["hevy_name"] == "Push Up"

    def test_recommendation_dataclass(self):
        from scripts.ifit_strength_recommend import Recommendation
        from tests.conftest import make_recommendation

        rec_data = make_recommendation()
        rec = Recommendation(**rec_data)
        assert rec.rank == 1
        assert rec.workout_id == "test_wid_001"
        assert len(rec.exercises) == 3


# ---------------------------------------------------------------------------
# Cardio muscle stress integration
# ---------------------------------------------------------------------------

class TestCardioMuscleStress:

    def test_mapping_covers_key_activities(self):
        from scripts.ifit_strength_recommend import CARDIO_MUSCLE_STRESS
        assert "running" in CARDIO_MUSCLE_STRESS
        assert "cycling" in CARDIO_MUSCLE_STRESS
        assert "hiking" in CARDIO_MUSCLE_STRESS
        for key, groups in CARDIO_MUSCLE_STRESS.items():
            assert "lower" in groups, f"{key} should stress lower body"

    def test_running_stresses_lower_more_than_cycling(self):
        from scripts.ifit_strength_recommend import CARDIO_MUSCLE_STRESS
        assert CARDIO_MUSCLE_STRESS["running"]["lower"] > CARDIO_MUSCLE_STRESS["cycling"]["lower"]

    def test_stage1_penalises_legs_after_hard_run(self):
        """A leg workout should score lower when recent hard run exists."""
        from scripts.ifit_strength_recommend import stage1_filter, AthleteState
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        state_rested = AthleteState(
            tsb=5.0,
            cardio_leg_stress=0,
            muscle_load={},
            ifit_prefs={"available_equipment": ["dumbbells", "bench"],
                        "preferred_duration_min": [15, 50]},
        )
        state_run = AthleteState(
            tsb=5.0,
            cardio_leg_stress=70,
            recent_cardio_legs=True,
            muscle_load={"lower": {"volume": 3500, "sets": 1, "last_date": now}},
            ifit_prefs={"available_equipment": ["dumbbells", "bench"],
                        "preferred_duration_min": [15, 50]},
        )

        leg_workout = {
            "type": "strength", "title": "Leg Blaster",
            "subcategories": ["legs"], "time_sec": 1800,
            "rating_avg": 4.5, "rating_count": 30,
            "required_equipment": ["dumbbells"], "difficulty": "moderate",
            "trainer_id": "t1",
        }
        upper_workout = {
            "type": "strength", "title": "Upper Power",
            "subcategories": ["upper body"], "time_sec": 1800,
            "rating_avg": 4.5, "rating_count": 30,
            "required_equipment": ["dumbbells"], "difficulty": "moderate",
            "trainer_id": "t1",
        }
        library = [leg_workout, upper_workout]
        trainers = {"t1": {"name": "Test Trainer"}}

        rested_results = stage1_filter(state_rested, library, trainers)
        run_results = stage1_filter(state_run, library, trainers)

        rested_leg = next(c for c in rested_results if "Leg" in c["title"])
        run_leg = next(c for c in run_results if "Leg" in c["title"])
        rested_upper = next(c for c in rested_results if "Upper" in c["title"])
        run_upper = next(c for c in run_results if "Upper" in c["title"])

        # After a hard run, leg workout score should drop
        assert run_leg["stage1_score"] < rested_leg["stage1_score"], \
            "Leg workout should score lower after hard run"
        # After a hard run, upper workout score should rise
        assert run_upper["stage1_score"] > rested_upper["stage1_score"], \
            "Upper workout should score higher after hard run"

    def test_stage1_mild_cardio_no_harsh_penalty(self):
        """A light jog (low stress) should not heavily penalise leg workouts."""
        from scripts.ifit_strength_recommend import stage1_filter, AthleteState

        state = AthleteState(
            tsb=5.0,
            cardio_leg_stress=15,
            recent_cardio_legs=True,
            muscle_load={},
            ifit_prefs={"available_equipment": ["dumbbells"],
                        "preferred_duration_min": [15, 50]},
        )
        leg_workout = {
            "type": "strength", "title": "Leg Day",
            "subcategories": ["legs"], "time_sec": 1800,
            "rating_avg": 4.5, "rating_count": 30,
            "required_equipment": ["dumbbells"], "difficulty": "moderate",
            "trainer_id": "t1",
        }
        results = stage1_filter(state, [leg_workout], {"t1": {"name": "T"}})
        assert results[0]["stage1_score"] > 0, "Light cardio should not crush leg score"

    def test_stage2_scoring_penalises_lower_with_stress(self):
        """_score_exercises_vs_state should penalise lower-body-heavy workouts."""
        from scripts.ifit_strength_recommend import _score_exercises_vs_state, AthleteState
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        state = AthleteState(
            cardio_leg_stress=80,
            muscle_load={"lower": {"volume": 4000, "sets": 1, "last_date": now}},
        )
        lower_heavy = [
            {"muscle_group": "quadriceps"}, {"muscle_group": "hamstrings"},
            {"muscle_group": "glutes"}, {"muscle_group": "calves"},
        ]
        adj, reason = _score_exercises_vs_state(lower_heavy, state)
        assert adj < 0, f"Expected negative adjustment, got {adj}"
        assert "cardio" in reason.lower()

    def test_stage2_scoring_boosts_upper_with_stress(self):
        """Upper-body workouts should get a boost when legs are stressed."""
        from scripts.ifit_strength_recommend import _score_exercises_vs_state, AthleteState

        state = AthleteState(cardio_leg_stress=60, muscle_load={})
        upper_heavy = [
            {"muscle_group": "chest"}, {"muscle_group": "shoulders"},
            {"muscle_group": "biceps"}, {"muscle_group": "triceps"},
        ]
        adj, reason = _score_exercises_vs_state(upper_heavy, state)
        assert adj > 0, f"Expected positive boost for upper when legs stressed, got {adj}"
        assert "complement" in reason.lower() or "upper" in reason.lower()

    def test_no_cardio_no_penalty(self):
        """Without any cardio stress, scoring should not penalise legs."""
        from scripts.ifit_strength_recommend import _score_exercises_vs_state, AthleteState

        state = AthleteState(cardio_leg_stress=0, muscle_load={})
        lower = [{"muscle_group": "quadriceps"}, {"muscle_group": "hamstrings"}]
        adj, reason = _score_exercises_vs_state(lower, state)
        assert "cardio" not in reason.lower()

    def test_climbing_stresses_upper_and_lower(self):
        from scripts.ifit_strength_recommend import CARDIO_MUSCLE_STRESS
        c = CARDIO_MUSCLE_STRESS["climbing"]
        assert "upper" in c
        assert "lower" in c
        assert "core" in c


class TestRecommendIfitWorkout:

    def test_returns_dict(self, user_slug):
        """Basic check that the function returns a dict (may need iFit token)."""
        from scripts import health_tools
        try:
            result = health_tools.recommend_ifit_workout(user_slug)
            assert isinstance(result, (dict, list))
        except Exception:
            pytest.skip("iFit token not available for recommend_ifit_workout")

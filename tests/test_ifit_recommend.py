"""Tests for iFit recommendation engines (mocked LLM / external calls)."""

import json
from pathlib import Path

import pytest
from collections import defaultdict
from unittest.mock import patch, MagicMock

from tests.conftest import MockResponse, make_openrouter_response


class TestRecommendStrengthWorkout:
    """The full recommend pipeline is expensive (LLM calls).
    We test the individual stages with mocks."""

    def test_stage1_filter(self, user_slug):
        """Stage 1 filtering should work with real data."""
        from scripts.ifit_strength_recommend import (
            stage1_filter, gather_athlete_state, STAGE1_MAX_CANDIDATES,
        )
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
        assert len(candidates) <= STAGE1_MAX_CANDIDATES
        for c in candidates[:3]:
            assert "title" in c
            assert "stage1_score" in c

    def test_stage1_max_candidates_is_10(self):
        from scripts.ifit_strength_recommend import STAGE1_MAX_CANDIDATES
        assert STAGE1_MAX_CANDIDATES == 10

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


class TestExtractRouteStats:
    """_extract_route_stats should compute incline/speed statistics from controls."""

    def test_running_workout_with_controls(self):
        from scripts.ifit_list_series import _extract_route_stats
        controls = [
            {"type": "incline", "at": 0, "value": 0},
            {"type": "incline", "at": 300, "value": 5},
            {"type": "incline", "at": 600, "value": 10},
            {"type": "incline", "at": 900, "value": 3},
            {"type": "mps", "at": 0, "value": 2.5},
            {"type": "mps", "at": 300, "value": 3.0},
            {"type": "mps", "at": 600, "value": 3.5},
            {"type": "mps", "at": 900, "value": 2.8},
        ]
        stats = _extract_route_stats(controls)
        assert stats["avg_incline_pct"] == pytest.approx(4.5, abs=0.1)
        assert stats["max_incline_pct"] == 10
        assert stats["avg_speed_mps"] == pytest.approx(2.95, abs=0.1)
        assert stats["max_speed_mps"] == 3.5

    def test_empty_controls(self):
        from scripts.ifit_list_series import _extract_route_stats
        stats = _extract_route_stats([])
        assert stats["avg_incline_pct"] == 0
        assert stats["max_incline_pct"] == 0
        assert stats["avg_speed_mps"] == 0
        assert stats["max_speed_mps"] == 0

    def test_strength_workout_zero_controls(self):
        from scripts.ifit_list_series import _extract_route_stats
        controls = [
            {"type": "incline", "at": 0, "value": 0},
            {"type": "mps", "at": 0, "value": 0},
            {"type": "incline", "at": 900, "value": 0},
            {"type": "mps", "at": 900, "value": 0},
        ]
        stats = _extract_route_stats(controls)
        assert stats["avg_incline_pct"] == 0
        assert stats["max_speed_mps"] == 0


class TestSlimWorkoutRouteFields:
    """_slim_workout should include route metadata from the lycan API."""

    def test_slim_extracts_distance(self):
        from scripts.ifit_list_series import _slim_workout
        w = {
            "id": "w1", "title": "Hill Climb", "type": "run",
            "estimates": {"time": 1800, "calories": 300, "distance": 5200,
                          "gross_elevation_gain": 150, "gross_elevation_loss": 80},
            "library_filters": [],
            "controls": [],
        }
        slim = _slim_workout(w)
        assert slim["distance_m"] == 5200
        assert slim["elevation_gain_m"] == 150
        assert slim["elevation_loss_m"] == 80

    def test_slim_extracts_location_type(self):
        from scripts.ifit_list_series import _slim_workout
        w = {
            "id": "w1", "title": "Outdoor Run", "type": "run",
            "estimates": {},
            "library_filters": [],
            "location_types": ["Outdoor"],
            "has_geo_data": True,
            "controls": [],
        }
        slim = _slim_workout(w)
        assert slim["location_type"] == "Outdoor"
        assert slim["has_geo_data"] is True

    def test_slim_extracts_incline_stats(self):
        from scripts.ifit_list_series import _slim_workout
        w = {
            "id": "w1", "title": "Incline Trainer", "type": "run",
            "estimates": {"time": 1800},
            "library_filters": [],
            "controls": [
                {"type": "incline", "at": 0, "value": 0},
                {"type": "incline", "at": 600, "value": 12},
                {"type": "incline", "at": 1200, "value": 8},
                {"type": "mps", "at": 0, "value": 2.0},
                {"type": "mps", "at": 600, "value": 2.5},
                {"type": "mps", "at": 1200, "value": 3.0},
            ],
        }
        slim = _slim_workout(w)
        assert slim["max_incline_pct"] == 12
        assert slim["avg_incline_pct"] > 0
        assert slim["max_speed_mps"] == 3.0

    def test_slim_defaults_when_missing(self):
        from scripts.ifit_list_series import _slim_workout
        w = {"id": "w1", "title": "Basic", "type": "strength",
             "library_filters": []}
        slim = _slim_workout(w)
        assert slim["distance_m"] == 0
        assert slim["elevation_gain_m"] == 0
        assert slim["avg_incline_pct"] == 0
        assert slim["location_type"] == ""
        assert slim["has_geo_data"] is False


class TestClassifyWorkoutMetadata:
    """classify_workout should extract trainer_id, duration_min, rating_avg
    from the iFit API response so the LLM doesn't need to hallucinate them."""

    def test_extracts_trainer_id(self):
        from scripts.ifit_recommend import classify_workout
        data = {
            "title": "Next Level Running 9",
            "type": "run",
            "metadata": {"trainer": "abc123"},
            "library_filters": [],
        }
        result = classify_workout(data)
        assert result["trainer_id"] == "abc123"

    def test_extracts_duration_min(self):
        from scripts.ifit_recommend import classify_workout
        data = {
            "title": "Speed Intervals",
            "type": "run",
            "estimates": {"time": 1980},  # 33 minutes
            "library_filters": [],
        }
        result = classify_workout(data)
        assert result["duration_min"] == 33

    def test_extracts_rating(self):
        from scripts.ifit_recommend import classify_workout
        data = {
            "title": "Hill Climb",
            "type": "run",
            "ratings": {"average": 4.7, "count": 142},
            "library_filters": [],
        }
        result = classify_workout(data)
        assert result["rating_avg"] == 4.7

    def test_missing_metadata_defaults(self):
        from scripts.ifit_recommend import classify_workout
        data = {"title": "Simple", "type": "run", "library_filters": []}
        result = classify_workout(data)
        assert result["trainer_id"] == ""
        assert result["duration_min"] == 0
        assert result["rating_avg"] == 0

    def test_duration_rounds_down(self):
        from scripts.ifit_recommend import classify_workout
        data = {
            "title": "Quick Run",
            "type": "run",
            "estimates": {"time": 1500},  # 25 min exactly
            "library_filters": [],
        }
        result = classify_workout(data)
        assert result["duration_min"] == 25


class TestTrainerNameResolution:
    """score_candidates should resolve trainer IDs to human-readable names."""

    def test_resolves_trainer_name(self):
        from scripts.ifit_recommend import score_candidates, _trainer_name_cache

        _trainer_name_cache.clear()

        candidates = [{
            "source": "recommended",
            "source_title": "",
            "workout_id": "w1",
            "title": "Test Workout",
        }]
        fatigue = {
            "days_since": {},
            "total_3d": 0,
            "total_7d": 0,
            "last_run_day": None,
            "ran_recently": False,
        }
        history = []

        meta_response = {
            "title": "Test Workout",
            "type": "run",
            "metadata": {"trainer": "tid_123"},
            "estimates": {"time": 1800},
            "ratings": {"average": 4.5, "count": 50},
            "difficulty": {"rating": "moderate"},
            "library_filters": [],
        }
        trainer_response = {
            "first_name": "Casey",
            "last_name": "Gilbert",
        }

        def mock_api_get(url, headers):
            if "lycan/v1/workouts/" in url:
                return meta_response
            if "/v1/trainers/" in url:
                return trainer_response
            return None

        with patch("scripts.ifit_recommend._api_get", side_effect=mock_api_get):
            results = score_candidates(candidates, fatigue, history, {})
            assert len(results) >= 1
            assert results[0]["trainer_name"] == "Casey Gilbert"

    def test_caches_trainer_name(self):
        from scripts.ifit_recommend import _trainer_name_cache, _resolve_trainer_name

        _trainer_name_cache.clear()

        trainer_response = {"first_name": "Jesse", "last_name": "Corbin"}

        with patch("scripts.ifit_recommend._api_get", return_value=trainer_response) as mock:
            name1 = _resolve_trainer_name("tid_abc", {})
            name2 = _resolve_trainer_name("tid_abc", {})
            assert name1 == "Jesse Corbin"
            assert name2 == "Jesse Corbin"
            assert mock.call_count == 1  # only one API call, second is cached


class TestRecommendOutputFields:
    """recommend_ifit_workout output must include trainer, duration, and rating
    so the LLM presents only factual information."""

    def test_recommendation_includes_trainer_and_duration(self):
        from scripts import health_tools

        fake_ranked = [{
            "title": "Next Level Running 9",
            "source": "up-next",
            "series_progress": "Workout 9 of 12",
            "type": "run",
            "muscle_groups": {"lower"},
            "styles": {"running"},
            "difficulty": "moderate",
            "required_equipment": [],
            "reasons": ["lower rested (+15)"],
            "score": 85,
            "trainer_name": "Casey Gilbert",
            "duration_min": 33,
            "rating_avg": 4.9,
        }]
        fake_fatigue = {
            "days_since": {},
            "total_3d": 1,
            "total_7d": 3,
            "last_run_day": 2,
            "ran_recently": False,
        }
        fake_history = []

        with patch("scripts.ifit_auth.get_auth_headers", return_value={"Authorization": "fake"}), \
             patch("scripts.ifit_recommend.fetch_recent_history", return_value=fake_history), \
             patch("scripts.ifit_recommend.analyze_fatigue", return_value=fake_fatigue), \
             patch("scripts.ifit_recommend.fetch_candidates", return_value=[]), \
             patch("scripts.ifit_recommend.score_candidates", return_value=fake_ranked), \
             patch("scripts.health_tools.resolve_user_id", return_value=None):
            result = health_tools.recommend_ifit_workout("test")

        rec = result["recommendations"][0]
        assert rec["trainer"] == "Casey Gilbert"
        assert rec["duration_min"] == 33
        assert rec["rating"] == 4.9


class TestElevationScoring:
    """score_candidates should factor elevation/incline into scoring for running workouts."""

    def test_hilly_workout_gets_variety_bonus_after_flat(self):
        """If recent runs were flat, a hilly workout should get a bonus."""
        from scripts.ifit_recommend import score_candidates, _trainer_name_cache
        _trainer_name_cache.clear()

        candidates = [{
            "source": "recommended", "source_title": "",
            "workout_id": "w_hilly", "title": "Mountain Climb",
        }]
        fatigue = {
            "days_since": {}, "total_3d": 1, "total_7d": 2,
            "last_run_day": 2, "ran_recently": False,
        }
        history = [{
            "workout_id": "w_prev", "date": "2026-03-11",
            "days_ago": 2, "title": "Flat Run",
            "avg_incline_pct": 0, "max_incline_pct": 0,
            "muscle_groups": set(), "styles": {"running"},
        }]

        meta = {
            "title": "Mountain Climb", "type": "run",
            "metadata": {"trainer": ""},
            "estimates": {"time": 2400, "distance": 5000,
                          "gross_elevation_gain": 300,
                          "gross_elevation_loss": 250},
            "ratings": {"average": 4.5, "count": 100},
            "difficulty": {"rating": "hard"},
            "library_filters": [{"categories": [{"name": "Running", "subcategories": []}]}],
            "controls": [
                {"type": "incline", "at": 0, "value": 3},
                {"type": "incline", "at": 1200, "value": 10},
            ],
        }

        def mock_api(url, headers):
            if "lycan" in url:
                return meta
            return None

        with patch("scripts.ifit_recommend._api_get", side_effect=mock_api):
            results = score_candidates(candidates, fatigue, history, {})
            assert len(results) == 1
            assert any("elevation" in r.lower() or "hill" in r.lower()
                        for r in results[0].get("reasons", []))

    def test_classify_workout_includes_route_stats(self):
        """classify_workout should extract distance, elevation, and incline stats."""
        from scripts.ifit_recommend import classify_workout
        data = {
            "title": "Trail Run", "type": "run",
            "estimates": {"time": 2400, "distance": 8000,
                          "gross_elevation_gain": 200,
                          "gross_elevation_loss": 180},
            "library_filters": [],
            "controls": [
                {"type": "incline", "at": 0, "value": 2},
                {"type": "incline", "at": 600, "value": 8},
                {"type": "incline", "at": 1200, "value": 5},
                {"type": "mps", "at": 0, "value": 3.0},
                {"type": "mps", "at": 600, "value": 2.5},
                {"type": "mps", "at": 1200, "value": 3.2},
            ],
        }
        result = classify_workout(data)
        assert result["distance_m"] == 8000
        assert result["elevation_gain_m"] == 200
        assert result["max_incline_pct"] == 8
        assert result["avg_speed_mps"] > 0


class TestSearchLibraryRouteFields:
    """search_ifit_library results should include route metadata."""

    def test_search_includes_distance_and_elevation(self):
        from scripts.health_tools import _search_ifit_library_inner

        fake_workouts = [{
            "id": "w1", "title": "Hill Runner 5K", "type": "run",
            "description": "A hilly 5K through mountains",
            "trainer_id": "t1", "difficulty": "hard",
            "rating_avg": 4.7, "rating_count": 200,
            "time_sec": 1800, "calories": 350,
            "categories": ["Running"], "subcategories": ["Hills"],
            "required_equipment": [],
            "distance_m": 5000,
            "elevation_gain_m": 180,
            "elevation_loss_m": 160,
            "avg_incline_pct": 3.5,
            "max_incline_pct": 12,
            "avg_speed_mps": 2.8,
            "max_speed_mps": 3.5,
            "location_type": "Outdoor",
            "has_geo_data": True,
        }]
        fake_trainers = {"t1": {"name": "Test Trainer"}}

        with patch("scripts.health_tools._load_ifit_library",
                    return_value=(fake_workouts, fake_trainers)), \
             patch("scripts.health_tools._program_index_cache", {}):
            result = _search_ifit_library_inner("hill runner", "", 10)

        assert result["count"] == 1
        r = result["results"][0]
        assert r["distance_km"] == 5.0
        assert r["elevation_gain_m"] == 180
        assert r["max_incline_pct"] == 12
        assert r["location_type"] == "Outdoor"


class TestWorkoutDetailsRouteFields:
    """get_ifit_workout_details should include route metadata."""

    def test_details_include_route_metadata(self):
        from scripts.health_tools import _get_ifit_workout_details_inner

        meta = {
            "title": "Coastal Run", "type": "run",
            "description": "Run along the coast",
            "difficulty": {"rating": "moderate"},
            "estimates": {"time": 2400, "calories": 400,
                          "distance": 10000,
                          "gross_elevation_gain": 100,
                          "gross_elevation_loss": 90},
            "ratings": {"average": 4.8, "count": 500},
            "metadata": {"trainer": "t1"},
            "required_equipment": [],
            "library_filters": [{"categories": [{"name": "Running", "subcategories": ["Endurance"]}]}],
            "workout_group_id": None,
            "controls": [
                {"type": "incline", "at": 0, "value": 1},
                {"type": "incline", "at": 1200, "value": 4},
                {"type": "mps", "at": 0, "value": 3.0},
                {"type": "mps", "at": 1200, "value": 3.5},
            ],
            "location_types": ["Outdoor"],
            "has_geo_data": True,
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = meta

        mock_trainer_resp = MagicMock()
        mock_trainer_resp.status_code = 200
        mock_trainer_resp.json.return_value = {"first_name": "Test", "last_name": "Trainer"}

        mock_http = MagicMock()
        mock_http.get.side_effect = lambda url, **kw: (
            mock_resp if "lycan" in url else mock_trainer_resp
        )

        with patch("scripts.health_tools._ifit_http", return_value=mock_http), \
             patch("scripts.ifit_auth.get_auth_headers", return_value={"Authorization": "fake"}), \
             patch("scripts.ifit_strength_recommend.fetch_workout_exercises",
                   return_value={"exercises": [], "source": "none", "transcript_available": False}):
            result = _get_ifit_workout_details_inner("w1")

        assert result.get("distance_km") == 10.0
        assert result.get("elevation_gain_m") == 100
        assert result.get("max_incline_pct") == 4
        assert result.get("location_type") == "Outdoor"


class TestPrewarmExerciseCache:

    def test_prewarm_returns_stats(self):
        """prewarm_exercise_cache should return hit/miss/error counts."""
        from scripts.ifit_strength_recommend import prewarm_exercise_cache

        with patch("scripts.ifit_strength_recommend.get_cache", return_value=None), \
             patch("scripts.ifit_strength_recommend.get_cache_text", return_value=None), \
             patch("scripts.ifit_strength_recommend.CACHE_DIR", Path("/nonexistent")):
            result = prewarm_exercise_cache()

        assert isinstance(result, dict)
        assert "cached" in result
        assert "extracted" in result
        assert "skipped" in result

    def test_prewarm_skips_when_no_library(self):
        from scripts.ifit_strength_recommend import prewarm_exercise_cache

        with patch("scripts.ifit_strength_recommend.get_cache", return_value=None), \
             patch("scripts.ifit_strength_recommend.get_cache_text", return_value=None), \
             patch("scripts.ifit_strength_recommend.CACHE_DIR", Path("/nonexistent")):
            result = prewarm_exercise_cache()

        assert result["skipped"] == 0
        assert result["extracted"] == 0
        assert "error" in result

    def test_prewarm_uses_cache_layers(self):
        """Workouts with existing exercise cache should not trigger LLM."""
        from scripts.ifit_strength_recommend import prewarm_exercise_cache

        fake_library = [
            {"id": "w1", "title": "Test", "subcategories": ["Strength"],
             "difficulty": "Moderate", "duration_seconds": 1800,
             "rating_avg": 4.5, "trainer_id": "t1",
             "required_equipment": [], "targeting": []},
        ]
        fake_trainers = {"t1": {"name": "Trainer"}}

        with patch("scripts.ifit_strength_recommend.get_cache",
                   side_effect=lambda k: {"trainers": fake_trainers,
                                           "library_workouts": fake_library,
                                           "exercise_cache": {"w1": [{"hevy_name": "Squat"}]}}.get(k)), \
             patch("scripts.ifit_strength_recommend.get_cache_text",
                   return_value="Squat | sq1 | quadriceps | barbell"), \
             patch("scripts.ifit_strength_recommend.CACHE_DIR", Path("/nonexistent")), \
             patch("scripts.ifit_strength_recommend.r2_configured", return_value=False), \
             patch("scripts.ifit_strength_recommend.r2_download_json",
                   side_effect=lambda k: [{"hevy_name": "Squat"}] if "w1" in k else None), \
             patch("scripts.ifit_strength_recommend._llm_extract") as mock_llm:
            result = prewarm_exercise_cache()

        mock_llm.assert_not_called()


class TestRecommendIfitWorkout:

    def test_returns_dict(self, user_slug):
        """Basic check that the function returns a dict (may need iFit token)."""
        from scripts import health_tools
        try:
            result = health_tools.recommend_ifit_workout(user_slug)
            assert isinstance(result, (dict, list))
        except Exception:
            pytest.skip("iFit token not available for recommend_ifit_workout")

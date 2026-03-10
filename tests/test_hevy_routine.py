"""Tests for Hevy routine creation, exercise resolver, and feedback loop.

All Hevy API calls are mocked — no real routines are created.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from scripts import health_tools
from tests.conftest import (
    FakeR2Store, MockResponse,
    make_hevy_routine_response, make_hevy_exercise_template_response,
    make_openrouter_response, make_recommendation,
    make_ifit_exercises, SAMPLE_HEVY_EXERCISES_JSON,
)


# ---------------------------------------------------------------------------
# Exercise resolver
# ---------------------------------------------------------------------------

class TestHevyExerciseResolver:

    @pytest.fixture(autouse=True)
    def _setup_hevy_exercises(self, tmp_path):
        """Write a temp hevy_exercises.json for the resolver to load."""
        exercises_path = tmp_path / "hevy_exercises.json"
        exercises_path.write_text(json.dumps(SAMPLE_HEVY_EXERCISES_JSON))
        self._exercises_path = exercises_path

    def test_id_match(self, _setup_hevy_exercises):
        from scripts.hevy_exercise_resolver import resolve_hevy_exercises
        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", self._exercises_path), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False):
            exercises = [{"hevy_name": "Squat (Barbell)", "hevy_id": "ABC123",
                          "muscle_group": "quadriceps", "sets": 3, "reps": 10, "weight": "barbell", "notes": ""}]
            result = resolve_hevy_exercises(exercises, hevy_api_key="test-key")
            assert result[0]["resolution"] == "id_match"
            assert result[0]["hevy_id"] == "ABC123"

    def test_fuzzy_match(self, _setup_hevy_exercises):
        from scripts.hevy_exercise_resolver import resolve_hevy_exercises
        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", self._exercises_path), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False):
            exercises = [{"hevy_name": "Bicep Curl (Dumbbell)", "hevy_id": "",
                          "muscle_group": "biceps", "sets": 3, "reps": 12, "weight": "dumbbell", "notes": ""}]
            result = resolve_hevy_exercises(exercises, hevy_api_key="test-key")
            assert result[0]["resolution"] == "fuzzy_match"
            assert result[0]["hevy_id"] == "GHI789"

    def test_custom_creation_mocked(self, _setup_hevy_exercises, tmp_path):
        from scripts.hevy_exercise_resolver import resolve_hevy_exercises

        classify_response = json.dumps({
            "exercise_type": "bodyweight_reps",
            "equipment_category": "none",
            "muscle_group": "abdominals",
            "other_muscles": [],
        })

        empty_custom_map = tmp_path / "hevy_custom_map.json"
        empty_custom_map.write_text("{}")

        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", self._exercises_path), \
             patch("scripts.hevy_exercise_resolver.CUSTOM_MAP_PATH", empty_custom_map), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.hevy_exercise_resolver.httpx.post") as mock_post:

            mock_post.side_effect = [
                MockResponse(200, {"choices": [{"message": {"content": classify_response}}]}),
                make_hevy_exercise_template_response("CUSTOM001", "Plank Hold"),
            ]

            exercises = [{"hevy_name": "Plank Hold", "hevy_id": "",
                          "muscle_group": "abdominals", "sets": 3, "reps": "30s", "weight": "bodyweight", "notes": ""}]
            result = resolve_hevy_exercises(exercises, hevy_api_key="test-key")
            assert result[0]["resolution"] == "custom_created"
            assert result[0]["hevy_id"] == "CUSTOM001"

    def test_cached_custom_map(self, _setup_hevy_exercises):
        from scripts.hevy_exercise_resolver import resolve_hevy_exercises

        fake_r2 = FakeR2Store({"hevy/custom_exercise_map.json": {"plank hold": "CACHED001"}})
        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", self._exercises_path), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=True), \
             patch("scripts.hevy_exercise_resolver._r2_download_json", fake_r2.download_json), \
             patch("scripts.hevy_exercise_resolver._r2_upload_json", fake_r2.upload_json):

            exercises = [{"hevy_name": "Plank Hold", "hevy_id": "",
                          "muscle_group": "abdominals", "sets": 3, "reps": "30s", "weight": "", "notes": ""}]
            result = resolve_hevy_exercises(exercises, hevy_api_key="test-key")
            assert result[0]["resolution"] == "custom_cached"
            assert result[0]["hevy_id"] == "CACHED001"

    def test_r2_resolution_cache(self, _setup_hevy_exercises):
        from scripts.hevy_exercise_resolver import resolve_hevy_exercises

        cached_resolved = [
            {"hevy_name": "Squat", "hevy_id": "ABC123", "resolution": "id_match",
             "muscle_group": "quadriceps", "sets": 3, "reps": 10, "weight": "", "notes": ""},
        ]
        fake_r2 = FakeR2Store({"hevy/resolved/wid123.json": cached_resolved})
        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", self._exercises_path), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=True), \
             patch("scripts.hevy_exercise_resolver._r2_download_json", fake_r2.download_json):
            result = resolve_hevy_exercises([], hevy_api_key="test-key", workout_id="wid123")
            assert len(result) == 1
            assert result[0]["hevy_id"] == "ABC123"


# ---------------------------------------------------------------------------
# Routine creation (mocked Hevy API)
# ---------------------------------------------------------------------------

class TestCreateHevyRoutine:

    @pytest.fixture
    def recommendations_file(self, tmp_path):
        rec = make_recommendation()
        cache_path = tmp_path / "recommendations.json"
        cache_path.write_text(json.dumps([rec]))
        return cache_path

    def test_create_routine_mocked(self, recommendations_file, tmp_path):
        from scripts.ifit_strength_recommend import create_hevy_routine, Recommendation

        rec_data = json.loads(recommendations_file.read_text())[0]
        rec = Recommendation(**rec_data)

        hevy_exercises_path = tmp_path / "hevy_exercises.json"
        hevy_exercises_path.write_text(json.dumps(SAMPLE_HEVY_EXERCISES_JSON))

        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", hevy_exercises_path), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.hevy_exercise_resolver.httpx.post") as mock_classify, \
             patch("scripts.ifit_strength_recommend.httpx.post") as mock_hevy, \
             patch("scripts.ifit_strength_recommend._save_routine_mapping"):

            classify_json = json.dumps({
                "exercise_type": "duration", "equipment_category": "none",
                "muscle_group": "abdominals", "other_muscles": [],
            })
            mock_classify.side_effect = [
                MockResponse(200, {"choices": [{"message": {"content": classify_json}}]}),
                make_hevy_exercise_template_response("CUSTOM_PLANK", "Plank"),
            ]
            mock_hevy.return_value = make_hevy_routine_response("routine-abc", "iFit: Test Strength Workout")

            result = create_hevy_routine(rec, "test-hevy-key")
            assert result["status"] == "created"
            assert result["routine_id"] == "routine-abc"

    def test_create_routine_from_recommendation_index(self, recommendations_file, tmp_path):
        hevy_exercises_path = tmp_path / "hevy_exercises.json"
        hevy_exercises_path.write_text(json.dumps(SAMPLE_HEVY_EXERCISES_JSON))

        with patch("scripts.health_tools.ROOT", tmp_path.parent), \
             patch("scripts.ifit_strength_recommend.create_hevy_routine") as mock_create:
            (tmp_path.parent / ".ifit_capture").mkdir(exist_ok=True)
            import shutil
            shutil.copy(recommendations_file, tmp_path.parent / ".ifit_capture" / "recommendations.json")
            mock_create.return_value = {"status": "created", "routine_id": "r-001"}

            result = health_tools.create_hevy_routine_from_recommendation(
                user_slug="test", recommendation_index=0, hevy_api_key="key"
            )
            assert result["status"] == "created"


# ---------------------------------------------------------------------------
# Routine review
# ---------------------------------------------------------------------------

class TestGetHevyRoutineReview:

    def test_review_by_ifit_id(self, mock_r2, fake_r2):
        mapping = {
            "routine-abc": {
                "ifit_workout_id": "ifit_w001",
                "title": "iFit: Test Workout",
                "predicted_exercises": [
                    {"hevy_name": "Squat", "hevy_id": "ABC", "sets": 3, "reps": 10,
                     "weight": "barbell", "resolution": "id_match", "muscle_group": "quadriceps"},
                ],
                "created_at": "2026-03-09T10:00:00",
            }
        }
        fake_r2.upload_json("hevy/routine_map.json", mapping)
        result = health_tools.get_hevy_routine_review("test", ifit_workout_id="ifit_w001")
        assert result["hevy_routine_id"] == "routine-abc"
        assert result["title"] == "iFit: Test Workout"
        assert len(result["predicted_exercises"]) == 1

    def test_review_by_routine_id(self, mock_r2, fake_r2):
        mapping = {
            "routine-xyz": {
                "ifit_workout_id": "ifit_w002",
                "title": "iFit: Another Workout",
                "predicted_exercises": [],
                "created_at": "2026-03-09T10:00:00",
            }
        }
        fake_r2.upload_json("hevy/routine_map.json", mapping)
        result = health_tools.get_hevy_routine_review("test", hevy_routine_id="routine-xyz")
        assert result["ifit_workout_id"] == "ifit_w002"

    def test_review_not_found(self, mock_r2, fake_r2):
        fake_r2.upload_json("hevy/routine_map.json", {})
        result = health_tools.get_hevy_routine_review("test", ifit_workout_id="missing")
        assert "error" in result

    def test_no_mapping_at_all(self, mock_r2, fake_r2):
        result = health_tools.get_hevy_routine_review("test", ifit_workout_id="any")
        assert "error" in result


# ---------------------------------------------------------------------------
# Exercise feedback
# ---------------------------------------------------------------------------

class TestApplyExerciseFeedback:

    def test_update_exercise(self, mock_r2, fake_r2):
        exercises = [
            {"hevy_name": "Squat", "hevy_id": "ABC", "muscle_group": "quadriceps",
             "sets": 3, "reps": 10, "weight": "barbell", "notes": ""},
            {"hevy_name": "Curl", "hevy_id": "", "muscle_group": "biceps",
             "sets": 3, "reps": 12, "weight": "dumbbell", "notes": ""},
        ]
        fake_r2.upload_json("exercises/wid001.json", exercises)

        corrections = [
            {"action": "update", "exercise_name": "Squat", "sets": 4, "reps": "8", "weight": "barbell 60kg"},
        ]
        result = health_tools.apply_exercise_feedback("test", "wid001", corrections)
        assert result["status"] == "applied"

        updated = fake_r2.download_json("exercises/wid001.json")
        squat = [e for e in updated if "squat" in e.get("hevy_name", "").lower()][0]
        assert squat["sets"] == 4
        assert squat["reps"] == "8"
        assert squat["weight"] == "barbell 60kg"
        assert squat["user_corrected"] is True

    def test_add_exercise(self, mock_r2, fake_r2):
        fake_r2.upload_json("exercises/wid002.json", [
            {"hevy_name": "Existing", "hevy_id": "", "sets": 3, "reps": 10},
        ])
        corrections = [
            {"action": "add", "exercise_name": "New Exercise",
             "new_name": "Bulgarian Split Squat", "sets": 3, "reps": 10,
             "muscle_group": "quadriceps"},
        ]
        result = health_tools.apply_exercise_feedback("test", "wid002", corrections)
        assert result["status"] == "applied"
        updated = fake_r2.download_json("exercises/wid002.json")
        assert len(updated) == 2
        assert updated[1]["hevy_name"] == "Bulgarian Split Squat"

    def test_remove_exercise(self, mock_r2, fake_r2):
        exercises = [
            {"hevy_name": "Keep", "hevy_id": "", "sets": 3, "reps": 10},
            {"hevy_name": "Remove Me", "hevy_id": "", "sets": 3, "reps": 10},
        ]
        fake_r2.upload_json("exercises/wid003.json", exercises)
        corrections = [{"action": "remove", "exercise_name": "Remove Me"}]
        result = health_tools.apply_exercise_feedback("test", "wid003", corrections)
        assert result["updated_exercise_count"] == 1
        updated = fake_r2.download_json("exercises/wid003.json")
        assert len(updated) == 1
        assert updated[0]["hevy_name"] == "Keep"

    def test_clears_resolved_cache(self, mock_r2, fake_r2):
        fake_r2.upload_json("exercises/wid004.json", [{"hevy_name": "X", "hevy_id": ""}])
        fake_r2.upload_json("hevy/resolved/wid004.json", [{"cached": True}])
        corrections = [{"action": "update", "exercise_name": "X", "sets": 5}]
        health_tools.apply_exercise_feedback("test", "wid004", corrections)
        assert not fake_r2.exists("hevy/resolved/wid004.json")

    def test_no_exercises_found(self, mock_r2, fake_r2):
        result = health_tools.apply_exercise_feedback("test", "missing_wid", [])
        assert "error" in result

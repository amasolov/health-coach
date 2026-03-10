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

    def test_custom_creation_json_response(self, _setup_hevy_exercises, tmp_path):
        """Hevy API returns JSON with exercise_template.id -- should extract ID."""
        from scripts.hevy_exercise_resolver import resolve_hevy_exercises

        classify_response = json.dumps({
            "exercise_type": "bodyweight_reps",
            "equipment_category": "none",
            "muscle_group": "abdominals",
            "other_muscles": [],
        })

        empty_custom_map = tmp_path / "hevy_custom_map.json"
        empty_custom_map.write_text("{}")

        no_existing = MockResponse(200, {"exercise_templates": [], "page_count": 1})

        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", self._exercises_path), \
             patch("scripts.hevy_exercise_resolver.CUSTOM_MAP_PATH", empty_custom_map), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.hevy_exercise_resolver.httpx.post") as mock_post, \
             patch("scripts.hevy_exercise_resolver.httpx.get", return_value=no_existing):

            mock_post.side_effect = [
                MockResponse(200, {"choices": [{"message": {"content": classify_response}}]}),
                make_hevy_exercise_template_response("CUSTOM001", "Plank Hold"),
            ]

            exercises = [{"hevy_name": "Plank Hold", "hevy_id": "",
                          "muscle_group": "abdominals", "sets": 3, "reps": "30s", "weight": "bodyweight", "notes": ""}]
            result = resolve_hevy_exercises(exercises, hevy_api_key="test-key")
            assert result[0]["resolution"] == "custom_created"
            assert result[0]["hevy_id"] == "CUSTOM001"

    def test_custom_creation_raw_uuid_response(self, _setup_hevy_exercises, tmp_path):
        """Hevy API returns raw UUID string (text/html) -- the real-world format."""
        from scripts.hevy_exercise_resolver import resolve_hevy_exercises

        classify_response = json.dumps({
            "exercise_type": "weight_reps",
            "equipment_category": "dumbbell",
            "muscle_group": "biceps",
            "other_muscles": [],
        })

        empty_custom_map = tmp_path / "hevy_custom_map.json"
        empty_custom_map.write_text("{}")

        uuid_resp = MagicMock()
        uuid_resp.status_code = 200
        uuid_resp.json.side_effect = Exception("not JSON")
        uuid_resp.text = "c651524e-d332-40d1-9351-dcaf665f6853"

        no_existing = MockResponse(200, {"exercise_templates": [], "page_count": 1})

        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", self._exercises_path), \
             patch("scripts.hevy_exercise_resolver.CUSTOM_MAP_PATH", empty_custom_map), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.hevy_exercise_resolver.httpx.post") as mock_post, \
             patch("scripts.hevy_exercise_resolver.httpx.get", return_value=no_existing):

            mock_post.side_effect = [
                MockResponse(200, {"choices": [{"message": {"content": classify_response}}]}),
                uuid_resp,
            ]

            exercises = [{"hevy_name": "Dumbbell Bicep Hold", "hevy_id": "",
                          "muscle_group": "biceps", "sets": 3, "reps": "45s", "weight": "dumbbell", "notes": ""}]
            result = resolve_hevy_exercises(exercises, hevy_api_key="test-key")
            assert result[0]["resolution"] == "custom_created"
            assert result[0]["hevy_id"] == "c651524e-d332-40d1-9351-dcaf665f6853"

    def test_custom_creation_integer_id(self, _setup_hevy_exercises, tmp_path):
        """Hevy API returns JSON with integer id -- should convert to string."""
        from scripts.hevy_exercise_resolver import resolve_hevy_exercises

        classify_response = json.dumps({
            "exercise_type": "bodyweight_reps",
            "equipment_category": "none",
            "muscle_group": "abdominals",
            "other_muscles": [],
        })

        empty_custom_map = tmp_path / "hevy_custom_map.json"
        empty_custom_map.write_text("{}")

        no_existing = MockResponse(200, {"exercise_templates": [], "page_count": 1})

        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", self._exercises_path), \
             patch("scripts.hevy_exercise_resolver.CUSTOM_MAP_PATH", empty_custom_map), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.hevy_exercise_resolver.httpx.post") as mock_post, \
             patch("scripts.hevy_exercise_resolver.httpx.get", return_value=no_existing):

            mock_post.side_effect = [
                MockResponse(200, {"choices": [{"message": {"content": classify_response}}]}),
                MockResponse(200, {"id": 12345}),
            ]

            exercises = [{"hevy_name": "Plank Hold", "hevy_id": "",
                          "muscle_group": "abdominals", "sets": 3, "reps": "30s", "weight": "bodyweight", "notes": ""}]
            result = resolve_hevy_exercises(exercises, hevy_api_key="test-key")
            assert result[0]["resolution"] == "custom_created"
            assert result[0]["hevy_id"] == "12345"
            assert isinstance(result[0]["hevy_id"], str)

    def test_custom_creation_dedup_existing(self, _setup_hevy_exercises, tmp_path):
        """If exercise already exists in Hevy, reuse it instead of creating a duplicate."""
        from scripts.hevy_exercise_resolver import resolve_hevy_exercises

        classify_response = json.dumps({
            "exercise_type": "weight_reps",
            "equipment_category": "dumbbell",
            "muscle_group": "biceps",
            "other_muscles": [],
        })

        empty_custom_map = tmp_path / "hevy_custom_map.json"
        empty_custom_map.write_text("{}")

        existing_resp = MockResponse(200, {
            "exercise_templates": [
                {"id": "EXISTING-001", "title": "Dumbbell Bicep Hold", "is_custom": True},
            ],
            "page_count": 1,
        })

        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", self._exercises_path), \
             patch("scripts.hevy_exercise_resolver.CUSTOM_MAP_PATH", empty_custom_map), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.hevy_exercise_resolver.httpx.post") as mock_post, \
             patch("scripts.hevy_exercise_resolver.httpx.get", return_value=existing_resp):

            mock_post.side_effect = [
                MockResponse(200, {"choices": [{"message": {"content": classify_response}}]}),
            ]

            exercises = [{"hevy_name": "Dumbbell Bicep Hold", "hevy_id": "",
                          "muscle_group": "biceps", "sets": 3, "reps": "45s", "weight": "dumbbell", "notes": ""}]
            result = resolve_hevy_exercises(exercises, hevy_api_key="test-key")
            assert result[0]["resolution"] == "custom_created"
            assert result[0]["hevy_id"] == "EXISTING-001"

    def test_custom_creation_truly_empty_response(self, _setup_hevy_exercises, tmp_path):
        """Hevy API returns 200 but completely empty body -- should fail gracefully."""
        from scripts.hevy_exercise_resolver import resolve_hevy_exercises

        classify_response = json.dumps({
            "exercise_type": "bodyweight_reps",
            "equipment_category": "none",
            "muscle_group": "abdominals",
            "other_muscles": [],
        })

        empty_custom_map = tmp_path / "hevy_custom_map.json"
        empty_custom_map.write_text("{}")

        empty_resp = MagicMock()
        empty_resp.status_code = 200
        empty_resp.json.side_effect = Exception("empty body")
        empty_resp.text = ""

        no_existing = MockResponse(200, {"exercise_templates": [], "page_count": 1})

        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", self._exercises_path), \
             patch("scripts.hevy_exercise_resolver.CUSTOM_MAP_PATH", empty_custom_map), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.hevy_exercise_resolver.httpx.post") as mock_post, \
             patch("scripts.hevy_exercise_resolver.httpx.get", return_value=no_existing):

            mock_post.side_effect = [
                MockResponse(200, {"choices": [{"message": {"content": classify_response}}]}),
                empty_resp,
            ]

            exercises = [{"hevy_name": "Plank Hold", "hevy_id": "",
                          "muscle_group": "abdominals", "sets": 3, "reps": "30s", "weight": "bodyweight", "notes": ""}]
            result = resolve_hevy_exercises(exercises, hevy_api_key="test-key")
            assert result[0]["resolution"] == "creation_failed"
            assert result[0]["hevy_id"] == ""

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
# Duplicate detection
# ---------------------------------------------------------------------------

class TestDuplicateDetection:

    def test_duplicate_found_by_ifit_mapping(self):
        """If routine map already has this iFit workout ID, return existing."""
        from scripts.ifit_strength_recommend import create_hevy_routine, Recommendation

        rec = Recommendation(**make_recommendation(workout_id="ifit_w100", title="Upper Pull"))

        mapping = {
            "hevy-r-001": {
                "ifit_workout_id": "ifit_w100",
                "title": "iFit: Upper Pull",
                "predicted_exercises": [],
                "created_at": "2026-03-09T10:00:00",
            }
        }

        get_routine_resp = MockResponse(200, {
            "routine": {
                "id": "hevy-r-001",
                "title": "iFit: Upper Pull",
                "exercises": [{"id": "e1"}, {"id": "e2"}, {"id": "e3"}],
            }
        })

        with patch("scripts.ifit_strength_recommend._load_routine_map", return_value=mapping), \
             patch("scripts.ifit_strength_recommend.httpx.get", return_value=get_routine_resp):
            result = create_hevy_routine(rec, "test-key")
            assert result["status"] == "already_exists"
            assert result["routine_id"] == "hevy-r-001"
            assert result["exercise_count"] == 3

    def test_duplicate_found_by_title_match(self):
        """If no mapping but Hevy has a routine with matching title, return it."""
        from scripts.ifit_strength_recommend import create_hevy_routine, Recommendation

        rec = Recommendation(**make_recommendation(workout_id="ifit_w200", title="Lower Push"))

        list_resp = MockResponse(200, {
            "routines": [
                {"id": "hevy-r-050", "title": "Personal Routine", "exercises": []},
                {"id": "hevy-r-051", "title": "iFit: Lower Push", "exercises": [{"id": "e1"}, {"id": "e2"}]},
            ]
        })

        with patch("scripts.ifit_strength_recommend._load_routine_map", return_value={}), \
             patch("scripts.ifit_strength_recommend.httpx.get", return_value=list_resp):
            result = create_hevy_routine(rec, "test-key")
            assert result["status"] == "already_exists"
            assert result["routine_id"] == "hevy-r-051"
            assert result["exercise_count"] == 2

    def test_no_duplicate_proceeds_to_create(self, tmp_path):
        """If no existing routine found, proceed with creation."""
        from scripts.ifit_strength_recommend import create_hevy_routine, Recommendation

        all_resolved = [
            {"hevy_name": "Squat (Barbell)", "hevy_id": "ABC123", "muscle_group": "quadriceps",
             "sets": 3, "reps": 10, "weight": "barbell", "notes": "", "equipment": "barbell"},
        ]
        rec = Recommendation(**make_recommendation(
            workout_id="ifit_w300", title="New Workout", exercises=all_resolved,
        ))

        hevy_exercises_path = tmp_path / "hevy_exercises.json"
        hevy_exercises_path.write_text(json.dumps(SAMPLE_HEVY_EXERCISES_JSON))

        list_resp = MockResponse(200, {
            "routines": [
                {"id": "hevy-r-099", "title": "Some Other Routine", "exercises": []},
            ]
        })

        with patch("scripts.ifit_strength_recommend._load_routine_map", return_value={}), \
             patch("scripts.ifit_strength_recommend.httpx.get", return_value=list_resp), \
             patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", hevy_exercises_path), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.ifit_strength_recommend.httpx.post") as mock_post, \
             patch("scripts.ifit_strength_recommend._save_routine_mapping"):

            mock_post.return_value = make_hevy_routine_response("new-r-001", "iFit: New Workout")

            result = create_hevy_routine(rec, "test-key")
            assert result["status"] == "created"
            assert result["routine_id"] == "new-r-001"

    def test_stale_mapping_deleted_routine(self, tmp_path):
        """If mapping points to a deleted routine, fall back to title check then create."""
        from scripts.ifit_strength_recommend import create_hevy_routine, Recommendation

        all_resolved = [
            {"hevy_name": "Squat (Barbell)", "hevy_id": "ABC123", "muscle_group": "quadriceps",
             "sets": 3, "reps": 10, "weight": "barbell", "notes": "", "equipment": "barbell"},
        ]
        rec = Recommendation(**make_recommendation(
            workout_id="ifit_w400", title="Deleted Workout", exercises=all_resolved,
        ))

        mapping = {
            "hevy-r-gone": {
                "ifit_workout_id": "ifit_w400",
                "title": "iFit: Deleted Workout",
                "predicted_exercises": [],
                "created_at": "2026-03-01T10:00:00",
            }
        }

        hevy_exercises_path = tmp_path / "hevy_exercises.json"
        hevy_exercises_path.write_text(json.dumps(SAMPLE_HEVY_EXERCISES_JSON))

        def mock_get(url, **kwargs):
            if "hevy-r-gone" in url:
                return MockResponse(404, {"error": "Not found"})
            return MockResponse(200, {"routines": []})

        with patch("scripts.ifit_strength_recommend._load_routine_map", return_value=mapping), \
             patch("scripts.ifit_strength_recommend.httpx.get", side_effect=mock_get), \
             patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", hevy_exercises_path), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.ifit_strength_recommend.httpx.post") as mock_post, \
             patch("scripts.ifit_strength_recommend._save_routine_mapping"):

            mock_post.return_value = make_hevy_routine_response("new-r-002", "iFit: Deleted Workout")

            result = create_hevy_routine(rec, "test-key")
            assert result["status"] == "created"
            assert result["routine_id"] == "new-r-002"


# ---------------------------------------------------------------------------
# Routine creation (mocked Hevy API)
# ---------------------------------------------------------------------------

class TestCreateHevyRoutine:

    def test_create_routine_mocked(self, tmp_path):
        from scripts.ifit_strength_recommend import create_hevy_routine, Recommendation

        all_resolved_exercises = [
            {"hevy_name": "Squat (Barbell)", "hevy_id": "ABC123", "muscle_group": "quadriceps",
             "sets": 3, "reps": 10, "weight": "barbell", "notes": "", "equipment": "barbell"},
            {"hevy_name": "Bent Over Row", "hevy_id": "DEF456", "muscle_group": "lats",
             "sets": 4, "reps": 8, "weight": "barbell 40kg", "notes": "", "equipment": "barbell"},
        ]
        rec = Recommendation(**make_recommendation(exercises=all_resolved_exercises))

        hevy_exercises_path = tmp_path / "hevy_exercises.json"
        hevy_exercises_path.write_text(json.dumps(SAMPLE_HEVY_EXERCISES_JSON))

        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", hevy_exercises_path), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.ifit_strength_recommend.httpx.post") as mock_hevy, \
             patch("scripts.ifit_strength_recommend._save_routine_mapping"):

            mock_hevy.return_value = make_hevy_routine_response("routine-abc", "iFit: Test Strength Workout")

            result = create_hevy_routine(rec, "test-hevy-key")
            assert result["status"] == "created"
            assert result["routine_id"] == "routine-abc"
            assert "skipped_exercises" not in result

    def test_create_routine_hevy_empty_response(self, tmp_path):
        """Hevy API returns 201 but empty body -- should still return 'created'."""
        from scripts.ifit_strength_recommend import create_hevy_routine, Recommendation

        all_resolved_exercises = [
            {"hevy_name": "Squat (Barbell)", "hevy_id": "ABC123", "muscle_group": "quadriceps",
             "sets": 3, "reps": 10, "weight": "barbell", "notes": "", "equipment": "barbell"},
        ]
        rec = Recommendation(**make_recommendation(exercises=all_resolved_exercises))

        hevy_exercises_path = tmp_path / "hevy_exercises.json"
        hevy_exercises_path.write_text(json.dumps(SAMPLE_HEVY_EXERCISES_JSON))

        empty_resp = MagicMock()
        empty_resp.status_code = 201
        empty_resp.json.side_effect = Exception("empty body")
        empty_resp.text = ""

        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", hevy_exercises_path), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.ifit_strength_recommend.httpx.post") as mock_hevy, \
             patch("scripts.ifit_strength_recommend._save_routine_mapping"):

            mock_hevy.return_value = empty_resp

            result = create_hevy_routine(rec, "test-hevy-key")
            assert result["status"] == "created"
            assert result["routine_id"] == ""

    def test_create_routine_hevy_api_error(self, tmp_path):
        """Hevy API returns 400 error -- should return error dict."""
        from scripts.ifit_strength_recommend import create_hevy_routine, Recommendation

        all_resolved_exercises = [
            {"hevy_name": "Squat (Barbell)", "hevy_id": "ABC123", "muscle_group": "quadriceps",
             "sets": 3, "reps": 10, "weight": "barbell", "notes": "", "equipment": "barbell"},
        ]
        rec = Recommendation(**make_recommendation(exercises=all_resolved_exercises))

        hevy_exercises_path = tmp_path / "hevy_exercises.json"
        hevy_exercises_path.write_text(json.dumps(SAMPLE_HEVY_EXERCISES_JSON))

        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", hevy_exercises_path), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.ifit_strength_recommend.httpx.post") as mock_hevy:

            mock_hevy.return_value = MockResponse(400, {"error": "Invalid request body"}, text='{"error":"Invalid request body"}')

            result = create_hevy_routine(rec, "test-hevy-key")
            assert "error" in result

    def test_create_routine_incomplete(self, tmp_path):
        """Routine created but some exercises could not be resolved -- status is created_incomplete."""
        from scripts.ifit_strength_recommend import create_hevy_routine, Recommendation

        exercises = [
            {"hevy_name": "Squat (Barbell)", "hevy_id": "ABC123", "muscle_group": "quadriceps",
             "sets": 3, "reps": 10, "weight": "barbell", "notes": "", "equipment": "barbell"},
            {"hevy_name": "Impossible Exercise", "hevy_id": "", "muscle_group": "biceps",
             "sets": 3, "reps": 12, "weight": "dumbbell", "notes": "", "equipment": "dumbbell"},
        ]
        rec = Recommendation(**make_recommendation(exercises=exercises))

        hevy_exercises_path = tmp_path / "hevy_exercises.json"
        hevy_exercises_path.write_text(json.dumps(SAMPLE_HEVY_EXERCISES_JSON))

        empty_custom_map = tmp_path / "hevy_custom_map.json"
        empty_custom_map.write_text("{}")

        with patch("scripts.hevy_exercise_resolver.EXERCISES_JSON", hevy_exercises_path), \
             patch("scripts.hevy_exercise_resolver.CUSTOM_MAP_PATH", empty_custom_map), \
             patch("scripts.hevy_exercise_resolver._r2_available", return_value=False), \
             patch("scripts.hevy_exercise_resolver._create_custom_exercise", return_value=None), \
             patch("scripts.ifit_strength_recommend.httpx.post") as mock_hevy, \
             patch("scripts.ifit_strength_recommend._save_routine_mapping"):

            mock_hevy.return_value = make_hevy_routine_response("routine-partial", "iFit: Partial")

            result = create_hevy_routine(rec, "test-hevy-key")
            assert result["status"] == "created_incomplete"
            assert "Impossible Exercise" in result["skipped_exercises"]
            assert "warning" in result
            assert result["exercises_created"] == 1
            assert result["exercises_total"] == 2

    def test_create_routine_from_recommendation_index(self, tmp_path):
        rec = make_recommendation()
        cache_dir = tmp_path / ".ifit_capture"
        cache_dir.mkdir()
        (cache_dir / "recommendations.json").write_text(json.dumps([rec]))

        with patch("scripts.health_tools.ROOT", tmp_path), \
             patch("scripts.ifit_strength_recommend.create_hevy_routine") as mock_create:
            mock_create.return_value = {"status": "created", "routine_id": "r-001"}

            result = health_tools.create_hevy_routine_from_recommendation(
                user_slug="test", recommendation_index=0, hevy_api_key="key"
            )
            assert result["status"] == "created"

    def test_create_routine_by_workout_id_from_cache(self, tmp_path):
        """When ifit_workout_id is provided, look up by ID in cached recommendations."""
        rec = make_recommendation(workout_id="ifit_w500", title="Target Workout")
        other_rec = make_recommendation(workout_id="ifit_w999", title="Wrong Workout")
        cache_dir = tmp_path / ".ifit_capture"
        cache_dir.mkdir()
        (cache_dir / "recommendations.json").write_text(json.dumps([other_rec, rec]))

        with patch("scripts.health_tools.ROOT", tmp_path), \
             patch("scripts.ifit_strength_recommend.create_hevy_routine") as mock_create:
            mock_create.return_value = {"status": "created", "routine_id": "r-target"}

            result = health_tools.create_hevy_routine_from_recommendation(
                user_slug="test", ifit_workout_id="ifit_w500", hevy_api_key="key"
            )
            assert result["status"] == "created"
            created_rec = mock_create.call_args[0][0]
            assert created_rec.workout_id == "ifit_w500"
            assert created_rec.title == "Target Workout"

    def test_create_routine_by_workout_id_on_the_fly(self, tmp_path):
        """When ifit_workout_id is not in cached recs, fetch details on-the-fly."""
        cache_dir = tmp_path / ".ifit_capture"
        cache_dir.mkdir()
        (cache_dir / "recommendations.json").write_text(json.dumps([]))

        workout_details = {
            "title": "On The Fly Workout",
            "trainer": {"name": "Test Trainer"},
            "duration_min": 30,
            "difficulty": "moderate",
            "rating_avg": 4.5,
            "subcategories": ["upper body"],
            "required_equipment": ["dumbbell"],
            "exercises": [
                {"hevy_name": "Curl", "hevy_id": "C001", "muscle_group": "biceps",
                 "sets": 3, "reps": 12, "weight": "dumbbell", "notes": ""},
            ],
        }

        with patch("scripts.health_tools.ROOT", tmp_path), \
             patch("scripts.health_tools.get_ifit_workout_details", return_value=workout_details), \
             patch("scripts.ifit_strength_recommend.create_hevy_routine") as mock_create:
            mock_create.return_value = {"status": "created", "routine_id": "r-fly"}

            result = health_tools.create_hevy_routine_from_recommendation(
                user_slug="test", ifit_workout_id="ifit_fly_001", hevy_api_key="key"
            )
            assert result["status"] == "created"
            created_rec = mock_create.call_args[0][0]
            assert created_rec.workout_id == "ifit_fly_001"
            assert created_rec.title == "On The Fly Workout"
            assert len(created_rec.exercises) == 1

    def test_create_routine_by_workout_id_no_exercises(self, tmp_path):
        """When workout has no exercises, return an error."""
        cache_dir = tmp_path / ".ifit_capture"
        cache_dir.mkdir()
        (cache_dir / "recommendations.json").write_text(json.dumps([]))

        workout_details = {
            "title": "Empty Workout",
            "exercises": [],
        }

        with patch("scripts.health_tools.ROOT", tmp_path), \
             patch("scripts.health_tools.get_ifit_workout_details", return_value=workout_details):
            result = health_tools.create_hevy_routine_from_recommendation(
                user_slug="test", ifit_workout_id="ifit_empty", hevy_api_key="key"
            )
            assert "error" in result

    def test_workout_id_takes_priority_over_index(self, tmp_path):
        """workout_id lookup should take priority over recommendation_index."""
        rec_a = make_recommendation(workout_id="ifit_a", title="Workout A")
        rec_b = make_recommendation(workout_id="ifit_b", title="Workout B")
        cache_dir = tmp_path / ".ifit_capture"
        cache_dir.mkdir()
        (cache_dir / "recommendations.json").write_text(json.dumps([rec_a, rec_b]))

        with patch("scripts.health_tools.ROOT", tmp_path), \
             patch("scripts.ifit_strength_recommend.create_hevy_routine") as mock_create:
            mock_create.return_value = {"status": "created", "routine_id": "r-b"}

            result = health_tools.create_hevy_routine_from_recommendation(
                user_slug="test",
                recommendation_index=0,
                ifit_workout_id="ifit_b",
                hevy_api_key="key",
            )
            assert result["status"] == "created"
            created_rec = mock_create.call_args[0][0]
            assert created_rec.workout_id == "ifit_b"
            assert created_rec.title == "Workout B"


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

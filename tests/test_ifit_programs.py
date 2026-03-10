"""Tests for iFit program details and series discovery.

Uses mock R2 to avoid external API calls.
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from scripts import health_tools
from tests.conftest import SAMPLE_PROGRAM, FakeR2Store


class TestGetIfitProgramDetails:

    def test_returns_from_r2_cache(self, mock_r2, fake_r2):
        fake_r2.upload_json("programs/test_series_001.json", SAMPLE_PROGRAM)
        result = health_tools.get_ifit_program_details("test_series_001")
        assert result["title"] == "Test Training Series"
        assert result["workout_count"] == 4

    def test_has_schedule_when_weeks(self, mock_r2, fake_r2):
        fake_r2.upload_json("programs/test_series_001.json", SAMPLE_PROGRAM)
        result = health_tools.get_ifit_program_details("test_series_001")
        assert "schedule" in result
        schedule = result["schedule"]
        assert len(schedule) == 2
        assert schedule[0]["week"] == "Week 1"
        assert schedule[0]["workout_count"] == 2

    def test_week1_workout1_identifiable(self, mock_r2, fake_r2):
        """The key use case: user asks 'what is week 1, workout 1?'"""
        fake_r2.upload_json("programs/test_series_001.json", SAMPLE_PROGRAM)
        result = health_tools.get_ifit_program_details("test_series_001")
        w1 = result["schedule"][0]
        assert w1["week"] == "Week 1"
        first_workout = w1["workouts"][0]
        assert first_workout["position"] == 1
        assert first_workout["title"] == "Week 1 Run A"
        assert first_workout["id"] == "wid_w1_1"

    def test_all_workouts_in_schedule(self, mock_r2, fake_r2):
        fake_r2.upload_json("programs/test_series_001.json", SAMPLE_PROGRAM)
        result = health_tools.get_ifit_program_details("test_series_001")
        all_ids = []
        for week in result["schedule"]:
            for w in week["workouts"]:
                all_ids.append(w["id"])
        assert set(all_ids) == {"wid_w1_1", "wid_w1_2", "wid_w2_1", "wid_w2_2"}

    def test_no_schedule_without_weeks(self, mock_r2, fake_r2):
        program_no_weeks = {**SAMPLE_PROGRAM}
        del program_no_weeks["weeks"]
        fake_r2.upload_json("programs/test_series_002.json", program_no_weeks)
        result = health_tools.get_ifit_program_details("test_series_002")
        assert "schedule" not in result or result.get("schedule") == []

    def test_missing_program(self, mock_r2):
        with patch("scripts.ifit_auth.get_auth_headers", side_effect=RuntimeError("No token")):
            result = health_tools.get_ifit_program_details("nonexistent_id")
        assert "error" in result


class TestWeekStructure:
    """Test the _build_weeks_from_api helper."""

    def test_build_weeks(self):
        from scripts.ifit_r2_sync import _build_weeks_from_api

        api_response = {
            "workouts": [
                {"itemId": "w1", "title": "Run A"},
                {"itemId": "w2", "title": "Run B"},
                {"itemId": "w3", "title": "Run C"},
                {"itemId": "w4", "title": "Run D"},
            ],
            "workoutSections": [
                {"title": "Week 1", "workoutIds": ["w1", "w2"]},
                {"title": "Week 2", "workoutIds": ["w3", "w4"]},
            ],
        }
        weeks = _build_weeks_from_api(api_response)
        assert len(weeks) == 2
        assert weeks[0]["name"] == "Week 1"
        assert len(weeks[0]["workouts"]) == 2
        assert weeks[0]["workouts"][0] == {"id": "w1", "title": "Run A"}
        assert weeks[1]["workouts"][1] == {"id": "w4", "title": "Run D"}

    def test_build_weeks_empty(self):
        from scripts.ifit_r2_sync import _build_weeks_from_api
        weeks = _build_weeks_from_api({})
        assert weeks == []

    def test_week_position_lookup(self):
        from scripts.ifit_r2_sync import _week_position_from_program
        week, pos = _week_position_from_program(SAMPLE_PROGRAM, "wid_w2_1")
        assert week == "Week 2"
        assert pos == 1

    def test_week_position_not_found(self):
        from scripts.ifit_r2_sync import _week_position_from_program
        week, pos = _week_position_from_program(SAMPLE_PROGRAM, "nonexistent")
        assert week is None
        assert pos is None

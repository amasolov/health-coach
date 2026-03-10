"""Tests for strength training tools."""

import pytest
from scripts import health_tools


class TestGetStrengthSessions:

    def test_returns_list(self, user_id):
        result = health_tools.get_strength_sessions(user_id, days=90)
        assert isinstance(result, list)

    def test_has_data(self, user_id):
        result = health_tools.get_strength_sessions(user_id, days=90)
        assert len(result) > 0, "Expected strength data in last 90 days"

    def test_session_fields(self, user_id):
        result = health_tools.get_strength_sessions(user_id, days=90)
        if not result:
            pytest.skip("No strength data")
        row = result[0]
        for key in ("exercise_name", "set_number", "time"):
            assert key in row, f"Missing key: {key}"

    def test_exercise_filter(self, user_id):
        all_sets = health_tools.get_strength_sessions(user_id, days=90)
        if not all_sets:
            pytest.skip("No strength data")
        exercise_name = all_sets[0]["exercise_name"]
        filtered = health_tools.get_strength_sessions(
            user_id, exercise=exercise_name[:10], days=90
        )
        assert len(filtered) > 0
        for row in filtered:
            assert exercise_name[:10].lower() in row["exercise_name"].lower()

    def test_short_range(self, user_id):
        result = health_tools.get_strength_sessions(user_id, days=1)
        assert isinstance(result, list)

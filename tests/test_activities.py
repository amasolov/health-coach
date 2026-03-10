"""Tests for activity tools: get_activities, get_activity_detail."""

import pytest
from scripts import health_tools


class TestGetActivities:

    def test_returns_list(self, user_id):
        result = health_tools.get_activities(user_id, days=30)
        assert isinstance(result, list)

    def test_default_returns_data(self, user_id):
        result = health_tools.get_activities(user_id)
        assert len(result) > 0, "Expected at least one activity in last 30 days"

    def test_activity_has_required_fields(self, user_id):
        result = health_tools.get_activities(user_id, days=30, limit=5)
        if result:
            act = result[0]
            for key in ("time", "activity_type", "duration_s"):
                assert key in act, f"Missing key: {key}"

    def test_sport_filter(self, user_id):
        result = health_tools.get_activities(user_id, sport="running", days=90)
        for act in result:
            assert "run" in act.get("activity_type", "").lower()

    def test_limit_parameter(self, user_id):
        result = health_tools.get_activities(user_id, days=365, limit=3)
        assert len(result) <= 3

    def test_date_range_filter(self, user_id):
        result = health_tools.get_activities(
            user_id, start_date="2026-01-01", end_date="2026-01-31"
        )
        assert isinstance(result, list)

    def test_empty_for_future_dates(self, user_id):
        result = health_tools.get_activities(
            user_id, start_date="2030-01-01", end_date="2030-12-31"
        )
        assert len(result) == 0


class TestGetActivityDetail:

    def test_returns_dict(self, user_id):
        activities = health_tools.get_activities(user_id, days=30, limit=1)
        if not activities:
            pytest.skip("No recent activities")
        timestamp = activities[0]["time"]
        result = health_tools.get_activity_detail(user_id, str(timestamp))
        assert isinstance(result, dict)

    def test_detail_has_metrics(self, user_id):
        activities = health_tools.get_activities(user_id, days=30, limit=1)
        if not activities:
            pytest.skip("No recent activities")
        timestamp = activities[0]["time"]
        result = health_tools.get_activity_detail(user_id, str(timestamp))
        assert "activity_type" in result

    def test_invalid_timestamp(self, user_id):
        try:
            result = health_tools.get_activity_detail(user_id, "2099-01-01T00:00:00Z")
            assert isinstance(result, dict)
            assert "error" in result or len(result) == 0 or result.get("activity_type") is None
        except (ValueError, KeyError):
            pass  # function may raise on missing activity

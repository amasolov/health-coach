"""Tests for body composition and vitals tools."""

import pytest
from scripts import health_tools


class TestGetBodyComposition:

    def test_returns_list(self, user_id):
        result = health_tools.get_body_composition(user_id, days=365)
        assert isinstance(result, list)

    def test_has_weight(self, user_id):
        result = health_tools.get_body_composition(user_id, days=365)
        if not result:
            pytest.skip("No body composition data")
        row = result[0]
        assert "weight_kg" in row

    def test_date_range(self, user_id):
        result = health_tools.get_body_composition(
            user_id, start_date="2025-01-01", end_date="2025-12-31"
        )
        assert isinstance(result, list)


class TestGetVitals:

    def test_returns_list(self, user_id):
        result = health_tools.get_vitals(user_id, days=30)
        assert isinstance(result, list)

    def test_has_data(self, user_id):
        result = health_tools.get_vitals(user_id, days=30)
        assert len(result) > 0, "Expected vitals data"

    def test_vitals_fields(self, user_id):
        result = health_tools.get_vitals(user_id, days=7)
        if not result:
            pytest.skip("No recent vitals")
        row = result[0]
        possible_keys = {"resting_hr", "hrv_ms", "sleep_score", "stress_avg", "body_battery_high"}
        assert any(k in row for k in possible_keys), f"Expected at least one vitals field, got: {list(row.keys())}"

    def test_date_range(self, user_id):
        result = health_tools.get_vitals(
            user_id, start_date="2026-01-01", end_date="2026-01-31"
        )
        assert isinstance(result, list)

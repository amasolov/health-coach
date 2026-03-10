"""Tests for fitness / PMC tools: get_fitness_summary, get_training_load."""

import pytest
from scripts import health_tools


class TestGetFitnessSummary:

    def test_returns_dict(self, user_id):
        result = health_tools.get_fitness_summary(user_id)
        assert isinstance(result, dict)

    def test_has_ctl_atl_tsb(self, user_id):
        result = health_tools.get_fitness_summary(user_id)
        for key in ("ctl_fitness", "atl_fatigue", "tsb_form"):
            assert key in result, f"Missing key: {key}"

    def test_ctl_atl_numeric(self, user_id):
        result = health_tools.get_fitness_summary(user_id)
        assert float(result["ctl_fitness"])
        assert float(result["atl_fatigue"])

    def test_tsb_equals_ctl_minus_atl(self, user_id):
        result = health_tools.get_fitness_summary(user_id)
        ctl = float(result["ctl_fitness"])
        atl = float(result["atl_fatigue"])
        tsb = float(result["tsb_form"])
        expected = ctl - atl
        assert abs(tsb - expected) < 0.5

    def test_has_ramp_rate(self, user_id):
        result = health_tools.get_fitness_summary(user_id)
        assert "ramp_rate" in result

    def test_has_interpretation(self, user_id):
        result = health_tools.get_fitness_summary(user_id)
        assert "interpretation" in result or "form_status" in result


class TestGetTrainingLoad:

    def test_returns_list(self, user_id):
        result = health_tools.get_training_load(user_id, days=7)
        assert isinstance(result, (list, dict))

    def test_default_90_days(self, user_id):
        result = health_tools.get_training_load(user_id)
        if isinstance(result, list):
            assert len(result) > 0

    def test_custom_date_range(self, user_id):
        result = health_tools.get_training_load(
            user_id, start_date="2026-01-01", end_date="2026-01-31"
        )
        if isinstance(result, list):
            for row in result:
                assert "date" in row or "time" in row

    def test_short_range(self, user_id):
        result = health_tools.get_training_load(user_id, days=3)
        if isinstance(result, list):
            assert len(result) <= 5

    def test_each_row_has_tss_ctl_atl(self, user_id):
        result = health_tools.get_training_load(user_id, days=7)
        if isinstance(result, list) and result:
            row = result[0]
            for key in ("tss", "ctl", "atl", "tsb"):
                assert key in row, f"Missing key: {key}"

"""Tests for profile, zones, treadmill, goals, and integrations tools."""

import pytest
from scripts import health_tools


class TestGetAthleteProfile:

    def test_returns_dict(self, user_slug):
        result = health_tools.get_athlete_profile(user_slug)
        assert isinstance(result, dict)

    def test_has_thresholds(self, user_slug):
        result = health_tools.get_athlete_profile(user_slug)
        assert "thresholds" in result or "body" in result

    def test_has_goals(self, user_slug):
        result = health_tools.get_athlete_profile(user_slug)
        assert "goals" in result or "training" in result


class TestGetTrainingZones:

    def test_returns_dict(self, user_slug):
        result = health_tools.get_training_zones(user_slug)
        assert isinstance(result, dict)

    def test_has_zone_types(self, user_slug):
        result = health_tools.get_training_zones(user_slug)
        assert len(result) > 0, "Expected at least one zone type"


class TestListTreadmillTemplates:

    def test_returns_list(self):
        result = health_tools.list_treadmill_templates()
        assert isinstance(result, (list, dict))

    def test_templates_have_names(self):
        result = health_tools.list_treadmill_templates()
        if isinstance(result, list):
            for t in result:
                assert "name" in t or "key" in t


class TestGenerateTreadmillWorkout:

    def test_returns_dict(self, user_slug):
        templates = health_tools.list_treadmill_templates()
        if isinstance(templates, list) and templates:
            key = templates[0].get("key", templates[0].get("name", ""))
        elif isinstance(templates, dict):
            key = list(templates.keys())[0] if templates else None
        else:
            pytest.skip("No treadmill templates")
        if not key:
            pytest.skip("No template key found")
        result = health_tools.generate_treadmill_workout(user_slug, key)
        assert isinstance(result, dict)


class TestGetSupportedIntegrations:

    def test_returns_data(self):
        result = health_tools.get_supported_integrations()
        assert isinstance(result, (list, dict))

    def test_category_filter(self):
        result = health_tools.get_supported_integrations(category="wearable")
        assert isinstance(result, (list, dict))


class TestUserGoals:

    def test_get_goals(self, user_slug):
        result = health_tools.get_user_goals(user_slug)
        assert isinstance(result, dict)

    def test_get_onboarding_questions(self, user_slug):
        result = health_tools.get_onboarding_questions(user_slug)
        assert isinstance(result, (dict, list))


class TestUserIntegrations:

    def test_get_integrations(self, user_slug):
        result = health_tools.get_user_integrations(user_slug)
        assert isinstance(result, (dict, list))

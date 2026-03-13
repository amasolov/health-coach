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


class TestOnboardingLocationQuestion:
    """Onboarding includes location and running-preference questions."""

    def test_location_question_exists(self):
        ids = [q["id"] for q in health_tools.ONBOARDING_QUESTIONS]
        assert "location" in ids

    def test_outdoor_running_question_exists(self):
        ids = [q["id"] for q in health_tools.ONBOARDING_QUESTIONS]
        assert "outdoor_running" in ids

    def test_location_question_has_instructions(self):
        q = next(q for q in health_tools.ONBOARDING_QUESTIONS if q["id"] == "location")
        assert "instructions" in q
        assert "update_athlete_profile" in q["instructions"]

    def test_location_unanswered_when_missing(self, user_slug):
        """When no location is set, it appears in unanswered."""
        from scripts import athlete_store
        user = athlete_store.load(user_slug) or {}
        user.pop("location", None)
        athlete_store.save(user_slug, user)

        result = health_tools.get_onboarding_questions(user_slug)
        unanswered_ids = [q["id"] for q in result["unanswered"]]
        assert "location" in unanswered_ids

    def test_location_answered_when_set(self, user_slug):
        """When location is set, it appears in answered."""
        from scripts import athlete_store
        user = athlete_store.load(user_slug) or {}
        user["location"] = {"lat": -33.87, "lon": 151.21, "label": "Sydney"}
        athlete_store.save(user_slug, user)

        result = health_tools.get_onboarding_questions(user_slug)
        answered_ids = [q["id"] for q in result["answered"]]
        assert "location" in answered_ids

    def test_running_prefs_answered_when_set(self, user_slug):
        """When running_preferences is set, outdoor_running appears in answered."""
        from scripts import athlete_store
        user = athlete_store.load(user_slug) or {}
        user["running_preferences"] = {"preferred_distance_km": [5, 10]}
        athlete_store.save(user_slug, user)

        result = health_tools.get_onboarding_questions(user_slug)
        answered_ids = [q["id"] for q in result["answered"]]
        assert "outdoor_running" in answered_ids


class TestMissingProfileNudges:
    """get_missing_profile_nudges returns prompts for unset fields."""

    def test_returns_location_nudge_when_missing(self, user_slug):
        from scripts import athlete_store
        user = athlete_store.load(user_slug) or {}
        user.pop("location", None)
        athlete_store.save(user_slug, user)

        nudges = health_tools.get_missing_profile_nudges(user_slug)
        assert any("location" in n["field"] for n in nudges)

    def test_no_location_nudge_when_set(self, user_slug):
        from scripts import athlete_store
        user = athlete_store.load(user_slug) or {}
        user["location"] = {"lat": -33.87, "lon": 151.21, "label": "Sydney"}
        athlete_store.save(user_slug, user)

        nudges = health_tools.get_missing_profile_nudges(user_slug)
        assert not any("location" in n["field"] for n in nudges)

    def test_returns_empty_when_all_set(self, user_slug):
        from scripts import athlete_store
        user = athlete_store.load(user_slug) or {}
        user["location"] = {"lat": -33.87, "lon": 151.21, "label": "Sydney"}
        user["running_preferences"] = {"preferred_distance_km": [5]}
        athlete_store.save(user_slug, user)

        nudges = health_tools.get_missing_profile_nudges(user_slug)
        assert len(nudges) == 0

    def test_nudge_has_required_keys(self, user_slug):
        from scripts import athlete_store
        user = athlete_store.load(user_slug) or {}
        user.pop("location", None)
        athlete_store.save(user_slug, user)

        nudges = health_tools.get_missing_profile_nudges(user_slug)
        for n in nudges:
            assert "field" in n
            assert "prompt" in n
            assert "instructions" in n


class TestUpdateAthleteProfileDict:
    """update_athlete_profile accepts dict values (e.g. location)."""

    def test_set_location_dict(self, user_slug):
        loc = {"lat": 40.7128, "lon": -74.0060, "label": "New York"}
        result = health_tools.update_athlete_profile(user_slug, "location", loc)
        assert result["updated"] == "location"
        assert result["value"] == loc

        from scripts import athlete_store
        user = athlete_store.load(user_slug) or {}
        assert user["location"]["lat"] == 40.7128
        assert user["location"]["label"] == "New York"

    def test_set_running_preferences_dict(self, user_slug):
        prefs = {
            "preferred_distance_km": [5, 10],
            "surface": ["trail"],
            "prefer_loop": True,
        }
        result = health_tools.update_athlete_profile(
            user_slug, "running_preferences", prefs,
        )
        assert result["updated"] == "running_preferences"

        from scripts import athlete_store
        user = athlete_store.load(user_slug) or {}
        assert user["running_preferences"]["prefer_loop"] is True


class TestUserIntegrations:

    def test_get_integrations(self, user_slug):
        result = health_tools.get_user_integrations(user_slug)
        assert isinstance(result, (dict, list))

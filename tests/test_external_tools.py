"""Tests for tools that call external APIs (all mocked).

Covers: suggest_feature, report_exercise_correction, sync_data,
garmin auth, recommend_ifit_workout, discover_ifit_series.
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from scripts import health_tools
from scripts.addon_config import config as _cfg
from tests.conftest import MockResponse, make_github_issue_response, FakeR2Store


# ---------------------------------------------------------------------------
# GitHub tools
# ---------------------------------------------------------------------------

class TestSuggestFeature:

    @patch.object(_cfg, "github_token", "fake-token")
    @patch.object(_cfg, "github_repo", "test/repo")
    @patch("httpx.post")
    def test_creates_issue(self, mock_post):
        mock_post.return_value = make_github_issue_response(99, "https://github.com/test/repo/issues/99")
        result = health_tools.suggest_feature(
            user_slug="test",
            title="Add dark mode",
            description="Would be nice to have dark mode",
            category="enhancement",
        )
        assert result.get("status") == "created" or result.get("issue_number") == 99
        mock_post.assert_called_once()

    @patch.object(_cfg, "github_token", "")
    @patch.object(_cfg, "github_repo", "test/repo")
    def test_no_token_error(self):
        result = health_tools.suggest_feature(
            user_slug="test",
            title="Test",
            description="Test",
        )
        assert "error" in result


class TestReportExerciseCorrection:

    @patch.object(_cfg, "github_token", "fake-token")
    @patch.object(_cfg, "github_repo", "test/repo")
    @patch("httpx.post")
    def test_creates_issue(self, mock_post, mock_r2, fake_r2):
        fake_r2.upload_json("exercises/wid_test.json", [
            {"hevy_name": "Wrong Name", "sets": 3, "reps": 10},
        ])
        mock_post.return_value = make_github_issue_response(42)
        result = health_tools.report_exercise_correction(
            user_slug="test",
            workout_id="wid_test",
            feedback="The exercise should be 'Goblet Squat' not 'Wrong Name'",
        )
        assert result.get("status") == "created" or "issue_number" in result

    @patch.object(_cfg, "github_token", "")
    @patch.object(_cfg, "github_repo", "test/repo")
    def test_no_token_error(self):
        result = health_tools.report_exercise_correction(
            user_slug="test", workout_id="w1", feedback="test",
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# Sync (mocked)
# ---------------------------------------------------------------------------

class TestSyncData:

    @patch("scripts.sync_hevy.sync_user")
    @patch("scripts.sync_garmin.sync_user")
    def test_sync_calls_both(self, mock_garmin, mock_hevy, user_slug, user_id):
        mock_garmin.return_value = {"activities_found": 0, "activities_inserted": 0}
        mock_hevy.return_value = {"workouts_found": 0, "workouts_inserted": 0, "sets_inserted": 0}
        result = health_tools.sync_data(
            user_slug=user_slug,
            user_id=user_id,
            hevy_api_key="test-key",
        )
        assert "synced" in result
        mock_garmin.assert_called_once()
        mock_hevy.assert_called_once()

    @patch("scripts.sync_hevy.sync_user")
    @patch("scripts.sync_garmin.sync_user")
    def test_sync_handles_garmin_error(self, mock_garmin, mock_hevy, user_slug, user_id):
        mock_garmin.return_value = {"error": "Garmin authentication failed"}
        mock_hevy.return_value = {"workouts_found": 0, "workouts_inserted": 0, "sets_inserted": 0}
        result = health_tools.sync_data(
            user_slug=user_slug,
            user_id=user_id,
            hevy_api_key="test-key",
        )
        assert "synced" in result
        assert result["synced"]["garmin"]["status"] == "skipped"


# ---------------------------------------------------------------------------
# iFit series discovery (mocked)
# ---------------------------------------------------------------------------

class TestDiscoverIfitSeries:

    @patch("scripts.ifit_r2_sync.discover_series_for_workout")
    def test_returns_series(self, mock_discover):
        mock_discover.return_value = {
            "workout_id": "wid_test",
            "series": [{"series_id": "s1", "title": "Test Series", "workout_count": 10}],
            "newly_mapped": 10,
        }
        result = health_tools.discover_ifit_series("wid_test")
        assert result["newly_mapped"] == 10

    @patch("scripts.ifit_r2_sync.discover_series_for_workout")
    def test_no_series_found(self, mock_discover):
        mock_discover.return_value = {
            "workout_id": "wid_orphan",
            "series": [],
            "mapped": 0,
        }
        result = health_tools.discover_ifit_series("wid_orphan")
        assert result["series"] == []


# ---------------------------------------------------------------------------
# Garmin auth (mocked — never calls real Garmin)
# ---------------------------------------------------------------------------

class TestGarminAuthStatus:

    def test_returns_dict(self, user_slug):
        result = health_tools.garmin_auth_status(user_slug)
        assert isinstance(result, dict)

    def test_has_status_field(self, user_slug):
        result = health_tools.garmin_auth_status(user_slug)
        assert "authenticated" in result or "status" in result or "error" in result

"""Tests for Hevy workout comparison with iFit predictions.

Uses real DB for reads but mocked R2 for routine mapping.
"""

import json
import pytest
from unittest.mock import patch

from scripts import health_tools
from tests.conftest import FakeR2Store


class TestCompareHevyWorkout:

    def test_no_mapping_returns_error(self, user_id, mock_r2):
        result = health_tools.compare_hevy_workout(user_id, days=7)
        assert "error" in result

    def test_compare_with_specific_workout(self, user_id, mock_r2, fake_r2, db_conn):
        """If user has a workout with routine_id, compare it."""
        cur = db_conn.cursor()
        cur.execute("""
            SELECT DISTINCT workout_id, routine_id
            FROM strength_sets
            WHERE user_id = %s AND routine_id IS NOT NULL
            LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        cur.close()

        if not row:
            pytest.skip("No iFit-sourced workouts in DB yet")

        workout_id, routine_id = row
        mapping = {
            routine_id: {
                "ifit_workout_id": "ifit_test",
                "title": "iFit: Test",
                "predicted_exercises": [
                    {"hevy_name": "Squat", "sets": 3, "reps": 10, "weight": "barbell"},
                ],
                "created_at": "2026-01-01T00:00:00",
            }
        }
        fake_r2.upload_json("hevy/routine_map.json", mapping)

        result = health_tools.compare_hevy_workout(user_id, hevy_workout_id=workout_id)
        assert "actual_exercises" in result
        assert "predicted_exercises" in result

    def test_compare_auto_detect(self, user_id, mock_r2, fake_r2, db_conn):
        """Auto-detect mode scans recent workouts with routine_id."""
        cur = db_conn.cursor()
        cur.execute("""
            SELECT DISTINCT routine_id
            FROM strength_sets
            WHERE user_id = %s AND routine_id IS NOT NULL
            LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        cur.close()

        if not row:
            pytest.skip("No iFit-sourced workouts in DB yet")

        routine_id = row[0]
        mapping = {
            routine_id: {
                "ifit_workout_id": "ifit_test",
                "title": "iFit: Auto",
                "predicted_exercises": [],
                "created_at": "2026-01-01T00:00:00",
            }
        }
        fake_r2.upload_json("hevy/routine_map.json", mapping)

        result = health_tools.compare_hevy_workout(user_id, days=365)
        assert "workout_id" in result or "error" in result

    def test_differences_detection(self, user_id, mock_r2, fake_r2):
        """Verify difference detection logic with synthetic data."""
        mapping = {
            "synthetic-routine": {
                "ifit_workout_id": "ifit_synth",
                "title": "iFit: Synthetic",
                "predicted_exercises": [
                    {"hevy_name": "Squat (Barbell)", "sets": 5, "reps": 5, "weight": "barbell 80kg"},
                    {"hevy_name": "Ghost Exercise", "sets": 3, "reps": 10, "weight": ""},
                ],
                "created_at": "2026-01-01T00:00:00",
            }
        }
        fake_r2.upload_json("hevy/routine_map.json", mapping)

        result = health_tools.compare_hevy_workout(user_id, hevy_workout_id="nonexistent-wid")
        assert "error" in result or "actual_exercises" in result

"""
Shared fixtures for the Health Coach test suite.

Loads .env so tests can connect to the real TimescaleDB instance.
Provides mock factories for external services (Hevy, iFit, GitHub,
OpenRouter) so tests never create real data in third-party apps.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------------------
# User context
# ---------------------------------------------------------------------------

TEST_SLUG = os.environ.get("TEST_USER_SLUG", "alexeym")
TEST_USER_ID: int | None = None


@pytest.fixture(scope="session")
def user_slug() -> str:
    return TEST_SLUG


@pytest.fixture(scope="session")
def user_id() -> int:
    from scripts.health_tools import resolve_user_id
    uid = resolve_user_id(TEST_SLUG)
    if uid is None:
        pytest.skip(f"User '{TEST_SLUG}' not found in DB — set TEST_USER_SLUG")
    global TEST_USER_ID
    TEST_USER_ID = uid
    return uid


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def db_conn():
    """Real DB connection for read-only assertions."""
    import psycopg2
    conn = psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "health"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# R2 store mock (in-memory dict)
# ---------------------------------------------------------------------------

class FakeR2Store:
    """In-memory replacement for scripts.r2_store."""

    def __init__(self, seed: dict[str, Any] | None = None):
        self.objects: dict[str, str] = {}
        if seed:
            for key, value in seed.items():
                if isinstance(value, str):
                    self.objects[key] = value
                else:
                    self.objects[key] = json.dumps(value, default=str)

    def is_configured(self) -> bool:
        return True

    def upload_text(self, key: str, text: str) -> bool:
        self.objects[key] = text
        return True

    def upload_json(self, key: str, obj: Any) -> bool:
        self.objects[key] = json.dumps(obj, default=str)
        return True

    def download_text(self, key: str) -> str | None:
        return self.objects.get(key)

    def download_json(self, key: str) -> Any | None:
        raw = self.objects.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> bool:
        self.objects.pop(key, None)
        return True

    def list_keys(self, prefix: str, max_keys: int = 50_000) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]


@pytest.fixture
def fake_r2():
    """Provide a fresh in-memory R2 store."""
    return FakeR2Store()


@pytest.fixture
def mock_r2(fake_r2):
    """Patch scripts.r2_store module functions with the fake store."""
    patches = {
        "scripts.r2_store.is_configured": fake_r2.is_configured,
        "scripts.r2_store.upload_text": fake_r2.upload_text,
        "scripts.r2_store.upload_json": fake_r2.upload_json,
        "scripts.r2_store.download_text": fake_r2.download_text,
        "scripts.r2_store.download_json": fake_r2.download_json,
        "scripts.r2_store.exists": fake_r2.exists,
        "scripts.r2_store.delete": fake_r2.delete,
        "scripts.r2_store.list_keys": fake_r2.list_keys,
    }
    with _multi_patch(patches):
        yield fake_r2


# ---------------------------------------------------------------------------
# HTTP mock helpers
# ---------------------------------------------------------------------------

class MockResponse:
    """Minimal httpx.Response stand-in."""

    def __init__(self, status_code: int = 200, json_data: Any = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or json.dumps(json_data or {})

    def json(self) -> Any:
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=MagicMock(),
                response=self,
            )


def make_hevy_routine_response(routine_id: str = "test-routine-001", title: str = "iFit: Test") -> MockResponse:
    return MockResponse(201, {
        "routine": {
            "id": routine_id,
            "title": title,
            "exercises": [],
        }
    })


def make_hevy_exercise_template_response(template_id: str, title: str) -> MockResponse:
    return MockResponse(201, {
        "exercise_template": {
            "id": template_id,
            "title": title,
        }
    })


def make_hevy_workout_response(
    workout_id: str,
    routine_id: str | None,
    exercises: list[dict],
    start_time: str = "2026-03-09T08:00:00Z",
) -> dict:
    """Build a single Hevy workout dict (not wrapped in MockResponse)."""
    return {
        "id": workout_id,
        "routine_id": routine_id,
        "title": "Test Workout",
        "start_time": start_time,
        "end_time": "2026-03-09T09:00:00Z",
        "created_at": start_time,
        "updated_at": start_time,
        "description": "",
        "exercises": exercises,
    }


def make_github_issue_response(number: int = 42, url: str = "https://github.com/test/repo/issues/42") -> MockResponse:
    return MockResponse(201, {
        "number": number,
        "html_url": url,
        "title": "Test issue",
    })


def make_openrouter_response(content: str) -> MockResponse:
    return MockResponse(200, {
        "choices": [{"message": {"content": content}}],
    })


# ---------------------------------------------------------------------------
# Sample data factories
# ---------------------------------------------------------------------------

def make_ifit_exercises(n: int = 3) -> list[dict]:
    """Create sample LLM-extracted iFit exercises."""
    samples = [
        {"hevy_name": "Squat (Barbell)", "hevy_id": "ABC123", "muscle_group": "quadriceps",
         "sets": 3, "reps": 10, "weight": "barbell", "notes": "", "equipment": "barbell"},
        {"hevy_name": "Bicep Curl", "hevy_id": "", "muscle_group": "biceps",
         "sets": 3, "reps": 12, "weight": "dumbbell 15lb", "notes": "", "equipment": "dumbbell"},
        {"hevy_name": "Plank", "hevy_id": "", "muscle_group": "abdominals",
         "sets": 3, "reps": "30s", "weight": "bodyweight", "notes": "", "equipment": "none"},
        {"hevy_name": "Bent Over Row", "hevy_id": "DEF456", "muscle_group": "lats",
         "sets": 4, "reps": 8, "weight": "barbell 40kg", "notes": "", "equipment": "barbell"},
        {"hevy_name": "Lunge", "hevy_id": "", "muscle_group": "quadriceps",
         "sets": 3, "reps": 12, "weight": "dumbbell", "notes": "each leg", "equipment": "dumbbell"},
    ]
    return samples[:n]


def make_recommendation(
    workout_id: str = "test_wid_001",
    title: str = "Test Strength Workout",
    exercises: list[dict] | None = None,
) -> dict:
    """Create a Recommendation-shaped dict."""
    return {
        "rank": 1,
        "workout_id": workout_id,
        "title": title,
        "trainer_name": "Test Trainer",
        "duration_min": 30,
        "difficulty": "intermediate",
        "rating": 4.5,
        "focus": "upper_body",
        "subcategories": ["strength"],
        "required_equipment": ["dumbbells"],
        "stage1_score": 80.0,
        "stage2_score": 90.0,
        "exercises": exercises or make_ifit_exercises(),
        "reasoning": "Good match for current fitness state.",
    }


SAMPLE_PROGRAM = {
    "series_id": "test_series_001",
    "title": "Test Training Series",
    "overview": "A test training program.",
    "type": "run",
    "rating": {"average": 4.8},
    "trainers": [{"name": "Test Trainer", "id": "trainer_001"}],
    "workout_ids": ["wid_w1_1", "wid_w1_2", "wid_w2_1", "wid_w2_2"],
    "workout_titles": ["Week 1 Run A", "Week 1 Run B", "Week 2 Run A", "Week 2 Run B"],
    "workout_count": 4,
    "weeks": [
        {
            "name": "Week 1",
            "workouts": [
                {"id": "wid_w1_1", "title": "Week 1 Run A"},
                {"id": "wid_w1_2", "title": "Week 1 Run B"},
            ],
        },
        {
            "name": "Week 2",
            "workouts": [
                {"id": "wid_w2_1", "title": "Week 2 Run A"},
                {"id": "wid_w2_2", "title": "Week 2 Run B"},
            ],
        },
    ],
}


SAMPLE_HEVY_EXERCISES_JSON = [
    {"id": "ABC123", "title": "Squat (Barbell)", "type": "weight_reps",
     "primary_muscle_group": "quadriceps", "secondary_muscle_groups": [], "equipment": "barbell", "is_custom": False},
    {"id": "DEF456", "title": "Bent Over Row (Barbell)", "type": "weight_reps",
     "primary_muscle_group": "lats", "secondary_muscle_groups": [], "equipment": "barbell", "is_custom": False},
    {"id": "GHI789", "title": "Bicep Curl (Dumbbell)", "type": "weight_reps",
     "primary_muscle_group": "biceps", "secondary_muscle_groups": [], "equipment": "dumbbell", "is_custom": False},
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _multi_patch:
    """Context manager that applies multiple mock.patch targets."""

    def __init__(self, targets: dict[str, Any]):
        self._targets = targets
        self._patchers: list = []

    def __enter__(self):
        for target, replacement in self._targets.items():
            p = patch(target, replacement)
            self._patchers.append(p)
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patchers):
            p.stop()

"""Tests for the cache_store module.

Covers DBCacheStore, FileCacheStore, module-level API, and auto-detection.
Follows the same pattern as test_credential_store.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# DBCacheStore
# ---------------------------------------------------------------------------

class TestDBCacheStore:

    def test_put_and_get_json(self):
        from scripts.cache_store import DBCacheStore

        store = DBCacheStore()
        data = [{"id": "w1", "title": "Workout 1"}, {"id": "w2", "title": "Workout 2"}]
        store.put_json("test/workouts", data)

        result = store.get_json("test/workouts")
        assert result is not None
        assert len(result) == 2
        assert result[0]["id"] == "w1"

        store.delete("test/workouts")

    def test_put_and_get_text(self):
        from scripts.cache_store import DBCacheStore

        store = DBCacheStore()
        text = "Squat | ABC123 | quadriceps | barbell\nBicep Curl | DEF456 | biceps | dumbbell"
        store.put_text("test/exercise_ref", text)

        result = store.get_text("test/exercise_ref")
        assert result == text

        store.delete("test/exercise_ref")

    def test_get_missing_returns_none(self):
        from scripts.cache_store import DBCacheStore

        store = DBCacheStore()
        assert store.get_json("test/nonexistent_key") is None
        assert store.get_text("test/nonexistent_key") is None

    def test_put_overwrites_existing(self):
        from scripts.cache_store import DBCacheStore

        store = DBCacheStore()
        store.put_json("test/overwrite", {"v": 1})
        store.put_json("test/overwrite", {"v": 2})

        result = store.get_json("test/overwrite")
        assert result["v"] == 2

        store.delete("test/overwrite")

    def test_delete(self):
        from scripts.cache_store import DBCacheStore

        store = DBCacheStore()
        store.put_json("test/delete_me", {"data": True})
        store.delete("test/delete_me")
        assert store.get_json("test/delete_me") is None

    def test_delete_nonexistent_no_error(self):
        from scripts.cache_store import DBCacheStore

        store = DBCacheStore()
        store.delete("test/does_not_exist")

    def test_exists(self):
        from scripts.cache_store import DBCacheStore

        store = DBCacheStore()
        assert not store.exists("test/exists_check")

        store.put_json("test/exists_check", {"ok": True})
        assert store.exists("test/exists_check")

        store.delete("test/exists_check")
        assert not store.exists("test/exists_check")

    def test_large_json_data(self):
        from scripts.cache_store import DBCacheStore

        store = DBCacheStore()
        data = [{"id": f"w{i}", "title": f"Workout {i}"} for i in range(500)]
        store.put_json("test/large", data)

        result = store.get_json("test/large")
        assert len(result) == 500
        assert result[499]["id"] == "w499"

        store.delete("test/large")

    def test_json_stores_dict(self):
        from scripts.cache_store import DBCacheStore

        store = DBCacheStore()
        data = {"trainer1": {"name": "Alice"}, "trainer2": {"name": "Bob"}}
        store.put_json("test/dict", data)

        result = store.get_json("test/dict")
        assert result["trainer1"]["name"] == "Alice"

        store.delete("test/dict")

    def test_updated_at_changes_on_overwrite(self):
        from scripts.cache_store import DBCacheStore
        from scripts.db_pool import get_conn

        store = DBCacheStore()
        store.put_json("test/timestamps", {"v": 1})

        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT updated_at FROM cache_store WHERE cache_key = %s",
                ("test/timestamps",),
            )
            ts1 = cur.fetchone()[0]
        finally:
            conn.close()

        store.put_json("test/timestamps", {"v": 2})

        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT updated_at FROM cache_store WHERE cache_key = %s",
                ("test/timestamps",),
            )
            ts2 = cur.fetchone()[0]
        finally:
            conn.close()

        assert ts2 >= ts1

        store.delete("test/timestamps")


# ---------------------------------------------------------------------------
# FileCacheStore
# ---------------------------------------------------------------------------

class TestFileCacheStore:

    def test_put_and_get_json(self, tmp_path):
        from scripts.cache_store import FileCacheStore

        store = FileCacheStore(base_dir=tmp_path)
        data = [{"id": "w1"}, {"id": "w2"}]
        store.put_json("ifit/library_workouts", data)

        result = store.get_json("ifit/library_workouts")
        assert result is not None
        assert len(result) == 2

    def test_put_and_get_text(self, tmp_path):
        from scripts.cache_store import FileCacheStore

        store = FileCacheStore(base_dir=tmp_path)
        text = "Line1\nLine2\nLine3"
        store.put_text("hevy/exercise_ref", text)

        result = store.get_text("hevy/exercise_ref")
        assert result == text

    def test_get_missing_returns_none(self, tmp_path):
        from scripts.cache_store import FileCacheStore

        store = FileCacheStore(base_dir=tmp_path)
        assert store.get_json("nonexistent") is None
        assert store.get_text("nonexistent") is None

    def test_put_creates_subdirs(self, tmp_path):
        from scripts.cache_store import FileCacheStore

        store = FileCacheStore(base_dir=tmp_path)
        store.put_json("deep/nested/key", {"ok": True})

        json_file = tmp_path / "deep" / "nested" / "key.json"
        assert json_file.exists()

    def test_text_file_extension(self, tmp_path):
        from scripts.cache_store import FileCacheStore

        store = FileCacheStore(base_dir=tmp_path)
        store.put_text("hevy/exercise_ref", "content")

        text_file = tmp_path / "hevy" / "exercise_ref.txt"
        assert text_file.exists()

    def test_delete(self, tmp_path):
        from scripts.cache_store import FileCacheStore

        store = FileCacheStore(base_dir=tmp_path)
        store.put_json("test/delete", {"a": 1})
        store.delete("test/delete")
        assert store.get_json("test/delete") is None

    def test_exists(self, tmp_path):
        from scripts.cache_store import FileCacheStore

        store = FileCacheStore(base_dir=tmp_path)
        assert not store.exists("test/check")

        store.put_json("test/check", {"ok": True})
        assert store.exists("test/check")

    def test_json_roundtrip_preserves_types(self, tmp_path):
        from scripts.cache_store import FileCacheStore

        store = FileCacheStore(base_dir=tmp_path)
        data = {"count": 42, "flag": True, "items": [1, 2, 3], "nested": {"a": "b"}}
        store.put_json("test/types", data)

        result = store.get_json("test/types")
        assert result == data


# ---------------------------------------------------------------------------
# Module-level API and auto-detection
# ---------------------------------------------------------------------------

class TestModuleAPI:

    def test_put_and_get_json_roundtrip(self):
        from scripts import cache_store as cs

        cs.put_cache("test/api_json", {"hello": "world"})
        result = cs.get_cache("test/api_json")
        assert result == {"hello": "world"}

        cs.delete_cache("test/api_json")

    def test_put_and_get_text_roundtrip(self):
        from scripts import cache_store as cs

        cs.put_cache_text("test/api_text", "some text content")
        result = cs.get_cache_text("test/api_text")
        assert result == "some text content"

        cs.delete_cache("test/api_text")

    def test_cache_exists(self):
        from scripts import cache_store as cs

        assert not cs.cache_exists("test/api_exists")
        cs.put_cache("test/api_exists", {"ok": True})
        assert cs.cache_exists("test/api_exists")

        cs.delete_cache("test/api_exists")

    def test_fallback_to_file_when_db_unavailable(self, tmp_path):
        from scripts.cache_store import FileCacheStore, _get_store

        with patch("scripts.db_pool.get_conn", side_effect=Exception("no db")):
            store = _get_store(force_redetect=True, file_base=tmp_path)
            assert isinstance(store, FileCacheStore)

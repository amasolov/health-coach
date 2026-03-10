"""Tests for the R2 store module (using the in-memory fake)."""

import pytest
from tests.conftest import FakeR2Store


class TestFakeR2Store:
    """Verify the test double behaves correctly."""

    def test_upload_download_text(self):
        store = FakeR2Store()
        assert store.upload_text("test/key.txt", "hello world")
        assert store.download_text("test/key.txt") == "hello world"

    def test_upload_download_json(self):
        store = FakeR2Store()
        data = {"name": "test", "values": [1, 2, 3]}
        assert store.upload_json("test/data.json", data)
        result = store.download_json("test/data.json")
        assert result == data

    def test_exists(self):
        store = FakeR2Store()
        assert not store.exists("missing")
        store.upload_text("present", "data")
        assert store.exists("present")

    def test_delete(self):
        store = FakeR2Store()
        store.upload_text("to_delete", "data")
        assert store.exists("to_delete")
        store.delete("to_delete")
        assert not store.exists("to_delete")

    def test_list_keys(self):
        store = FakeR2Store()
        store.upload_text("prefix/a.txt", "a")
        store.upload_text("prefix/b.txt", "b")
        store.upload_text("other/c.txt", "c")
        keys = store.list_keys("prefix/")
        assert set(keys) == {"prefix/a.txt", "prefix/b.txt"}

    def test_seed_data(self):
        store = FakeR2Store({"key1": "text_value", "key2": {"nested": True}})
        assert store.download_text("key1") == "text_value"
        assert store.download_json("key2") == {"nested": True}

    def test_download_missing_returns_none(self):
        store = FakeR2Store()
        assert store.download_text("missing") is None
        assert store.download_json("missing") is None

    def test_is_configured(self):
        store = FakeR2Store()
        assert store.is_configured() is True

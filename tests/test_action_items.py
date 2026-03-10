"""Tests for action items CRUD.

These tests use the DB-backed athlete_store. The fixture saves and
restores the athlete config around each test.
"""

import copy
import pytest
from scripts import health_tools, athlete_store


@pytest.fixture(autouse=True)
def _backup_athlete_config(user_slug):
    """Back up and restore athlete config around each test."""
    original = athlete_store.load(user_slug)
    yield
    if original is not None:
        athlete_store.save(user_slug, original)


class TestActionItems:

    def test_get_returns_dict(self, user_slug):
        result = health_tools.get_action_items(user_slug)
        assert isinstance(result, (dict, list))

    def test_add_item(self, user_slug):
        result = health_tools.add_action_item(
            user_slug,
            title="Test action item",
            description="Created by test suite",
            category="testing",
            priority="low",
        )
        assert isinstance(result, dict)
        assert "added" in result or "id" in result or "item_id" in result

    def test_add_and_retrieve(self, user_slug):
        add_result = health_tools.add_action_item(
            user_slug,
            title="Unique test item 12345",
            description="Should appear in list",
        )
        items = health_tools.get_action_items(user_slug)
        if isinstance(items, dict):
            all_items = []
            for group in items.values():
                if isinstance(group, list):
                    all_items.extend(group)
            found = any("12345" in str(i) for i in all_items)
        else:
            found = any("12345" in str(i) for i in items)
        assert found, "Added action item not found in list"

    def test_complete_item(self, user_slug):
        add_result = health_tools.add_action_item(
            user_slug,
            title="Item to complete",
            description="Will be completed",
        )
        item_id = add_result.get("id") or add_result.get("item_id", "")
        if not item_id:
            pytest.skip("Could not get item ID from add result")
        result = health_tools.complete_action_item(user_slug, item_id, note="Done in test")
        assert isinstance(result, dict)

    def test_update_item(self, user_slug):
        add_result = health_tools.add_action_item(
            user_slug,
            title="Item to update",
            description="Original description",
        )
        item_id = add_result.get("id") or add_result.get("item_id", "")
        if not item_id:
            pytest.skip("Could not get item ID from add result")
        result = health_tools.update_action_item(
            user_slug, item_id, priority="high", note="Escalated by test"
        )
        assert isinstance(result, dict)

    def test_filter_by_status(self, user_slug):
        result = health_tools.get_action_items(user_slug, status_filter="pending")
        assert isinstance(result, (dict, list))

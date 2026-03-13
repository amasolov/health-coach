"""Tests for _localise_rows date label generation.

Verifies that localised rows include a human-readable date_label with the
correct weekday, so the LLM doesn't need to compute day-of-week itself.

Regression test for https://github.com/amasolov/health-coach/issues/37
"""

from __future__ import annotations

import pytest

from scripts.health_tools import _localise_rows


class TestLocaliseDateLabel:
    """_localise_rows must add a date_label field with the correct weekday."""

    def test_date_label_present_after_localisation(self):
        rows = [{"time": "2026-03-12T09:17:40+00:00"}]
        result = _localise_rows(rows, "Australia/Sydney")
        assert "date_label" in result[0], (
            "_localise_rows should add a date_label field"
        )

    def test_date_label_contains_correct_weekday(self):
        """March 12, 2026 is a Thursday in Australia/Sydney (AEDT)."""
        rows = [{"time": "2026-03-12T09:17:40+00:00"}]
        result = _localise_rows(rows, "Australia/Sydney")
        label = result[0]["date_label"]
        assert "Thursday" in label, (
            f"March 12 2026 is Thursday, got: {label!r}"
        )

    def test_date_label_contains_date_number(self):
        rows = [{"time": "2026-03-12T09:17:40+00:00"}]
        result = _localise_rows(rows, "Australia/Sydney")
        label = result[0]["date_label"]
        assert "12" in label, f"Label should contain day number, got: {label!r}"

    def test_date_label_uses_user_timezone(self):
        """A UTC timestamp near midnight should use the user's local date.
        2026-03-11T14:00:00Z = 2026-03-12T01:00:00+11:00 (AEDT) = Thursday."""
        rows = [{"time": "2026-03-11T14:00:00+00:00"}]
        result = _localise_rows(rows, "Australia/Sydney")
        label = result[0]["date_label"]
        assert "Thursday" in label and "12" in label, (
            f"Near-midnight UTC should map to next day in AEDT, got: {label!r}"
        )

    def test_multiple_rows_get_labels(self):
        rows = [
            {"time": "2026-03-12T09:17:40+00:00"},
            {"time": "2026-03-12T08:45:04+00:00"},
            {"time": "2026-03-11T07:00:00+00:00"},
        ]
        result = _localise_rows(rows, "Australia/Sydney")
        for row in result:
            assert "date_label" in row

    def test_no_label_without_tz_name(self):
        """When tz_name is empty, rows should pass through unchanged."""
        rows = [{"time": "2026-03-12T09:17:40+00:00"}]
        result = _localise_rows(rows, "")
        assert "date_label" not in result[0]

    def test_issue_37_all_weekdays_correct(self):
        """Reproduce the exact scenario from issue #37: a week of activities
        where the LLM showed every weekday name off by one day."""
        utc_rows = [
            {"time": "2026-03-06T20:00:00+00:00"},  # Sat 7 Mar 07:00 AEDT
            {"time": "2026-03-07T20:00:00+00:00"},  # Sun 8 Mar 07:00 AEDT
            {"time": "2026-03-08T20:00:00+00:00"},  # Mon 9 Mar 07:00 AEDT
            {"time": "2026-03-09T20:00:00+00:00"},  # Tue 10 Mar 07:00 AEDT
            {"time": "2026-03-10T20:00:00+00:00"},  # Wed 11 Mar 07:00 AEDT
            {"time": "2026-03-12T09:17:40+00:00"},  # Thu 12 Mar 20:17 AEDT
        ]
        expected_weekdays = [
            "Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
        ]
        result = _localise_rows(utc_rows, "Australia/Sydney")
        for row, expected_day in zip(result, expected_weekdays):
            assert expected_day in row["date_label"], (
                f"Expected {expected_day} in label, got: {row['date_label']!r}"
            )

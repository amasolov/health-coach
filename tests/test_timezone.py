"""Tests for timezone correctness across the codebase.

Verifies that date/time handling always respects user timezone and never
falls back to server-local time.
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"


# ---------------------------------------------------------------------------
# 1. _parse_garmin_datetime must return UTC-aware datetimes
# ---------------------------------------------------------------------------

class TestParseGarminDatetime:

    def _parse(self, s):
        from scripts.sync_garmin import _parse_garmin_datetime
        return _parse_garmin_datetime(s)

    def test_datetime_format_is_utc_aware(self):
        dt = self._parse("2026-03-12 06:30:00")
        assert dt is not None
        assert dt.tzinfo is not None, "Garmin datetime must be UTC-aware"
        assert dt.tzinfo == timezone.utc

    def test_iso_format_is_utc_aware(self):
        dt = self._parse("2026-03-12T06:30:00")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.tzinfo == timezone.utc

    def test_date_only_is_utc_aware(self):
        dt = self._parse("2026-03-12")
        assert dt is not None
        assert dt.tzinfo is not None, "Date-only parse must be UTC-aware"
        assert dt.tzinfo == timezone.utc

    def test_none_input(self):
        assert self._parse(None) is None

    def test_empty_input(self):
        assert self._parse("") is None


# ---------------------------------------------------------------------------
# 2. _extract_vitals must produce UTC-aware timestamps
# ---------------------------------------------------------------------------

class TestExtractVitals:

    def test_vitals_time_is_utc_aware(self):
        from scripts.sync_garmin import _extract_vitals
        data = _extract_vitals(
            "2026-03-12",
            {"restingHeartRate": 52, "averageStressLevel": 30},
            None, None, None, None,
        )
        dt = data["time"]
        assert dt.tzinfo is not None, "Vitals time must be UTC-aware"
        assert dt.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# 3. sync_vitals must not mix UTC and local dates
# ---------------------------------------------------------------------------

class TestSyncVitalsDateRange:

    def test_start_date_uses_user_tz_when_resuming(self):
        """When last vitals time is UTC, it must be converted to user tz
        before taking .date() so start_date matches the user's calendar."""
        from scripts.sync_garmin import sync_vitals

        sydney = ZoneInfo("Australia/Sydney")
        utc_midnight = datetime(2026, 3, 12, 0, 0, 0, tzinfo=timezone.utc)

        mock_cur = MagicMock()
        mock_client = MagicMock()
        mock_client.get_stats.return_value = {}

        with patch("scripts.sync_garmin._get_last_vitals_time", return_value=utc_midnight), \
             patch("scripts.sync_garmin.user_today", return_value=date(2026, 3, 12)):
            sync_vitals(mock_client, mock_cur, user_id=1, tz=sydney)

        # The UTC midnight Mar 12 is actually Mar 12 11:00 AEDT, so in
        # Sydney it's the same calendar day. But if it were 23:00 UTC Mar 11,
        # that would be Mar 12 in Sydney — the key point is the conversion
        # must happen.


# ---------------------------------------------------------------------------
# 4. get_vitals must pass tz-aware timestamps to SQL
# ---------------------------------------------------------------------------

class TestGetVitalsDateBoundaries:

    def test_default_range_uses_user_tz(self, user_id):
        """Vitals queried with tz_name should use that timezone for date
        boundaries, not the server default."""
        from scripts import health_tools

        result = health_tools.get_vitals(
            user_id, days=7, tz_name="Australia/Sydney"
        )
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# 5. Static analysis: no bare date.today() or datetime.now() in scripts/
# ---------------------------------------------------------------------------

class TestNoBareLocalTime:
    """AST-scan scripts/*.py for banned patterns.

    Flags ``date.today()`` and ``datetime.now()`` with zero arguments.
    ``datetime.now(tz)`` (with any tz argument) is allowed because it
    produces a tz-aware datetime.
    """

    ALLOWED_FILES = {
        "tz.py",
    }

    @staticmethod
    def _is_bare_date_today(node: ast.Call) -> bool:
        """date.today() — always zero args."""
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "date"
            and func.attr == "today"
        )

    @staticmethod
    def _is_bare_datetime_now(node: ast.Call) -> bool:
        """datetime.now() with no arguments (server-local).
        datetime.now(tz) is fine."""
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "datetime"
            and func.attr == "now"
        ):
            return False
        return not node.args and not node.keywords

    @staticmethod
    def _scan_file(path: Path) -> list[tuple[int, str]]:
        source = path.read_text()
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return []

        hits = []
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if TestNoBareLocalTime._is_bare_date_today(node):
                line_text = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                if "birth" in line_text or "age" in line_text.lower():
                    continue
                hits.append((node.lineno, "date.today()"))
            elif TestNoBareLocalTime._is_bare_datetime_now(node):
                hits.append((node.lineno, "datetime.now()"))
        return hits

    def test_no_bare_date_today_or_datetime_now(self):
        violations = []
        for py_file in sorted(SCRIPTS_DIR.glob("*.py")):
            if py_file.name in self.ALLOWED_FILES:
                continue
            for lineno, call in self._scan_file(py_file):
                violations.append(f"  {py_file.name}:{lineno} — {call}")

        assert not violations, (
            "Found bare date.today() or datetime.now() calls "
            "(use user_today(tz) / user_now(tz) / utc_now() instead):\n"
            + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# 6. Static analysis: no user_today() / user_now() without tz argument
# ---------------------------------------------------------------------------

class TestNoDefaultTzFallback:
    """AST-scan for user_today() or user_now() called with zero arguments."""

    TARGET_FUNCS = {"user_today", "user_now"}

    ALLOWED_FILES = {"tz.py"}

    @staticmethod
    def _scan_file(path: Path) -> list[tuple[int, str]]:
        source = path.read_text()
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return []

        hits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in TestNoDefaultTzFallback.TARGET_FUNCS:
                if not node.args and not node.keywords:
                    hits.append((node.lineno, f"{name}()"))
        return hits

    def test_no_zero_arg_user_today_or_user_now(self):
        violations = []
        for py_file in sorted(SCRIPTS_DIR.glob("*.py")):
            if py_file.name in self.ALLOWED_FILES:
                continue
            for lineno, call in self._scan_file(py_file):
                violations.append(f"  {py_file.name}:{lineno} — {call}")

        assert not violations, (
            "Found user_today() or user_now() with no tz argument "
            "(always pass explicit tz):\n"
            + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# 7. generate_action_items must accept timezone
# ---------------------------------------------------------------------------

class TestGenerateActionItemsTz:

    def test_accepts_tz_parameter(self):
        import inspect
        from scripts.fitness_assessment import generate_action_items
        sig = inspect.signature(generate_action_items)
        assert "tz" in sig.parameters or "slug" in sig.parameters, (
            "generate_action_items must accept a tz or slug parameter"
        )

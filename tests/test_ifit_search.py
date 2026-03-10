"""Tests for iFit search and program tools.

Marked slow — these scan the full 12K+ workout library on every call.
Run explicitly with:  pytest -m slow tests/test_ifit_search.py
"""

import json
import pytest
from scripts import health_tools

pytestmark = pytest.mark.slow


class TestSearchIfitLibrary:

    def test_basic_search(self):
        result = health_tools.search_ifit_library(query="strength")
        assert isinstance(result, (list, dict))

    def test_returns_results(self):
        result = health_tools.search_ifit_library(query="run")
        if isinstance(result, dict):
            results = result.get("results", [])
        else:
            results = result
        assert len(results) > 0, "Expected search results for 'run'"

    def test_result_fields(self):
        result = health_tools.search_ifit_library(query="strength", limit=3)
        if isinstance(result, dict):
            results = result.get("results", [])
        else:
            results = result
        if results:
            r = results[0]
            assert "title" in r or "name" in r
            assert "id" in r or "workout_id" in r

    def test_type_filter(self):
        result = health_tools.search_ifit_library(query="", workout_type="strength", limit=5)
        if isinstance(result, dict):
            results = result.get("results", [])
        else:
            results = result
        assert isinstance(results, list)

    def test_limit_parameter(self):
        result = health_tools.search_ifit_library(query="recovery", limit=3)
        if isinstance(result, dict):
            results = result.get("results", [])
        else:
            results = result
        assert len(results) <= 3

    def test_trainer_search(self):
        result = health_tools.search_ifit_library(query="Tommy Rivs")
        if isinstance(result, dict):
            results = result.get("results", [])
        else:
            results = result
        assert len(results) > 0, "Expected results for trainer Tommy Rivs"

    def test_series_phrase_search(self):
        """The phrase matching boost should rank series matches highly."""
        result = health_tools.search_ifit_library(query="10K Training Series Part 1", limit=10)
        if isinstance(result, dict):
            results = result.get("results", [])
        else:
            results = result
        titles = [r.get("title", "") for r in results]
        programs = []
        for r in results:
            progs = r.get("programs", [])
            for p in progs:
                programs.append(p.get("title", ""))
        has_series = any("10k" in t.lower() for t in titles + programs)
        assert has_series, f"Expected '10K Training' in results, got titles: {titles}"

    def test_empty_query(self):
        result = health_tools.search_ifit_library(query="", limit=5)
        assert isinstance(result, (list, dict))

    def test_no_results_for_nonsense(self):
        result = health_tools.search_ifit_library(query="xyzzyplugh42", limit=5)
        if isinstance(result, dict):
            results = result.get("results", [])
        else:
            results = result
        assert len(results) == 0


class TestSearchIfitPrograms:

    def test_basic_search(self):
        result = health_tools.search_ifit_programs(query="10K Training")
        assert isinstance(result, (list, dict))

    def test_returns_results(self):
        result = health_tools.search_ifit_programs(query="training")
        if isinstance(result, dict):
            results = result.get("results", result.get("programs", []))
        else:
            results = result
        assert len(results) > 0, "Expected program results for 'training'"

    def test_program_has_workouts(self):
        result = health_tools.search_ifit_programs(query="10K", limit=3)
        if isinstance(result, dict):
            results = result.get("results", result.get("programs", []))
        else:
            results = result
        if results:
            prog = results[0]
            assert "workout_count" in prog or "workouts" in prog or "workout_ids" in prog

    def test_limit(self):
        result = health_tools.search_ifit_programs(query="run", limit=2)
        if isinstance(result, dict):
            results = result.get("results", result.get("programs", []))
        else:
            results = result
        assert len(results) <= 2

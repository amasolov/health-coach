"""Tests for route discovery module (OSM Overpass integration + scoring)."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch, MagicMock

import pytest

from scripts.route_discovery import (
    Route,
    _classify_surface,
    _haversine,
    _is_loop,
    _way_length,
    _explain_recommendation,
    parse_routes,
    score_routes,
    recommend_outdoor_run,
    DEFAULT_RUNNING_PREFS,
)


# ---------------------------------------------------------------------------
# Fixtures: sample OSM Overpass elements
# ---------------------------------------------------------------------------

def _make_way(
    osm_id: int = 1001,
    name: str = "Centennial Park Loop",
    highway: str = "footway",
    surface: str = "compacted",
    nodes: list[dict] | None = None,
) -> dict:
    """Build a minimal Overpass way element."""
    if nodes is None:
        nodes = [
            {"lat": -33.8960, "lon": 151.2330},
            {"lat": -33.8970, "lon": 151.2350},
            {"lat": -33.8980, "lon": 151.2340},
            {"lat": -33.8960, "lon": 151.2330},  # loop back
        ]
    return {
        "type": "way",
        "id": osm_id,
        "tags": {"name": name, "highway": highway, "surface": surface},
        "geometry": nodes,
    }


def _make_linear_way(
    osm_id: int = 2001,
    name: str = "Coastal Walk",
    highway: str = "path",
    surface: str = "dirt",
    length_nodes: int = 20,
) -> dict:
    """Build a longer linear (non-loop) way."""
    nodes = [
        {"lat": -33.87 + i * 0.001, "lon": 151.21 + i * 0.001}
        for i in range(length_nodes)
    ]
    return {
        "type": "way",
        "id": osm_id,
        "tags": {"name": name, "highway": highway, "surface": surface},
        "geometry": nodes,
    }


# ---------------------------------------------------------------------------
# Geometry tests
# ---------------------------------------------------------------------------

class TestGeometry:

    def test_haversine_zero_distance(self):
        assert _haversine(0, 0, 0, 0) == 0.0

    def test_haversine_known_distance(self):
        dist = _haversine(-33.8688, 151.2093, -33.8778, 151.2193)
        assert 1000 < dist < 2000

    def test_way_length_positive(self):
        nodes = [
            {"lat": -33.87, "lon": 151.21},
            {"lat": -33.88, "lon": 151.22},
        ]
        assert _way_length(nodes) > 0

    def test_is_loop_true(self):
        nodes = [
            {"lat": -33.87, "lon": 151.21},
            {"lat": -33.88, "lon": 151.22},
            {"lat": -33.87, "lon": 151.21},
        ]
        assert _is_loop(nodes) is True

    def test_is_loop_false(self):
        nodes = [
            {"lat": -33.87, "lon": 151.21},
            {"lat": -33.88, "lon": 151.22},
            {"lat": -33.89, "lon": 151.23},
        ]
        assert _is_loop(nodes) is False

    def test_is_loop_too_few_nodes(self):
        assert _is_loop([{"lat": 0, "lon": 0}]) is False


# ---------------------------------------------------------------------------
# Surface classification
# ---------------------------------------------------------------------------

class TestSurfaceClassification:

    def test_asphalt_is_sealed(self):
        assert _classify_surface({"surface": "asphalt"}) == "sealed_road"

    def test_dirt_is_trail(self):
        assert _classify_surface({"surface": "dirt"}) == "trail"

    def test_compacted_is_unsealed(self):
        assert _classify_surface({"surface": "compacted"}) == "unsealed_road"

    def test_fallback_to_highway(self):
        assert _classify_surface({"highway": "footway"}) == "trail"

    def test_unknown_is_mixed(self):
        assert _classify_surface({}) == "mixed"


# ---------------------------------------------------------------------------
# Route parsing
# ---------------------------------------------------------------------------

class TestRouteParsing:

    def test_parse_way_element(self):
        elements = [_make_way()]
        routes = parse_routes(elements)
        assert len(routes) == 1
        r = routes[0]
        assert r.osm_id == 1001
        assert r.name == "Centennial Park Loop"
        assert r.is_loop is True

    def test_parse_skips_non_ways(self):
        elements = [{"type": "node", "id": 1, "tags": {}}]
        assert parse_routes(elements) == []

    def test_parse_skips_short_ways(self):
        elements = [_make_way(nodes=[
            {"lat": -33.87, "lon": 151.21},
            {"lat": -33.8700001, "lon": 151.2100001},
        ])]
        assert parse_routes(elements) == []

    def test_parse_unnamed_gets_label(self):
        el = _make_way()
        del el["tags"]["name"]
        routes = parse_routes([el])
        assert len(routes) == 1
        assert routes[0].name.startswith("Unnamed")

    def test_parse_linear_route(self):
        elements = [_make_linear_way()]
        routes = parse_routes(elements)
        assert len(routes) == 1
        assert routes[0].is_loop is False


# ---------------------------------------------------------------------------
# Route scoring
# ---------------------------------------------------------------------------

class TestRouteScoring:

    def _make_route(self, **overrides) -> Route:
        defaults = {
            "osm_id": 1001,
            "name": "Test Route",
            "distance_m": 5000,
            "surface_type": "trail",
            "highway_type": "footway",
            "is_loop": True,
            "lat": -33.87,
            "lon": 151.21,
        }
        defaults.update(overrides)
        return Route(**defaults)

    def test_scoring_returns_sorted(self):
        routes = [
            self._make_route(distance_m=5000, surface_type="trail"),
            self._make_route(osm_id=1002, distance_m=50000, surface_type="sealed_road", name="Far Route"),
        ]
        scored = score_routes(routes, {"preferred_distance_km": [5], "surface": ["trail"]})
        assert scored[0].score >= scored[1].score

    def test_distance_match_boosts_score(self):
        route_close = self._make_route(distance_m=5000)
        route_far = self._make_route(osm_id=1002, distance_m=25000)
        prefs = {"preferred_distance_km": [5], "surface": ["trail"]}
        score_routes([route_close], prefs)
        score_routes([route_far], prefs)
        assert route_close.score > route_far.score

    def test_surface_match_boosts_score(self):
        route_match = self._make_route(
            surface_type="trail", is_loop=False, highway_type="track",
            name="Unnamed path", distance_m=20000,
        )
        route_no_match = self._make_route(
            osm_id=1002, surface_type="sealed_road", is_loop=False,
            highway_type="track", name="Unnamed path", distance_m=20000,
        )
        prefs = {"preferred_distance_km": [5], "surface": ["trail"],
                 "prefer_loop": False, "avoid_high_traffic": False}
        score_routes([route_match], prefs)
        score_routes([route_no_match], prefs)
        assert route_match.score > route_no_match.score

    def test_loop_preference(self):
        route_loop = self._make_route(
            is_loop=True, name="Unnamed path", distance_m=20000,
            highway_type="track", surface_type="sealed_road",
        )
        route_linear = self._make_route(
            osm_id=1002, is_loop=False, name="Unnamed path",
            distance_m=20000, highway_type="track", surface_type="sealed_road",
        )
        prefs = {"preferred_distance_km": [5], "surface": [],
                 "prefer_loop": True, "avoid_high_traffic": False}
        score_routes([route_loop], prefs)
        score_routes([route_linear], prefs)
        assert route_loop.score > route_linear.score

    def test_proximity_boost(self):
        route_near = self._make_route(
            lat=-33.87, lon=151.21, name="Unnamed path",
            distance_m=20000, highway_type="track", surface_type="sealed_road",
            is_loop=False,
        )
        route_far = self._make_route(
            osm_id=1002, lat=-33.95, lon=151.30, name="Unnamed path",
            distance_m=20000, highway_type="track", surface_type="sealed_road",
            is_loop=False,
        )
        prefs = {"preferred_distance_km": [5], "surface": [],
                 "prefer_loop": False, "avoid_high_traffic": False}
        score_routes([route_near], prefs, user_lat=-33.87, user_lon=151.21)
        score_routes([route_far], prefs, user_lat=-33.87, user_lon=151.21)
        assert route_near.score > route_far.score

    def test_low_traffic_boost(self):
        route_path = self._make_route(
            highway_type="footway", name="Unnamed path",
            distance_m=20000, surface_type="sealed_road", is_loop=False,
        )
        route_road = self._make_route(
            osm_id=1002, highway_type="secondary", name="Unnamed path",
            distance_m=20000, surface_type="sealed_road", is_loop=False,
        )
        prefs = {"preferred_distance_km": [5], "surface": [],
                 "prefer_loop": False, "avoid_high_traffic": True}
        score_routes([route_path], prefs)
        score_routes([route_road], prefs)
        assert route_path.score > route_road.score


class TestRouteToDict:

    def test_to_dict_has_required_fields(self):
        route = Route(
            osm_id=1001, name="Test", distance_m=5000,
            surface_type="trail", highway_type="footway",
            is_loop=True, lat=-33.87, lon=151.21, score=85,
        )
        d = route.to_dict()
        assert d["osm_id"] == 1001
        assert d["distance_km"] == 5.0
        assert d["osm_url"].startswith("https://")
        assert d["score"] == 85


# ---------------------------------------------------------------------------
# Explanation helper
# ---------------------------------------------------------------------------

class TestExplanation:

    def test_distance_match_mentioned(self):
        route = Route(
            osm_id=1, name="X", distance_m=5200,
            surface_type="trail", highway_type="footway",
            is_loop=True, lat=0, lon=0,
        )
        explanation = _explain_recommendation(route, {"preferred_distance_km": [5]})
        assert "5.2" in explanation

    def test_loop_mentioned(self):
        route = Route(
            osm_id=1, name="X", distance_m=10000,
            surface_type="trail", highway_type="footway",
            is_loop=True, lat=0, lon=0,
        )
        explanation = _explain_recommendation(route, {"preferred_distance_km": [10]})
        assert "loop" in explanation.lower()


# ---------------------------------------------------------------------------
# Integration: recommend_outdoor_run()
# ---------------------------------------------------------------------------

class TestRecommendOutdoorRun:

    def test_no_location_raises(self, user_slug):
        with patch("scripts.athlete_store.load", return_value={"profile": {}}):
            with pytest.raises(ValueError, match="No location configured"):
                recommend_outdoor_run(user_slug)

    def test_bad_weather_returns_indoor(self, user_slug):
        config = {
            "profile": {"timezone": "America/New_York"},
            "location": {"lat": -33.87, "lon": 151.21, "label": "Sydney"},
            "weather": {},
        }
        bad_weather_result = {
            "location": "Sydney",
            "target_date": "2026-03-13",
            "suitability": {"suitable": False, "score": 10, "reasons": ["Thunderstorm"], "warnings": [], "best_windows": []},
            "forecast": [],
            "current_conditions": {},
        }
        with patch("scripts.athlete_store.load", return_value=config), \
             patch("scripts.weather.check_weather", return_value=bad_weather_result):
            result = recommend_outdoor_run(user_slug)
            assert result["recommendation"] == "indoor"
            assert len(result["routes"]) == 0

    def test_good_weather_returns_routes(self, user_slug):
        config = {
            "profile": {"timezone": "America/New_York"},
            "location": {"lat": -33.87, "lon": 151.21, "label": "Sydney"},
            "running_preferences": DEFAULT_RUNNING_PREFS,
        }
        good_weather_result = {
            "location": "Sydney",
            "target_date": "2026-03-13",
            "suitability": {"suitable": True, "score": 85, "reasons": ["Clear sky"], "warnings": [], "best_windows": []},
            "forecast": [],
            "current_conditions": {},
        }
        osm_elements = [
            _make_way(osm_id=1001, name="Park Run Loop"),
            _make_linear_way(osm_id=2001, name="Bay Trail"),
        ]
        with patch("scripts.athlete_store.load", return_value=config), \
             patch("scripts.weather.check_weather", return_value=good_weather_result), \
             patch("scripts.route_discovery.fetch_routes", return_value=osm_elements):
            result = recommend_outdoor_run(user_slug)
            assert result["recommendation"] == "outdoor"
            assert len(result["routes"]) > 0
            assert "why" in result["routes"][0]

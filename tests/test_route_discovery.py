"""Tests for route discovery module (OSM Overpass integration + scoring).

Covers Phase 2 (basic routing), Phase 3 (popularity, novelty, ratings),
and Phase 4 (training context, weather nudge).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch, MagicMock

import pytest

from scripts.route_discovery import (
    Route,
    _classify_surface,
    _compute_popularity,
    _extract_relations,
    _explain_recommendation,
    _haversine,
    _is_loop,
    _training_adjusted_distances,
    _training_context_bonus,
    _training_suggestion,
    _way_length,
    get_weather_nudge,
    infer_training_context,
    parse_routes,
    rate_route,
    recommend_outdoor_run,
    score_routes,
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
    extra_tags: dict | None = None,
) -> dict:
    """Build a minimal Overpass way element."""
    if nodes is None:
        nodes = [
            {"lat": -33.8960, "lon": 151.2330},
            {"lat": -33.8970, "lon": 151.2350},
            {"lat": -33.8980, "lon": 151.2340},
            {"lat": -33.8960, "lon": 151.2330},  # loop back
        ]
    tags = {"name": name, "highway": highway, "surface": surface}
    if extra_tags:
        tags.update(extra_tags)
    return {
        "type": "way",
        "id": osm_id,
        "tags": tags,
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


def _make_relation(
    rel_id: int = 5001,
    name: str = "Sydney Harbour Walk",
    way_ids: list[int] | None = None,
    route_type: str = "hiking",
) -> dict:
    """Build a minimal Overpass route relation."""
    way_ids = way_ids or [1001, 2001]
    return {
        "type": "relation",
        "id": rel_id,
        "tags": {"type": "route", "route": route_type, "name": name},
        "members": [{"type": "way", "ref": wid, "role": ""} for wid in way_ids],
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
# Phase 3: Relation extraction
# ---------------------------------------------------------------------------

class TestRelationExtraction:

    def test_extracts_relation_members(self):
        elements = [
            _make_way(osm_id=1001),
            _make_relation(rel_id=5001, way_ids=[1001, 2001]),
        ]
        rels = _extract_relations(elements)
        assert 1001 in rels
        assert 2001 in rels
        assert rels[1001] == ["Sydney Harbour Walk"]

    def test_multiple_relations_per_way(self):
        elements = [
            _make_relation(rel_id=5001, name="Trail A", way_ids=[1001]),
            _make_relation(rel_id=5002, name="Trail B", way_ids=[1001]),
        ]
        rels = _extract_relations(elements)
        assert len(rels[1001]) == 2
        assert "Trail A" in rels[1001]
        assert "Trail B" in rels[1001]

    def test_ignores_non_relations(self):
        elements = [_make_way(osm_id=1001)]
        rels = _extract_relations(elements)
        assert len(rels) == 0

    def test_unnamed_relation_uses_ref(self):
        rel = _make_relation(rel_id=5001, way_ids=[1001])
        rel["tags"]["name"] = ""
        rel["tags"]["ref"] = "GR-10"
        rels = _extract_relations([rel])
        assert rels[1001] == ["GR-10"]


# ---------------------------------------------------------------------------
# Phase 3: Popularity scoring
# ---------------------------------------------------------------------------

class TestPopularity:

    def test_no_signals_zero(self):
        assert _compute_popularity({}, 0) == 0.0

    def test_route_relation_boosts(self):
        pop_one = _compute_popularity({}, 1)
        pop_three = _compute_popularity({}, 3)
        assert pop_one >= 25
        assert pop_three >= 40

    def test_foot_designated_boosts(self):
        pop = _compute_popularity({"foot": "designated"}, 0)
        assert pop >= 15

    def test_lit_boosts(self):
        pop = _compute_popularity({"lit": "yes"}, 0)
        assert pop >= 10

    def test_scenic_boosts(self):
        pop = _compute_popularity({"leisure": "park"}, 0)
        assert pop >= 10

    def test_wikipedia_boosts(self):
        pop = _compute_popularity({"wikipedia": "en:Hyde Park"}, 0)
        assert pop >= 10

    def test_combined_capped_at_100(self):
        pop = _compute_popularity(
            {"foot": "designated", "lit": "yes", "leisure": "park",
             "wikipedia": "en:X", "wheelchair": "yes"},
            5,
        )
        assert pop <= 100

    def test_parse_routes_includes_popularity(self):
        elements = [
            _make_way(osm_id=1001, extra_tags={"foot": "designated", "lit": "yes"}),
            _make_relation(rel_id=5001, way_ids=[1001]),
        ]
        routes = parse_routes(elements)
        assert len(routes) == 1
        assert routes[0].popularity > 0
        assert routes[0].relation_names == ["Sydney Harbour Walk"]


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

    # --- Phase 3: Popularity affects scoring ---

    def test_popularity_boosts_score(self):
        route_popular = self._make_route(
            popularity=80, name="Unnamed x", distance_m=20000,
            highway_type="track", is_loop=False, surface_type="sealed_road",
        )
        route_unknown = self._make_route(
            osm_id=1002, popularity=0, name="Unnamed x", distance_m=20000,
            highway_type="track", is_loop=False, surface_type="sealed_road",
        )
        prefs = {"preferred_distance_km": [5], "surface": [],
                 "prefer_loop": False, "avoid_high_traffic": False}
        score_routes([route_popular], prefs)
        score_routes([route_unknown], prefs)
        assert route_popular.score > route_unknown.score

    # --- Phase 3: Novelty bonus ---

    def test_novelty_bonus_for_unseen_routes(self):
        route_new = self._make_route(
            osm_id=9999, name="Unnamed x", distance_m=20000,
            highway_type="track", is_loop=False, surface_type="sealed_road",
        )
        route_seen = self._make_route(
            osm_id=1002, name="Unnamed x", distance_m=20000,
            highway_type="track", is_loop=False, surface_type="sealed_road",
        )
        prefs = {"preferred_distance_km": [5], "surface": [],
                 "prefer_loop": False, "avoid_high_traffic": False}
        score_routes([route_new], prefs, recently_shown_ids={1002})
        score_routes([route_seen], prefs, recently_shown_ids={1002})
        assert route_new.score > route_seen.score

    # --- Phase 4: Training context ---

    def test_training_context_easy_day(self):
        route_flat = self._make_route(
            highway_type="pedestrian", surface_type="sealed_road",
            name="Unnamed x", distance_m=20000, is_loop=False,
        )
        route_trail = self._make_route(
            osm_id=1002, highway_type="track", surface_type="trail",
            name="Unnamed x", distance_m=20000, is_loop=False,
        )
        prefs = {"preferred_distance_km": [5], "surface": [],
                 "prefer_loop": False, "avoid_high_traffic": False}
        tc = {"run_type": "easy", "tsb": -20}
        score_routes([route_flat], prefs, training_context=tc)
        score_routes([route_trail], prefs, training_context=tc)
        assert route_flat.score > route_trail.score

    def test_training_context_long_run(self):
        route_scenic_loop = self._make_route(
            is_loop=True, tags={"leisure": "park"}, relation_names=["Great Trail"],
            name="Unnamed x", distance_m=20000, highway_type="track",
            surface_type="sealed_road",
        )
        route_plain = self._make_route(
            osm_id=1002, is_loop=False, name="Unnamed x",
            distance_m=20000, highway_type="track", surface_type="sealed_road",
        )
        prefs = {"preferred_distance_km": [5], "surface": [],
                 "prefer_loop": False, "avoid_high_traffic": False}
        tc = {"run_type": "long", "tsb": 15}
        score_routes([route_scenic_loop], prefs, training_context=tc)
        score_routes([route_plain], prefs, training_context=tc)
        assert route_scenic_loop.score > route_plain.score


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

    def test_to_dict_includes_popularity(self):
        route = Route(
            osm_id=1, name="X", distance_m=1000,
            surface_type="trail", highway_type="footway",
            is_loop=False, lat=0, lon=0, popularity=75,
        )
        assert route.to_dict()["popularity"] == 75

    def test_to_dict_includes_relations(self):
        route = Route(
            osm_id=1, name="X", distance_m=1000,
            surface_type="trail", highway_type="footway",
            is_loop=False, lat=0, lon=0,
            relation_names=["Bay Trail"],
        )
        d = route.to_dict()
        assert "part_of_routes" in d
        assert d["part_of_routes"] == ["Bay Trail"]


# ---------------------------------------------------------------------------
# Phase 4: Training helpers
# ---------------------------------------------------------------------------

class TestTrainingAdjustedDistances:

    def test_normal_unchanged(self):
        dists = _training_adjusted_distances([5, 10], {"run_type": "normal"})
        assert dists == [5, 10]

    def test_easy_shorter(self):
        dists = _training_adjusted_distances([10], {"run_type": "easy"})
        assert dists[0] < 10

    def test_long_longer(self):
        dists = _training_adjusted_distances([10], {"run_type": "long"})
        assert dists[0] > 10


class TestTrainingContextBonus:

    def test_easy_day_prefers_sealed(self):
        route = Route(
            osm_id=1, name="X", distance_m=3000,
            surface_type="sealed_road", highway_type="pedestrian",
            is_loop=False, lat=0, lon=0,
        )
        bonus = _training_context_bonus(route, {"run_type": "easy"})
        assert bonus > 0

    def test_long_run_prefers_loop(self):
        route = Route(
            osm_id=1, name="X", distance_m=15000,
            surface_type="trail", highway_type="path",
            is_loop=True, lat=0, lon=0,
            relation_names=["Scenic Trail"],
        )
        bonus = _training_context_bonus(route, {"run_type": "long"})
        assert bonus > 0

    def test_normal_no_bonus(self):
        route = Route(
            osm_id=1, name="X", distance_m=5000,
            surface_type="trail", highway_type="path",
            is_loop=False, lat=0, lon=0,
        )
        assert _training_context_bonus(route, {"run_type": "normal"}) == 0


class TestTrainingSuggestion:

    def test_easy_suggestion(self):
        s = _training_suggestion("easy", -20)
        assert "fatigue" in s.lower() or "recovery" in s.lower()

    def test_long_suggestion(self):
        s = _training_suggestion("long", 15)
        assert "long" in s.lower()

    def test_normal_suggestion(self):
        s = _training_suggestion("normal", 0)
        assert "normal" in s.lower()


class TestInferTrainingContext:

    def test_fatigued_returns_easy(self, user_slug):
        summary = {"tsb_form": -20, "form_status": "Fatigued"}
        with patch("scripts.health_tools.resolve_user_id", return_value=1), \
             patch("scripts.health_tools.get_fitness_summary", return_value=summary), \
             patch("scripts.athlete_store.load", return_value={"profile": {}}):
            tc = infer_training_context(user_slug)
            assert tc["run_type"] == "easy"

    def test_fresh_returns_long_on_weekend(self, user_slug):
        summary = {"tsb_form": 15, "form_status": "Fresh"}
        config = {"profile": {}, "goals": {"preferred_sports": ["running"]}}
        with patch("scripts.health_tools.resolve_user_id", return_value=1), \
             patch("scripts.health_tools.get_fitness_summary", return_value=summary), \
             patch("scripts.athlete_store.load", return_value=config), \
             patch("scripts.tz.user_today") as mock_today:
            from datetime import date as _d
            mock_today.return_value = _d(2026, 3, 14)  # Saturday
            tc = infer_training_context(user_slug)
            assert tc["run_type"] == "long"


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

    def test_relation_mentioned(self):
        route = Route(
            osm_id=1, name="X", distance_m=5000,
            surface_type="trail", highway_type="footway",
            is_loop=False, lat=0, lon=0,
            relation_names=["Bay Trail"],
        )
        explanation = _explain_recommendation(route, {"preferred_distance_km": [5]})
        assert "Bay Trail" in explanation

    def test_training_context_in_explanation(self):
        route = Route(
            osm_id=1, name="X", distance_m=3000,
            surface_type="sealed_road", highway_type="footway",
            is_loop=False, lat=0, lon=0,
        )
        explanation = _explain_recommendation(
            route, {"preferred_distance_km": [5]}, {"run_type": "easy"},
        )
        assert "recovery" in explanation.lower()


# ---------------------------------------------------------------------------
# Phase 4: Weather nudge
# ---------------------------------------------------------------------------

class TestWeatherNudge:

    def _make_good_forecast(self):
        from scripts.weather import DailyWeather
        from datetime import date as _d
        return {
            "daily": {
                "time": ["2026-03-13"],
                "temperature_2m_min": [14.0],
                "temperature_2m_max": [22.0],
                "apparent_temperature_min": [12.0],
                "apparent_temperature_max": [20.0],
                "precipitation_sum": [0.0],
                "precipitation_hours": [0.0],
                "wind_speed_10m_max": [10.0],
                "wind_gusts_10m_max": [15.0],
                "weather_code": [1],
                "uv_index_max": [5.0],
                "sunrise": ["06:30"],
                "sunset": ["18:00"],
            },
        }

    def test_nudge_returns_string_on_good_weather(self, user_slug):
        config = {
            "profile": {"timezone": "America/New_York"},
            "location": {"lat": -33.87, "lon": 151.21, "label": "Sydney"},
        }
        good_aqi = {
            "hourly": {
                "time": [f"2026-03-13T{h:02d}:00" for h in range(24)],
                "pm2_5": [8.0] * 24,
                "pm10": [15.0] * 24,
                "us_aqi": [30] * 24,
            }
        }
        with patch("scripts.athlete_store.load", return_value=config), \
             patch("scripts.weather.fetch_forecast", return_value=self._make_good_forecast()), \
             patch("scripts.weather.fetch_air_quality", return_value=good_aqi), \
             patch("scripts.tz.user_today", return_value=date(2026, 3, 13)), \
             patch("scripts.route_discovery.infer_training_context",
                   return_value={"run_type": "normal", "tsb": 0}):
            nudge = get_weather_nudge(user_slug)
            assert nudge is not None
            assert "running weather" in nudge.lower() or "weather" in nudge.lower()

    def test_nudge_returns_none_without_location(self, user_slug):
        with patch("scripts.athlete_store.load", return_value={"profile": {}}):
            assert get_weather_nudge(user_slug) is None

    def test_nudge_swallows_errors(self, user_slug):
        with patch("scripts.athlete_store.load", side_effect=Exception("boom")):
            assert get_weather_nudge(user_slug) is None


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
            "forecast": [
                {"date": "2026-03-14", "suitable_for_running": True},
            ],
            "current_conditions": {},
        }
        with patch("scripts.athlete_store.load", return_value=config), \
             patch("scripts.weather.check_weather", return_value=bad_weather_result), \
             patch("scripts.route_discovery.infer_training_context",
                   return_value={"run_type": "normal", "tsb": 0}):
            result = recommend_outdoor_run(user_slug)
            assert result["recommendation"] == "indoor"
            assert "training_context" in result
            assert any("2026-03-14" in s for s in result["suggestions"])

    def test_good_weather_returns_routes_with_context(self, user_slug):
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
             patch("scripts.route_discovery.fetch_routes", return_value=osm_elements), \
             patch("scripts.route_discovery.infer_training_context",
                   return_value={"run_type": "normal", "tsb": 5, "suggestion": "Normal day."}), \
             patch("scripts.health_tools.resolve_user_id", return_value=1), \
             patch("scripts.route_discovery._get_recently_shown_ids", return_value=set()):
            result = recommend_outdoor_run(user_slug)
            assert result["recommendation"] == "outdoor"
            assert "training_context" in result
            assert len(result["routes"]) > 0
            assert "why" in result["routes"][0]


# ---------------------------------------------------------------------------
# Phase 3: Rate route
# ---------------------------------------------------------------------------

class TestRateRoute:

    def test_invalid_rating_raises(self, user_slug):
        with pytest.raises(ValueError, match="between 1 and 5"):
            rate_route(user_slug, 1001, 0)

    def test_rating_too_high_raises(self, user_slug):
        with pytest.raises(ValueError, match="between 1 and 5"):
            rate_route(user_slug, 1001, 6)

"""Outdoor running route discovery via OpenStreetMap Overpass API.

Finds nearby running-suitable paths/trails and scores them against
the athlete's preferences (distance, surface, elevation, loop vs
out-and-back, traffic exposure).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

DEFAULT_RUNNING_PREFS: dict[str, Any] = {
    "preferred_distance_km": [5, 10],
    "surface": ["sealed_road", "trail", "mixed"],
    "max_elevation_gain_m": 300,
    "prefer_flat": False,
    "prefer_loop": True,
    "avoid_high_traffic": True,
    "scenic_preference": "medium",
}

# OSM highway tags suitable for running, grouped by surface category
_HIGHWAY_SURFACE_MAP: dict[str, str] = {
    "footway": "trail",
    "path": "trail",
    "track": "unsealed_road",
    "cycleway": "sealed_road",
    "pedestrian": "sealed_road",
    "living_street": "sealed_road",
    "residential": "sealed_road",
    "service": "sealed_road",
    "tertiary": "sealed_road",
    "secondary": "sealed_road",
    "unclassified": "mixed",
    "bridleway": "trail",
    "steps": "trail",
}

# OSM surface tags mapped to our categories
_SURFACE_MAP: dict[str, str] = {
    "paved": "sealed_road",
    "asphalt": "sealed_road",
    "concrete": "sealed_road",
    "concrete:plates": "sealed_road",
    "paving_stones": "sealed_road",
    "sett": "sealed_road",
    "compacted": "unsealed_road",
    "fine_gravel": "unsealed_road",
    "gravel": "unsealed_road",
    "ground": "trail",
    "dirt": "trail",
    "earth": "trail",
    "grass": "trail",
    "sand": "trail",
    "mud": "trail",
    "wood": "trail",
    "unpaved": "unsealed_road",
}


@dataclass
class Route:
    """A discovered running route."""
    osm_id: int
    name: str
    distance_m: float
    surface_type: str
    highway_type: str
    is_loop: bool
    lat: float
    lon: float
    tags: dict = field(default_factory=dict)
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "osm_id": self.osm_id,
            "name": self.name,
            "distance_m": round(self.distance_m),
            "distance_km": round(self.distance_m / 1000, 1),
            "surface_type": self.surface_type,
            "highway_type": self.highway_type,
            "is_loop": self.is_loop,
            "lat": self.lat,
            "lon": self.lon,
            "score": round(self.score),
            "osm_url": f"https://www.openstreetmap.org/way/{self.osm_id}",
        }


# ---------------------------------------------------------------------------
# Overpass queries
# ---------------------------------------------------------------------------

def _http():
    try:
        from scripts.http_clients import open_meteo_client
        return open_meteo_client()
    except Exception:
        return httpx


def _build_overpass_query(lat: float, lon: float, radius_m: int = 10000) -> str:
    """Build an Overpass QL query for running-suitable ways near a point."""
    highway_types = "|".join(_HIGHWAY_SURFACE_MAP.keys())
    return f"""
[out:json][timeout:30];
(
  way["highway"~"^({highway_types})$"]
    ["access"!="private"]
    ["foot"!="no"]
    (around:{radius_m},{lat},{lon});
);
out body geom;
"""


def fetch_routes(lat: float, lon: float, radius_m: int = 10000) -> list[dict]:
    """Fetch running-suitable ways from OSM Overpass API."""
    query = _build_overpass_query(lat, lon, radius_m)

    resp = _http().post(
        OVERPASS_URL,
        data={"data": query},
        timeout=45,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("elements", [])


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres between two lat/lon points."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _way_length(geometry: list[dict]) -> float:
    """Calculate total length of a way from its geometry nodes."""
    total = 0.0
    for i in range(len(geometry) - 1):
        total += _haversine(
            geometry[i]["lat"], geometry[i]["lon"],
            geometry[i + 1]["lat"], geometry[i + 1]["lon"],
        )
    return total


def _is_loop(geometry: list[dict], threshold_m: float = 100) -> bool:
    """Check if a way forms a loop (start ~= end)."""
    if len(geometry) < 3:
        return False
    return _haversine(
        geometry[0]["lat"], geometry[0]["lon"],
        geometry[-1]["lat"], geometry[-1]["lon"],
    ) < threshold_m


def _centroid(geometry: list[dict]) -> tuple[float, float]:
    """Average lat/lon of way nodes."""
    if not geometry:
        return 0.0, 0.0
    avg_lat = sum(n["lat"] for n in geometry) / len(geometry)
    avg_lon = sum(n["lon"] for n in geometry) / len(geometry)
    return avg_lat, avg_lon


# ---------------------------------------------------------------------------
# Route parsing + scoring
# ---------------------------------------------------------------------------

def _classify_surface(tags: dict) -> str:
    """Determine surface type from OSM tags."""
    surface_tag = tags.get("surface", "")
    if surface_tag in _SURFACE_MAP:
        return _SURFACE_MAP[surface_tag]
    highway = tags.get("highway", "")
    return _HIGHWAY_SURFACE_MAP.get(highway, "mixed")


def parse_routes(elements: list[dict]) -> list[Route]:
    """Convert raw Overpass elements into Route objects."""
    routes = []
    for el in elements:
        if el.get("type") != "way":
            continue
        geom = el.get("geometry", [])
        if len(geom) < 2:
            continue

        tags = el.get("tags", {})
        name = tags.get("name", "")
        if not name:
            highway = tags.get("highway", "path")
            name = f"Unnamed {highway}"

        dist = _way_length(geom)
        if dist < 200:
            continue

        clat, clon = _centroid(geom)
        routes.append(Route(
            osm_id=el["id"],
            name=name,
            distance_m=dist,
            surface_type=_classify_surface(tags),
            highway_type=tags.get("highway", ""),
            is_loop=_is_loop(geom),
            lat=clat,
            lon=clon,
            tags=tags,
        ))
    return routes


def score_routes(
    routes: list[Route],
    prefs: dict | None = None,
    user_lat: float = 0,
    user_lon: float = 0,
) -> list[Route]:
    """Score and rank routes against user preferences.

    Modifies each route's ``score`` field in place and returns the list
    sorted by score descending.
    """
    p = {**DEFAULT_RUNNING_PREFS, **(prefs or {})}
    pref_dists = p["preferred_distance_km"]
    pref_surfaces = p["surface"]

    for route in routes:
        score = 50.0  # base score

        # Distance match (high weight)
        dist_km = route.distance_m / 1000
        min_dist_diff = min(abs(dist_km - d) for d in pref_dists)
        if min_dist_diff < 1:
            score += 25
        elif min_dist_diff < 3:
            score += 15
        elif min_dist_diff < 5:
            score += 5

        # Surface match (high weight)
        if route.surface_type in pref_surfaces:
            score += 20
        elif route.surface_type == "mixed":
            score += 10

        # Loop preference
        if p["prefer_loop"] and route.is_loop:
            score += 10
        elif not p["prefer_loop"] and not route.is_loop:
            score += 5

        # Traffic avoidance
        if p["avoid_high_traffic"]:
            if route.highway_type in ("footway", "path", "track", "pedestrian"):
                score += 10
            elif route.highway_type in ("secondary", "tertiary"):
                score -= 10

        # Named routes are preferred over unnamed
        if not route.name.startswith("Unnamed"):
            score += 5

        # Proximity bonus (closer to user location)
        if user_lat and user_lon:
            dist_from_user = _haversine(user_lat, user_lon, route.lat, route.lon)
            if dist_from_user < 2000:
                score += 15
            elif dist_from_user < 5000:
                score += 10
            elif dist_from_user < 10000:
                score += 5

        route.score = max(0, min(100, score))

    routes.sort(key=lambda r: r.score, reverse=True)
    return routes


# ---------------------------------------------------------------------------
# DB cache
# ---------------------------------------------------------------------------

def get_cached_routes(
    user_id: int,
    lat: float,
    lon: float,
    max_age_hours: int = 168,
) -> list[dict] | None:
    """Return cached routes if still fresh (default: 7 days)."""
    from scripts.health_tools import get_conn
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT name, distance_m, elevation_gain_m, surface_types,
                      is_loop, popularity_score, metadata, lat, lon,
                      id
               FROM route_cache
               WHERE user_id = %s
               AND ABS(lat - %s) < 0.05 AND ABS(lon - %s) < 0.05
               AND fetched_at > NOW() - INTERVAL '%s hours'
               ORDER BY popularity_score DESC NULLS LAST
               LIMIT 50""",
            (user_id, lat, lon, max_age_hours),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        return [
            {
                "name": r[0],
                "distance_m": r[1],
                "elevation_gain_m": r[2],
                "surface_types": r[3],
                "is_loop": r[4],
                "popularity_score": r[5],
                "metadata": r[6],
                "lat": r[7],
                "lon": r[8],
                "cache_id": r[9],
            }
            for r in rows
        ]
    finally:
        conn.close()


def cache_routes(user_id: int, routes: list[Route]) -> None:
    """Cache discovered routes in the database."""
    from scripts.health_tools import get_conn
    conn = get_conn()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        from psycopg2.extras import Json
        for route in routes:
            cur.execute(
                """INSERT INTO route_cache
                       (user_id, source, name, lat, lon, distance_m,
                        surface_types, is_loop, metadata, fetched_at,
                        expires_at)
                   VALUES (%s, 'osm', %s, %s, %s, %s, %s, %s, %s, NOW(),
                           NOW() + INTERVAL '7 days')
                   ON CONFLICT DO NOTHING""",
                (
                    user_id,
                    route.name,
                    route.lat,
                    route.lon,
                    route.distance_m,
                    [route.surface_type],
                    route.is_loop,
                    Json({"osm_id": route.osm_id, "highway": route.highway_type}),
                ),
            )
        cur.close()
    except Exception:
        log.warning("Failed to cache routes", exc_info=True)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

def recommend_outdoor_run(slug: str, target_date: str = "") -> dict:
    """Recommend outdoor running routes based on weather and preferences.

    1. Check weather for the target date
    2. If suitable, fetch and score nearby routes
    3. Return top recommendations with weather context
    """
    from scripts import athlete_store, weather
    from scripts.tz import load_user_tz, user_today

    config = athlete_store.load(slug) or {}
    location = config.get("location")
    if not location or "lat" not in location or "lon" not in location:
        raise ValueError(
            "No location configured. Set location with update_athlete_profile "
            "(field_path='location', value={'lat': ..., 'lon': ..., 'label': '...'})."
        )

    lat = location["lat"]
    lon = location["lon"]
    label = location.get("label", f"{lat}, {lon}")

    tz = load_user_tz(slug)
    today = user_today(tz)
    td = date_module.fromisoformat(target_date) if target_date else today

    # Step 1: Check weather
    weather_result = weather.check_weather(slug, td.isoformat())
    suitability = weather_result["suitability"]

    if not suitability["suitable"]:
        return {
            "location": label,
            "target_date": td.isoformat(),
            "recommendation": "indoor",
            "reason": "Weather not suitable for outdoor running",
            "weather": weather_result,
            "suggestions": [
                "Consider a treadmill workout instead",
                "Check the forecast for better days",
            ],
            "routes": [],
        }

    # Step 2: Fetch and score routes
    running_prefs = config.get("running_preferences", {})

    elements = fetch_routes(lat, lon, radius_m=15000)
    routes = parse_routes(elements)
    scored = score_routes(routes, running_prefs, user_lat=lat, user_lon=lon)
    top_routes = scored[:5]

    # Step 3: Build recommendations
    recommendations = []
    for route in top_routes:
        rd = route.to_dict()
        rd["why"] = _explain_recommendation(route, running_prefs)
        recommendations.append(rd)

    return {
        "location": label,
        "target_date": td.isoformat(),
        "recommendation": "outdoor",
        "weather": weather_result,
        "routes": recommendations,
    }


def _explain_recommendation(route: Route, prefs: dict) -> str:
    """Generate a human-readable explanation for why a route was recommended."""
    parts = []
    dist_km = route.distance_m / 1000
    pref_dists = prefs.get("preferred_distance_km", [5, 10])
    min_diff = min(abs(dist_km - d) for d in pref_dists)

    if min_diff < 1:
        parts.append(f"Matches your {dist_km:.1f} km distance preference")
    else:
        parts.append(f"{dist_km:.1f} km route")

    pref_surfaces = prefs.get("surface", [])
    if route.surface_type in pref_surfaces:
        parts.append(f"preferred surface ({route.surface_type})")

    if route.is_loop:
        parts.append("loop route")

    if route.highway_type in ("footway", "path"):
        parts.append("low traffic")

    return ". ".join(parts) + "." if parts else "Nearby route."


# We need a reference to `date` that doesn't shadow the dataclass field
from datetime import date as date_module

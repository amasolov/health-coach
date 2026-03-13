"""Outdoor running route discovery via OpenStreetMap Overpass API.

Finds nearby running-suitable paths/trails and scores them against
the athlete's preferences (distance, surface, elevation, loop vs
out-and-back, traffic exposure).

Phase 3 additions: popularity scoring from OSM metadata (route
relations, designated foot access, lit paths, scenic proximity),
variety/novelty bonus, and user route ratings.

Phase 4 additions: training-aware route suggestions (easy day → flat,
long run → scenic loop) based on current TSB and recent activity.
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
    popularity: float = 0.0
    relation_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
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
            "popularity": round(self.popularity),
            "osm_url": f"https://www.openstreetmap.org/way/{self.osm_id}",
        }
        if self.relation_names:
            d["part_of_routes"] = self.relation_names
        return d


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
    """Build an Overpass QL query for running-suitable ways near a point.

    Also fetches route relations (running/hiking/walking/fitness trails)
    that contain these ways, used as a popularity proxy.
    """
    highway_types = "|".join(_HIGHWAY_SURFACE_MAP.keys())
    return f"""
[out:json][timeout:45];
(
  way["highway"~"^({highway_types})$"]
    ["access"!="private"]
    ["foot"!="no"]
    (around:{radius_m},{lat},{lon});
);
out body geom;
(._;)->.ways;
rel(bw.ways)["type"="route"]["route"~"^(foot|hiking|running|walking|fitness_trail)$"];
out body;
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


def _extract_relations(elements: list[dict]) -> dict[int, list[str]]:
    """Extract route relation membership: way_id -> [relation names].

    Relations are returned by the Overpass query as separate elements
    with type=relation. Each has "members" listing the ways it contains.
    """
    way_to_relations: dict[int, list[str]] = {}

    for el in elements:
        if el.get("type") != "relation":
            continue
        rel_name = el.get("tags", {}).get("name", "")
        if not rel_name:
            rel_name = el.get("tags", {}).get("ref", f"Route {el['id']}")
        for member in el.get("members", []):
            if member.get("type") == "way":
                way_id = member["ref"]
                way_to_relations.setdefault(way_id, []).append(rel_name)

    return way_to_relations


def _compute_popularity(tags: dict, relation_count: int) -> float:
    """Compute a 0–100 popularity proxy from OSM metadata.

    Signals (additive):
    - Part of named route relations (strongest signal)
    - foot=designated or foot=yes
    - lit=yes (maintained, usable at night)
    - Part of a park or green space
    - Has a Wikipedia/Wikidata reference (notable)
    """
    pop = 0.0

    if relation_count >= 3:
        pop += 40
    elif relation_count >= 1:
        pop += 25

    foot = tags.get("foot", "")
    if foot == "designated":
        pop += 15
    elif foot == "yes":
        pop += 10

    if tags.get("lit") == "yes":
        pop += 10

    if tags.get("leisure") in ("park", "nature_reserve", "garden"):
        pop += 10
    if tags.get("natural") in ("water", "wood", "heath"):
        pop += 5

    if tags.get("wikipedia") or tags.get("wikidata"):
        pop += 10

    if tags.get("wheelchair") in ("yes", "limited"):
        pop += 5

    return min(100, pop)


def parse_routes(elements: list[dict]) -> list[Route]:
    """Convert raw Overpass elements into Route objects."""
    way_relations = _extract_relations(elements)

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

        osm_id = el["id"]
        rel_names = way_relations.get(osm_id, [])
        popularity = _compute_popularity(tags, len(rel_names))

        clat, clon = _centroid(geom)
        routes.append(Route(
            osm_id=osm_id,
            name=name,
            distance_m=dist,
            surface_type=_classify_surface(tags),
            highway_type=tags.get("highway", ""),
            is_loop=_is_loop(geom),
            lat=clat,
            lon=clon,
            tags=tags,
            popularity=popularity,
            relation_names=rel_names,
        ))
    return routes


def score_routes(
    routes: list[Route],
    prefs: dict | None = None,
    user_lat: float = 0,
    user_lon: float = 0,
    recently_shown_ids: set[int] | None = None,
    training_context: dict | None = None,
) -> list[Route]:
    """Score and rank routes against user preferences.

    Modifies each route's ``score`` field in place and returns the list
    sorted by score descending.

    ``recently_shown_ids``: OSM IDs of routes shown in the last 7 days;
    routes *not* in this set get a novelty bonus.

    ``training_context``: dict with keys ``run_type`` (easy/long/normal),
    ``tsb``, ``form_status`` to bias route selection for the day's training.
    """
    p = {**DEFAULT_RUNNING_PREFS, **(prefs or {})}
    pref_dists = p["preferred_distance_km"]
    pref_surfaces = p["surface"]
    recently_shown = recently_shown_ids or set()
    tc = training_context or {}

    for route in routes:
        score = 40.0  # base score (lowered from 50 to leave room for popularity)

        # Distance match (high weight)
        dist_km = route.distance_m / 1000
        effective_dists = _training_adjusted_distances(pref_dists, tc)
        min_dist_diff = min(abs(dist_km - d) for d in effective_dists)
        if min_dist_diff < 1:
            score += 25
        elif min_dist_diff < 3:
            score += 15
        elif min_dist_diff < 5:
            score += 5

        # Surface match (high weight)
        if route.surface_type in pref_surfaces:
            score += 15
        elif route.surface_type == "mixed":
            score += 8

        # Loop preference
        if p["prefer_loop"] and route.is_loop:
            score += 8
        elif not p["prefer_loop"] and not route.is_loop:
            score += 4

        # Traffic avoidance
        if p["avoid_high_traffic"]:
            if route.highway_type in ("footway", "path", "track", "pedestrian"):
                score += 8
            elif route.highway_type in ("secondary", "tertiary"):
                score -= 8

        # Named routes are preferred over unnamed
        if not route.name.startswith("Unnamed"):
            score += 4

        # Proximity bonus (closer to user location)
        if user_lat and user_lon:
            dist_from_user = _haversine(user_lat, user_lon, route.lat, route.lon)
            if dist_from_user < 2000:
                score += 12
            elif dist_from_user < 5000:
                score += 8
            elif dist_from_user < 10000:
                score += 4

        # Phase 3: Popularity from OSM metadata
        score += route.popularity * 0.15  # up to +15 points

        # Phase 3: Novelty bonus — routes not recently shown
        if route.osm_id not in recently_shown:
            score += 5

        # Phase 4: Training-context adjustments
        score += _training_context_bonus(route, tc)

        route.score = max(0, min(100, score))

    routes.sort(key=lambda r: r.score, reverse=True)
    return routes


# ---------------------------------------------------------------------------
# Phase 4: Training-aware helpers
# ---------------------------------------------------------------------------

def _training_adjusted_distances(
    pref_dists: list[float | int],
    tc: dict,
) -> list[float]:
    """Adjust preferred distances based on training context.

    Easy/recovery day → shorter distances.
    Long run day → longer distances.
    """
    run_type = tc.get("run_type", "normal")
    if run_type == "easy":
        return [d * 0.6 for d in pref_dists]
    elif run_type == "long":
        return [d * 1.5 for d in pref_dists]
    return list(pref_dists)


def _training_context_bonus(route: Route, tc: dict) -> float:
    """Additional scoring based on today's training context."""
    if not tc:
        return 0.0

    bonus = 0.0
    run_type = tc.get("run_type", "normal")

    if run_type == "easy":
        # Prefer flat, shorter, sealed routes for easy days
        if route.highway_type in ("footway", "pedestrian", "cycleway"):
            bonus += 5
        if route.surface_type == "sealed_road":
            bonus += 3
    elif run_type == "long":
        # Prefer scenic, loop routes for long runs
        if route.is_loop:
            bonus += 5
        scenic_tags = {"leisure", "natural"}
        if scenic_tags & set(route.tags.keys()):
            bonus += 5
        if route.relation_names:
            bonus += 3

    return bonus


def infer_training_context(slug: str) -> dict:
    """Infer today's appropriate run type from the athlete's training state.

    Returns a dict with ``run_type`` (easy/long/normal), ``tsb``, and
    ``form_status`` based on current TSB and recent activity patterns.
    """
    from scripts import health_tools, athlete_store
    from scripts.tz import load_user_tz, user_today

    config = athlete_store.load(slug) or {}
    tz = load_user_tz(slug)
    today = user_today(tz)

    uid = health_tools.resolve_user_id(slug)
    if not uid:
        return {"run_type": "normal", "tsb": 0, "form_status": "unknown"}

    try:
        summary = health_tools.get_fitness_summary(uid)
    except Exception:
        return {"run_type": "normal", "tsb": 0, "form_status": "unknown"}

    if "status" in summary:
        return {"run_type": "normal", "tsb": 0, "form_status": "unknown"}

    tsb = float(summary.get("tsb_form", 0))
    form = summary.get("form_status", "")

    run_type = "normal"
    if tsb < -15:
        run_type = "easy"
    elif tsb > 10:
        run_type = "long"
    else:
        # Check day of week and recent volume for long run scheduling
        weekday = today.weekday()  # 0=Mon ... 6=Sun
        goals = config.get("goals", {})
        sports = goals.get("preferred_sports", [])
        if "running" in sports and weekday in (5, 6):
            run_type = "long"

    return {
        "run_type": run_type,
        "tsb": tsb,
        "form_status": form,
        "suggestion": _training_suggestion(run_type, tsb),
    }


def _training_suggestion(run_type: str, tsb: float) -> str:
    """Human-readable suggestion for route selection context."""
    if run_type == "easy":
        return (
            f"You're carrying fatigue (TSB {tsb:.0f}). "
            "Suggesting shorter, flatter routes for an easy recovery run."
        )
    elif run_type == "long":
        return (
            f"Good form (TSB {tsb:.0f}) — great day for a longer run. "
            "Suggesting scenic loop routes."
        )
    return "Normal training day. Routes matched to your standard preferences."


# ---------------------------------------------------------------------------
# Phase 3: Route ratings (user feedback)
# ---------------------------------------------------------------------------

def rate_route(slug: str, osm_id: int, rating: int, notes: str = "") -> dict:
    """Record a user's rating for a route (1–5 stars).

    Updates the route_cache popularity_score and stores the rating in
    the metadata for future preference learning.
    """
    if not 1 <= rating <= 5:
        raise ValueError("Rating must be between 1 and 5.")

    from scripts import athlete_store
    from scripts.health_tools import get_conn, resolve_user_id

    uid = resolve_user_id(slug)
    if not uid:
        raise ValueError(f"User '{slug}' not found.")

    conn = get_conn()
    try:
        conn.autocommit = True
        cur = conn.cursor()

        # Update existing cached route if present
        cur.execute(
            """UPDATE route_cache
               SET popularity_score = %s,
                   metadata = metadata || %s::jsonb
               WHERE user_id = %s
               AND metadata->>'osm_id' = %s
               RETURNING id""",
            (
                rating * 20.0,  # 1-5 → 20-100
                f'{{"user_rating": {rating}, "user_notes": "{notes}"}}',
                uid,
                str(osm_id),
            ),
        )
        updated = cur.fetchone()

        # Also store in athlete config for preference learning
        config = athlete_store.load(slug) or {}
        route_ratings = config.get("route_ratings", {})
        route_ratings[str(osm_id)] = {
            "rating": rating,
            "notes": notes,
            "rated_at": datetime.now(timezone.utc).isoformat(),
        }
        athlete_store.update_field(slug, "route_ratings", route_ratings)

        cur.close()
        return {
            "status": "saved",
            "osm_id": osm_id,
            "rating": rating,
            "notes": notes,
            "cache_updated": updated is not None,
        }
    finally:
        conn.close()


def _get_recently_shown_ids(user_id: int) -> set[int]:
    """Get OSM IDs of routes shown in the last 7 days from the cache."""
    from scripts.health_tools import get_conn
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT metadata->>'osm_id'
               FROM route_cache
               WHERE user_id = %s
               AND fetched_at > NOW() - INTERVAL '7 days'""",
            (user_id,),
        )
        return {int(r[0]) for r in cur.fetchall() if r[0] and r[0].isdigit()}
    except Exception:
        return set()
    finally:
        conn.close()


def _get_user_ratings(slug: str) -> dict[str, dict]:
    """Load user's route ratings from athlete config."""
    from scripts import athlete_store
    config = athlete_store.load(slug) or {}
    return config.get("route_ratings", {})


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
                        surface_types, is_loop, popularity_score,
                        metadata, fetched_at, expires_at)
                   VALUES (%s, 'osm', %s, %s, %s, %s, %s, %s, %s, %s, NOW(),
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
                    route.popularity,
                    Json({"osm_id": route.osm_id, "highway": route.highway_type,
                          "relation_names": route.relation_names}),
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
    2. Infer training context (easy/long/normal)
    3. If suitable, fetch and score nearby routes with popularity + novelty
    4. Return top recommendations with weather + training context
    """
    from scripts import athlete_store, weather
    from scripts.tz import load_user_tz, user_today
    from scripts.health_tools import resolve_user_id

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

    # Step 2: Infer training context
    tc = infer_training_context(slug)

    if not suitability["suitable"]:
        # Find next suitable day from the forecast
        next_good_day = None
        for f in weather_result.get("forecast", []):
            if f.get("suitable_for_running") and f["date"] != td.isoformat():
                next_good_day = f["date"]
                break

        suggestions = ["Consider a treadmill workout instead"]
        if next_good_day:
            suggestions.append(f"Better weather expected on {next_good_day}")

        return {
            "location": label,
            "target_date": td.isoformat(),
            "recommendation": "indoor",
            "reason": "Weather not suitable for outdoor running",
            "weather": weather_result,
            "training_context": tc,
            "suggestions": suggestions,
            "routes": [],
        }

    # Step 3: Fetch and score routes with popularity + novelty + training
    running_prefs = config.get("running_preferences", {})

    uid = resolve_user_id(slug)
    recently_shown = _get_recently_shown_ids(uid) if uid else set()

    elements = fetch_routes(lat, lon, radius_m=15000)
    routes = parse_routes(elements)
    scored = score_routes(
        routes, running_prefs,
        user_lat=lat, user_lon=lon,
        recently_shown_ids=recently_shown,
        training_context=tc,
    )
    top_routes = scored[:5]

    # Step 4: Build recommendations
    recommendations = []
    for route in top_routes:
        rd = route.to_dict()
        rd["why"] = _explain_recommendation(route, running_prefs, tc)
        recommendations.append(rd)

    return {
        "location": label,
        "target_date": td.isoformat(),
        "recommendation": "outdoor",
        "weather": weather_result,
        "training_context": tc,
        "routes": recommendations,
    }


def _explain_recommendation(route: Route, prefs: dict, tc: dict | None = None) -> str:
    """Generate a human-readable explanation for why a route was recommended."""
    parts = []
    dist_km = route.distance_m / 1000
    pref_dists = prefs.get("preferred_distance_km", [5, 10])
    if tc:
        pref_dists = _training_adjusted_distances(pref_dists, tc)
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

    if route.relation_names:
        parts.append(f"part of {route.relation_names[0]}")

    if route.popularity >= 50:
        parts.append("popular route")

    run_type = (tc or {}).get("run_type", "normal")
    if run_type == "easy":
        parts.append("good for recovery")
    elif run_type == "long":
        parts.append("great for a long run")

    return ". ".join(parts) + "." if parts else "Nearby route."


# ---------------------------------------------------------------------------
# Weather nudge for system prompt (Phase 4)
# ---------------------------------------------------------------------------

def get_weather_nudge(slug: str) -> str | None:
    """Check weather and return a system prompt nudge if conditions are good.

    Called during system prompt building. Returns None if weather is bad,
    location is not configured, or the check fails. Errors are swallowed
    so this never blocks conversation start.
    """
    try:
        from scripts import athlete_store, weather
        from scripts.tz import load_user_tz, user_today

        config = athlete_store.load(slug) or {}
        location = config.get("location")
        if not location or "lat" not in location or "lon" not in location:
            return None

        tz = load_user_tz(slug)
        today = user_today(tz)
        tz_name = str(tz)

        result = weather.fetch_forecast(
            location["lat"], location["lon"],
            days=2, tz_name=tz_name,
        )
        daily = weather.parse_daily(result)

        for day in daily:
            if day.date == today:
                s = weather.score_daily(day, config.get("weather", {}))
                if s.suitable and s.score >= 65:
                    tc = infer_training_context(slug)
                    label = location.get("label", "your area")
                    run_type = tc.get("run_type", "normal")

                    nudge = (
                        f"\n🏃 Weather alert: Great running weather today in {label}! "
                        f"{day.weather_label}, {day.temp_min_c:.0f}–{day.temp_max_c:.0f}°C. "
                        f"Run suitability: {s.score}/100."
                    )
                    if run_type == "easy":
                        nudge += (
                            f" TSB is {tc['tsb']:.0f} — suggest an easy recovery run "
                            "if the user asks about training today."
                        )
                    elif run_type == "long":
                        nudge += (
                            f" TSB is {tc['tsb']:.0f} — good form for a long run "
                            "if the user asks about training today."
                        )
                    else:
                        nudge += (
                            " Suggest an outdoor run if the user asks about "
                            "training today. Use recommend_outdoor_run for routes."
                        )
                    return nudge
                break
    except Exception:
        log.debug("Weather nudge check failed", exc_info=True)
    return None


# We need a reference to `date` that doesn't shadow the dataclass field
from datetime import date as date_module

"""Weather integration via Open-Meteo (free, no API key).

Fetches current + forecast conditions for an athlete's location and
evaluates running suitability based on configurable thresholds.

All timestamps respect the athlete's timezone (see scripts.tz).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default weather thresholds for "good running weather"
# Tuned for Australian conditions (high UV, humid summers).
# ---------------------------------------------------------------------------

DEFAULT_WEATHER_PREFS: dict[str, Any] = {
    "temp_min_c": 5,
    "temp_max_c": 32,
    "wind_max_kmh": 30,
    "precip_max_mm": 2.0,
    "uv_caution_threshold": 6,
    "humidity_divergence_c": 4,
    "aqi_warn_threshold": 50,
    "aqi_unsuitable_threshold": 100,
}

# WMO weather codes that indicate severe conditions
_SEVERE_CODES = {
    65, 66, 67,   # heavy/freezing rain
    75, 77,       # heavy snow, snow grains
    82,           # violent rain showers
    85, 86,       # snow showers
    95, 96, 99,   # thunderstorm (with/without hail)
}

_CODE_LABELS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class HourlyWeather:
    time: datetime
    temperature_c: float
    apparent_temperature_c: float
    humidity_pct: float
    precipitation_mm: float
    wind_speed_kmh: float
    wind_gusts_kmh: float
    weather_code: int
    uv_index: float
    cloud_cover_pct: float

    @property
    def weather_label(self) -> str:
        return _CODE_LABELS.get(self.weather_code, f"Code {self.weather_code}")

    @property
    def is_severe(self) -> bool:
        return self.weather_code in _SEVERE_CODES


@dataclass
class DailyWeather:
    date: date
    temp_min_c: float
    temp_max_c: float
    apparent_temp_min_c: float
    apparent_temp_max_c: float
    precipitation_sum_mm: float
    precipitation_hours: float
    wind_max_kmh: float
    wind_gusts_max_kmh: float
    weather_code: int
    uv_index_max: float
    sunrise: str
    sunset: str

    @property
    def weather_label(self) -> str:
        return _CODE_LABELS.get(self.weather_code, f"Code {self.weather_code}")

    @property
    def is_severe(self) -> bool:
        return self.weather_code in _SEVERE_CODES


@dataclass
class AirQuality:
    """Summarised air quality for a date (from Open-Meteo AQI API)."""
    pm25_avg: float
    pm25_max: float
    pm10_avg: float
    us_aqi_max: int

    def to_dict(self) -> dict:
        return {
            "pm25_avg": round(self.pm25_avg, 1),
            "pm25_max": round(self.pm25_max, 1),
            "pm10_avg": round(self.pm10_avg, 1),
            "us_aqi_max": self.us_aqi_max,
            "category": self.category,
        }

    @property
    def category(self) -> str:
        if self.us_aqi_max <= 50:
            return "Good"
        elif self.us_aqi_max <= 100:
            return "Moderate"
        elif self.us_aqi_max <= 150:
            return "Unhealthy for sensitive groups"
        elif self.us_aqi_max <= 200:
            return "Unhealthy"
        elif self.us_aqi_max <= 300:
            return "Very unhealthy"
        return "Hazardous"


@dataclass
class RunSuitability:
    """Assessment of whether conditions are suitable for outdoor running."""
    suitable: bool
    score: int  # 0–100
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    best_windows: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "suitable": self.suitable,
            "score": self.score,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "best_windows": self.best_windows,
        }


# ---------------------------------------------------------------------------
# Open-Meteo API
# ---------------------------------------------------------------------------

OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"


def _http():
    """Get HTTP client (allows test patching)."""
    try:
        from scripts.http_clients import open_meteo_client
        return open_meteo_client()
    except Exception:
        return httpx


def fetch_forecast(
    lat: float,
    lon: float,
    days: int = 3,
    tz_name: str = "auto",
) -> dict[str, Any]:
    """Fetch weather forecast from Open-Meteo.

    Returns raw API response dict with hourly + daily data.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "relative_humidity_2m",
            "precipitation",
            "weather_code",
            "wind_speed_10m",
            "wind_gusts_10m",
            "uv_index",
            "cloud_cover",
        ]),
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "apparent_temperature_max",
            "apparent_temperature_min",
            "precipitation_sum",
            "precipitation_hours",
            "wind_speed_10m_max",
            "wind_gusts_10m_max",
            "weather_code",
            "uv_index_max",
            "sunrise",
            "sunset",
        ]),
        "timezone": tz_name,
        "forecast_days": days,
    }

    resp = _http().get(OPEN_METEO_BASE, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_daily(raw: dict) -> list[DailyWeather]:
    """Parse daily forecast from raw Open-Meteo response."""
    d = raw.get("daily", {})
    dates = d.get("time", [])
    result = []
    for i, ds in enumerate(dates):
        result.append(DailyWeather(
            date=date.fromisoformat(ds),
            temp_min_c=d["temperature_2m_min"][i],
            temp_max_c=d["temperature_2m_max"][i],
            apparent_temp_min_c=d["apparent_temperature_min"][i],
            apparent_temp_max_c=d["apparent_temperature_max"][i],
            precipitation_sum_mm=d["precipitation_sum"][i],
            precipitation_hours=d["precipitation_hours"][i],
            wind_max_kmh=d["wind_speed_10m_max"][i],
            wind_gusts_max_kmh=d["wind_gusts_10m_max"][i],
            weather_code=d["weather_code"][i],
            uv_index_max=d["uv_index_max"][i],
            sunrise=d["sunrise"][i],
            sunset=d["sunset"][i],
        ))
    return result


def parse_hourly(raw: dict) -> list[HourlyWeather]:
    """Parse hourly forecast from raw Open-Meteo response."""
    h = raw.get("hourly", {})
    times = h.get("time", [])
    result = []
    for i, ts in enumerate(times):
        result.append(HourlyWeather(
            time=datetime.fromisoformat(ts),
            temperature_c=h["temperature_2m"][i],
            apparent_temperature_c=h["apparent_temperature"][i],
            humidity_pct=h["relative_humidity_2m"][i],
            precipitation_mm=h["precipitation"][i],
            wind_speed_kmh=h["wind_speed_10m"][i],
            wind_gusts_kmh=h["wind_gusts_10m"][i],
            weather_code=h["weather_code"][i],
            uv_index=h["uv_index"][i],
            cloud_cover_pct=h["cloud_cover"][i],
        ))
    return result


# ---------------------------------------------------------------------------
# Air Quality API (Open-Meteo)
# ---------------------------------------------------------------------------

AIR_QUALITY_BASE = "https://air-quality-api.open-meteo.com/v1/air-quality"


def fetch_air_quality(
    lat: float,
    lon: float,
    days: int = 3,
    tz_name: str = "auto",
) -> dict[str, Any]:
    """Fetch air quality forecast from Open-Meteo AQI API.

    Returns raw response with hourly PM2.5, PM10, and US AQI.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "pm2_5,pm10,us_aqi",
        "timezone": tz_name,
        "forecast_days": days,
    }
    resp = _http().get(AIR_QUALITY_BASE, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_air_quality(raw: dict, target_date: date) -> AirQuality | None:
    """Parse and aggregate hourly AQI data for a single date."""
    h = raw.get("hourly", {})
    times = h.get("time", [])
    pm25s = h.get("pm2_5", [])
    pm10s = h.get("pm10", [])
    aqis = h.get("us_aqi", [])

    day_pm25 = []
    day_pm10 = []
    day_aqi = []

    td_str = target_date.isoformat()
    for i, ts in enumerate(times):
        if ts.startswith(td_str):
            if i < len(pm25s) and pm25s[i] is not None:
                day_pm25.append(pm25s[i])
            if i < len(pm10s) and pm10s[i] is not None:
                day_pm10.append(pm10s[i])
            if i < len(aqis) and aqis[i] is not None:
                day_aqi.append(int(aqis[i]))

    if not day_pm25:
        return None

    return AirQuality(
        pm25_avg=sum(day_pm25) / len(day_pm25),
        pm25_max=max(day_pm25),
        pm10_avg=sum(day_pm10) / len(day_pm10) if day_pm10 else 0,
        us_aqi_max=max(day_aqi) if day_aqi else 0,
    )


# ---------------------------------------------------------------------------
# Suitability scoring
# ---------------------------------------------------------------------------

def score_daily(
    day: DailyWeather,
    prefs: dict | None = None,
    air_quality: AirQuality | None = None,
) -> RunSuitability:
    """Score a single day for running suitability (0–100).

    Factors: temperature, wind, precipitation, UV (graduated), humidity
    (apparent-temp divergence), and air quality (PM2.5 / US AQI).
    """
    p = {**DEFAULT_WEATHER_PREFS, **(prefs or {})}
    score = 100
    reasons: list[str] = []
    warnings: list[str] = []

    if day.is_severe:
        return RunSuitability(
            suitable=False, score=0,
            reasons=[f"Severe weather: {day.weather_label}"],
        )

    # Temperature
    mid_temp = (day.temp_min_c + day.temp_max_c) / 2
    if mid_temp < p["temp_min_c"]:
        penalty = min(40, (p["temp_min_c"] - mid_temp) * 5)
        score -= int(penalty)
        warnings.append(f"Cold: avg {mid_temp:.0f}°C (pref ≥{p['temp_min_c']}°C)")
    elif mid_temp > p["temp_max_c"]:
        penalty = min(40, (mid_temp - p["temp_max_c"]) * 5)
        score -= int(penalty)
        warnings.append(f"Hot: avg {mid_temp:.0f}°C (pref ≤{p['temp_max_c']}°C)")
    else:
        reasons.append(f"Temperature {mid_temp:.0f}°C — comfortable")

    # Wind
    if day.wind_max_kmh > p["wind_max_kmh"]:
        penalty = min(30, (day.wind_max_kmh - p["wind_max_kmh"]) * 2)
        score -= int(penalty)
        warnings.append(f"Windy: gusts up to {day.wind_max_kmh:.0f} km/h")
    elif day.wind_max_kmh < 15:
        reasons.append("Light wind")

    # Precipitation
    if day.precipitation_sum_mm > p["precip_max_mm"]:
        penalty = min(30, (day.precipitation_sum_mm - p["precip_max_mm"]) * 5)
        score -= int(penalty)
        warnings.append(f"Rain: {day.precipitation_sum_mm:.1f} mm expected")
    elif day.precipitation_sum_mm < 0.5:
        reasons.append("Dry conditions")

    # Graduated UV scoring
    uv = day.uv_index_max
    uv_threshold = p["uv_caution_threshold"]
    if uv >= 11:
        score -= 25
        warnings.append(
            f"Extreme UV ({uv:.0f}) — avoid midday sun, run early morning "
            "or evening. Sunscreen, hat, and sunglasses essential."
        )
    elif uv >= 8:
        score -= 15
        warnings.append(
            f"Very high UV ({uv:.0f}) — sun protection required, "
            "prefer early morning or evening."
        )
    elif uv >= uv_threshold:
        score -= 8
        warnings.append(
            f"High UV ({uv:.0f}) — wear sun protection (sunscreen, hat)."
        )
    elif uv >= 3:
        reasons.append(f"UV {uv:.0f} — sun protection recommended")

    # Humidity / apparent temperature divergence
    humidity_threshold = p.get("humidity_divergence_c", 4)
    apparent_max = day.apparent_temp_max_c
    actual_max = day.temp_max_c
    divergence = apparent_max - actual_max
    if divergence > humidity_threshold + 4:
        score -= 15
        warnings.append(
            f"Very humid — feels like {apparent_max:.0f}°C "
            f"(actual {actual_max:.0f}°C). Hydrate well."
        )
    elif divergence > humidity_threshold:
        score -= 8
        warnings.append(
            f"Humid — feels like {apparent_max:.0f}°C "
            f"(actual {actual_max:.0f}°C). Extra hydration advised."
        )

    # Air quality (PM2.5 / US AQI) — critical for Australian bushfire smoke
    if air_quality is not None:
        aqi = air_quality.us_aqi_max
        if aqi > 200:
            score -= 80
            warnings.append(
                f"Dangerous air quality (AQI {aqi}, {air_quality.category}) — "
                f"PM2.5 {air_quality.pm25_max:.0f} µg/m³. "
                "Do not run outdoors."
            )
        elif aqi > 150:
            score -= 55
            warnings.append(
                f"Unhealthy air quality (AQI {aqi}) — "
                f"PM2.5 {air_quality.pm25_max:.0f} µg/m³. "
                "Avoid prolonged outdoor exercise."
            )
        elif aqi > p.get("aqi_unsuitable_threshold", 100):
            score -= 30
            warnings.append(
                f"Poor air quality (AQI {aqi}) — "
                f"PM2.5 avg {air_quality.pm25_avg:.0f} µg/m³. "
                "Reduce outdoor exercise intensity."
            )
        elif aqi > p.get("aqi_warn_threshold", 50):
            score -= 10
            warnings.append(
                f"Moderate air quality (AQI {aqi}) — "
                f"PM2.5 avg {air_quality.pm25_avg:.0f} µg/m³. "
                "Sensitive individuals should take caution."
            )

    score = max(0, min(100, score))
    suitable = score >= 50

    if suitable and not reasons:
        reasons.append(day.weather_label)

    return RunSuitability(suitable=suitable, score=score, reasons=reasons, warnings=warnings)


def score_hourly_windows(
    hours: list[HourlyWeather],
    target_date: date,
    prefs: dict | None = None,
) -> list[dict]:
    """Find the best running windows on a given date.

    Returns windows sorted by score (best first), each with start/end
    times, score, and conditions summary.
    """
    p = {**DEFAULT_WEATHER_PREFS, **(prefs or {})}
    day_hours = [h for h in hours if h.time.date() == target_date]

    windows: list[dict] = []
    for h in day_hours:
        s = 100
        notes = []

        if h.is_severe:
            continue

        if h.temperature_c < p["temp_min_c"]:
            s -= min(40, (p["temp_min_c"] - h.temperature_c) * 5)
        elif h.temperature_c > p["temp_max_c"]:
            s -= min(40, (h.temperature_c - p["temp_max_c"]) * 5)

        if h.wind_speed_kmh > p["wind_max_kmh"]:
            s -= min(30, (h.wind_speed_kmh - p["wind_max_kmh"]) * 2)

        if h.precipitation_mm > p["precip_max_mm"]:
            s -= min(30, (h.precipitation_mm - p["precip_max_mm"]) * 10)

        # Graduated UV
        uv = h.uv_index
        if uv >= 11:
            s -= 25
            notes.append("Extreme UV — avoid")
        elif uv >= 8:
            s -= 15
            notes.append("Very high UV")
        elif uv >= p["uv_caution_threshold"]:
            s -= 8
            notes.append("High UV — sun protection")
        elif uv >= 3:
            notes.append("Sun protection recommended")

        # Humidity (apparent vs actual temp)
        divergence = h.apparent_temperature_c - h.temperature_c
        hum_threshold = p.get("humidity_divergence_c", 4)
        if divergence > hum_threshold + 4:
            s -= 12
            notes.append(f"Very humid (feels {h.apparent_temperature_c:.0f}°C)")
        elif divergence > hum_threshold:
            s -= 5
            notes.append("Humid")

        s = max(0, min(100, s))
        if s >= 40:
            windows.append({
                "time": h.time.strftime("%H:%M"),
                "score": s,
                "temp_c": h.temperature_c,
                "feels_like_c": h.apparent_temperature_c,
                "wind_kmh": h.wind_speed_kmh,
                "precip_mm": h.precipitation_mm,
                "uv": h.uv_index,
                "conditions": h.weather_label,
                "notes": notes,
            })

    # Merge consecutive good hours into windows
    merged = _merge_windows(windows)
    merged.sort(key=lambda w: w["avg_score"], reverse=True)
    return merged[:5]


def _merge_windows(hour_entries: list[dict]) -> list[dict]:
    """Merge consecutive hours into time windows."""
    if not hour_entries:
        return []

    merged = []
    current: list[dict] = [hour_entries[0]]

    for entry in hour_entries[1:]:
        prev_hour = int(current[-1]["time"].split(":")[0])
        this_hour = int(entry["time"].split(":")[0])

        if this_hour == prev_hour + 1:
            current.append(entry)
        else:
            merged.append(_summarize_window(current))
            current = [entry]

    merged.append(_summarize_window(current))
    return merged


def _summarize_window(entries: list[dict]) -> dict:
    """Summarize a group of consecutive hour entries into a window."""
    scores = [e["score"] for e in entries]
    temps = [e["temp_c"] for e in entries]
    return {
        "start": entries[0]["time"],
        "end": f"{int(entries[-1]['time'].split(':')[0]) + 1:02d}:00",
        "duration_hours": len(entries),
        "avg_score": round(sum(scores) / len(scores)),
        "min_score": min(scores),
        "temp_range_c": f"{min(temps):.0f}–{max(temps):.0f}",
        "conditions": entries[len(entries) // 2]["conditions"],
        "notes": list({n for e in entries for n in e.get("notes", [])}),
    }


# ---------------------------------------------------------------------------
# DB cache helpers
# ---------------------------------------------------------------------------

def get_cached_forecast(lat: float, lon: float, forecast_date: date) -> dict | None:
    """Return cached forecast data if fresh (< 3 hours old)."""
    from scripts.health_tools import get_conn
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT data FROM weather_cache
               WHERE lat = %s AND lon = %s AND forecast_date = %s
               AND fetched_at > NOW() - INTERVAL '3 hours'""",
            (lat, lon, forecast_date),
        )
        row = cur.fetchone()
        if row:
            import json
            return row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return None
    finally:
        conn.close()


def cache_forecast(lat: float, lon: float, forecast_date: date, data: dict) -> None:
    """Upsert forecast data into the cache."""
    from scripts.health_tools import get_conn
    conn = get_conn()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        from psycopg2.extras import Json
        cur.execute(
            """INSERT INTO weather_cache (lat, lon, forecast_date, data, fetched_at)
               VALUES (%s, %s, %s, %s, NOW())
               ON CONFLICT (lat, lon, forecast_date) DO UPDATE SET
                   data = EXCLUDED.data,
                   fetched_at = NOW()""",
            (lat, lon, forecast_date, Json(data)),
        )
        cur.close()
    except Exception:
        log.warning("Failed to cache weather forecast", exc_info=True)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

def check_weather(
    slug: str,
    target_date: str = "",
) -> dict:
    """Check weather and running suitability for an athlete's location.

    Returns forecast summary, suitability score, best running windows,
    and any warnings.
    """
    from scripts import athlete_store
    from scripts.tz import load_user_tz, user_today

    config = athlete_store.load(slug) or {}
    location = config.get("location")
    if not location or "lat" not in location or "lon" not in location:
        raise ValueError(
            "No location configured. Ask the user where they are based and "
            "store it with update_athlete_profile(field_path='location', "
            "value={'lat': ..., 'lon': ..., 'label': '...'})."
        )

    lat = location["lat"]
    lon = location["lon"]
    label = location.get("label", f"{lat}, {lon}")

    tz = load_user_tz(slug)
    tz_name = str(tz)
    today = user_today(tz)

    if target_date:
        td = date.fromisoformat(target_date)
    else:
        td = today

    weather_prefs = config.get("weather", {})

    raw = fetch_forecast(lat, lon, days=3, tz_name=tz_name)

    daily = parse_daily(raw)
    hourly = parse_hourly(raw)

    # Fetch air quality (non-blocking — swallow errors)
    aqi: AirQuality | None = None
    aqi_raw: dict | None = None
    try:
        aqi_raw = fetch_air_quality(lat, lon, days=3, tz_name=tz_name)
        aqi = parse_air_quality(aqi_raw, td)
    except Exception:
        log.debug("Air quality fetch failed", exc_info=True)

    target_day = None
    for d in daily:
        if d.date == td:
            target_day = d
            break

    if target_day is None:
        raise ValueError(
            f"No forecast available for {td}. "
            f"Available dates: {[d.date.isoformat() for d in daily]}"
        )

    suitability = score_daily(target_day, weather_prefs, air_quality=aqi)
    windows = score_hourly_windows(hourly, td, weather_prefs)
    suitability.best_windows = windows

    forecast_summary = []
    for d in daily:
        d_aqi: AirQuality | None = None
        try:
            if aqi_raw:
                d_aqi = parse_air_quality(aqi_raw, d.date)
        except Exception:
            pass
        ds = score_daily(d, weather_prefs, air_quality=d_aqi)
        forecast_summary.append({
            "date": d.date.isoformat(),
            "weather": d.weather_label,
            "temp_range": f"{d.temp_min_c:.0f}–{d.temp_max_c:.0f}°C",
            "precipitation_mm": d.precipitation_sum_mm,
            "wind_max_kmh": d.wind_max_kmh,
            "uv_max": d.uv_index_max,
            "run_suitability_score": ds.score,
            "suitable_for_running": ds.suitable,
        })

    result: dict[str, Any] = {
        "location": label,
        "target_date": td.isoformat(),
        "suitability": suitability.to_dict(),
        "forecast": forecast_summary,
        "current_conditions": {
            "weather": target_day.weather_label,
            "temp_range": f"{target_day.temp_min_c:.0f}–{target_day.temp_max_c:.0f}°C",
            "wind": f"{target_day.wind_max_kmh:.0f} km/h",
            "precipitation": f"{target_day.precipitation_sum_mm:.1f} mm",
            "uv_max": target_day.uv_index_max,
            "sunrise": target_day.sunrise,
            "sunset": target_day.sunset,
        },
    }

    if aqi is not None:
        result["air_quality"] = aqi.to_dict()

    return result

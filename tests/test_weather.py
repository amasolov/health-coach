"""Tests for the weather module (Open-Meteo integration + suitability scoring)."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch, MagicMock

import pytest

from scripts.weather import (
    AirQuality,
    DailyWeather,
    HourlyWeather,
    RunSuitability,
    check_weather,
    fetch_air_quality,
    parse_air_quality,
    parse_daily,
    parse_hourly,
    score_daily,
    score_hourly_windows,
    DEFAULT_WEATHER_PREFS,
)


# ---------------------------------------------------------------------------
# Fixtures: sample Open-Meteo API responses
# ---------------------------------------------------------------------------

def _make_daily_raw(
    dates: list[str] | None = None,
    temps_min: list[float] | None = None,
    temps_max: list[float] | None = None,
    precip: list[float] | None = None,
    wind: list[float] | None = None,
    codes: list[int] | None = None,
    uv: list[float] | None = None,
) -> dict:
    """Build a minimal Open-Meteo daily response."""
    dates = dates or ["2026-03-13", "2026-03-14", "2026-03-15"]
    n = len(dates)
    return {
        "daily": {
            "time": dates,
            "temperature_2m_min": temps_min or [12.0] * n,
            "temperature_2m_max": temps_max or [22.0] * n,
            "apparent_temperature_min": temps_min or [10.0] * n,
            "apparent_temperature_max": temps_max or [20.0] * n,
            "precipitation_sum": precip or [0.0] * n,
            "precipitation_hours": [0.0] * n,
            "wind_speed_10m_max": wind or [15.0] * n,
            "wind_gusts_10m_max": [25.0] * n,
            "weather_code": codes or [0] * n,
            "uv_index_max": uv or [5.0] * n,
            "sunrise": ["06:30"] * n,
            "sunset": ["18:00"] * n,
        }
    }


def _make_hourly_raw(target_date: str = "2026-03-13") -> dict:
    """Build a minimal hourly response with 24 hours for one day."""
    times = [f"{target_date}T{h:02d}:00" for h in range(24)]
    n = len(times)
    temps = [10 + (h % 12) for h in range(n)]
    return {
        "hourly": {
            "time": times,
            "temperature_2m": temps,
            "apparent_temperature": [t - 2 for t in temps],
            "relative_humidity_2m": [60.0] * n,
            "precipitation": [0.0] * n,
            "wind_speed_10m": [10.0] * n,
            "wind_gusts_10m": [15.0] * n,
            "weather_code": [0] * n,
            "uv_index": [min(h, 12 - abs(h - 12)) for h in range(n)],
            "cloud_cover": [30.0] * n,
        }
    }


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------

class TestParsing:

    def test_parse_daily_returns_correct_count(self):
        raw = _make_daily_raw()
        result = parse_daily(raw)
        assert len(result) == 3

    def test_parse_daily_types(self):
        raw = _make_daily_raw()
        result = parse_daily(raw)
        assert isinstance(result[0], DailyWeather)
        assert isinstance(result[0].date, date)

    def test_parse_daily_values(self):
        raw = _make_daily_raw(
            temps_min=[10.0], temps_max=[25.0],
            precip=[1.5], wind=[20.0],
            codes=[2], uv=[7.0],
            dates=["2026-03-13"],
        )
        d = parse_daily(raw)[0]
        assert d.temp_min_c == 10.0
        assert d.temp_max_c == 25.0
        assert d.precipitation_sum_mm == 1.5
        assert d.wind_max_kmh == 20.0
        assert d.weather_code == 2
        assert d.uv_index_max == 7.0

    def test_parse_hourly_returns_24_entries(self):
        raw = _make_hourly_raw()
        result = parse_hourly(raw)
        assert len(result) == 24

    def test_parse_hourly_types(self):
        raw = _make_hourly_raw()
        result = parse_hourly(raw)
        assert isinstance(result[0], HourlyWeather)

    def test_weather_label(self):
        raw = _make_daily_raw(codes=[0], dates=["2026-03-13"])
        d = parse_daily(raw)[0]
        assert d.weather_label == "Clear sky"

    def test_severe_detection(self):
        raw = _make_daily_raw(codes=[95], dates=["2026-03-13"])
        d = parse_daily(raw)[0]
        assert d.is_severe is True

    def test_non_severe_detection(self):
        raw = _make_daily_raw(codes=[0], dates=["2026-03-13"])
        d = parse_daily(raw)[0]
        assert d.is_severe is False


# ---------------------------------------------------------------------------
# Scoring tests
# ---------------------------------------------------------------------------

class TestScoring:

    def _make_day(self, **overrides) -> DailyWeather:
        defaults = {
            "date": date(2026, 3, 13),
            "temp_min_c": 12.0,
            "temp_max_c": 22.0,
            "apparent_temp_min_c": 10.0,
            "apparent_temp_max_c": 20.0,
            "precipitation_sum_mm": 0.0,
            "precipitation_hours": 0.0,
            "wind_max_kmh": 15.0,
            "wind_gusts_max_kmh": 25.0,
            "weather_code": 0,
            "uv_index_max": 5.0,
            "sunrise": "06:30",
            "sunset": "18:00",
        }
        defaults.update(overrides)
        return DailyWeather(**defaults)

    def test_perfect_day_high_score(self):
        day = self._make_day()
        result = score_daily(day)
        assert result.suitable is True
        assert result.score >= 80

    def test_severe_weather_zero_score(self):
        day = self._make_day(weather_code=95)
        result = score_daily(day)
        assert result.suitable is False
        assert result.score == 0

    def test_too_hot_reduces_score(self):
        day = self._make_day(temp_min_c=35.0, temp_max_c=45.0)
        result = score_daily(day)
        assert result.score < 70
        assert any("Hot" in w for w in result.warnings)

    def test_too_cold_reduces_score(self):
        day = self._make_day(temp_min_c=-5.0, temp_max_c=2.0)
        result = score_daily(day)
        assert result.score < 70
        assert any("Cold" in w for w in result.warnings)

    def test_heavy_wind_reduces_score(self):
        day = self._make_day(wind_max_kmh=45.0)
        result = score_daily(day)
        assert result.score < 80
        assert any("Windy" in w for w in result.warnings)

    def test_rain_reduces_score(self):
        day = self._make_day(precipitation_sum_mm=10.0)
        result = score_daily(day)
        assert result.score <= 70
        assert any("Rain" in w for w in result.warnings)

    def test_high_uv_warning(self):
        day = self._make_day(uv_index_max=8.0)
        result = score_daily(day)
        assert any("UV" in w for w in result.warnings)

    def test_custom_prefs_temp_range(self):
        day = self._make_day(temp_min_c=0.0, temp_max_c=4.0)
        result_default = score_daily(day)
        result_cold_ok = score_daily(day, {"temp_min_c": -5})
        assert result_cold_ok.score > result_default.score

    def test_suitability_boundary(self):
        day = self._make_day(
            temp_min_c=36.0, temp_max_c=44.0,
            wind_max_kmh=40.0,
            precipitation_sum_mm=8.0,
        )
        result = score_daily(day)
        assert result.suitable is False


class TestHourlyWindows:

    def test_returns_windows_for_good_day(self):
        raw = _make_hourly_raw("2026-03-13")
        hours = parse_hourly(raw)
        windows = score_hourly_windows(hours, date(2026, 3, 13))
        assert len(windows) > 0

    def test_window_has_required_fields(self):
        raw = _make_hourly_raw("2026-03-13")
        hours = parse_hourly(raw)
        windows = score_hourly_windows(hours, date(2026, 3, 13))
        w = windows[0]
        assert "start" in w
        assert "end" in w
        assert "avg_score" in w
        assert "duration_hours" in w

    def test_no_windows_for_wrong_date(self):
        raw = _make_hourly_raw("2026-03-13")
        hours = parse_hourly(raw)
        windows = score_hourly_windows(hours, date(2026, 3, 14))
        assert len(windows) == 0


# ---------------------------------------------------------------------------
# Integration: check_weather()
# ---------------------------------------------------------------------------

class TestCheckWeather:

    GOOD_FORECAST = {
        **_make_daily_raw(),
        **_make_hourly_raw(),
    }

    def test_check_weather_no_location_raises(self, user_slug):
        with patch("scripts.weather.fetch_forecast") as mock_fetch, \
             patch("scripts.athlete_store.load", return_value={"profile": {}}):
            with pytest.raises(ValueError, match="No location configured"):
                check_weather(user_slug)

    def test_check_weather_returns_suitability(self, user_slug):
        config = {
            "profile": {"timezone": "America/New_York"},
            "location": {"lat": -33.87, "lon": 151.21, "label": "Sydney"},
            "weather": {},
        }
        with patch("scripts.weather.fetch_forecast", return_value=self.GOOD_FORECAST), \
             patch("scripts.athlete_store.load", return_value=config):
            result = check_weather(user_slug)
            assert "suitability" in result
            assert "forecast" in result
            assert result["location"] == "Sydney"
            assert result["suitability"]["score"] >= 0

    def test_check_weather_with_target_date(self, user_slug):
        config = {
            "profile": {"timezone": "America/New_York"},
            "location": {"lat": -33.87, "lon": 151.21, "label": "Sydney"},
        }
        with patch("scripts.weather.fetch_forecast", return_value=self.GOOD_FORECAST), \
             patch("scripts.athlete_store.load", return_value=config):
            result = check_weather(user_slug, "2026-03-13")
            assert result["target_date"] == "2026-03-13"


# ---------------------------------------------------------------------------
# Air quality tests
# ---------------------------------------------------------------------------

def _make_aqi_raw(
    pm25: list[float] | None = None,
    pm10: list[float] | None = None,
    us_aqi: list[int] | None = None,
    times: list[str] | None = None,
) -> dict:
    """Build a minimal Open-Meteo Air Quality response."""
    times = times or [f"2026-03-13T{h:02d}:00" for h in range(24)]
    n = len(times)
    return {
        "hourly": {
            "time": times,
            "pm2_5": pm25 or [10.0] * n,
            "pm10": pm10 or [20.0] * n,
            "us_aqi": us_aqi or [30] * n,
        }
    }


class TestAirQuality:

    def test_parse_air_quality(self):
        raw = _make_aqi_raw(pm25=[12.0] * 24, us_aqi=[45] * 24)
        aqi = parse_air_quality(raw, date(2026, 3, 13))
        assert isinstance(aqi, AirQuality)
        assert aqi.pm25_avg > 0
        assert aqi.us_aqi_max > 0

    def test_good_air_quality_no_penalty(self):
        day = DailyWeather(
            date=date(2026, 3, 13), temp_min_c=12, temp_max_c=22,
            apparent_temp_min_c=10, apparent_temp_max_c=20,
            precipitation_sum_mm=0, precipitation_hours=0,
            wind_max_kmh=10, wind_gusts_max_kmh=15,
            weather_code=0, uv_index_max=3, sunrise="06:30", sunset="18:00",
        )
        aqi = AirQuality(pm25_avg=8.0, pm25_max=12.0, pm10_avg=15.0, us_aqi_max=35)
        result = score_daily(day, air_quality=aqi)
        assert result.score >= 90
        assert not any("air" in w.lower() for w in result.warnings)

    def test_moderate_aqi_warning(self):
        """PM2.5 25-50 µg/m³ or US AQI 51-100 should warn but stay suitable."""
        day = DailyWeather(
            date=date(2026, 3, 13), temp_min_c=12, temp_max_c=22,
            apparent_temp_min_c=10, apparent_temp_max_c=20,
            precipitation_sum_mm=0, precipitation_hours=0,
            wind_max_kmh=10, wind_gusts_max_kmh=15,
            weather_code=0, uv_index_max=3, sunrise="06:30", sunset="18:00",
        )
        aqi = AirQuality(pm25_avg=35.0, pm25_max=48.0, pm10_avg=50.0, us_aqi_max=80)
        result = score_daily(day, air_quality=aqi)
        assert result.suitable is True
        assert any("air" in w.lower() for w in result.warnings)

    def test_bad_aqi_unsuitable(self):
        """PM2.5 > 50 or US AQI > 150 should make running unsuitable."""
        day = DailyWeather(
            date=date(2026, 3, 13), temp_min_c=12, temp_max_c=22,
            apparent_temp_min_c=10, apparent_temp_max_c=20,
            precipitation_sum_mm=0, precipitation_hours=0,
            wind_max_kmh=10, wind_gusts_max_kmh=15,
            weather_code=0, uv_index_max=3, sunrise="06:30", sunset="18:00",
        )
        aqi = AirQuality(pm25_avg=80.0, pm25_max=120.0, pm10_avg=100.0, us_aqi_max=170)
        result = score_daily(day, air_quality=aqi)
        assert result.score < 50

    def test_hazardous_aqi_zero_score(self):
        """US AQI > 200 (hazardous) should be near-zero."""
        day = DailyWeather(
            date=date(2026, 3, 13), temp_min_c=12, temp_max_c=22,
            apparent_temp_min_c=10, apparent_temp_max_c=20,
            precipitation_sum_mm=0, precipitation_hours=0,
            wind_max_kmh=10, wind_gusts_max_kmh=15,
            weather_code=0, uv_index_max=3, sunrise="06:30", sunset="18:00",
        )
        aqi = AirQuality(pm25_avg=200.0, pm25_max=300.0, pm10_avg=250.0, us_aqi_max=280)
        result = score_daily(day, air_quality=aqi)
        assert result.score <= 20
        assert any("hazardous" in w.lower() or "dangerous" in w.lower()
                    for w in result.warnings)


# ---------------------------------------------------------------------------
# Graduated UV scoring tests
# ---------------------------------------------------------------------------

class TestGraduatedUV:

    def _make_day(self, uv: float) -> DailyWeather:
        return DailyWeather(
            date=date(2026, 3, 13), temp_min_c=12, temp_max_c=22,
            apparent_temp_min_c=10, apparent_temp_max_c=20,
            precipitation_sum_mm=0, precipitation_hours=0,
            wind_max_kmh=10, wind_gusts_max_kmh=15,
            weather_code=0, uv_index_max=uv,
            sunrise="06:30", sunset="18:00",
        )

    def test_low_uv_no_penalty(self):
        result = score_daily(self._make_day(3.0))
        assert result.score >= 95
        assert not any("UV" in w for w in result.warnings)

    def test_moderate_uv_small_penalty(self):
        """UV 6-8: moderate penalty, sun protection advisory."""
        result = score_daily(self._make_day(7.0))
        assert 80 <= result.score <= 95
        assert any("UV" in w for w in result.warnings)

    def test_high_uv_larger_penalty(self):
        """UV 8-10: larger penalty."""
        result = score_daily(self._make_day(9.0))
        high_result = score_daily(self._make_day(7.0))
        assert result.score < high_result.score

    def test_extreme_uv_major_penalty(self):
        """UV 11+: major penalty (typical Australian summer)."""
        result = score_daily(self._make_day(12.0))
        assert result.score < 80
        assert any("extreme" in w.lower() or "avoid" in w.lower()
                    for w in result.warnings)

    def test_sun_protection_guidance(self):
        """UV >= 3 should include sun protection guidance."""
        result = score_daily(self._make_day(6.0))
        all_text = " ".join(result.warnings + result.reasons)
        assert "sun protection" in all_text.lower() or "sunscreen" in all_text.lower()


# ---------------------------------------------------------------------------
# Humidity / apparent temperature tests
# ---------------------------------------------------------------------------

class TestHumidityAwareness:

    def test_high_humidity_divergence_warns(self):
        """When feels-like temp >> actual, warn about humidity."""
        day = DailyWeather(
            date=date(2026, 3, 13),
            temp_min_c=22, temp_max_c=30,
            apparent_temp_min_c=26, apparent_temp_max_c=36,
            precipitation_sum_mm=0, precipitation_hours=0,
            wind_max_kmh=10, wind_gusts_max_kmh=15,
            weather_code=0, uv_index_max=4,
            sunrise="06:30", sunset="18:00",
        )
        result = score_daily(day)
        assert any("humid" in w.lower() or "feels like" in w.lower()
                    for w in result.warnings)

    def test_low_humidity_no_warning(self):
        """When actual ≈ apparent, no humidity warning."""
        day = DailyWeather(
            date=date(2026, 3, 13),
            temp_min_c=12, temp_max_c=22,
            apparent_temp_min_c=11, apparent_temp_max_c=21,
            precipitation_sum_mm=0, precipitation_hours=0,
            wind_max_kmh=10, wind_gusts_max_kmh=15,
            weather_code=0, uv_index_max=4,
            sunrise="06:30", sunset="18:00",
        )
        result = score_daily(day)
        assert not any("humid" in w.lower() for w in result.warnings)

    def test_humidity_affects_score(self):
        """High humidity should reduce the score compared to dry conditions."""
        dry_day = DailyWeather(
            date=date(2026, 3, 13),
            temp_min_c=22, temp_max_c=30,
            apparent_temp_min_c=21, apparent_temp_max_c=29,
            precipitation_sum_mm=0, precipitation_hours=0,
            wind_max_kmh=10, wind_gusts_max_kmh=15,
            weather_code=0, uv_index_max=4,
            sunrise="06:30", sunset="18:00",
        )
        humid_day = DailyWeather(
            date=date(2026, 3, 13),
            temp_min_c=22, temp_max_c=30,
            apparent_temp_min_c=26, apparent_temp_max_c=38,
            precipitation_sum_mm=0, precipitation_hours=0,
            wind_max_kmh=10, wind_gusts_max_kmh=15,
            weather_code=0, uv_index_max=4,
            sunrise="06:30", sunset="18:00",
        )
        dry_result = score_daily(dry_day)
        humid_result = score_daily(humid_day)
        assert humid_result.score < dry_result.score


# ---------------------------------------------------------------------------
# check_weather includes air quality
# ---------------------------------------------------------------------------

class TestCheckWeatherWithAQI:

    def test_check_weather_includes_air_quality(self, user_slug):
        config = {
            "profile": {"timezone": "Australia/Sydney"},
            "location": {"lat": -33.87, "lon": 151.21, "label": "Sydney"},
            "weather": {},
        }
        forecast_data = {**_make_daily_raw(), **_make_hourly_raw()}
        aqi_data = _make_aqi_raw()
        with patch("scripts.weather.fetch_forecast", return_value=forecast_data), \
             patch("scripts.weather.fetch_air_quality", return_value=aqi_data), \
             patch("scripts.athlete_store.load", return_value=config):
            result = check_weather(user_slug)
            assert "air_quality" in result

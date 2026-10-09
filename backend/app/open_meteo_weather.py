"""Free current weather from Open-Meteo using latitude/longitude (no API key)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"


def _weather_words(code: int | None) -> str:
    if code is None:
        return "conditions unknown"
    c = int(code)
    # WMO Weather interpretation codes (Open-Meteo)
    if c == 0:
        return "clear sky"
    if c in (1, 2, 3):
        return {1: "mainly clear", 2: "partly cloudy", 3: "overcast"}[c]
    if c in (45, 48):
        return "foggy"
    if c in (51, 53, 55):
        return "drizzle"
    if c in (56, 57):
        return "freezing drizzle"
    if c in (61, 63, 65):
        return {61: "light rain", 63: "moderate rain", 65: "heavy rain"}[c]
    if c in (66, 67):
        return "freezing rain"
    if c in (71, 73, 75):
        return {71: "light snow", 73: "moderate snow", 75: "heavy snow"}[c]
    if c == 77:
        return "snow grains"
    if c in (80, 81, 82):
        return "rain showers"
    if c in (85, 86):
        return "snow showers"
    if c == 95:
        return "thunderstorm"
    if c in (96, 99):
        return "thunderstorm with hail"
    return f"weather code {c}"


async def fetch_local_weather_context(latitude: float, longitude: float) -> str:
    """
    Weather block for the LLM: Google Weather API when Maps Platform is enabled and works;
    otherwise Open-Meteo (no key).
    """
    from app.maps_platform import facade as gmaps_facade
    from app.maps_platform.config import is_platform_enabled

    if is_platform_enabled():
        w = await gmaps_facade.weather_current_markdown(latitude, longitude)
        if w.strip():
            return w
    return await _fetch_open_meteo_only(latitude, longitude)


async def _fetch_open_meteo_only(latitude: float, longitude: float) -> str:
    params: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "current": (
            "temperature_2m,relative_humidity_2m,apparent_temperature,"
            "precipitation,weather_code,cloud_cover,wind_speed_10m,wind_direction_10m,is_day"
        ),
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(OPEN_METEO_FORECAST, params=params)
        if r.status_code >= 400:
            logger.warning("Open-Meteo HTTP %s", r.status_code)
            return ""
        data = r.json()
        cur = data.get("current") or {}
        tz = str(data.get("timezone") or "").strip()
        units_t = str(((data.get("current_units") or {}).get("temperature_2m")) or "°C")

        t = cur.get("temperature_2m")
        ap = cur.get("apparent_temperature")
        rh = cur.get("relative_humidity_2m")
        pcip = cur.get("precipitation")
        cc = cur.get("cloud_cover")
        ws = cur.get("wind_speed_10m")
        wd = cur.get("wind_direction_10m")
        is_day = cur.get("is_day")
        desc = _weather_words(cur.get("weather_code"))

        parts: list[str] = [
            "## Device location — current weather (Open-Meteo, approximate GPS)",
            f"Coordinates: {latitude:.5f}, {longitude:.5f}" + (f" · timezone `{tz}`" if tz else ""),
            f"Right now: **{desc}**.",
        ]
        if t is not None:
            line = f"Air temperature about **{t}{units_t}**"
            if ap is not None:
                line += f", feels like **{ap}{units_t}**"
            parts.append(line + ".")
        if rh is not None:
            parts.append(f"Relative humidity roughly **{rh}%**.")
        if pcip is not None and float(pcip) > 0:
            parts.append(f"Recent precipitation **{pcip} mm**.")
        if cc is not None:
            parts.append(f"Cloud cover about **{cc}%**.")
        if ws is not None:
            wline = f"Wind around **{ws}** (model units)."
            if wd is not None:
                wline = f"Wind **{ws}** from **{wd}°**."
            parts.append(wline)
        if is_day is not None:
            parts.append("**Daytime.**" if is_day else "**Nighttime.**")
        parts.append(
            "Use this block for “weather here / today” questions. "
            "Do not invent a different city; if the user asks elsewhere, say you only have this location."
        )
        return "\n".join(parts)
    except Exception as e:
        logger.warning("Open-Meteo fetch failed: %s", e)
        return ""

"""Google Maps Platform Weather API — current conditions (markdown for LLM)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.maps_platform.config import (
    google_maps_api_key,
    http_timeout_seconds,
    weather_current_lookup_url,
    weather_units,
)

logger = logging.getLogger(__name__)


async def fetch_current_conditions_dict(latitude: float, longitude: float) -> dict[str, Any]:
    key = google_maps_api_key()
    if not key:
        return {}
    us = "IMPERIAL" if weather_units() == "IMPERIAL" else "METRIC"
    params = {
        "key": key,
        "location.latitude": str(latitude),
        "location.longitude": str(longitude),
        "unitsSystem": us,
    }
    try:
        async with httpx.AsyncClient(timeout=http_timeout_seconds(25.0)) as client:
            r = await client.get(weather_current_lookup_url(), params=params)
        if r.status_code >= 400:
            logger.warning("Google Weather HTTP %s: %s", r.status_code, r.text[:200])
            return {}
        data = r.json()
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Google Weather failed: %s", e)
        return {}


def current_conditions_to_markdown(latitude: float, longitude: float, data: dict[str, Any]) -> str:
    if not data:
        return ""
    wc = data.get("weatherCondition")
    desc = "unknown"
    if isinstance(wc, dict):
        dsub = wc.get("description")
        if isinstance(dsub, dict) and dsub.get("text"):
            desc = str(dsub["text"]).strip()
        elif wc.get("type"):
            desc = str(wc["type"]).strip()
    tz_obj = data.get("timeZone")
    tz = str(tz_obj.get("id", "") if isinstance(tz_obj, dict) else "").strip()
    temp = data.get("temperature") or {}
    feels = data.get("feelsLikeTemperature") or {}
    tdeg = temp.get("degrees")
    tunit = temp.get("unit", "")
    fdeg = feels.get("degrees")
    rh = data.get("relativeHumidity")
    precip = data.get("precipitation") or {}
    prob = (precip.get("probability") or {}) if isinstance(precip.get("probability"), dict) else {}
    p_pct = prob.get("percent")
    p_type = prob.get("type", "")
    wind = data.get("wind") or {}
    wspd = (wind.get("speed") or {}) if isinstance(wind.get("speed"), dict) else {}
    wdir = (wind.get("direction") or {}) if isinstance(wind.get("direction"), dict) else {}
    vis = data.get("visibility") or {}
    cc = data.get("cloudCover")
    lines = [
        "## Device location — current weather (Google Maps Platform Weather API)",
        f"Coordinates: `{latitude:.6f}`, `{longitude:.6f}`" + (f" · time zone `{tz}`" if tz else ""),
        f"**Condition:** {desc}",
        "**Daytime.**" if data.get("isDaytime") else "**Nighttime.**",
    ]
    if tdeg is not None:
        s = f"Temperature **{tdeg} {tunit}**"
        if fdeg is not None:
            s += f", feels like **{fdeg} {tunit}**"
        lines.append(s + ".")
    if rh is not None:
        lines.append(f"Relative humidity **{rh}%**.")
    if p_pct is not None:
        lines.append(f"Precipitation chance **{p_pct}%** ({p_type or 'type n/a'}).")
    if wspd.get("value") is not None:
        cardinal = wdir.get("cardinal", "")
        lines.append(
            f"Wind **{wspd.get('value')} {wspd.get('unit', '')}**"
            + (f" from **{cardinal}**" if cardinal else "")
            + "."
        )
    if vis.get("distance") is not None:
        lines.append(f"Visibility **{vis.get('distance')} {vis.get('unit', '')}**.")
    if cc is not None:
        lines.append(f"Cloud cover **{cc}%**.")
    lines.append(
        "Use this for umbrella / jacket hints. Do not invent a different location than the coordinates above."
    )
    return "\n".join(lines)


async def fetch_current_conditions_markdown(latitude: float, longitude: float) -> str:
    data = await fetch_current_conditions_dict(latitude, longitude)
    return current_conditions_to_markdown(latitude, longitude, data)

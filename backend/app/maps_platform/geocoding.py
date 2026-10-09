"""Google Geocoding API (forward + reverse). Server-side key as query param."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.maps_platform.config import (
    geocode_base_url,
    google_maps_api_key,
    http_timeout_seconds,
    language_code,
    region_code,
    waypoint_address_max_len,
)

logger = logging.getLogger(__name__)


async def reverse_geocode_components(
    latitude: float,
    longitude: float,
    *,
    language: str | None = None,
) -> dict[str, Any]:
    key = google_maps_api_key()
    if not key:
        return {}
    lang = (language or "").strip() or language_code()
    params: dict[str, str] = {
        "latlng": f"{latitude},{longitude}",
        "key": key,
    }
    if lang:
        params["language"] = lang[:12]
    try:
        async with httpx.AsyncClient(timeout=http_timeout_seconds(25.0)) as client:
            r = await client.get(geocode_base_url(), params=params)
        if r.status_code >= 400:
            logger.warning("Google Geocoding HTTP %s", r.status_code)
            return {}
        data = r.json()
        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            logger.warning("Google Geocoding status %s", data.get("status"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Google Geocoding failed: %s", e)
        return {}


async def forward_geocode(
    address: str,
    *,
    language: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    key = google_maps_api_key()
    if not key or not (address or "").strip():
        return {}
    lang = (language or "").strip() or language_code()
    reg = (region or "").strip() or region_code()
    params: dict[str, str] = {
        "address": address.strip()[: waypoint_address_max_len()],
        "key": key,
    }
    if lang:
        params["language"] = lang[:12]
    if reg:
        params["region"] = reg.lower()[:2]
    try:
        async with httpx.AsyncClient(timeout=http_timeout_seconds(25.0)) as client:
            r = await client.get(geocode_base_url(), params=params)
        if r.status_code >= 400:
            logger.warning("Google Geocoding HTTP %s", r.status_code)
            return {}
        data = r.json()
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Google Geocoding forward failed: %s", e)
        return {}

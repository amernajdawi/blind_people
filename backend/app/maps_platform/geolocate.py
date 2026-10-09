"""Browser Geolocation API companion: Wi‑Fi / cell tower position (server-side key)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.maps_platform.config import geolocate_url, google_maps_api_key, http_timeout_seconds

logger = logging.getLogger(__name__)


async def geolocate_raw(body: dict[str, Any] | None = None) -> dict[str, Any]:
    key = google_maps_api_key()
    if not key:
        return {"error": {"message": "missing_api_key"}}
    params = {"key": key}
    try:
        async with httpx.AsyncClient(timeout=http_timeout_seconds(25.0)) as client:
            r = await client.post(
                geolocate_url(),
                params=params,
                json=body if body else {},
            )
        data = r.json()
        if r.status_code >= 400:
            logger.warning("Geolocation API HTTP %s: %s", r.status_code, str(data)[:400])
            return {"error": data, "status": r.status_code}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Geolocation API failed: %s", e)
        return {"error": {"message": str(e)}}

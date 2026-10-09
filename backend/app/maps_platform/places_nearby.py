"""Places API (New) — searchNearby for short LLM context."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.maps_platform.config import (
    google_maps_api_key,
    http_timeout_seconds,
    nearby_included_types,
    nearby_markdown_max_items,
    nearby_max_result_count,
    nearby_search_radius_m,
    places_response_field_mask,
    places_search_nearby_url,
)

logger = logging.getLogger(__name__)


async def search_nearby_raw(
    latitude: float,
    longitude: float,
    *,
    radius_m: float | None = None,
    max_results: int | None = None,
    included_types: list[str] | None = None,
) -> dict[str, Any]:
    key = google_maps_api_key()
    if not key:
        return {}
    types = included_types if included_types is not None else nearby_included_types()
    if not types:
        logger.info("Skipping Places searchNearby: GOOGLE_MAPS_NEARBY_INCLUDED_TYPES is empty")
        return {}
    r_m = radius_m if radius_m is not None else nearby_search_radius_m()
    cap = max_results if max_results is not None else nearby_max_result_count()
    body = {
        "includedTypes": types[:10],
        "maxResultCount": cap,
        "rankPreference": "DISTANCE",
        "locationRestriction": {
            "circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": float(r_m),
            }
        },
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": places_response_field_mask(),
    }
    try:
        async with httpx.AsyncClient(timeout=http_timeout_seconds(25.0)) as client:
            r = await client.post(places_search_nearby_url(), headers=headers, json=body)
        if r.status_code >= 400:
            logger.warning("Places searchNearby HTTP %s: %s", r.status_code, r.text[:300])
            return {}
        data = r.json()
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Places searchNearby failed: %s", e)
        return {}


def nearby_results_to_markdown(
    latitude: float,
    longitude: float,
    radius_m: float,
    data: dict[str, Any],
) -> str:
    places = data.get("places")
    if not isinstance(places, list) or not places:
        return ""
    max_n = nearby_markdown_max_items()
    lines = [
        "## Nearby points of interest (Google Places API, distance-ranked)",
        f"Search origin: `{latitude:.6f}`, `{longitude:.6f}` · radius **{radius_m:.0f} m**",
        "These are **approximate** business/POI matches, not a guarantee of what is directly in front of the user.",
    ]
    for i, p in enumerate(places[:max_n], 1):
        if not isinstance(p, dict):
            continue
        dn = (p.get("displayName") or {}) if isinstance(p.get("displayName"), dict) else {}
        name = str(dn.get("text") or dn.get("name") or "Unknown").strip()
        addr = str(p.get("shortFormattedAddress") or p.get("formattedAddress") or "").strip()
        ptype = str(p.get("primaryType") or "").strip()
        loc = p.get("location") if isinstance(p.get("location"), dict) else {}
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        coord = (
            f"`{lat:.5f}, {lon:.5f}`"
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float))
            else ""
        )
        bit = f"{i}. **{name}**"
        if ptype:
            bit += f" ({ptype})"
        if addr:
            bit += f" — {addr}"
        if coord:
            bit += f" · {coord}"
        lines.append(bit)
    lines.append("Mention only relevant POIs; distances are not computed in this list (ranked by Google).")
    return "\n".join(lines)


async def fetch_nearby_markdown(latitude: float, longitude: float) -> str:
    if not nearby_included_types():
        return ""
    radius = nearby_search_radius_m()
    data = await search_nearby_raw(latitude, longitude, radius_m=radius)
    return nearby_results_to_markdown(latitude, longitude, radius, data)

"""Pre-fetch Routes API directions for chat grounding (no LLM tool call required)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.maps_platform.config import intent_route_prefetch_try_transit, waypoint_address_max_len
from app.maps_platform.routes_compute import compute_routes_raw, routes_response_to_markdown

logger = logging.getLogger(__name__)


def _place_to_route_destination(place: dict[str, Any]) -> dict[str, Any] | None:
    """Prefer lat/lng; else address string for computeRoutes."""
    loc = place.get("location") if isinstance(place.get("location"), dict) else None
    if loc:
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        if lat is not None and lng is not None:
            return {"latitude": float(lat), "longitude": float(lng)}
    addr = str(
        place.get("shortFormattedAddress") or place.get("formattedAddress") or ""
    ).strip()
    dn = place.get("displayName") if isinstance(place.get("displayName"), dict) else {}
    name = str(dn.get("text") or "").strip()
    cap = waypoint_address_max_len()
    if name and addr:
        return {"address": f"{name}, {addr}"[:cap]}
    if addr:
        return {"address": addr[:cap]}
    if name:
        return {"address": name[:cap]}
    return None


def _routes_non_empty(raw: dict[str, Any]) -> bool:
    routes = raw.get("routes")
    return isinstance(routes, list) and len(routes) > 0


async def markdown_routes_for_intent_first_place(
    *,
    latitude: float,
    longitude: float,
    search_nearby_json: dict[str, Any],
) -> str:
    """
    Build markdown using TRANSIT first (optional), then WALK if transit did not return a route.
    """
    places = search_nearby_json.get("places")
    if not isinstance(places, list) or not places:
        return ""
    p0 = places[0]
    if not isinstance(p0, dict):
        return ""
    dest = _place_to_route_destination(p0)
    if not dest:
        return ""

    origin = {"latitude": latitude, "longitude": longitude}
    pieces: list[str] = []
    transit_ok = False

    if intent_route_prefetch_try_transit():
        dep = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_t = await compute_routes_raw(
            origin=origin,
            destination=dest,
            travel_mode="TRANSIT",
            departure_time_rfc3339=dep,
        )
        if _routes_non_empty(raw_t):
            md_t = routes_response_to_markdown(raw_t).strip()
            pieces.append("## Turn-by-turn directions (Google Routes — public transport)\n\n" + md_t)
            transit_ok = True
        else:
            err = raw_t.get("error") or raw_t.get("details")
            if err:
                logger.info("Intent route prefetch: TRANSIT empty — %s", str(err)[:220])

    if not transit_ok:
        raw_w = await compute_routes_raw(
            origin=origin,
            destination=dest,
            travel_mode="WALK",
        )
        md_w = routes_response_to_markdown(raw_w).strip()
        label = (
            "## Turn-by-turn directions (Google Routes — walking)\n\n"
            if _routes_non_empty(raw_w)
            else "## Routing (Google Routes — walking attempt)\n\n"
        )
        if md_w:
            pieces.append(label + md_w)
        elif not pieces:
            return ""

    return "\n\n---\n\n".join(pieces) if pieces else ""

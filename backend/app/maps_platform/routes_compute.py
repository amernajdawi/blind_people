"""Routes API (v2) — computeRoutes. Field-masked responses."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.maps_platform.config import (
    google_maps_api_key,
    http_timeout_routes_seconds,
    language_code,
    region_code,
    routes_compute_url,
    routes_response_field_mask,
    waypoint_address_max_len,
)

logger = logging.getLogger(__name__)


def extract_routes_api_error_message(data: dict[str, Any]) -> str | None:
    """
    Turn Routes ``computeRoutes`` JSON error payloads into a short string
    (billing, API-key restrictions, disabled API, etc.).
    """
    if data.get("error") == "http_error":
        details = data.get("details")
        if isinstance(details, dict):
            err = details.get("error")
            if isinstance(err, dict):
                parts: list[str] = []
                if err.get("status"):
                    parts.append(str(err["status"]))
                if err.get("code") is not None:
                    parts.append(str(err["code"]))
                msg = err.get("message")
                if msg:
                    return (": ".join(parts) + " — " + str(msg)) if parts else str(msg)
            top = details.get("message")
            if top:
                return str(top)
        if details is not None:
            return str(details)[:900]
        st = data.get("status")
        return f"HTTP {st}" if st else "HTTP error (no body)"
    e = data.get("error")
    if e and e != "http_error":
        if isinstance(e, dict) and e.get("message"):
            return str(e["message"])
        return str(e)[:500]
    return None


def _waypoint_from_payload(loc: dict[str, Any]) -> dict[str, Any]:
    if loc.get("address"):
        return {"address": str(loc["address"]).strip()[: waypoint_address_max_len()]}
    lat = loc.get("latitude")
    lon = loc.get("longitude")
    if lat is not None and lon is not None:
        return {"location": {"latLng": {"latitude": float(lat), "longitude": float(lon)}}}
    raise ValueError("Each of origin/destination needs address or latitude+longitude")


async def compute_routes_raw(
    *,
    origin: dict[str, Any],
    destination: dict[str, Any],
    travel_mode: str = "WALK",
    departure_time_rfc3339: str | None = None,
    arrival_time_rfc3339: str | None = None,
    compute_alternatives: bool = False,
    field_mask: str | None = None,
    language_code_override: str | None = None,
    region_code_override: str | None = None,
) -> dict[str, Any]:
    """
    ``travel_mode``: WALK, BICYCLE, DRIVE, TWO_WHEELER, TRANSIT.
    For TRANSIT, set ``departure_time_rfc3339`` or ``arrival_time_rfc3339`` (not both).
    """
    key = google_maps_api_key()
    if not key:
        return {"error": "missing_api_key"}

    body: dict[str, Any] = {
        "origin": _waypoint_from_payload(origin),
        "destination": _waypoint_from_payload(destination),
        "travelMode": travel_mode.upper(),
        "computeAlternativeRoutes": bool(compute_alternatives),
    }
    lang = (language_code_override or "").strip() or language_code()
    reg = (region_code_override or "").strip() or region_code()
    if lang:
        body["languageCode"] = lang[:12]
    if reg:
        body["regionCode"] = reg.upper()[:2]
    if travel_mode.upper() == "TRANSIT":
        if departure_time_rfc3339:
            body["departureTime"] = departure_time_rfc3339
        elif arrival_time_rfc3339:
            body["arrivalTime"] = arrival_time_rfc3339

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": field_mask or routes_response_field_mask(),
    }
    try:
        async with httpx.AsyncClient(timeout=http_timeout_routes_seconds()) as client:
            r = await client.post(routes_compute_url(), headers=headers, json=body)
        data = r.json()
        if r.status_code >= 400:
            err_txt = ""
            if isinstance(data, dict):
                inner = data.get("error")
                if isinstance(inner, dict) and inner.get("message"):
                    err_txt = str(inner.get("message", ""))[:400]
            logger.warning(
                "computeRoutes HTTP %s: %s",
                r.status_code,
                err_txt or str(data)[:400],
            )
            return {"error": "http_error", "status": r.status_code, "details": data}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("computeRoutes failed: %s", e)
        return {"error": str(e)}


def routes_response_to_markdown(data: dict[str, Any]) -> str:
    """Readable steps for LLM / UI."""
    routes = data.get("routes")
    if not isinstance(routes, list) or not routes:
        if data.get("error") == "http_error":
            api_err = extract_routes_api_error_message(data) or "unknown error"
            return (
                "(No route — Google Routes API rejected the request. **Common fixes:** (1) **Billing** enabled on "
                "the same GCP project as the key; (2) Credentials → your API key → **API restrictions** includes "
                "**Routes API**; (3) **Application restrictions** — for a backend server use **IP addresses** "
                "(your server’s egress IPs) or **None**; **not** “HTTP referrers” or Android/iOS-only. "
                "**Google said:** "
                + api_err
                + ")"
            )
        if data.get("error") == "missing_api_key":
            return "(No route: GOOGLE_MAPS_API_KEY is not set.)"
        plain = data.get("error")
        if isinstance(plain, str) and plain and plain != "http_error":
            return f"(No route: {plain})"
        err = data.get("details")
        if err:
            return f"(No route: {err})"
        return "(No routes returned)"
    route0 = routes[0]
    if not isinstance(route0, dict):
        return "(Invalid route)"
    lines = ["## Google Routes API — suggested path", "### Steps (summarized)"]
    legs = route0.get("legs")
    if not isinstance(legs, list):
        legs = []
    step_no = 0
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        steps = leg.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_no += 1
            ni = step.get("navigationInstruction") if isinstance(step.get("navigationInstruction"), dict) else {}
            instr = str(ni.get("instructions") or ni.get("maneuver") or "").strip()
            mode = str(step.get("travelMode") or "").strip()
            lv = step.get("localizedValues") if isinstance(step.get("localizedValues"), dict) else {}
            dist = (lv.get("distance") or {}) if isinstance(lv.get("distance"), dict) else {}
            dtxt = str(dist.get("text") or "").strip()
            td = step.get("transitDetails") if isinstance(step.get("transitDetails"), dict) else {}
            line = (td.get("transitLine") or {}) if isinstance(td.get("transitLine"), dict) else {}
            line_name = str(line.get("name") or line.get("shortName") or "").strip()
            headsign = str(td.get("headsign") or "").strip()
            piece = f"{step_no}. "
            if mode:
                piece += f"[{mode}] "
            if instr:
                piece += instr
            elif line_name or headsign:
                piece += f"Transit: {line_name} → {headsign}".strip()
            else:
                piece += "(step)"
            if dtxt:
                piece += f" — {dtxt}"
            lines.append(piece)
    if step_no == 0:
        lines.append("(No steps in response — try expanding GOOGLE_MAPS_ROUTES_FIELD_MASK or check origin/destination.)")
    return "\n".join(lines)

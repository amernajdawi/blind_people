"""Reverse geocoding via OSM Nominatim (named place from lat/lon).

Policy: https://operations.osmfoundation.org/policies/nominatim/
Use a descriptive User-Agent and avoid excessive requests — we cache by rounded coordinates.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
CACHE_TTL_SEC = float(os.environ.get("NOMINATIM_CACHE_SECS", "3600"))


def _ua_from_contact(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return f"camera-blind-assist/1.0 ({email})"


_user_agent = (
    os.environ.get("NOMINATIM_USER_AGENT", "").strip()
    or _ua_from_contact(os.environ.get("NOMINATIM_CONTACT_EMAIL", "").strip())
    or "camera-blind-assist/1.0 (local demo; set NOMINATIM_CONTACT_EMAIL in .env per OSM policy)"
)


_cache: dict[tuple[float, float], tuple[float, str]] = {}


def _cache_key(latitude: float, longitude: float) -> tuple[float, float]:
    return (round(latitude, 4), round(longitude, 4))


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)].rstrip() + "…"


def _address_lines(addr: dict[str, Any]) -> list[str]:
    """Pick human-readable rows from Nominatim `address` object (order matters)."""
    if not isinstance(addr, dict):
        return []
    pairs: list[tuple[str, str]] = []
    keys = (
        ("house_number", "House number"),
        ("road", "Street / road"),
        ("pedestrian", "Pedestrian area"),
        ("neighbourhood", "Neighbourhood"),
        ("suburb", "Suburb"),
        ("city_district", "City district"),
        ("quarter", "Quarter"),
        ("town", "Town"),
        ("city", "City"),
        ("village", "Village"),
        ("hamlet", "Hamlet"),
        ("municipality", "Municipality"),
        ("county", "County"),
        ("state", "State / region"),
        ("region", "Region"),
        ("postcode", "Postcode"),
        ("country", "Country"),
    )
    seen: set[str] = set()
    for key, label in keys:
        val = addr.get(key)
        if val is None:
            continue
        t = str(val).strip()
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        pairs.append((label, t))
    out = [f"- **{lab}**: {val}" for lab, val in pairs]
    return out


async def fetch_place_context(latitude: float, longitude: float) -> str:
    """
    Returns markdown for the LLM: named place + structured fields, or empty on failure.
    """
    key = _cache_key(latitude, longitude)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now < hit[0]:
        return hit[1]

    params = {
        "lat": latitude,
        "lon": longitude,
        "format": "json",
        "addressdetails": 1,
        "zoom": 18,
        "accept-language": os.environ.get("NOMINATIM_ACCEPT_LANGUAGE", "en").strip() or "en",
    }
    headers = {"User-Agent": _user_agent}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(NOMINATIM_URL, params=params, headers=headers)
        if r.status_code >= 400:
            logger.warning("Nominatim HTTP %s", r.status_code)
            return ""
        data = r.json()
    except Exception as e:
        logger.warning("Nominatim reverse failed: %s", e)
        return ""

    disp = _truncate(str(data.get("display_name") or ""), 520)
    addr = data.get("address") if isinstance(data.get("address"), dict) else {}
    lines = _address_lines(addr)

    chunks = [
        "## Approximate place from your GPS (OpenStreetMap Nominatim)",
        "Reverse-geocoding maps coordinates to the nearest known address in OpenStreetMap. "
        "It can be **wrong by a street or more** if GPS drifts or OSM data is incomplete. "
        "Always say **approximately** — not survey-grade.",
        f"**Coordinates:** `{latitude:.6f}`, `{longitude:.6f}`",
    ]
    if disp:
        chunks.append(f"**Map-style description:** {disp}")
    if lines:
        chunks.append("**Structured fields:**\n" + "\n".join(lines))
    chunks.append(
        "When answering out loud, give **city/town + country** first, then neighbourhood or street "
        "if helpful; mention that this comes from map data tied to their device location."
    )
    text = "\n\n".join(chunks)
    _cache[key] = (now + CACHE_TTL_SEC, text)
    return text

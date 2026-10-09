"""Reverse geocoding via Photon (Komoot) — OSM-derived, no API key, no email setup.

Public endpoint: https://photon.komoot.io — use moderately (results cached server-side).
Docs: https://github.com/komoot/photon
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PHOTON_BASE = os.environ.get("PHOTON_BASE_URL", "https://photon.komoot.io").rstrip("/")
CACHE_TTL_SEC = float(os.environ.get("REVERSE_GEO_CACHE_SECS", "3600"))

USER_AGENT = os.environ.get("PHOTON_USER_AGENT", "camera-blind-assist/1.0").strip() or "camera-blind-assist/1.0"

_cache: dict[tuple[float, float], tuple[float, str]] = {}


def _cache_key(latitude: float, longitude: float) -> tuple[float, float]:
    return (round(latitude, 4), round(longitude, 4))


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)].rstrip() + "…"


def _structured_lines(props: dict[str, Any]) -> list[str]:
    keys = (
        ("name", "Place name"),
        ("housenumber", "House number"),
        ("street", "Street / road"),
        ("locality", "Locality"),
        ("district", "District"),
        ("city", "City"),
        ("town", "Town"),
        ("village", "Village"),
        ("county", "County"),
        ("state", "State / region"),
        ("postcode", "Postcode"),
        ("country", "Country"),
    )
    seen: set[str] = set()
    out: list[str] = []
    for key, label in keys:
        val = props.get(key)
        if val is None:
            continue
        t = str(val).strip()
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(f"- **{label}**: {t}")
    cc = props.get("countrycode")
    if cc and str(cc).strip().upper() not in seen:
        out.append(f"- **Country code**: {str(cc).strip().upper()}")
    return out


def _guess_display(props: dict[str, Any]) -> str:
    """Human-readable one-liner when Photon has no single display_name."""
    parts: list[str] = []
    street = props.get("street")
    hn = props.get("housenumber")
    if street:
        parts.append(f"{street} {hn or ''}".strip())
    for key in ("locality", "district", "city", "town", "village"):
        v = props.get(key)
        if v and str(v).strip():
            parts.append(str(v).strip())
            break
    st = props.get("state")
    if st:
        parts.append(str(st).strip())
    c = props.get("country")
    if c:
        parts.append(str(c).strip())
    return ", ".join(parts) if parts else ""


async def fetch_place_context(latitude: float, longitude: float) -> str:
    """Markdown block for the LLM: named place from Photon reverse lookup."""
    key = _cache_key(latitude, longitude)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now < hit[0]:
        return hit[1]

    lang = (os.environ.get("PHOTON_LANG") or "en").strip() or "en"
    url = f"{PHOTON_BASE}/reverse"
    params = {"lat": latitude, "lon": longitude, "lang": lang}
    headers = {"User-Agent": USER_AGENT}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params, headers=headers)
        if r.status_code >= 400:
            logger.warning("Photon HTTP %s", r.status_code)
            return ""
        data = r.json()
    except Exception as e:
        logger.warning("Photon reverse failed: %s", e)
        return ""

    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list) or not feats:
        return ""

    feat0 = feats[0]
    props = feat0.get("properties") if isinstance(feat0, dict) else None
    if not isinstance(props, dict):
        return ""

    lines = _structured_lines(props)
    disp = ""
    if props.get("name"):
        disp = _truncate(str(props["name"]), 120)
        if props.get("street"):
            disp = _truncate(f'{props.get("name")} — {props.get("street")}', 520)
    if not disp:
        disp = _truncate(_guess_display(props), 520)

    geom = feat0.get("geometry") if isinstance(feat0, dict) else None
    snapped = ""
    if isinstance(geom, dict) and geom.get("type") == "Point":
        coords = geom.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            snapped = f"\n\n**Snapped map point** (library placement): `{coords[1]:.6f}`, `{coords[0]:.6f}`"

    chunks = [
        "## Approximate place from your GPS (Photon / OpenStreetMap data)",
        "Photon maps coordinates to OSM features. Results can be **off by a street or more**; "
        "coordinates below are what the **device** reported unless a snapped point is listed.",
        f"**Your device coordinates:** `{latitude:.6f}`, `{longitude:.6f}`",
    ]
    if snapped.strip():
        chunks.append(snapped.strip())
    if disp:
        chunks.append(f"**Summary:** {disp}")
    if lines:
        chunks.append("**Structured fields:**\n" + "\n".join(lines))
    chunks.append(
        "Say **approximately** where they are: city and country first, then street or area if useful."
    )
    text = "\n\n".join(chunks)
    _cache[key] = (now + CACHE_TTL_SEC, text)
    return text

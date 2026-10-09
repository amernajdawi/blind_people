"""Reverse geocoding via Geoapify (requires GEOAPIFY_API_KEY).

Docs: https://apidocs.geoapify.com/docs/geocoding/reverse-geocoding/
Place-details (by feature id) is separate from reverse; this module uses /v1/geocode/reverse for GPS → address.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GEOAPIFY_REVERSE_URL = "https://api.geoapify.com/v1/geocode/reverse"
CACHE_TTL_SEC = float(os.environ.get("REVERSE_GEO_CACHE_SECS", "3600"))

_cache: dict[tuple[float, float], tuple[float, str]] = {}


def _cache_key(latitude: float, longitude: float) -> tuple[float, float]:
    return (round(latitude, 4), round(longitude, 4))


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)].rstrip() + "…"


def _lines_from_properties(p: dict[str, Any]) -> list[str]:
    pairs = (
        ("housenumber", "House number"),
        ("street", "Street"),
        ("address_line1", "Address line 1"),
        ("address_line2", "Address line 2"),
        ("suburb", "Suburb"),
        ("district", "District"),
        ("city", "City"),
        ("county", "County"),
        ("state", "State / region"),
        ("postcode", "Postcode"),
        ("country", "Country"),
    )
    seen: set[str] = set()
    out: list[str] = []
    for key, label in pairs:
        val = p.get(key)
        if val is None:
            continue
        t = str(val).strip()
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(f"- **{label}**: {t}")
    cc = p.get("country_code")
    if cc and str(cc).strip():
        out.append(f"- **Country code**: {str(cc).strip().upper()}")
    rt = p.get("result_type")
    if rt:
        out.append(f"- **Match level**: {rt}")
    rk = p.get("rank")
    if isinstance(rk, dict):
        conf = rk.get("confidence")
        if conf is not None:
            try:
                out.append(f"- **Confidence** (0–1): {float(conf):.2f}")
            except (TypeError, ValueError):
                pass
    tz = p.get("timezone")
    if isinstance(tz, dict) and tz.get("name"):
        out.append(f"- **Timezone**: {tz.get('name')}")
    return out


async def fetch_place_context(latitude: float, longitude: float, api_key: str | None = None) -> str:
    """Markdown block for the LLM, or empty if no key / no result."""
    key = (api_key or os.environ.get("GEOAPIFY_API_KEY", "")).strip()
    if not key:
        return ""

    ck = _cache_key(latitude, longitude)
    now = time.monotonic()
    hit = _cache.get(ck)
    if hit and now < hit[0]:
        return hit[1]

    lang = (os.environ.get("GEOAPIFY_LANG") or "en").strip() or "en"
    limit = int(os.environ.get("GEOAPIFY_REVERSE_LIMIT", "1") or "1")
    limit = max(1, min(5, limit))

    params = {
        "lat": latitude,
        "lon": longitude,
        "apiKey": key,
        "limit": limit,
        "lang": lang,
        "format": "json",
    }

    try:
        async with httpx.AsyncClient(timeout=18.0) as client:
            r = await client.get(GEOAPIFY_REVERSE_URL, params=params)
        if r.status_code >= 400:
            logger.warning("Geoapify reverse HTTP %s — %s", r.status_code, r.text[:300])
            return ""
        data = r.json()
    except Exception as e:
        logger.warning("Geoapify reverse failed: %s", e)
        return ""

    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list) or not feats:
        return ""

    feat0 = feats[0]
    props = feat0.get("properties") if isinstance(feat0, dict) else None
    if not isinstance(props, dict):
        return ""

    formatted = _truncate(str(props.get("formatted") or ""), 520)
    lines = _lines_from_properties(props)

    chunks = [
        "## Approximate place from your GPS (Geoapify reverse geocoding)",
        "Commercial geodata — still **approximate**; say roughly where they are, not survey-grade.",
        f"**Your device coordinates:** `{latitude:.6f}`, `{longitude:.6f}`",
    ]
    if formatted:
        chunks.append(f"**Formatted address:** {formatted}")
    if lines:
        chunks.append("**Structured fields:**\n" + "\n".join(lines))
    chunks.append(
        "Answer with city/region/country first, then street if helpful; mention uncertainty for blind users."
    )
    text = "\n\n".join(chunks)
    _cache[ck] = (now + CACHE_TTL_SEC, text)
    return text

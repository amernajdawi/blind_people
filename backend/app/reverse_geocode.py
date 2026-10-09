"""Pick reverse-geocoding: Geoapify (if API key), else Photon; optional Nominatim."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _falsy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("0", "false", "no", "off")


async def fetch_place_context(latitude: float, longitude: float) -> str:
    from app.maps_platform import facade as gmaps_facade
    from app.maps_platform.config import is_platform_enabled

    if is_platform_enabled():
        g = await gmaps_facade.reverse_geocode_markdown(latitude, longitude)
        if g.strip():
            return g

    from app import geoapify_geocode, nominatim_geocode, photon_geocode

    provider = os.environ.get("REVERSE_GEO_PROVIDER", "photon").strip().lower()
    geo_key = os.environ.get("GEOAPIFY_API_KEY", "").strip()

    if provider == "nominatim":
        return await nominatim_geocode.fetch_place_context(latitude, longitude)

    if provider == "geoapify" and not geo_key:
        logger.warning("REVERSE_GEO_PROVIDER=geoapify but GEOAPIFY_API_KEY is unset")
        return ""

    if geo_key:
        g = await geoapify_geocode.fetch_place_context(latitude, longitude, geo_key)
        if g.strip():
            return g
        if provider == "geoapify" and _falsy_env("REVERSE_GEO_FALLBACK_PHOTON"):
            return ""

    text = await photon_geocode.fetch_place_context(latitude, longitude)
    if text.strip():
        return text
    if _truthy_env("REVERSE_GEO_FALLBACK_NOMINATIM"):
        return await nominatim_geocode.fetch_place_context(latitude, longitude)
    return ""

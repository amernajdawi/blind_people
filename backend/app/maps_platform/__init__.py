"""Google Maps Platform clients (Routes, Places, Geocoding, Weather).

All requests use ``GOOGLE_MAPS_API_KEY`` on the server only. Enable with
``GOOGLE_MAPS_PLATFORM_ENABLED=true``. See ``.env.example``.
"""

from __future__ import annotations

from app.maps_platform.config import is_platform_enabled
from app.maps_platform.facade import (
    fetch_nearby_places_markdown,
    reverse_geocode_markdown,
    weather_current_markdown,
)

__all__ = [
    "is_platform_enabled",
    "reverse_geocode_markdown",
    "weather_current_markdown",
    "fetch_nearby_places_markdown",
]

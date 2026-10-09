"""High-level markdown helpers used by chat grounding and geocoding bridge."""

from __future__ import annotations

from app.maps_platform.config import is_platform_enabled
from app.maps_platform.geocoding import reverse_geocode_components
from app.maps_platform.places_nearby import fetch_nearby_markdown
from app.maps_platform.weather import fetch_current_conditions_markdown


async def reverse_geocode_markdown(
    latitude: float,
    longitude: float,
    *,
    language: str | None = None,
) -> str:
    if not is_platform_enabled():
        return ""
    data = await reverse_geocode_components(latitude, longitude, language=language)
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return ""
    r0 = results[0]
    if not isinstance(r0, dict):
        return ""

    formatted = str(r0.get("formatted_address") or "").strip()
    parts = r0.get("address_components")
    comp_lines: list[str] = []
    if isinstance(parts, list):
        for c in parts[:12]:
            if not isinstance(c, dict):
                continue
            long_name = str(c.get("long_name") or "").strip()
            types = c.get("types")
            if not long_name or not isinstance(types, list):
                continue
            comp_lines.append(f"- **{types[0]}**: {long_name}")

    chunks = [
        "## Approximate place from your GPS (Google Geocoding API)",
        f"**Your device coordinates:** `{latitude:.6f}`, `{longitude:.6f}`",
    ]
    if formatted:
        chunks.append(f"**Formatted address:** {formatted}")
    if comp_lines:
        chunks.append("**Components:**\n" + "\n".join(comp_lines))
    chunks.append(
        "Treat as **approximate**; GPS error and map offsets can put the pin on the wrong side of a building."
    )
    return "\n\n".join(chunks)


async def weather_current_markdown(latitude: float, longitude: float) -> str:
    if not is_platform_enabled():
        return ""
    return await fetch_current_conditions_markdown(latitude, longitude)


async def fetch_nearby_places_markdown(latitude: float, longitude: float) -> str:
    if not is_platform_enabled():
        return ""
    return await fetch_nearby_markdown(latitude, longitude)

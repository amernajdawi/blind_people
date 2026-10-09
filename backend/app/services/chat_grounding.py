"""System-prompt additions: optional GPS / weather context, live web excerpts, runtime clock."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app import open_meteo_weather, reverse_geocode, web_search
from app.prompt_loader import load_prompt

OPTIONAL_GPS_CONTEXT_HEADER = load_prompt("optional_gps_context_header")
RETRIEVED_WEB_EXCERPTS_INTRO = load_prompt("retrieved_web_excerpts_intro")
_RUNTIME_DATETIME_TEMPLATE = load_prompt("runtime_datetime_context")
_GPS_ACCURACY_FOOTER = load_prompt("gps_horizontal_accuracy_footer")
CHAT_ADDL_INSTRUCTIONS_HEADER = load_prompt("chat_additional_instructions_header")


def runtime_datetime_context_block() -> str:
    now_local = datetime.now().astimezone()
    now_utc = now_local.astimezone(timezone.utc)
    return _RUNTIME_DATETIME_TEMPLATE.format(
        local_human=now_local.strftime("%A, %B %d, %Y %H:%M:%S %Z"),
        local_iso=now_local.isoformat(timespec="seconds"),
        utc_human=now_utc.strftime("%A, %B %d, %Y %H:%M:%S UTC"),
        utc_iso=now_utc.isoformat(timespec="seconds"),
    )


def append_runtime_datetime_context(system_text: str | None) -> str:
    base = (system_text or "").strip()
    dt_ctx = runtime_datetime_context_block()
    return f"{base}\n\n{dt_ctx}" if base else dt_ctx


_INTENT_PLACES_GROUNDING_FOOTER = (
    "The blocks above are **live Google data** from this request. **Read the directions aloud** "
    "step by step for users who rely on voice. Do **not** say there is a “technical issue” with "
    "routing or tell them to open a maps app **unless** the directions block above clearly shows "
    "an API error and no steps. You may add one short safety note (verify with signage, watch traffic)."
)


async def grounding_suffix_for_text_chat(
    *,
    last_user_text: str,
    latitude: float | None,
    longitude: float | None,
    location_accuracy_m: float | None,
) -> tuple[str, bool]:
    """
    Optional system suffix: device GPS + weather, **live** web snippets for the last user turn.
    Returns ``(suffix, prefetched_web_ok)``.
    """
    coords_ok = (
        latitude is not None
        and longitude is not None
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )
    chunks: list[str] = []
    prefetched_web_ok = False

    place_md = ""
    wx = ""
    nearby_md = ""
    intent_md = ""
    if coords_ok:
        lat, lon = latitude, longitude

        from app.maps_platform.config import (
            chat_nearby_places_enabled,
            grounding_intent_navigation_query,
            grounding_intent_place_types_for_user_text,
            is_platform_enabled,
            nearby_max_result_count,
            tool_nearby_search_radius_m,
        )
        from app.maps_platform import facade as gmaps_facade
        from app.maps_platform.intent_directions import markdown_routes_for_intent_first_place
        from app.maps_platform.places_nearby import nearby_results_to_markdown, search_nearby_raw

        place_md, wx = await asyncio.gather(
            reverse_geocode.fetch_place_context(lat, lon),
            open_meteo_weather.fetch_local_weather_context(lat, lon),
        )
        if is_platform_enabled() and chat_nearby_places_enabled():
            nearby_md = await gmaps_facade.fetch_nearby_places_markdown(lat, lon)

        if is_platform_enabled():
            intent_types = grounding_intent_place_types_for_user_text(last_user_text)
            if intent_types:
                rad = tool_nearby_search_radius_m()
                data = await search_nearby_raw(
                    lat,
                    lon,
                    radius_m=rad,
                    max_results=nearby_max_result_count(),
                    included_types=intent_types,
                )
                block = nearby_results_to_markdown(lat, lon, rad, data).strip()
                if block:
                    route_md = ""
                    if grounding_intent_navigation_query(last_user_text):
                        route_md = (
                            await markdown_routes_for_intent_first_place(
                                latitude=lat,
                                longitude=lon,
                                search_nearby_json=data,
                            )
                        ).strip()
                    intent_md = "## Nearby venues matched to the user’s request (Google Places)\n\n" + block
                    if route_md:
                        intent_md += "\n\n---\n\n" + route_md
                    intent_md += "\n\n" + _INTENT_PLACES_GROUNDING_FOOTER

        parts = [p for p in (place_md.strip(), wx.strip(), nearby_md.strip(), intent_md.strip()) if p]
        if parts:
            chunks.append(
                OPTIONAL_GPS_CONTEXT_HEADER
                + "\n\n"
                + "\n\n---\n\n".join(parts)
            )

    if web_search.should_fetch_web_when_grounding(last_user_text):
        query_slice = web_search.slice_for_grounding_web_query(last_user_text)
        search_q = web_search.build_grounding_search_query(
            query_slice,
            latitude=latitude if coords_ok else None,
            longitude=longitude if coords_ok else None,
            place_context_markdown=place_md if coords_ok else None,
        )
        ctx = (await web_search.fetch_web_snippets(search_q)).strip()
        if ctx:
            prefetched_web_ok = True
            provider = web_search.search_provider_hint()
            chunks.append(
                (
                    RETRIEVED_WEB_EXCERPTS_INTRO.format(provider=provider) + "\n\n" + ctx
                ).strip()
            )

    if chunks and coords_ok and location_accuracy_m is not None:
        chunks[-1] = (
            chunks[-1] + "\n\n" + _GPS_ACCURACY_FOOTER.format(meters=location_accuracy_m)
        )

    if not chunks:
        return "", False
    return "\n\n---\n\n".join(chunks), prefetched_web_ok

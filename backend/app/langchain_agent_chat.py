"""LangChain agent (ChatOllama + tools) for text chat — optional path for /api/llm/chat."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from typing import Any

from app.core.config import apply_langsmith_env_safety

apply_langsmith_env_safety()
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama

from app import ollama_client, open_meteo_weather, reverse_geocode, web_search
from app.maps_platform.config import (
    is_platform_enabled,
    nearby_max_result_count,
    tool_nearby_search_radius_m,
    waypoint_address_max_len,
)
from app.maps_platform.places_nearby import nearby_results_to_markdown, search_nearby_raw
from app.maps_platform.routes_compute import compute_routes_raw, routes_response_to_markdown
from app.prompt_loader import load_prompt

logger = logging.getLogger(__name__)

TOOL_GUIDANCE = load_prompt("langchain_tool_guidance")

_PLACE_TYPE_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$", re.I)
_ALLOWED_ROUTES_MODES = frozenset(
    {"WALK", "DRIVE", "TRANSIT", "BICYCLE", "TWO_WHEELER"}
)


def _parse_place_types_param(s: str) -> list[str]:
    out: list[str] = []
    for t in (s or "").split(","):
        u = t.strip().lower()
        if u and _PLACE_TYPE_TOKEN.match(u):
            out.append(u)
    return out[:10]


def langchain_chat_enabled(*, body_flag: bool) -> bool:
    if body_flag:
        return True
    v = os.environ.get("USE_LANGCHAIN_CHAT")
    if v is None or not str(v).strip():
        return False
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def _chat_model() -> ChatOllama:
    key = ollama_client.OLLAMA_API_KEY
    host = ollama_client.OLLAMA_HOST.rstrip("/")
    temp = float(
        os.environ.get(
            "OLLAMA_CHAT_TEMPERATURE",
            os.environ.get("OLLAMA_TEMPERATURE", "0.78"),
        )
    )
    top_p = float(
        os.environ.get(
            "OLLAMA_CHAT_TOP_P",
            os.environ.get("OLLAMA_TOP_P", "0.92"),
        )
    )
    n_predict_str = (
        os.environ.get("OLLAMA_CHAT_NUM_PREDICT", "").strip()
        or os.environ.get("OLLAMA_CHAT_MAX_TOKENS", "").strip()
        or os.environ.get("OLLAMA_MAX_TOKENS", "1536")
    )
    n_predict = int(n_predict_str)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    return ChatOllama(
        model=ollama_client.OLLAMA_MODEL,
        base_url=host,
        temperature=temp,
        top_p=top_p,
        num_predict=n_predict,
        client_kwargs={"headers": headers},
    )


def _build_tools(
    *,
    latitude: float | None,
    longitude: float | None,
    include_web_search: bool,
) -> list[Any]:
    tools: list[Any] = []

    coords_ok = (
        latitude is not None
        and longitude is not None
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )
    lat = lon = 0.0
    if coords_ok:
        lat, lon = float(latitude), float(longitude)

    if include_web_search:

        @tool
        async def search_web(query: str) -> str:
            """Search the web for fresh snippets (news, sports, stocks, events, map-style POI when GPS is shared, weather).
            Pass one short focused query; proximity- and local-weather phrasing is rewritten with device coordinates when helpful."""
            q = (query or "").strip()[:400]
            if not q:
                return "Empty query."
            run_q = web_search.build_grounding_search_query(
                q,
                latitude=lat if coords_ok else None,
                longitude=lon if coords_ok else None,
            )
            return await web_search.fetch_web_snippets(run_q)

        tools.append(search_web)

    if coords_ok:

        @tool
        async def local_weather_now() -> str:
            """Current weather at the user's shared device GPS (Open-Meteo). Use for temperature, rain, conditions here."""
            return await open_meteo_weather.fetch_local_weather_context(lat, lon)

        @tool
        async def approximate_place_name() -> str:
            """Rough address / city / country from device GPS (Photon / OSM). Use when the user asks where they are."""
            return await reverse_geocode.fetch_place_context(lat, lon)

        tools.extend([local_weather_now, approximate_place_name])

    if coords_ok and is_platform_enabled():
        p_lat, p_lon = float(latitude), float(longitude)

        @tool
        async def google_maps_nearby_pois(google_place_types: str) -> str:
            """Nearby places from the user's GPS using Google Places (distance-ranked).

            Use for closest gas station, supermarket, mall, café, etc. Pass **Google Place types** (snake_case), not free text.
            Examples: ``gas_station``; ``shopping_mall``; ``supermarket, cafe``. Up to 10 types, comma-separated.
            See Google "Place types" in Places API documentation."""
            types = _parse_place_types_param(google_place_types)
            if not types:
                return (
                    "No valid place types. Use comma-separated API types, e.g. gas_station, shopping_mall, supermarket."
                )
            radius = tool_nearby_search_radius_m()
            data = await search_nearby_raw(
                p_lat,
                p_lon,
                radius_m=radius,
                max_results=nearby_max_result_count(),
                included_types=types,
            )
            md = nearby_results_to_markdown(p_lat, p_lon, radius, data)
            if md.strip():
                return md
            return "(No nearby results for those types — try other types or a different query.)"

        @tool
        async def google_maps_directions(destination: str, travel_mode: str = "TRANSIT") -> str:
            """How to get from the user's shared GPS to a destination using Google Routes (steps, transit lines).

            Use when the user asks how to go somewhere, which bus or train, walking directions, or trip time.
            destination: address or venue name (e.g. ``Plus City, Pasching`` or ``Hauptplatz, Linz``).
            travel_mode: WALK, DRIVE, TRANSIT, BICYCLE, or TWO_WHEELER. Default TRANSIT (public transport; includes walking to stops).
            If the result says no route or an API error, call again with **WALK** (same destination)."""
            dest = (destination or "").strip()
            if not dest:
                return "No destination provided."
            mode = (travel_mode or "TRANSIT").strip().upper()
            if mode not in _ALLOWED_ROUTES_MODES:
                mode = "TRANSIT"
            dep: str | None = None
            if mode == "TRANSIT":
                dep = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            data = await compute_routes_raw(
                origin={"latitude": p_lat, "longitude": p_lon},
                destination={"address": dest[: waypoint_address_max_len()]},
                travel_mode=mode,
                departure_time_rfc3339=dep,
            )
            return routes_response_to_markdown(data)

        tools.extend([google_maps_nearby_pois, google_maps_directions])

    return tools


def _messages_from_history(msgs: Sequence[dict[str, str]]) -> list[Any]:
    out: list[Any] = []
    for m in msgs:
        role = (m.get("role") or "").strip()
        content = m.get("content") or ""
        if role == "user":
            out.append(HumanMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
    return out


async def run_langchain_chat_stream(
    *,
    api_messages: list[dict[str, str]],
    system_prompt: str | None,
    latitude: float | None,
    longitude: float | None,
    include_web_search: bool,
) -> AsyncIterator[str]:
    """Yield sequential chunks (after full agent run) for NDJSON compatibility with the existing UI."""
    tools = _build_tools(
        latitude=latitude,
        longitude=longitude,
        include_web_search=include_web_search,
    )
    sys_parts = [(system_prompt or "").strip()]
    if tools:
        sys_parts.append(TOOL_GUIDANCE.strip())
    sys_full = "\n\n".join(p for p in sys_parts if p).strip() or None

    model = _chat_model()
    agent = create_agent(model, tools if tools else None, system_prompt=sys_full)

    lc_messages = _messages_from_history(api_messages)
    inp = {"messages": lc_messages}

    try:
        final_state = await agent.ainvoke(inp)
    except Exception:
        logger.exception("LangChain agent invoke failed")
        raise

    msgs_out = final_state.get("messages") or []
    text = ""
    if msgs_out:
        last = msgs_out[-1]
        raw = getattr(last, "content", None)
        text = ollama_client.clean_gemma_reply(str(raw) if raw is not None else "")
    step = 56
    for i in range(0, len(text), step):
        yield text[i : i + step]


async def run_langchain_chat_blocking(
    *,
    api_messages: list[dict[str, str]],
    system_prompt: str | None,
    latitude: float | None,
    longitude: float | None,
    include_web_search: bool,
) -> str:
    tools = _build_tools(
        latitude=latitude,
        longitude=longitude,
        include_web_search=include_web_search,
    )
    sys_parts = [(system_prompt or "").strip()]
    if tools:
        sys_parts.append(TOOL_GUIDANCE.strip())
    sys_full = "\n\n".join(p for p in sys_parts if p).strip() or None

    model = _chat_model()
    agent = create_agent(model, tools if tools else None, system_prompt=sys_full)
    lc_messages = _messages_from_history(api_messages)
    final_state = await agent.ainvoke({"messages": lc_messages})
    msgs_out = final_state.get("messages") or []
    if not msgs_out:
        return ""
    last = msgs_out[-1]
    raw = getattr(last, "content", None)
    return ollama_client.clean_gemma_reply(str(raw) if raw is not None else "")

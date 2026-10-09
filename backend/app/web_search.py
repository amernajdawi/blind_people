"""
Web context for grounding text replies (agent-style «tool»: web_search).

**Tavily (recommended):** set `TAVILY_API_KEY` — same secret as Tavily dashboard / Remote MCP URL.
Uses `POST https://api.tavily.com/search` with optional concise answer + result snippets.

**Fallback:** `ddgs` / `duckduckgo_search` only when no Tavily key, or when
`TAVILY_FALLBACK_DDGS=true` and Tavily returns no results.

Remote MCP URL (Cursor, etc.): `https://mcp.tavily.com/mcp/?tavilyApiKey=<your_key>`
This backend does not speak MCP wire protocol; it calls Tavily HTTPS directly.

Disable entirely: ASSISTANT_WEB_SEARCH=off
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import httpx

from app.prompt_loader import load_prompt

logger = logging.getLogger(__name__)

MAX_SNIPPETS = int(os.environ.get("WEB_SEARCH_MAX_RESULTS", "5"))
MAX_CONTEXT_CHARS = int(os.environ.get("WEB_SEARCH_MAX_CHARS", "5500"))

# Tavily REST (not MCP transport — same API key as mcp.tavily.com Remote MCP URLs)
DEFAULT_TAVILY_DEPTH = os.environ.get("TAVILY_SEARCH_DEPTH", "fast").strip().lower()
# basic | advanced | fast | ultra-fast
TAVILY_INCLUDE_ANSWER = os.environ.get("TAVILY_INCLUDE_ANSWER", "basic").strip().lower()
# off | basic | advanced | true
TAVILY_FALLBACK_DDGS = os.environ.get("TAVILY_FALLBACK_DDGS", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

NEWS_TOPIC_HINT = re.compile(
    r"\b(news|headlines|breaking|election|minister|government|sport|scores?|won the|playoff)\b",
    re.I,
)

_web_disabled = os.environ.get("ASSISTANT_WEB_SEARCH", "").strip().lower() in (
    "0",
    "false",
    "no",
    "off",
)


def agent_web_tool_allowed() -> bool:
    """Whether LangChain (or other agents) may expose a live search tool — false when ASSISTANT_WEB_SEARCH=off."""
    return not _web_disabled

WEATHER_LOCATION_HINT = re.compile(
    r"\b("
    r"weather|forecast|temperature|raining|snow|rains|celsius|fahrenheit|humidity|"
    r"how cold|how hot|degrees\b|outside\b.*\b(cold|hot|warm|cool)|hot out|cold out"
    r")\b",
    re.I,
)


def heuristic_local_weather_query(user_text: str) -> bool:
    """True when the user likely wants weather **for their area** (use GPS + Open-Meteo when coords sent)."""
    return bool(WEATHER_LOCATION_HINT.search(user_text or ""))


WHERE_AM_I_HINT = re.compile(
    r"\b("
    r"where am i|where are we|where'?s my location|where is my location|what is my location|\bmy location\b|what'?s my location\b|which city am i|what city am i in|"
    r"what town am i in|what country am i|exactly where am i|where exactly am i|pinpoint my location|"
    r"what address am i|what street am i|reverse\s*-?\s*geocode|name this place\b"
    r")\b",
    re.I,
)


def heuristic_where_am_i_query(user_text: str) -> bool:
    """True when the user asks which place / city / address corresponds to their position."""
    return bool(WHERE_AM_I_HINT.search(user_text or ""))


# Spatial / deictic phrasing: user means “relative to my position in the world”, not a fixed list of business types.
PROXIMITY_LOCAL_HINT = re.compile(
    r"\b("
    r"nearest|closest|nearby|near me|near us|near here|around here|around me|"
    r"to me\b|for me\b|from here\b|"
    r"in this area|in my area|walking distance|"
    r"how far (is|are)|"
    r"what'?s (the )?closest|which (is |are )?the (closest|nearest)|"
    r"where'?s (the )?(nearest|closest)|"
    r"where (is|are) (the )?(nearest|closest)|"
    r"locate (the )?(nearest|closest)|find (the )?(nearest|closest)"
    r")\b",
    re.I,
)

# Do not pin the *web search query* to device GPS when the user clearly means the camera/view, not map POIs.
PROXIMITY_WEB_GPS_EXCLUDE = re.compile(
    r"\b("
    r"in this (photo|picture|frame|image)|from my camera|webcam|on screen|in the frame|"
    r"what do you see|describe (what'?s )?(in front|ahead)"
    r")\b",
    re.I,
)


# Proximity questions that usually mean *inside the current space*, not map/routing POIs — skip GPS tails on web search.
PROXIMITY_INDOOR_LANDMARK_HINT = re.compile(
    r"\b("
    r"(nearest|closest)\s+(the\s+)?(door|exit|stairs|bathroom|restroom|elevator|lift|wc)|"
    r"where(\s+is|\s*'s)\s+(the\s+)?(door|exit|stairs|bathroom|restroom|elevator|lift|kitchen)\b|"
    r"find (the\s+)?(door|exit|keys|wallet|remote|charger|bathroom|restroom)\b"
    r")\b",
    re.I,
)


def heuristic_proximity_local_web_query(user_text: str) -> bool:
    """
    Likely “near me in the real world” intent (nearest/closest/etc.), suitable for adding coordinates
    to a search query. Not tied to any specific category of place.
    """
    raw = user_text or ""
    if not raw.strip() or PROXIMITY_WEB_GPS_EXCLUDE.search(raw):
        return False
    if PROXIMITY_INDOOR_LANDMARK_HINT.search(raw):
        return False
    return bool(PROXIMITY_LOCAL_HINT.search(raw))


def should_enrich_web_query_with_device_coordinates(user_text: str) -> bool:
    """True when ``build_grounding_search_query`` will append GPS context (weather-at-device or proximity)."""
    raw = user_text or ""
    if not (raw or "").strip():
        return False
    return bool(
        heuristic_local_weather_query(raw) or heuristic_proximity_local_web_query(raw)
    )


_LABEL_VALUE_LINE = re.compile(r"- \*\*([^*]+)\*\*:\s*([^\n]+)")


def geo_search_hint_from_place_markdown(place: str, *, max_len: int = 100) -> str:
    """
    Pull city / region / country labels from our reverse-geocode markdown so search queries
    get human place names alongside coordinates (better POI ranking than coords alone).
    """
    if not (place or "").strip():
        return ""
    preferred = (
        "Place name",
        "City",
        "Town",
        "Village",
        "Municipality",
        "Locality",
        "District",
        "Street / road",
        "County",
        "State / region",
        "Country",
    )
    preferred_lc = {p.lower() for p in preferred}
    bits: list[str] = []
    seen_lc: set[str] = set()
    for m in _LABEL_VALUE_LINE.finditer(place):
        label = (m.group(1) or "").strip()
        val = (m.group(2) or "").strip()
        if not val or label.lower() not in preferred_lc:
            continue
        vlow = val.lower()
        if vlow in seen_lc:
            continue
        seen_lc.add(vlow)
        bits.append(val)
    if not bits:
        sm = re.search(r"\*\*Summary:\*\*\s*([^\n]+)", place, re.I)
        if sm:
            s = sm.group(1).strip()
            if s:
                bits.append(s[: max_len - 3].rstrip() + "…" if len(s) > max_len else s)
    out = ", ".join(bits)
    return out[:max_len].rstrip(", ")


def slice_for_grounding_web_query(raw: str) -> str:
    """
    Voice transcripts often concatenate unrelated clauses with *and*. Prefer the clause that
    carries an actionable local/web intent (proximity, weather, explicit web search, where-am-I).
    """
    text = (raw or "").strip()
    if len(text) < 5:
        return text
    chunks = re.split(r"\s+and\s+", text, flags=re.I)
    if len(chunks) < 2:
        return text

    def _actionable_clause(t: str) -> bool:
        s = (t or "").strip()
        if len(s) < 4:
            return False
        if PROXIMITY_WEB_GPS_EXCLUDE.search(s):
            return False
        return bool(
            heuristic_proximity_local_web_query(s)
            or heuristic_local_weather_query(s)
            or heuristic_web_lookup_intent(s)
            or heuristic_where_am_i_query(s)
        )

    for piece in reversed(chunks):
        if _actionable_clause(piece):
            return piece.strip()
    return text


def build_grounding_search_query(
    user_text: str,
    *,
    latitude: float | None,
    longitude: float | None,
    place_context_markdown: str | None = None,
) -> str:
    """
    Shape the user’s last message into a search query. When coordinates are provided, adds:
    - forecast context for local-weather wording, or
    - numeric lat/lon tail for proximity questions so engines return POIs near the device.

    Uses linguistic cues only (no fixed menus of business types).
    """
    q = (user_text or "").strip()
    if len(q) < 2:
        return q
    if latitude is None or longitude is None:
        return q
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return q
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return q

    if heuristic_local_weather_query(q):
        return f"{q} current weather forecast latitude {lat:.5f} longitude {lon:.5f}"

    if heuristic_proximity_local_web_query(q):
        hint = geo_search_hint_from_place_markdown(place_context_markdown or "").strip()
        geo = f"{lat:.5f} {lon:.5f}"
        if hint:
            merged = f"{q} {hint} {geo} street address map directions hours".strip()
        else:
            merged = f"{q} near {geo} POI street address opening hours".strip()
        return merged[:400]

    return q


# Explicit “use the web / search engine” intent — never answer from the webcam alone.
WEB_LOOKUP_HINT = re.compile(
    r"\b("
    r"(?:can|could)\s+you\s+(?:please\s+)?(?:check|google|search)|"
    r"(?:please\s+)?(?:check|test)\s+(?:my\s+|the\s+)?internet\b|"
    r"\bgoogle\b|(?:search|ask)\s+google|google\s+(?:if|whether|for|about)|"
    r"(?:look|search)\s+(?:it|this|that|that\s+up)\s+(?:up\s+)?(?:on\s+)?(?:google|online)|"
    r"(?:web\s+search|search\s+(?:the\s+)?web|search\s+online)|"
    r"\bsearch\s+for\b|\bduckduckgo\b|\bbing\b"
    r")\b",
    re.I,
)


def heuristic_web_lookup_intent(user_text: str) -> bool:
    """True when the user wants search-engine / web facts — Modality A + optional Tavily, not vision."""
    return bool(WEB_LOOKUP_HINT.search(user_text or ""))


def heuristic_needs_web(user_text: str) -> bool:
    """Backward-compatible name: same decision as :func:`should_fetch_web_when_grounding` (no keyword lists)."""
    return should_fetch_web_when_grounding(user_text)


def should_fetch_web_when_grounding(user_text: str) -> bool:
    """
    Client sent ``web_grounding=true`` (e.g. Modality A). When this returns True, the server performs
    a **live** Tavily/ddgs search for the latest user message — no keyword gating on topic.

    - Disabled when ``ASSISTANT_WEB_SEARCH=off``.
    - Skips empty / single-character queries.
    - Set ``WEB_SEARCH_HEURISTIC_ONLY=true`` to skip this **server prefetch**; use LangChain ``search_web`` only.
    """
    if _web_disabled:
        return False
    q = (user_text or "").strip()
    if len(q) < 2:
        return False
    if os.environ.get("WEB_SEARCH_HEURISTIC_ONLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    return True


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)].rstrip() + "…"


def _duckduckgo_sync(query: str) -> list[dict[str, Any]]:
    DDGS = None
    try:
        from ddgs import DDGS as _DDGS  # preferred package (renamed fork)
        DDGS = _DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS as _DDGS2

            DDGS = _DDGS2
        except ImportError:
            logger.warning("Install ddgs (`uv add ddgs` or `pip install ddgs`) for web grounding")
            return []

    hits: list[dict[str, Any]] = []
    try:
        with DDGS() as ddgs:
            rows = ddgs.text(query, max_results=MAX_SNIPPETS)
            for r in rows:
                title = _truncate(str(r.get("title") or ""), 260)
                body = _truncate(str(r.get("body") or ""), 800)
                href = str(r.get("href") or r.get("url") or "").strip()
                if title or body:
                    hits.append({"title": title, "body": body, "url": href})
    except Exception as e:
        logger.warning("DuckDuckGo-style search failed: %s", e)
    return hits


def _tavily_depth_value() -> str:
    v = DEFAULT_TAVILY_DEPTH or "fast"
    if v not in ("advanced", "basic", "fast", "ultra-fast"):
        return "fast"
    return v


def _tavily_answer_flag() -> bool | str:
    if TAVILY_INCLUDE_ANSWER in ("", "false", "0", "no", "off"):
        return False
    if TAVILY_INCLUDE_ANSWER == "advanced":
        return "advanced"
    # basic, true, on
    return True


async def _tavily_async(query: str, key: str) -> tuple[list[dict[str, Any]], str]:
    """Call Tavily Search API. Returns (result rows, optional short answer text)."""
    url = "https://api.tavily.com/search"
    topic = "news" if NEWS_TOPIC_HINT.search(query) else "general"
    payload: dict[str, Any] = {
        "api_key": key,
        "query": query[:400],
        "max_results": MAX_SNIPPETS,
        "search_depth": _tavily_depth_value(),
        "topic": topic,
    }
    af = _tavily_answer_flag()
    if af:
        payload["include_answer"] = af

    try:
        async with httpx.AsyncClient(timeout=55.0) as client:
            r = await client.post(url, json=payload)
        if r.status_code >= 400:
            logger.warning("Tavily HTTP %s — %s", r.status_code, r.text[:500])
            return [], ""
        data = r.json()
        brief = ""
        if isinstance(data.get("answer"), str) and data["answer"].strip():
            brief = data["answer"].strip()
        hits: list[dict[str, Any]] = []
        for item in data.get("results") or []:
            title = _truncate(item.get("title") or "", 260)
            body = _truncate(item.get("content") or item.get("snippet") or "", 900)
            href = str(item.get("url") or "").strip()
            if title or body:
                hits.append({"title": title, "body": body, "url": href})
        return hits, brief
    except Exception as e:
        logger.warning("Tavily search failed: %s", e)
        return [], ""


def format_snippets(hits: list[dict[str, Any]], *, tavily_answer: str = "") -> str:
    pre = ""
    if (tavily_answer or "").strip():
        brief = _truncate(tavily_answer.strip(), 1200)
        pre = load_prompt("web_search_tavily_answer_block").replace("{answer}", brief)
    chunks: list[str] = []
    for i, h in enumerate(hits, start=1):
        line = (
            f"{i}. {_truncate(h.get('title',''),260)}\n{_truncate(h.get('body',''),800)}"
        )
        url = str(h.get("url") or "").strip()
        if url:
            line += f"\n   URL: {url}"
        chunks.append(line)
    body = "\n\n".join(chunks).strip()
    text = (pre + body).strip()
    return _truncate(text, MAX_CONTEXT_CHARS)


def search_provider_hint() -> str:
    """For /api/health: which backend path is preferred."""
    if _web_disabled:
        return "off"
    if os.environ.get("TAVILY_API_KEY", "").strip():
        return "tavily"
    try:
        import ddgs  # noqa: F401

        return "ddgs"
    except ImportError:
        pass
    try:
        import duckduckgo_search  # noqa: F401

        return "duckduckgo_search"
    except ImportError:
        pass
    return "none"


async def fetch_web_snippets(query: str) -> str:
    if _web_disabled:
        return ""
    q = (query or "").strip()
    if not q:
        return ""

    loop = asyncio.get_running_loop()
    key = os.environ.get("TAVILY_API_KEY", "").strip()

    hits: list[dict[str, Any]] = []
    brief = ""

    if key:
        hits, brief = await _tavily_async(q, key)
        if hits or brief.strip():
            return format_snippets(hits, tavily_answer=brief)
        # Empty Tavily result
        if TAVILY_FALLBACK_DDGS:
            hits = await loop.run_in_executor(None, lambda: _duckduckgo_sync(q[:400]))
            return format_snippets(hits, tavily_answer="")
        return ""

    hits = await loop.run_in_executor(None, lambda: _duckduckgo_sync(q[:400]))
    return format_snippets(hits, tavily_answer="")


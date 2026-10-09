"""Google Maps Platform — all tunables come from environment variables.

Official service URLs may be overridden for mocks or proxies; defaults match Google documentation.
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)


def _truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _optional_trimmed(name: str) -> str | None:
    v = os.environ.get(name)
    if v is None:
        return None
    s = v.strip()
    return s if s else None


def google_maps_api_key() -> str:
    return (os.environ.get("GOOGLE_MAPS_API_KEY") or "").strip()


def is_platform_enabled() -> bool:
    return bool(google_maps_api_key()) and _truthy("GOOGLE_MAPS_PLATFORM_ENABLED", "false")


def geocode_base_url() -> str:
    """Geocoding API endpoint (REST)."""
    return (
        _optional_trimmed("GOOGLE_MAPS_GEOCODE_URL")
        or "https://maps.googleapis.com/maps/api/geocode/json"
    )


def routes_compute_url() -> str:
    return (
        _optional_trimmed("GOOGLE_MAPS_ROUTES_COMPUTE_URL")
        or "https://routes.googleapis.com/directions/v2:computeRoutes"
    )


def places_search_nearby_url() -> str:
    return (
        _optional_trimmed("GOOGLE_MAPS_PLACES_SEARCH_NEARBY_URL")
        or "https://places.googleapis.com/v1/places:searchNearby"
    )


def weather_current_lookup_url() -> str:
    return (
        _optional_trimmed("GOOGLE_MAPS_WEATHER_CURRENT_URL")
        or "https://weather.googleapis.com/v1/currentConditions:lookup"
    )


def geolocate_url() -> str:
    return (
        _optional_trimmed("GOOGLE_MAPS_GEOLOCATE_URL")
        or "https://www.googleapis.com/geolocation/v1/geolocate"
    )


def language_code() -> str | None:
    """
    Optional BCP-47 / API language (Geocoding, Routes).
    If unset, those query/body fields are omitted and Google uses its default.
    """
    s = _optional_trimmed("GOOGLE_MAPS_LANGUAGE_CODE")
    if not s:
        return None
    return s[:12]


def region_code() -> str | None:
    """
    Optional CLDR region bias (e.g. ``AT``) for Routes and forward Geocoding.
    If unset, ``region`` / ``regionCode`` is omitted where optional.
    """
    s = _optional_trimmed("GOOGLE_MAPS_REGION_CODE")
    if not s:
        return None
    return s.upper()[:2]


def http_timeout_seconds(default: float = 25.0) -> float:
    try:
        return float(os.environ.get("GOOGLE_MAPS_HTTP_TIMEOUT_SEC", str(default)))
    except ValueError:
        return default


def http_timeout_routes_seconds() -> float:
    try:
        return float(os.environ.get("GOOGLE_MAPS_ROUTES_HTTP_TIMEOUT_SEC", "40"))
    except ValueError:
        return 40.0


def nearby_search_radius_m() -> float:
    try:
        return max(20.0, min(500.0, float(os.environ.get("GOOGLE_MAPS_NEARBY_RADIUS_M", "120"))))
    except ValueError:
        return 120.0


def tool_nearby_search_radius_m() -> float:
    """
    Radius for on-demand nearby search (e.g. LangChain tool). Places API allows up to 50_000 m.
    """
    try:
        return max(50.0, min(50_000.0, float(os.environ.get("GOOGLE_MAPS_TOOL_NEARBY_RADIUS_M", "2000"))))
    except ValueError:
        return 2000.0


def nearby_max_result_count() -> int:
    try:
        return max(1, min(20, int(os.environ.get("GOOGLE_MAPS_NEARBY_MAX_RESULTS", "10"))))
    except ValueError:
        return 10


def nearby_markdown_max_items() -> int:
    try:
        return max(1, min(30, int(os.environ.get("GOOGLE_MAPS_NEARBY_MARKDOWN_MAX_ITEMS", "12"))))
    except ValueError:
        return 12


def nearby_included_types() -> list[str]:
    """
    Comma-separated Place types for searchNearby (see Places API docs).
    If empty, nearby search is skipped (no silent fallback list in code).
    """
    raw = os.environ.get("GOOGLE_MAPS_NEARBY_INCLUDED_TYPES", "").strip()
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()][:20]


def places_response_field_mask() -> str:
    """Required header for Places (New). Override entirely via env."""
    custom = _optional_trimmed("GOOGLE_MAPS_PLACES_FIELD_MASK")
    if custom:
        return custom
    return ",".join(
        (
            "places.displayName",
            "places.formattedAddress",
            "places.location",
            "places.primaryType",
            "places.types",
            "places.shortFormattedAddress",
            "places.googleMapsUri",
            "places.accessibilityOptions",
        )
    )


def routes_response_field_mask() -> str:
    custom = _optional_trimmed("GOOGLE_MAPS_ROUTES_FIELD_MASK")
    if custom:
        return custom
    return ",".join(
        (
            "routes.duration",
            "routes.staticDuration",
            "routes.distanceMeters",
            "routes.localizedValues",
            "routes.polyline.encodedPolyline",
            "routes.legs.duration",
            "routes.legs.distanceMeters",
            "routes.legs.staticDuration",
            "routes.legs.steps.navigationInstruction",
            "routes.legs.steps.localizedValues",
            "routes.legs.steps.travelMode",
            "routes.legs.steps.startLocation",
            "routes.legs.steps.endLocation",
            "routes.legs.steps.transitDetails",
            "routes.legs.steps.routeLabels",
        )
    )


def chat_nearby_places_enabled() -> bool:
    return _truthy("GOOGLE_MAPS_CHAT_NEARBY", "true")


# Generic place words → Google Place types. No business names. Disabled when
# ``GOOGLE_MAPS_GROUNDING_INTENT_NEARBY_OFF=true`` or when
# ``GOOGLE_MAPS_GROUNDING_INTENT_NEARBY`` is a non-empty JSON list.
_PLACE_WORD_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"shopping\s*malls?|shopping\s*(?:centers?|centres?)|\bmalls?\b", ("shopping_mall",)),
    (r"gas\s*stations?|petrol\s*stations?|fuel\s*stations?", ("gas_station",)),
    (r"pharmacies|pharmacy|drug\s*stores?|chemists?", ("pharmacy",)),
    (r"hospitals?|\bclinics?\b", ("hospital",)),
    (r"bus\s*stops?|bus\s*stations?", ("bus_station",)),
    (r"train\s*stations?|railway\s*stations?|\btrams?\b", ("train_station",)),
    (r"\batms?\b|cash\s*machines?", ("atm",)),
    (r"supermarkets?|grocery\s*stores?|grocer(?:y|ies)", ("supermarket",)),
    (r"cafés?|cafes?|coffee\s*shops?", ("cafe",)),
    (r"restaurants?", ("restaurant",)),
    (r"\bparks?\b", ("park",)),
    (r"banks?\b", ("bank",)),
)


def _builtin_intent_nearby_rules() -> list[tuple[re.Pattern, tuple[str, ...]]]:
    """Navigation-style phrases plus a place word, in either order."""
    lead = (
        r"(?i)\b(?:take\s+me|get\s+me|bring\s+me|navigate(?:\s+me)?|i\s+want\s+to\s+go|"
        r"how\s+do\s+i\s+get|directions?(?:\s+(?:to|for))?|guide\s+me|"
        r"nearest|closest|nearby|near\s+me)\b[\s\S]{0,80}(?:"
    )
    trail = (
        r"(?i)(?:"
    )
    out: list[tuple[re.Pattern, tuple[str, ...]]] = []
    for words, types in _PLACE_WORD_TYPES:
        try:
            out.append((re.compile(lead + words + r")\b"), types))
            out.append(
                (
                    re.compile(
                        trail
                        + words
                        + r")\b[\s\S]{0,48}\b(?:nearest|closest|nearby|near\s+me|to\s+me|from\s+here)\b"
                    ),
                    types,
                )
            )
        except re.error as e:
            logger.warning("Built-in place intent regex error: %s", e)
    return out


def grounding_intent_nearby_off() -> bool:
    return _truthy("GOOGLE_MAPS_GROUNDING_INTENT_NEARBY_OFF", "false")


def grounding_intent_nearby_rules() -> list[tuple[re.Pattern, tuple[str, ...]]]:
    """
    Optional regex → Places types for chat grounding. JSON in env replaces builtins:

    ``[{"pattern": "(?i)...", "types": ["shopping_mall"]}]``

    Set to ``[]`` to disable intent prefetch entirely.
    """
    if grounding_intent_nearby_off():
        return []
    raw = _optional_trimmed("GOOGLE_MAPS_GROUNDING_INTENT_NEARBY")
    if raw is not None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "GOOGLE_MAPS_GROUNDING_INTENT_NEARBY is not valid JSON; using built-in intent rules"
            )
        else:
            if not isinstance(data, list):
                return []
            if len(data) == 0:
                return []
            rules: list[tuple[re.Pattern, tuple[str, ...]]] = []
            tok = re.compile(r"^[a-z][a-z0-9_]*$", re.I)
            for item in data:
                if not isinstance(item, dict):
                    continue
                pat = str(item.get("pattern", "")).strip()
                types_raw = item.get("types")
                if not pat or not isinstance(types_raw, list):
                    continue
                types_list: list[str] = []
                for t in types_raw:
                    s = str(t).strip().lower()
                    if s and tok.match(s):
                        types_list.append(s)
                if not types_list:
                    continue
                try:
                    rules.append((re.compile(pat), tuple(types_list)))
                except re.error as e:
                    logger.warning(
                        "Invalid GOOGLE_MAPS_GROUNDING_INTENT_NEARBY regex %s: %s", pat[:72], e
                    )
            if rules:
                return rules

    out = _builtin_intent_nearby_rules()
    return out


def grounding_intent_place_types_for_user_text(text: str) -> list[str]:
    """Place types to fetch for grounding when ``text`` matches intent rules."""
    t = (text or "").strip()
    if not t:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for pat, types in grounding_intent_nearby_rules():
        if pat.search(t):
            for typ in types:
                if typ not in seen:
                    seen.add(typ)
                    ordered.append(typ)
    return ordered


# Match “take me”, “how do I get”, “guide me”, etc. Override via GOOGLE_MAPS_GROUNDING_INTENT_NAV_REGEX.
_BUILTIN_GROUNDING_INTENT_NAV_VERB = (
    r"(?i)\b(take\s+me|get\s+me|bring\s+me|navigate(\s+me)?|"
    r"i\s+want\s+to\s+go|how\s+do\s+i\s+get|directions?\s+(to|for)|"
    r"guide\s+me|show\s+me\s+how\s+to\s+(get|go|reach))\b"
)

def grounding_intent_navigation_query(text: str) -> bool:
    """True when the user is asking to be led to a place."""
    if grounding_intent_nearby_off():
        return False
    t = (text or "").strip()
    if not t:
        return False
    custom = _optional_trimmed("GOOGLE_MAPS_GROUNDING_INTENT_NAV_REGEX")
    if custom:
        try:
            return bool(re.search(custom, t))
        except re.error:
            return False
    try:
        if re.search(_BUILTIN_GROUNDING_INTENT_NAV_VERB, t):
            return True
    except re.error:
        pass
    if grounding_intent_place_types_for_user_text(t) and re.search(
        r"(?i)\b(?:nearest|closest|nearby|near\s+me|to\s+me|from\s+here|directions?)\b",
        t,
    ):
        return True
    return False


def intent_route_prefetch_try_transit() -> bool:
    """When prefetching directions in chat grounding, try TRANSIT before WALK-only fallback."""
    return _truthy("GOOGLE_MAPS_INTENT_PREFETCH_TRANSIT", "true")


def weather_units() -> str:
    u = (os.environ.get("GOOGLE_MAPS_WEATHER_UNITS") or "METRIC").strip().upper()
    return u if u in ("METRIC", "IMPERIAL") else "METRIC"


def waypoint_address_max_len() -> int:
    try:
        return max(50, min(1000, int(os.environ.get("GOOGLE_MAPS_WAYPOINT_ADDRESS_MAX_LEN", "500"))))
    except ValueError:
        return 500

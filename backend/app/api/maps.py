"""REST surface for Google Maps Platform — API key never sent to the browser."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.maps_platform.config import google_maps_api_key, is_platform_enabled
from app.maps_platform.facade import (
    fetch_nearby_places_markdown,
    reverse_geocode_markdown,
    weather_current_markdown,
)
from app.maps_platform import geocoding as maps_geocoding
from app.maps_platform import geolocate as maps_geolocate
from app.maps_platform.routes_compute import compute_routes_raw, routes_response_to_markdown

router = APIRouter(prefix="/api/maps", tags=["maps"])


@router.get("/status")
async def maps_status() -> dict[str, bool]:
    return {
        "enabled": is_platform_enabled(),
        "key_configured": bool(google_maps_api_key()),
    }


def _require_maps() -> None:
    if not is_platform_enabled():
        raise HTTPException(
            status_code=503,
            detail="Google Maps Platform is not enabled (set GOOGLE_MAPS_API_KEY and GOOGLE_MAPS_PLATFORM_ENABLED=true).",
        )


class LatLon(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


class ReverseGeocodeBody(LatLon):
    language: str | None = Field(default=None, max_length=12)


class WeatherBody(LatLon):
    pass


class NearbyBody(LatLon):
    pass


class GeocodeForwardBody(BaseModel):
    address: str = Field(..., min_length=1, max_length=500)
    language: str | None = Field(default=None, max_length=12)
    region: str | None = Field(default=None, max_length=2)


class RouteEndpoint(BaseModel):
    address: str | None = Field(default=None, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    def to_payload(self) -> dict[str, Any]:
        if self.address and self.address.strip():
            return {"address": self.address.strip()}
        if self.latitude is not None and self.longitude is not None:
            return {"latitude": self.latitude, "longitude": self.longitude}
        raise ValueError("Need address or latitude+longitude")


TravelMode = Literal["WALK", "BICYCLE", "DRIVE", "TWO_WHEELER", "TRANSIT"]


class ComputeRoutesBody(BaseModel):
    origin: RouteEndpoint
    destination: RouteEndpoint
    travel_mode: TravelMode = "WALK"
    departure_time_rfc3339: str | None = Field(default=None, max_length=40)
    arrival_time_rfc3339: str | None = Field(default=None, max_length=40)
    compute_alternatives: bool = False
    include_llm_markdown: bool = True
    language_code: str | None = Field(default=None, max_length=12)
    region_code: str | None = Field(default=None, max_length=2)


@router.post("/reverse-geocode")
async def post_reverse_geocode(body: ReverseGeocodeBody) -> dict[str, Any]:
    _require_maps()
    lang_for_api = (body.language or "").strip() or None
    data = await maps_geocoding.reverse_geocode_components(
        body.latitude, body.longitude, language=lang_for_api
    )
    md = await reverse_geocode_markdown(body.latitude, body.longitude, language=lang_for_api)
    return {"raw": data, "markdown_for_llm": md or None}


@router.post("/geocode")
async def post_geocode(body: GeocodeForwardBody) -> dict[str, Any]:
    _require_maps()
    lang_for_api = (body.language or "").strip() or None
    reg_for_api = (body.region or "").strip() or None
    if reg_for_api:
        reg_for_api = reg_for_api.upper()[:2]
    data = await maps_geocoding.forward_geocode(
        body.address, language=lang_for_api, region=reg_for_api
    )
    return {"raw": data}


@router.post("/weather")
async def post_weather(body: WeatherBody) -> dict[str, Any]:
    _require_maps()
    md = await weather_current_markdown(body.latitude, body.longitude)
    return {"markdown_for_llm": md or None}


@router.post("/nearby")
async def post_nearby(body: NearbyBody) -> dict[str, Any]:
    _require_maps()
    md = await fetch_nearby_places_markdown(body.latitude, body.longitude)
    return {"markdown_for_llm": md or None}


@router.post("/routes/compute")
async def post_compute_routes(body: ComputeRoutesBody) -> dict[str, Any]:
    _require_maps()
    try:
        origin = body.origin.to_payload()
        destination = body.destination.to_payload()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    raw = await compute_routes_raw(
        origin=origin,
        destination=destination,
        travel_mode=body.travel_mode,
        departure_time_rfc3339=body.departure_time_rfc3339,
        arrival_time_rfc3339=body.arrival_time_rfc3339,
        compute_alternatives=body.compute_alternatives,
        language_code_override=(body.language_code or "").strip() or None,
        region_code_override=(body.region_code or "").strip() or None,
    )
    out: dict[str, Any] = {"raw": raw}
    if body.include_llm_markdown:
        out["markdown_for_llm"] = routes_response_to_markdown(raw)
    return out


@router.post("/geolocation")
async def post_geolocation(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_maps()
    return await maps_geolocate.geolocate_raw(payload or {})

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=48_000)


class ChatIn(BaseModel):
    """Multi-turn text chat with Gemma (no image). History is supplied by the client."""

    messages: list[ChatTurn] = Field(..., min_length=1, max_length=40)
    stream: bool = True
    system_prompt: str | None = Field(
        None,
        max_length=24_000,
        description="When blind_assist is false: replaces default Gemma system prompt. When blind_assist is true: optional extra instructions appended after the default persona.",
    )
    blind_assist: bool = Field(
        False,
        description="Merge full default Gemma persona with a short blind/low-vision addendum instead of replacing it.",
    )
    use_langchain: bool = Field(
        False,
        description="Run text chat through LangChain create_agent + tools (web search, GPS weather/place, and Google Maps nearby + directions when the Maps Platform is enabled).",
    )
    web_grounding: bool = Field(
        False,
        description="If true, prepends live context (web excerpts when configured, GPS place/weather/nearby) and runs the LangChain agent so tools can run (Google nearby/directions need GPS + GOOGLE_MAPS_PLATFORM_ENABLED).",
    )
    latitude: float | None = Field(
        None,
        ge=-90,
        le=90,
        description="Browser/device latitude when the user shares location (pairs with longitude).",
    )
    longitude: float | None = Field(
        None,
        ge=-180,
        le=180,
        description="Browser/device longitude when the user shares location.",
    )
    location_accuracy_m: float | None = Field(
        None,
        ge=0,
        description="Optional GPS horizontal accuracy in meters from the Geolocation API.",
    )

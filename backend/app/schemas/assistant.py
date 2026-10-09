from __future__ import annotations

from pydantic import BaseModel, Field


class RouteBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=8_192)

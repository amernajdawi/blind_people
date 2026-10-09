from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app import modality_router
from app.schemas.assistant import RouteBody

router = APIRouter(tags=["assistant"])


@router.post("/api/assistant/route")
async def assistant_route(body: RouteBody):
    """Authoritative modality (A/B/C) + sampling plan for blind-assist UX."""
    txt = body.text.strip()
    if not txt:
        raise HTTPException(400, "text cannot be empty")
    plan, source = await modality_router.modality_route_with_llm_fallback(txt)
    return {
        "modality": plan.modality,
        "frames": plan.frames,
        "clip_ms": plan.clip_ms,
        "reason_short": plan.reason_line,
        "source": source,
        "scores": {
            "A": plan.score_a,
            "B": plan.score_b,
            "C": plan.score_c,
            "margin": plan.margin,
        },
    }

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core.access import (
    cookie_name,
    password_matches,
    password_required,
    request_is_unlocked,
    session_token,
)

router = APIRouter(tags=["auth"])


class LoginBody(BaseModel):
    password: str = Field(..., min_length=1, max_length=200)


@router.get("/api/auth/status")
async def auth_status(request: Request) -> dict[str, bool]:
    return {
        "required": password_required(),
        "unlocked": request_is_unlocked(request),
    }


@router.post("/api/auth/login")
async def auth_login(body: LoginBody, response: Response) -> dict[str, bool]:
    if not password_required():
        return {"required": False, "unlocked": True}
    if not password_matches(body.password):
        raise HTTPException(status_code=401, detail="Wrong password.")
    response.set_cookie(
        cookie_name(),
        session_token(),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 14,
        path="/",
    )
    return {"required": True, "unlocked": True}

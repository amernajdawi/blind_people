from __future__ import annotations

from pathlib import Path

from app.core.config import load_application_environment

load_application_environment(entry_file=Path(__file__))

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import register_api_routes
from app.core.access import client_key, password_required, rate_limit_allows, request_is_unlocked


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield


app = FastAPI(title="SeeingAEye App API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


_OPEN_API = {"/api/health", "/api/auth/status", "/api/auth/login"}
_LIMITED_PREFIXES = ("/api/llm", "/api/maps", "/api/device")


class _AppAccessMiddleware(BaseHTTPMiddleware):
    """Password gate (when set) and a simple per-minute cap on expensive routes."""

    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if path.startswith("/api/") and path not in _OPEN_API:
            if password_required() and not request_is_unlocked(request):
                return JSONResponse({"detail": "Sign in required."}, status_code=401)
            if any(path.startswith(prefix) for prefix in _LIMITED_PREFIXES):
                if not rate_limit_allows(client_key(request)):
                    return JSONResponse(
                        {"detail": "Too many requests. Wait a minute and try again."},
                        status_code=429,
                    )
        return await call_next(request)


class _CameraMicPermissionsMiddleware(BaseHTTPMiddleware):
    """Allow getUserMedia on this origin (some browsers consult Permissions-Policy)."""

    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        resp.headers["Permissions-Policy"] = (
            "camera=(self), microphone=(self), accelerometer=(self), gyroscope=(self), magnetometer=(self)"
        )
        return resp


app.add_middleware(_CameraMicPermissionsMiddleware)
app.add_middleware(_AppAccessMiddleware)

register_api_routes(app)

_backend_root = Path(__file__).resolve().parent.parent
_frontend = _backend_root / "frontend"
if not _frontend.is_dir():
    _frontend = _backend_root.parent / "frontend"
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")

"""HTTP routers. Wire with ``register_api_routes`` from ``app.main``."""

from __future__ import annotations

from fastapi import FastAPI

from app.api import assistant, auth, device, health, llm, maps


def register_api_routes(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(assistant.router)
    app.include_router(llm.router)
    app.include_router(maps.router)
    app.include_router(device.router)

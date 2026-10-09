from __future__ import annotations

import os

from fastapi import APIRouter

from app import langchain_agent_chat, modality_router, ollama_client, web_search

router = APIRouter(tags=["health"])


def _duckduckgo_available() -> bool:
    try:
        import ddgs  # noqa: F401
    except ImportError:
        try:
            import duckduckgo_search  # noqa: F401
        except ImportError:
            return False
    return True


@router.get("/api/health")
def health():
    tavily = bool(os.environ.get("TAVILY_API_KEY", "").strip())
    return {
        "status": "ok",
        "vision_llm": {"provider": "ollama", "model": ollama_client.OLLAMA_MODEL},
        "ollama": {
            "configured": ollama_client.ollama_configured(),
            "model": ollama_client.OLLAMA_MODEL,
            "host": ollama_client.OLLAMA_HOST,
        },
        "assistant": {
            "route_endpoint": "/api/assistant/route",
            "web": {
                "preferred_provider": web_search.search_provider_hint(),
                "tavily_api_key_configured": tavily,
                "tavily_remote_mcp_pattern": (
                    "https://mcp.tavily.com/mcp/?tavilyApiKey=<set TAVILY_API_KEY in env or paste key>"
                ),
                "ddgs_package_available": _duckduckgo_available(),
            },
            "reverse_geocode": {
                "geoapify_api_key_configured": bool(
                    os.environ.get("GEOAPIFY_API_KEY", "").strip()
                ),
                "provider_env": os.environ.get("REVERSE_GEO_PROVIDER", "photon"),
            },
            "router_heuristic_trust_margin": modality_router.HEURISTIC_TRUST_MARGIN,
            "web_disabled": getattr(web_search, "_web_disabled", False),
            "langchain_chat": {
                "enabled_via_env": langchain_agent_chat.langchain_chat_enabled(
                    body_flag=False
                ),
                "langsmith": {
                    "tracing_v2_enabled": os.environ.get(
                        "LANGCHAIN_TRACING_V2", ""
                    ).strip().lower()
                    in ("1", "true", "yes", "on"),
                    "api_key_configured": bool(
                        os.environ.get("LANGCHAIN_API_KEY", "").strip()
                        or os.environ.get("LANGSMITH_API_KEY", "").strip()
                    ),
                    "project": os.environ.get("LANGCHAIN_PROJECT")
                    or os.environ.get("LANGSMITH_PROJECT")
                    or None,
                },
            },
        },
    }

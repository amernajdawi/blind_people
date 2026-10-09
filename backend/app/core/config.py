"""Load `.env` relative to the repo / backend layout (same rules as the legacy monolithic `main`)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def apply_langsmith_env_safety() -> None:
    """
    When ``LANGCHAIN_TRACING_V2`` is on but no LangSmith API key is set, LangChain still tries to
    POST to ``api.smith.langchain.com`` and logs ``401 Unauthorized``. Turn tracing off in that case.
    If tracing is on **with** a key and you still see 401, the key is invalid or expired — fix or set
    ``LANGCHAIN_TRACING_V2=false``.
    """
    tracing = os.environ.get("LANGCHAIN_TRACING_V2", "").strip().lower()
    if tracing not in ("1", "true", "yes", "on"):
        return
    key = (
        os.environ.get("LANGCHAIN_API_KEY", "").strip()
        or os.environ.get("LANGSMITH_API_KEY", "").strip()
    )
    if not key:
        os.environ["LANGCHAIN_TRACING_V2"] = "false"


def load_application_environment(*, entry_file: Path) -> None:
    """
    Call with ``Path(__file__)`` from ``app/main.py`` so resolution matches the previous single-file app.

    - Dev: ``<repo>/backend/app/main.py`` → loads ``<repo>/.env``
    - Other layouts fall back to the same elif chain as before.
    """
    _here = entry_file.resolve()
    if _here.parent.name == "app" and _here.parent.parent.name == "backend":
        _env_dir = _here.parents[2]
    elif _here.parent.name == "app":
        _env_dir = _here.parents[1]
    else:
        _env_dir = _here.parents[2]
    load_dotenv(_env_dir / ".env")
    load_dotenv()
    apply_langsmith_env_safety()

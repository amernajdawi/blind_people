"""Optional app password for the browser and for glasses on the same network.

If ``SEEINGAEYE_APP_PASSWORD`` is empty, the API stays open (local demo).
Glasses send ``Authorization: Bearer <password>``. The browser gets an HttpOnly cookie after login.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import defaultdict, deque

from starlette.requests import Request

_COOKIE = "seeingaeye_session"
_SESSION_MSG = b"seeingaeye-v1"


def app_password() -> str:
    return (os.environ.get("SEEINGAEYE_APP_PASSWORD") or "").strip()


def password_required() -> bool:
    return bool(app_password())


def session_token() -> str:
    secret = app_password().encode("utf-8")
    return hmac.new(secret, _SESSION_MSG, hashlib.sha256).hexdigest()


def password_matches(candidate: str) -> bool:
    expected = app_password()
    if not expected:
        return True
    return hmac.compare_digest(expected, (candidate or "").strip())


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def request_is_unlocked(request: Request) -> bool:
    if not password_required():
        return True
    cookie = request.cookies.get(_COOKIE) or ""
    if cookie and hmac.compare_digest(cookie, session_token()):
        return True
    token = bearer_token(request)
    return bool(token) and password_matches(token)


def cookie_name() -> str:
    return _COOKIE


def rate_limit_per_minute() -> int:
    try:
        return max(0, int(os.environ.get("SEEINGAEYE_RATE_LIMIT_PER_MIN", "40")))
    except ValueError:
        return 40


_hits: dict[str, deque[float]] = defaultdict(deque)


def rate_limit_allows(client_key: str) -> bool:
    limit = rate_limit_per_minute()
    if limit <= 0:
        return True
    now = time.monotonic()
    bucket = _hits[client_key]
    while bucket and now - bucket[0] > 60.0:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def client_key(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client:
        return request.client.host or "unknown"
    return "unknown"

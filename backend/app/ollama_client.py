"""Ollama Cloud (Gemma 4). Docs: https://ollama.com/library/gemma4"""
from __future__ import annotations

import base64
import json
import os
import re
from io import BytesIO
from typing import Any, AsyncIterator

import httpx
from PIL import Image

from app.prompt_loader import load_prompt

OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "").strip()
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "https://ollama.com").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:31b-cloud")

# Persona blocks live in ``app/prompts/*.txt``; override entirely with OLLAMA_SYSTEM_PROMPT when needed.
GEMMA_SYSTEM_CORE = load_prompt("gemma_system_core")
GEMMA_BLIND_ASSIST_ADDENDUM = load_prompt("gemma_blind_assist_addendum")
GEMMA_VISION_ADDENDUM = load_prompt("gemma_vision_addendum")
GEMMA_MULTI_VISION_ADDENDUM = load_prompt("gemma_multi_vision_addendum")

_THINK_BLOCK = re.compile(
    r"<\|channel>thought\s*\n.*?<channel\|>",
    re.DOTALL | re.IGNORECASE,
)
_TAG = re.compile(r"<\|[^>]+\|>")


def clean_gemma_reply(text: str) -> str:
    if not text:
        return ""
    s = _THINK_BLOCK.sub("", text)
    s = _TAG.sub("", s)
    return s.strip()


def _thinking_prefix() -> str:
    """Gemma 4: https://ollama.com/library/gemma4 — <|think|> enables reasoning (slower, often smarter)."""
    if os.environ.get("OLLAMA_THINKING", "").strip().lower() in ("1", "true", "yes", "on"):
        return "<|think|>\n"
    return ""


def effective_system_for_chat(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("OLLAMA_SYSTEM_PROMPT", "").strip()
    if env:
        return env
    return _thinking_prefix() + GEMMA_SYSTEM_CORE


def effective_system_for_vision(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("OLLAMA_SYSTEM_PROMPT", "").strip()
    if env:
        return env
    return _thinking_prefix() + GEMMA_SYSTEM_CORE + "\n\n" + GEMMA_VISION_ADDENDUM


def effective_system_for_multi_vision(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("OLLAMA_SYSTEM_PROMPT", "").strip()
    if env:
        return env
    return (
        _thinking_prefix()
        + GEMMA_SYSTEM_CORE
        + "\n\n"
        + GEMMA_MULTI_VISION_ADDENDUM
    )


def ollama_configured() -> bool:
    return bool(OLLAMA_API_KEY)


def _prepare_image_b64(image_bytes: bytes) -> str:
    max_side = int(os.environ.get("OLLAMA_IMAGE_MAX_SIDE", "640"))
    quality = int(os.environ.get("OLLAMA_JPEG_QUALITY", "72"))
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _chat_options(*, vision: bool) -> dict[str, Any]:
    if vision:
        temp = float(os.environ.get("OLLAMA_TEMPERATURE", "0.85"))
        top_p = float(os.environ.get("OLLAMA_TOP_P", "0.9"))
        if os.environ.get("OLLAMA_NUM_PREDICT", "").strip():
            n_predict = int(os.environ["OLLAMA_NUM_PREDICT"])
        else:
            n_predict = int(os.environ.get("OLLAMA_MAX_TOKENS", "320"))
    else:
        temp = float(os.environ.get("OLLAMA_CHAT_TEMPERATURE", os.environ.get("OLLAMA_TEMPERATURE", "0.78")))
        top_p = float(os.environ.get("OLLAMA_CHAT_TOP_P", os.environ.get("OLLAMA_TOP_P", "0.92")))
        if os.environ.get("OLLAMA_CHAT_NUM_PREDICT", "").strip():
            n_predict = int(os.environ["OLLAMA_CHAT_NUM_PREDICT"])
        else:
            n_predict = int(os.environ.get("OLLAMA_CHAT_MAX_TOKENS", "1536"))
    return {
        "temperature": temp,
        "top_p": top_p,
        "top_k": int(os.environ.get("OLLAMA_TOP_K", "64")),
        "num_predict": n_predict,
    }


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {OLLAMA_API_KEY}",
        "Content-Type": "application/json",
    }


async def _stream_delta_lines(payload: dict) -> AsyncIterator[str]:
    url = f"{OLLAMA_HOST}/api/chat"
    timeout = httpx.Timeout(300.0, connect=45.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload, headers=_headers()) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")[:2000]
                try:
                    err_j = json.loads(body)
                    body = str(err_j.get("error", err_j.get("message", body)))
                except Exception:
                    pass
                raise RuntimeError(f"Ollama HTTP {resp.status_code}: {body}")

            acc = ""
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if err := obj.get("error"):
                    raise RuntimeError(str(err))
                msg = obj.get("message") or {}
                piece = msg.get("content") or ""
                if not piece:
                    continue
                if acc and piece.startswith(acc):
                    delta = piece[len(acc) :]
                    acc = piece
                else:
                    delta = piece
                    acc = acc + piece
                if delta:
                    yield delta


def _vision_payload(
    b64: str,
    user_text: str,
    system_override: str | None,
    stream: bool,
) -> dict[str, Any]:
    sys_text = effective_system_for_vision(system_override)
    messages: list[dict] = []
    if sys_text:
        messages.append({"role": "system", "content": sys_text})
    messages.append({"role": "user", "content": user_text, "images": [b64]})
    return {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": stream,
        "options": _chat_options(vision=True),
    }


def _vision_payload_multi(
    b64_list: list[str],
    user_text: str,
    system_override: str | None,
    stream: bool,
) -> dict[str, Any]:
    sys_text = effective_system_for_multi_vision(system_override)
    messages: list[dict] = []
    if sys_text:
        messages.append({"role": "system", "content": sys_text})
    messages.append({"role": "user", "content": user_text, "images": b64_list})
    return {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": stream,
        "options": _chat_options(vision=True),
    }


async def vision_chat_stream(
    image_bytes: bytes,
    user_prompt: str | None = None,
    system_prompt: str | None = None,
) -> AsyncIterator[str]:
    if not OLLAMA_API_KEY:
        raise ValueError("OLLAMA_API_KEY is not set")

    b64 = _prepare_image_b64(image_bytes)
    text = (
        user_prompt
        or "Hey Gemma—what do you see in this frame? Anything I'm holding or showing you?"
    ).strip()

    payload = _vision_payload(b64, text, system_prompt, stream=True)
    async for d in _stream_delta_lines(payload):
        yield d


async def vision_chat_stream_multi(
    image_bytes_iter: list[bytes],
    user_prompt: str | None = None,
    system_prompt: str | None = None,
) -> AsyncIterator[str]:
    if not OLLAMA_API_KEY:
        raise ValueError("OLLAMA_API_KEY is not set")
    if not image_bytes_iter:
        raise ValueError("At least one image is required")

    b64_list = [_prepare_image_b64(blob) for blob in image_bytes_iter]
    text = (
        user_prompt
        or "These frames come from my camera clip in order — what do they show?"
    ).strip()

    payload = _vision_payload_multi(b64_list, text, system_prompt, stream=True)
    async for d in _stream_delta_lines(payload):
        yield d


async def vision_chat(
    image_bytes: bytes,
    user_prompt: str | None = None,
    system_prompt: str | None = None,
) -> dict:
    if not OLLAMA_API_KEY:
        raise ValueError(
            "OLLAMA_API_KEY is not set. Copy .env.example to .env and add your key from https://ollama.com/settings/keys"
        )

    b64 = _prepare_image_b64(image_bytes)
    text = (
        user_prompt
        or "Hey Gemma—what do you see in this frame? Anything I'm holding or showing you?"
    ).strip()

    payload = _vision_payload(b64, text, system_prompt, stream=False)
    url = f"{OLLAMA_HOST}/api/chat"
    timeout = httpx.Timeout(300.0, connect=45.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=_headers())

    if resp.status_code >= 400:
        detail = resp.text[:2000]
        try:
            err_j = resp.json()
            detail = err_j.get("error", err_j.get("message", detail))
        except Exception:
            pass
        raise RuntimeError(f"Ollama HTTP {resp.status_code}: {detail}")

    data = resp.json()
    msg = data.get("message") or {}
    content = clean_gemma_reply(msg.get("content") or "")
    return {
        "reply": content,
        "model": OLLAMA_MODEL,
        "done": data.get("done", True),
    }


async def vision_chat_multi(
    image_bytes_iter: list[bytes],
    user_prompt: str | None = None,
    system_prompt: str | None = None,
) -> dict:
    if not OLLAMA_API_KEY:
        raise ValueError(
            "OLLAMA_API_KEY is not set. Copy .env.example to .env and add your key from https://ollama.com/settings/keys"
        )
    if not image_bytes_iter:
        raise ValueError("At least one image is required")

    b64_list = [_prepare_image_b64(blob) for blob in image_bytes_iter]
    text = (
        user_prompt
        or "These frames come from my camera clip in order — what do they show?"
    ).strip()

    payload = _vision_payload_multi(b64_list, text, system_prompt, stream=False)
    url = f"{OLLAMA_HOST}/api/chat"
    timeout = httpx.Timeout(300.0, connect=45.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=_headers())

    if resp.status_code >= 400:
        detail = resp.text[:2000]
        try:
            err_j = resp.json()
            detail = err_j.get("error", err_j.get("message", detail))
        except Exception:
            pass
        raise RuntimeError(f"Ollama HTTP {resp.status_code}: {detail}")

    data = resp.json()
    msg = data.get("message") or {}
    content = clean_gemma_reply(msg.get("content") or "")
    return {
        "reply": content,
        "model": OLLAMA_MODEL,
        "done": data.get("done", True),
    }


def _text_payload(
    messages: list[dict],
    system_override: str | None,
    stream: bool,
) -> dict[str, Any]:
    sys_text = effective_system_for_chat(system_override)
    full: list[dict] = []
    if sys_text:
        full.append({"role": "system", "content": sys_text})
    full.extend(messages)
    return {
        "model": OLLAMA_MODEL,
        "messages": full,
        "stream": stream,
        "options": _chat_options(vision=False),
    }


async def text_chat_stream(
    messages: list[dict],
    system_prompt: str | None = None,
) -> AsyncIterator[str]:
    if not OLLAMA_API_KEY:
        raise ValueError("OLLAMA_API_KEY is not set")
    payload = _text_payload(messages, system_prompt, stream=True)
    async for d in _stream_delta_lines(payload):
        yield d


async def text_chat(
    messages: list[dict],
    system_prompt: str | None = None,
) -> dict:
    if not OLLAMA_API_KEY:
        raise ValueError("OLLAMA_API_KEY is not set")
    payload = _text_payload(messages, system_prompt, stream=False)
    url = f"{OLLAMA_HOST}/api/chat"
    timeout = httpx.Timeout(300.0, connect=45.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=_headers())
    if resp.status_code >= 400:
        detail = resp.text[:2000]
        try:
            err_j = resp.json()
            detail = err_j.get("error", err_j.get("message", detail))
        except Exception:
            pass
        raise RuntimeError(f"Ollama HTTP {resp.status_code}: {detail}")
    data = resp.json()
    msg = data.get("message") or {}
    content = clean_gemma_reply(msg.get("content") or "")
    return {"reply": content, "model": OLLAMA_MODEL, "done": data.get("done", True)}


ROUTER_MODEL = os.environ.get("OLLAMA_ROUTER_MODEL", "").strip() or OLLAMA_MODEL

ROUTER_SYSTEM = load_prompt("router_system")


def _json_from_model_content(raw: str) -> dict[str, Any]:
    text = clean_gemma_reply(raw)
    lo = text.find("{")
    hi = text.rfind("}")
    if lo < 0 or hi <= lo:
        return {}
    try:
        return json.loads(text[lo : hi + 1])
    except json.JSONDecodeError:
        return {}


async def modality_route_completion(user_question: str) -> dict[str, str]:
    if not OLLAMA_API_KEY:
        raise ValueError("OLLAMA_API_KEY is not set")
    trimmed = (user_question or "").strip()[:2600]

    router_temp = float(os.environ.get("ROUTER_TEMPERATURE", "0.06"))
    router_tokens = int(os.environ.get("ROUTER_NUM_PREDICT", "120"))

    full: list[dict] = [
        {"role": "system", "content": ROUTER_SYSTEM},
        {"role": "user", "content": trimmed},
    ]
    payload = {
        "model": ROUTER_MODEL,
        "messages": full,
        "stream": False,
        "options": {
            "temperature": router_temp,
            "top_p": float(os.environ.get("ROUTER_TOP_P", "0.85")),
            "top_k": int(os.environ.get("ROUTER_TOP_K", "20")),
            "num_predict": router_tokens,
        },
    }

    url = f"{OLLAMA_HOST}/api/chat"
    timeout = httpx.Timeout(120.0, connect=35.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=_headers())
    if resp.status_code >= 400:
        detail = resp.text[:1500]
        try:
            err_j = resp.json()
            detail = str(err_j.get("error", err_j.get("message", detail)))
        except Exception:
            pass
        raise RuntimeError(f"Ollama router HTTP {resp.status_code}: {detail}")
    data = resp.json()
    msg = data.get("message") or {}
    content = msg.get("content") or ""
    jd = _json_from_model_content(content)
    modality = str(jd.get("modality") or "").strip().upper()[:1]
    if modality not in ("A", "B", "C"):
        modality = "A"
    reason = str(jd.get("reason") or jd.get("rationale") or "").strip()
    return {"modality": modality, "reason": reason}

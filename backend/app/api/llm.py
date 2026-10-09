from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app import langchain_agent_chat, ollama_client, web_search
from app.schemas.chat import ChatIn
from app.services.chat_grounding import (
    CHAT_ADDL_INSTRUCTIONS_HEADER,
    append_runtime_datetime_context,
    grounding_suffix_for_text_chat,
)

router = APIRouter(tags=["llm"])

_VISION_MULTI_MAX_FILES = 9


def _truthy_stream(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


@router.post("/api/llm/chat")
async def llm_chat(body: ChatIn):
    if not ollama_client.ollama_configured():
        raise HTTPException(
            503,
            "Ollama Cloud is not configured. Set OLLAMA_API_KEY in .env (see .env.example).",
        )
    if body.messages[-1].role != "user":
        raise HTTPException(400, "Last message must be from the user")
    msgs = [{"role": m.role, "content": m.content} for m in body.messages]
    if body.blind_assist:
        sys_turn = (
            ollama_client.effective_system_for_chat(None).strip()
            + "\n\n"
            + ollama_client.GEMMA_BLIND_ASSIST_ADDENDUM.strip()
        )
        extra = (body.system_prompt or "").strip()
        if extra:
            sys_turn += f"\n\n{CHAT_ADDL_INSTRUCTIONS_HEADER}\n" + extra
    else:
        explicit = (body.system_prompt or "").strip() or None
        sys_turn = ollama_client.effective_system_for_chat(explicit).strip()

    lc_on = langchain_agent_chat.langchain_chat_enabled(
        body_flag=(body.use_langchain or body.web_grounding)
    )
    langchain_include_web = body.web_grounding

    if body.web_grounding:
        last_turn = msgs[-1].get("content") or ""
        suffix, prefetched_web_ok = await grounding_suffix_for_text_chat(
            last_user_text=last_turn,
            latitude=body.latitude,
            longitude=body.longitude,
            location_accuracy_m=body.location_accuracy_m,
        )
        if suffix.strip():
            sys_turn = (sys_turn or "").strip() + "\n\n---\n\n" + suffix.strip()
        if lc_on:
            langchain_include_web = bool(
                not prefetched_web_ok and web_search.agent_web_tool_allowed()
            )

    sys_turn = append_runtime_datetime_context(sys_turn)

    if body.stream:

        async def ndjson():
            full: list[str] = []
            try:
                if lc_on:
                    async for piece in langchain_agent_chat.run_langchain_chat_stream(
                        api_messages=msgs,
                        system_prompt=sys_turn,
                        latitude=body.latitude,
                        longitude=body.longitude,
                        include_web_search=langchain_include_web,
                    ):
                        full.append(piece)
                        yield json.dumps({"content": piece}) + "\n"
                    text = ollama_client.clean_gemma_reply("".join(full))
                    yield json.dumps(
                        {"done": True, "model": ollama_client.OLLAMA_MODEL, "reply": text}
                    ) + "\n"
                    return
                async for piece in ollama_client.text_chat_stream(
                    msgs,
                    system_prompt=sys_turn,
                ):
                    full.append(piece)
                    yield json.dumps({"content": piece}) + "\n"
                text = ollama_client.clean_gemma_reply("".join(full))
                yield json.dumps(
                    {"done": True, "model": ollama_client.OLLAMA_MODEL, "reply": text}
                ) + "\n"
            except ValueError as e:
                yield json.dumps({"error": str(e)}) + "\n"
            except RuntimeError as e:
                yield json.dumps({"error": str(e)}) + "\n"
            except Exception as e:
                yield json.dumps({"error": f"LLM request failed: {e!s}"}) + "\n"

        return StreamingResponse(ndjson(), media_type="application/x-ndjson")

    try:
        if lc_on:
            text = await langchain_agent_chat.run_langchain_chat_blocking(
                api_messages=msgs,
                system_prompt=sys_turn,
                latitude=body.latitude,
                longitude=body.longitude,
                include_web_search=langchain_include_web,
            )
            return {
                "reply": text,
                "model": ollama_client.OLLAMA_MODEL,
                "done": True,
            }
        return await ollama_client.text_chat(msgs, system_prompt=sys_turn)
    except ValueError as e:
        raise HTTPException(503, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"LLM request failed: {e!s}") from e


@router.post("/api/llm/vision")
async def llm_vision(
    file: UploadFile = File(...),
    prompt: str = Form(""),
    system_prompt: str = Form(""),
    stream: str = Form("true"),
):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Expected an image file")
    if not ollama_client.ollama_configured():
        raise HTTPException(
            503,
            "Ollama Cloud is not configured. Set OLLAMA_API_KEY in .env (see .env.example).",
        )
    data = await file.read()
    sys_clean = (system_prompt or "").strip() or None
    prompt_clean = (prompt or "").strip() or None

    if _truthy_stream(stream):

        async def ndjson():
            full: list[str] = []
            try:
                async for piece in ollama_client.vision_chat_stream(
                    data,
                    user_prompt=prompt_clean,
                    system_prompt=sys_clean,
                ):
                    full.append(piece)
                    yield json.dumps({"content": piece}) + "\n"
                text = ollama_client.clean_gemma_reply("".join(full))
                yield json.dumps(
                    {"done": True, "model": ollama_client.OLLAMA_MODEL, "reply": text}
                ) + "\n"
            except ValueError as e:
                yield json.dumps({"error": str(e)}) + "\n"
            except RuntimeError as e:
                yield json.dumps({"error": str(e)}) + "\n"
            except Exception as e:
                yield json.dumps({"error": f"LLM request failed: {e!s}"}) + "\n"

        return StreamingResponse(ndjson(), media_type="application/x-ndjson")

    try:
        return await ollama_client.vision_chat(
            data,
            user_prompt=prompt_clean,
            system_prompt=sys_clean,
        )
    except ValueError as e:
        raise HTTPException(503, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"LLM request failed: {e!s}") from e


@router.post("/api/llm/vision/multi")
async def llm_vision_multi(
    files: list[UploadFile] = File(..., description="JPEG/PNG frames in chronological order (max 9)"),
    prompt: str = Form(""),
    system_prompt: str = Form(""),
    stream: str = Form("true"),
):
    if not files:
        raise HTTPException(400, "Expected one or more image files as `files`")
    if len(files) > _VISION_MULTI_MAX_FILES:
        raise HTTPException(
            400,
            f"At most {_VISION_MULTI_MAX_FILES} frames allowed; got {len(files)}",
        )
    datas: list[bytes] = []
    for i, uf in enumerate(files):
        ct = uf.content_type or ""
        if not ct.startswith("image/"):
            raise HTTPException(
                400,
                f"Frame {i + 1} must be image/*, got {ct or 'unknown'}",
            )
        datas.append(await uf.read())
    if not ollama_client.ollama_configured():
        raise HTTPException(
            503,
            "Ollama Cloud is not configured. Set OLLAMA_API_KEY in .env (see .env.example).",
        )
    sys_clean = (system_prompt or "").strip() or None
    prompt_clean = (prompt or "").strip() or None

    if _truthy_stream(stream):

        async def ndjson_multi():
            full: list[str] = []
            try:
                async for piece in ollama_client.vision_chat_stream_multi(
                    datas,
                    user_prompt=prompt_clean,
                    system_prompt=sys_clean,
                ):
                    full.append(piece)
                    yield json.dumps({"content": piece}) + "\n"
                text = ollama_client.clean_gemma_reply("".join(full))
                yield json.dumps(
                    {"done": True, "model": ollama_client.OLLAMA_MODEL, "reply": text}
                ) + "\n"
            except ValueError as e:
                yield json.dumps({"error": str(e)}) + "\n"
            except RuntimeError as e:
                yield json.dumps({"error": str(e)}) + "\n"
            except Exception as e:
                yield json.dumps({"error": f"LLM request failed: {e!s}"}) + "\n"

        return StreamingResponse(ndjson_multi(), media_type="application/x-ndjson")

    try:
        return await ollama_client.vision_chat_multi(
            datas,
            user_prompt=prompt_clean,
            system_prompt=sys_clean,
        )
    except ValueError as e:
        raise HTTPException(503, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"LLM request failed: {e!s}") from e

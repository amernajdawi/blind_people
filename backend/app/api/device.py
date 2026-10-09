"""One turn for glasses: microphone text, optional camera frames, GPS, spoken reply."""

from __future__ import annotations

import re

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app import modality_router, ollama_client
from app.api.llm import llm_chat
from app.schemas.chat import ChatIn, ChatTurn

router = APIRouter(tags=["device"])

_MAX_FRAMES = 9


def _vision_prompt(user_text: str, latitude: float | None, longitude: float | None) -> str:
    lines = [
        user_text.strip(),
        "",
        "The listener cannot see. Speak in short clear sentences for a speaker in glasses.",
        "The photo is the forward camera, not a mirror. Left in the image is the person's left.",
    ]
    if _hazard_look(user_text):
        lines.append(
            "Hazard check: first say stop or continue, then name only a curb, step, hole, pole, "
            "vehicle, bicycle, or person that the picture supports, and which side. If unsure, say stop and wait. "
            "This is a camera description, not a traffic signal."
        )
    if latitude is not None and longitude is not None:
        lines.append(f"Glasses GPS: {latitude:.5f}, {longitude:.5f}.")
    return "\n".join(lines).strip()


def _hazard_look(text: str) -> bool:
    return bool(
        re.search(
            r"\b(check ahead|in front of me|ahead of me|can i cross|safe to (walk|cross)|"
            r"curb|kerb|traffic|obstacle|obstacles|steps ahead|stairs ahead)\b",
            text or "",
            re.I,
        )
    )


@router.post("/api/device/turn")
async def device_turn(
    text: str = Form(...),
    latitude: float | None = Form(None),
    longitude: float | None = Form(None),
    location_accuracy_m: float | None = Form(None),
    files: list[UploadFile] | None = File(None),
) -> dict:
    """
    Glasses (or a phone bridge) send what the person said, where they are, and camera JPEGs.
    The reply is text for the glasses speaker.
    """
    said = (text or "").strip()
    if not said:
        raise HTTPException(400, "text cannot be empty")
    if not ollama_client.ollama_configured():
        raise HTTPException(503, "Ollama is not configured.")

    images: list[bytes] = []
    for uf in files or []:
        ct = uf.content_type or ""
        if ct and not ct.startswith("image/"):
            raise HTTPException(400, "Camera frames must be images.")
        blob = await uf.read()
        if blob:
            images.append(blob)
        if len(images) >= _MAX_FRAMES:
            break

    plan, source = await modality_router.modality_route_with_llm_fallback(said)
    use_camera = plan.modality in ("B", "C") and bool(images)

    if not use_camera:
        body = ChatIn(
            messages=[ChatTurn(role="user", content=said)],
            stream=False,
            blind_assist=True,
            web_grounding=True,
            latitude=latitude,
            longitude=longitude,
            location_accuracy_m=location_accuracy_m,
        )
        result = await llm_chat(body)
        reply = ""
        if isinstance(result, dict):
            reply = str(result.get("reply") or "")
        return {
            "reply": reply,
            "modality": "A" if plan.modality == "A" or not images else plan.modality,
            "route_source": source,
            "camera_frames": len(images),
            "speak": True,
        }

    prompt = _vision_prompt(said, latitude, longitude)
    system = ollama_client.effective_system_for_vision(None)
    try:
        if plan.modality == "C" and len(images) > 1:
            system = ollama_client.effective_system_for_multi_vision(None)
            result = await ollama_client.vision_chat_multi(
                images[:_MAX_FRAMES],
                user_prompt=prompt,
                system_prompt=system,
            )
        else:
            result = await ollama_client.vision_chat(
                images[0],
                user_prompt=prompt,
                system_prompt=system,
            )
    except ValueError as e:
        raise HTTPException(503, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e

    return {
        "reply": str(result.get("reply") or ""),
        "modality": "C" if plan.modality == "C" and len(images) > 1 else "B",
        "route_source": source,
        "camera_frames": len(images),
        "speak": True,
    }

"""
Blind-assist modality routing: A=text, B=single snapshot, C=multi-frame temporal sample.
Uses fast heuristics first; optionally escalates to a tiny LLM call when margins are ambiguous.
Mirrors frontend/app.js weights for comparable decisions.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ModalityPlan:
    modality: str  # "A" | "B" | "C"
    frames: int
    clip_ms: int
    reason_line: str
    score_a: int
    score_b: int
    score_c: int
    margin: float


def _plan_clip(lower: str, mod: str) -> tuple[int, int, str]:
    if mod != "C":
        if mod == "A":
            return 1, 3200, "Text / thinking — no camera capture this turn."
        return 1, 3200, "Single snapshot — static scene or text."
    n = 7
    if re.search(r"\bquick\b|brief scan|fast\b", lower, re.I):
        n -= 2
    if re.search(r"\b(find my|find the|locate|where('s|\s+is)\b)", lower, re.I):
        n = max(n, 7)
    if re.search(r"\b(layout|whole room|scan\b|orient\b)", lower, re.I):
        n = max(n, 8)
    if re.search(r"\b(approaching|movement|walking toward|motion)\b", lower, re.I):
        n = min(n, 5)
    if re.search(r"\b(pour|cooking|cook\b|spill)\b", lower, re.I):
        n = max(5, min(n, 7))
    n = max(3, min(9, n))
    clip_ms = 3200 if n <= 4 else 5500 if n >= 8 else 4200
    return n, clip_ms, f"Video sample — {n} frames from ~{clip_ms / 1000:.1f}s clip."


def _pick_mod(scores: dict[str, int]) -> str:
    rank = {"A": 0, "B": 1, "C": 2}
    chosen = "A"
    for m in ("B", "C"):
        sm, sc = scores[m], scores[chosen]
        if sm > sc:
            chosen = m
        elif sm == sc and rank[m] < rank[chosen]:
            chosen = m
    return chosen


def _margin(scores: dict[str, int]) -> float:
    s = sorted(scores.values(), reverse=True)
    return float(s[0] - s[1]) if len(s) >= 2 else float(s[0])


_PHOTO_TOOL_EXCEPTION = re.compile(
    r"\b("
    r"in this (photo|picture|frame|image)|from my camera|webcam|on screen|"
    r"see (the|this) thermometer|read (the|this) thermometer|temperature on (the|this|my)"
    r")\b",
    re.I,
)


def finalize_blind_modality(raw: str, modality: str) -> str:
    """
    Weather, **self-location** (GPS / map), and **web/search** questions must use Modality A —
    not guessing from the webcam — unless the user explicitly ties the question to the picture/camera.
    """
    if modality not in ("B", "C"):
        return modality
    from app import web_search

    if _PHOTO_TOOL_EXCEPTION.search(raw or ""):
        return modality

    if web_search.heuristic_local_weather_query(raw):
        return "A"
    if web_search.heuristic_where_am_i_query(raw):
        return "A"
    if web_search.heuristic_web_lookup_intent(raw):
        return "A"
    return modality


def modality_scores_heuristic(raw: str) -> tuple[dict[str, int], str]:
    q = (raw or "").strip()
    lower = q.lower()
    if not q:
        return {"A": 99, "B": 0, "C": 0}, lower

    a = b = c = 0
    if re.search(
        r"\b(weather|forecast|temperature|celsius|fahrenheit|raining|rains|snow|humidity|how cold|how hot|degrees\b)\b",
        lower,
        re.I,
    ):
        a += 3
    if re.search(
        r"\b(where am i|where are we|where'?s my location|where is my location|what is my location|\bmy location\b|what'?s my location|what city am i|what town am i in)\b",
        lower,
        re.I,
    ):
        a += 3
    if re.search(r"\b(news|headlines)\b", lower, re.I):
        a += 3
    if re.search(
        r"\b("
        r"(?:can|could)\s+you\s+(?:please\s+)?(?:check|google|search)|"
        r"(?:please\s+)?(?:check|test)\s+(?:my\s+|the\s+)?internet\b|"
        r"\bgoogle\b|(?:search|ask)\s+google|google\s+(?:if|whether|for|about)|"
        r"(?:web\s+search|search\s+(?:the\s+)?web|search\s+online)|"
        r"\bsearch\s+for\b"
        r")\b",
        lower,
        re.I,
    ):
        a += 3
    if re.search(r"\bcapital of\b", lower, re.I):
        a += 2
    if re.search(
        r"\bcalculate\b|convert.*\b(to|into)\b|^\s*[\d.]+\s*[\+\-*\/]", lower, re.I
    ):
        a += 3
    if re.search(r"\bmiles to\b|\bkilometers\b|distance\b.*\bbetween\b|\bhow far\b", lower, re.I):
        a += 2
    if re.search(r"^what time|^what'?s the time\b|timezone\b|current date\b", lower, re.I):
        a += 2
    if re.search(r"\b(who won|final score|playoff bracket)\b", lower, re.I):
        a += 2
    if re.search(
        r"\b(who (is|was|are)|meaning of life|capitalism|explain (the theory|concept))\b",
        lower,
        re.I,
    ):
        a += 1
    if re.search(r"\bwhat (is|'s|'re)\b", lower, re.I) and not re.search(
        r"\b(this|that|these|here|holding|in front of|camera|frame|desk|wallet|cup|shirt|dress|tie)\b",
        lower,
        re.I,
    ):
        a += 2
    if re.search(
        r"\b(history of|tell me about|describe (the planet|mars|jupiter))\b",
        lower,
        re.I,
    ):
        a += 1

    if re.search(
        r"\b(read|spell|dosage|ingredients\b|medicine bottle|pill bottle|label on|fine print)\b",
        lower,
        re.I,
    ):
        b += 4
    if re.search(
        r"\b(barcode|qr code|receipt|menu|price tag|sign\b|poster|captions)\b",
        lower,
        re.I,
    ):
        b += 3
    if re.search(r"\bwhat colour|what color|which colour|which color\b", lower, re.I):
        b += 2
    if re.search(
        r"\b(what'?s this|what is this|identify this object|what am i holding)\b",
        lower,
        re.I,
    ):
        b += 3
    if re.search(
        r"\b(describe what's in front|describe what'?s ahead|what'?s on (my desk|the table))\b",
        lower,
        re.I,
    ):
        b += 2
    if re.search(
        r"\b(camera|webcam|in the frame|this frame|live video|describe the picture|what do you see)\b",
        lower,
        re.I,
    ):
        b += 3
    if re.search(
        r"\b(check ahead|in front of me|ahead of me|can i cross|safe to (walk|cross)|curb|kerb|traffic|obstacle|obstacles|steps ahead|stairs ahead)\b",
        lower,
        re.I,
    ):
        b += 6

    if re.search(r"\bwhere('s|\s+is|\s+are|\s+would)\b", lower, re.I) and re.search(
        r"\b(door|exit|stairs|bathroom|kitchen|elevator|keys|wallet|cup|charger|bag|glass|remote)\b",
        lower,
        re.I,
    ):
        c += 4
    elif re.search(
        r"\b(where('s|\s+is|\s+are|\s+would)|locate|find the|lost my|finding the)\b",
        lower,
        re.I,
    ):
        c += 2
    if re.search(
        r"\b(layout|whole room|scan (the room|around)|orient (myself)?|sense of space)\b",
        lower,
        re.I,
    ):
        c += 3
    if re.search(r"\b(navigate\b|toward the|nearest exit\b|walking path)\b", lower, re.I):
        c += 2
    if re.search(
        r"\b(guide me|cooking|cook\b|knife\b|pour(ing|\b)|liquid\b|moving toward|approaching|someone (coming|walking))\b",
        lower,
        re.I,
    ):
        c += 3

    return {"A": a, "B": b, "C": c}, lower


def heuristic_plan(raw: str) -> ModalityPlan:
    scores, lower = modality_scores_heuristic(raw)
    mod = finalize_blind_modality(raw, _pick_mod(scores))
    frames, clip_ms, line = _plan_clip(lower, mod)
    m = _margin(scores)
    tag = "[A]" if mod == "A" else "[B]" if mod == "B" else "[C]"
    return ModalityPlan(
        modality=mod,
        frames=frames,
        clip_ms=clip_ms,
        reason_line=f"{tag} {line}",
        score_a=scores["A"],
        score_b=scores["B"],
        score_c=scores["C"],
        margin=m,
    )



HEURISTIC_TRUST_MARGIN = float(os.environ.get("ROUTER_HEURISTIC_TRUST_MARGIN", "1.65"))


def _tag(mod: str) -> str:
    return {"A": "[A]", "B": "[B]", "C": "[C]"}.get(mod, "[A]")


def clean_short_reason(s: str) -> str:
    s = " ".join((s or "").split())
    if len(s) > 120:
        s = s[:117].rstrip() + "…"
    return s


def _router_llm_first_enabled() -> bool:
    """Prefer conversational LLM routing when unset (GPT-like intent); set ROUTER_LLM_FIRST=false to skip."""
    v = os.environ.get("ROUTER_LLM_FIRST")
    if v is None or not str(v).strip():
        return True
    return str(v).strip().lower() not in ("0", "false", "no", "off")


async def modality_route_with_llm_fallback(raw: str) -> tuple[ModalityPlan, str]:
    """
    Modality A/B/C routing: optional LLM-first (conversational intent), else heuristic with LLM on ties.
    """
    from app import ollama_client

    hp = heuristic_plan(raw)
    scores, lower = modality_scores_heuristic(raw)

    def build_plan(mod: str, rsn: str, *, llm_suffix: bool) -> ModalityPlan:
        mod_clean = finalize_blind_modality(raw, mod if mod in ("A", "B", "C") else hp.modality)
        frames, clip_ms, clip_line = _plan_clip(lower, mod_clean)
        reason_line = _tag(mod_clean) + f" {clip_line}"
        rsn_c = clean_short_reason(rsn)
        if rsn_c:
            reason_line += f" — {rsn_c}"
        elif llm_suffix:
            reason_line += " — LLM review"
        return ModalityPlan(
            modality=mod_clean,
            frames=frames,
            clip_ms=clip_ms,
            reason_line=reason_line,
            score_a=scores["A"],
            score_b=scores["B"],
            score_c=scores["C"],
            margin=hp.margin,
        )

    configured = ollama_client.ollama_configured()
    skip_all_llm = os.environ.get("ROUTER_SKIP_LLM", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    llm_first = _router_llm_first_enabled()

    if configured and llm_first and not skip_all_llm:
        try:
            jr = await ollama_client.modality_route_completion(raw)
            mod = str(jr.get("modality") or "").strip().upper()[:1]
            rsn = str(jr.get("reason") or jr.get("rationale") or "").strip()
            return build_plan(mod, rsn, llm_suffix=True), "llm_first"
        except Exception:
            return hp, "heuristic"

    skip_ambiguous_llm = hp.margin >= HEURISTIC_TRUST_MARGIN or skip_all_llm or not configured
    if skip_ambiguous_llm:
        return hp, "heuristic"

    try:
        jr = await ollama_client.modality_route_completion(raw)
    except Exception:
        return hp, "heuristic"
    mod = str(jr.get("modality") or "").strip().upper()[:1]
    rsn = str(jr.get("reason") or jr.get("rationale") or "").strip()
    return build_plan(mod, rsn, llm_suffix=True), "llm"


# Back-compat alias if anything referenced the old name
router_margin_llm_trigger = HEURISTIC_TRUST_MARGIN

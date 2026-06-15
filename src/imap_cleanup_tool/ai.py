"""LLM evaluation for AI Cleanup.

Takes the local heuristic report (flagged senders + sample subjects) and asks a
model - via **litellm** (imported lazily; the ``[ai]`` extra) - to judge which
senders are safe to delete, returning strict JSON. Prompt building and response
parsing are plain functions so they can be unit-tested without litellm.
"""

from __future__ import annotations

import json
import re

_SYSTEM = (
    "You are an email-triage assistant. The user wants to bulk-delete junk, "
    "newsletters and promotional mail they do not read. You receive senders "
    "already pre-flagged by a heuristic, with stats and a few sample subjects. "
    "For each sender decide if it is SAFE to delete. Be conservative: keep "
    "anything that looks personal, transactional, financial, security-related "
    "or otherwise important. Respond with STRICT JSON only - no prose, no code "
    "fences - shaped exactly like: "
    '{"verdicts":[{"sender":"a@b.com","delete":true,"reason":"...",'
    '"confidence":0.0}]}')


def build_messages(report: dict) -> tuple[str, str]:
    """Return (system, user) messages for the flagged senders in ``report``."""
    items = []
    for s in report.get("senders", []):
        if not s.get("flagged"):
            continue
        items.append({
            "sender": s["sender"], "messages": s["count"],
            "unread_ratio": s["unread_ratio"], "per_week": s["per_week"],
            "list_unsubscribe": s["list_unsubscribe"],
            "heuristic_score": s["score"],
            "sample_subjects": [x["subject"] for x in s.get("samples", [])],
        })
    user = "Senders to evaluate:\n" + json.dumps(items, ensure_ascii=False)
    return _SYSTEM, user


def _extract_json(content: str):
    """Pull the first JSON object out of the reply (tolerant of fences/prose)."""
    if not content:
        return None
    text = content.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def validate_verdicts(content: str) -> dict:
    """Validate the reply with **pydantic** and return the flat verdicts dict.

    Raises ``ValueError`` if the reply is not valid JSON or not the expected
    schema - the caller uses that to retry the model.
    """
    from pydantic import BaseModel, ValidationError

    class _Verdict(BaseModel):
        sender: str
        delete: bool
        reason: str = ""
        confidence: float | None = None

    class _Response(BaseModel):
        verdicts: list[_Verdict]

    raw = _extract_json(content)
    if raw is None:
        raise ValueError("reply is not valid JSON")
    try:
        parsed = _Response.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"reply does not match the expected schema: {exc}") from exc
    out: dict = {}
    for v in parsed.verdicts:
        sender = v.sender.strip().lower()
        if sender:
            out[sender] = {"delete": v.delete, "reason": v.reason,
                           "confidence": v.confidence}
    return out


def parse_verdicts(content: str) -> dict:
    """Lenient parse: returns {} on any problem (no retry)."""
    try:
        return validate_verdicts(content)
    except ValueError:
        return {}


def evaluate(report: dict, model_cfg: dict, max_retries: int = 3) -> dict:
    """Call the LLM (with up to ``max_retries`` attempts to get valid JSON).

    Returns {verdicts, prompt_tokens, completion_tokens, cost} (tokens summed
    across attempts). ``model_cfg`` is an llm.load_model() dict. Raises
    RuntimeError if litellm (the ``[ai]`` extra) is missing or the model never
    returns a schema-valid JSON reply.
    """
    try:
        import litellm
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on extra
        raise RuntimeError("AI features need the [ai] extra: "
                           "pip install \"imap-cleanup-tool[ai]\"") from exc

    system, user = build_messages(report)
    kwargs = {"model": model_cfg["model"],
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}],
              "temperature": 0}
    if model_cfg.get("api_key"):
        kwargs["api_key"] = model_cfg["api_key"]
    if model_cfg.get("api_base"):
        kwargs["api_base"] = model_cfg["api_base"]

    pt = ct = 0
    verdicts = None
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            resp = litellm.completion(response_format={"type": "json_object"},
                                      **kwargs)
        except Exception:  # pragma: no cover - model may not support the param
            resp = litellm.completion(**kwargs)
        usage = getattr(resp, "usage", None)
        pt += int(getattr(usage, "prompt_tokens", 0) or 0)
        ct += int(getattr(usage, "completion_tokens", 0) or 0)
        try:
            verdicts = validate_verdicts(resp.choices[0].message.content)
            break
        except ValueError as exc:
            last_err = str(exc)
            continue
    if verdicts is None:
        raise RuntimeError(f"The model did not return valid JSON after "
                           f"{max_retries} attempts ({last_err}).")

    cost = None
    if model_cfg.get("track_costs"):
        cost = round(pt / 1e6 * float(model_cfg.get("cost_input", 0) or 0)
                     + ct / 1e6 * float(model_cfg.get("cost_output", 0) or 0), 6)
    return {"verdicts": verdicts, "prompt_tokens": pt,
            "completion_tokens": ct, "cost": cost}

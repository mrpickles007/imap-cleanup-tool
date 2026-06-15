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


def parse_verdicts(content: str) -> dict:
    """Parse the model's JSON reply into {sender_lower: {delete, reason, confidence}}.

    Tolerant of code fences or stray prose around the JSON object.
    """
    if not content:
        return {}
    text = content.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    out: dict = {}
    for v in (data.get("verdicts") or []):
        sender = (v.get("sender") or "").strip().lower()
        if sender:
            out[sender] = {"delete": bool(v.get("delete")),
                           "reason": v.get("reason", ""),
                           "confidence": v.get("confidence")}
    return out


def evaluate(report: dict, model_cfg: dict) -> dict:
    """Call the LLM and return {verdicts, prompt_tokens, completion_tokens, cost}.

    ``model_cfg`` is an llm.load_model() dict (model, api_key, api_base, costs).
    Raises RuntimeError if the ``[ai]`` extra (litellm) is not installed.
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
    try:
        resp = litellm.completion(response_format={"type": "json_object"}, **kwargs)
    except Exception:  # pragma: no cover - model may not support response_format
        resp = litellm.completion(**kwargs)

    content = resp.choices[0].message.content
    verdicts = parse_verdicts(content)
    usage = getattr(resp, "usage", None)
    pt = int(getattr(usage, "prompt_tokens", 0) or 0)
    ct = int(getattr(usage, "completion_tokens", 0) or 0)
    cost = None
    if model_cfg.get("track_costs"):
        cost = round(pt / 1e6 * float(model_cfg.get("cost_input", 0) or 0)
                     + ct / 1e6 * float(model_cfg.get("cost_output", 0) or 0), 6)
    return {"verdicts": verdicts, "prompt_tokens": pt,
            "completion_tokens": ct, "cost": cost}

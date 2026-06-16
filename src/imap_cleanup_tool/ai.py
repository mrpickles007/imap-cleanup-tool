"""LLM evaluation for AI Cleanup.

Takes the local heuristic report (flagged senders + sample subjects) and asks a
model - via **litellm** (imported lazily; the ``[ai]`` extra) - to judge which
senders are safe to delete, returning strict JSON. Prompt building and response
parsing are plain functions so they can be unit-tested without litellm.
"""

from __future__ import annotations

import json
import re

from .core import StopRequested, logger

# Senders are sent to the model in batches: one giant request for hundreds of
# senders overflows the model's output limit (truncated -> invalid JSON) and can
# hang. Small batches keep each call bounded, cancellable and loggable.
LLM_BATCH_SIZE = 25
LLM_TIMEOUT = 120          # seconds per LLM request

_SYSTEM = (
    "You are an email-triage assistant. The user wants to bulk-delete junk, "
    "newsletters and promotional mail they do not read. You receive senders "
    "already pre-flagged by a heuristic, with stats and a few sample subjects. "
    "For each sender decide if it is SAFE to delete. Be conservative - when in "
    "doubt, KEEP it (delete=false).\n"
    "NEVER mark as safe to delete anything that looks like: online orders, "
    "receipts, invoices, or shipping/delivery/tracking updates; appointments, "
    "reservations, bookings, or calendar invites; medical or health messages "
    "(doctor visits, test results, prescriptions, pharmacies, insurance); travel "
    "(flights, boarding passes, hotels, car rentals); banking, payments, tax, or "
    "other financial matters; security or account messages (2FA/verification "
    "codes, password resets, login or security alerts, account changes); "
    "government, legal, or official correspondence; or personal messages from a "
    "real person. If a sender mixes these with marketing, KEEP it.\n"
    "Only mark as safe to delete the obvious bulk senders the user clearly does "
    "not read: newsletters, promotions, marketing, sales, and automated social/"
    "app notifications.\n"
    "Respond with STRICT JSON only - no prose, no code fences - shaped exactly "
    'like: {"verdicts":[{"sender":"a@b.com","delete":true,"reason":"...",'
    '"confidence":0.0}]}')


def _sender_payload(s: dict) -> dict:
    """The compact, body-free per-sender data sent to the model."""
    return {
        "sender": s["sender"], "messages": s["count"],
        "unread_ratio": s["unread_ratio"], "per_week": s["per_week"],
        "list_unsubscribe": s["list_unsubscribe"],
        "heuristic_score": s["score"],
        "sample_subjects": [x["subject"] for x in s.get("samples", [])],
    }


def _user_message(senders: list[dict]) -> str:
    items = [_sender_payload(s) for s in senders]
    return "Senders to evaluate:\n" + json.dumps(items, ensure_ascii=False)


def build_messages(report: dict) -> tuple[str, str]:
    """Return (system, user) messages for the flagged senders in ``report``."""
    flagged = [s for s in report.get("senders", []) if s.get("flagged")]
    return _SYSTEM, _user_message(flagged)


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


def _call_once(litellm, kwargs: dict):
    """One LLM call, preferring strict JSON mode but tolerating models without it."""
    try:
        return litellm.completion(response_format={"type": "json_object"},
                                  **kwargs)
    except Exception:  # pragma: no cover - model may not support the param
        return litellm.completion(**kwargs)


def _evaluate_batch(litellm, base_kwargs: dict, senders: list[dict],
                    max_retries: int) -> tuple[dict, int, int]:
    """Evaluate one batch of senders; returns (verdicts, prompt_tok, compl_tok)."""
    kwargs = dict(base_kwargs)
    kwargs["messages"] = [{"role": "system", "content": _SYSTEM},
                          {"role": "user", "content": _user_message(senders)}]
    pt = ct = 0
    last_err = ""
    for _ in range(max_retries):
        resp = _call_once(litellm, kwargs)
        usage = getattr(resp, "usage", None)
        pt += int(getattr(usage, "prompt_tokens", 0) or 0)
        ct += int(getattr(usage, "completion_tokens", 0) or 0)
        try:
            return validate_verdicts(resp.choices[0].message.content), pt, ct
        except ValueError as exc:
            last_err = str(exc)
    raise RuntimeError(f"The model did not return valid JSON after "
                       f"{max_retries} attempts ({last_err}).")


def _batch_cost(model_cfg: dict, pt: int, ct: int):
    """Cost for pt/ct tokens, or None when the model has no cost tracking."""
    if not model_cfg.get("track_costs"):
        return None
    return round(pt / 1e6 * float(model_cfg.get("cost_input", 0) or 0)
                 + ct / 1e6 * float(model_cfg.get("cost_output", 0) or 0), 6)


def evaluate(report: dict, model_cfg: dict, max_retries: int = 3,
             batch_size: int = LLM_BATCH_SIZE, should_stop=None,
             timeout: int = LLM_TIMEOUT, record_cost=None,
             known_spam: set | None = None) -> dict:
    """Ask the LLM which flagged senders to delete, in batches.

    Flagged senders are sent ``batch_size`` at a time (each call retried up to
    ``max_retries`` for valid JSON, with a per-request ``timeout``); progress is
    logged and ``should_stop`` is honored between batches. Returns
    {verdicts, prompt_tokens, completion_tokens, cost}. ``model_cfg`` is an
    llm.load_model() dict.

    ``record_cost(prompt_tokens, completion_tokens, cost)`` is called **after each
    batch** so token usage is tracked even if a later batch fails or the run is
    stopped (the API has already billed for completed batches). Raises
    RuntimeError if litellm (the ``[ai]`` extra) is missing or a batch never
    returns schema-valid JSON.
    """
    try:
        import litellm
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on extra
        raise RuntimeError("AI features need the [ai] extra: "
                           "pip install \"imap-cleanup-tool[ai]\"") from exc

    base_kwargs = {"model": model_cfg["model"], "temperature": 0,
                   "timeout": timeout}
    if model_cfg.get("api_key"):
        base_kwargs["api_key"] = model_cfg["api_key"]
    if model_cfg.get("api_base"):
        base_kwargs["api_base"] = model_cfg["api_base"]

    flagged_all = [s for s in report.get("senders", []) if s.get("flagged")]
    verdicts: dict = {}
    # Senders already saved as spam (from earlier reports/runs) are accepted as
    # spam WITHOUT asking the LLM again - this saves tokens. They get a synthetic
    # verdict so the report/run treats them as confirmed for deletion.
    known = {a.strip().lower() for a in (known_spam or set()) if a}
    if known:
        skipped = 0
        for s in flagged_all:
            if s["sender"].lower() in known:
                verdicts[s["sender"].lower()] = {
                    "delete": True,
                    "reason": "already in saved spam list (LLM skipped)",
                    "confidence": 1.0}
                skipped += 1
        if skipped:
            logger.info("Skipping %d flagged sender(s) already saved as spam "
                        "(not sent to the LLM - saves tokens).", skipped)
    flagged = [s for s in flagged_all if s["sender"].lower() not in known]
    total = len(flagged)
    pt = ct = 0
    for start in range(0, total, max(1, batch_size)):
        if should_stop is not None and should_stop():
            raise StopRequested
        batch = flagged[start:start + batch_size]
        v, bpt, bct = _evaluate_batch(litellm, base_kwargs, batch, max_retries)
        verdicts.update(v)
        pt += bpt
        ct += bct
        if record_cost is not None and (bpt or bct):
            record_cost(bpt, bct, _batch_cost(model_cfg, bpt, bct) or 0)
        logger.info("LLM evaluated %d/%d flagged sender(s) ...",
                    min(start + batch_size, total), total)

    return {"verdicts": verdicts, "prompt_tokens": pt,
            "completion_tokens": ct, "cost": _batch_cost(model_cfg, pt, ct)}

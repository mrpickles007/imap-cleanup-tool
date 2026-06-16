"""Per-account spam-address store (local SQLite).

Every AI Cleanup report/run records the **flagged** senders (the potential spam)
together with the data we computed for them - the 0-10 heuristic spam score, the
signals, and the LLM verdict when one was produced - keyed by the **account**
(the connected mailbox address) so each connection has its own list.

The web UI exposes this as the *Spam addresses* tab: browse (paginated), remove
from the list, or flag the sender as spam on the mail server (move its messages
to the Junk/Spam folder). Pure standard library.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .scheduler import config_dir


def db_path() -> Path:
    """Path to the SQLite file holding per-account spam addresses."""
    return config_dir() / "spam.sqlite"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS spam ("
        " account TEXT NOT NULL,"
        " address TEXT NOT NULL,"
        " score REAL,"
        " messages INTEGER,"
        " unread INTEGER,"
        " unread_ratio REAL,"
        " per_week REAL,"
        " list_unsubscribe INTEGER,"
        " bulk INTEGER,"
        " sender_pattern INTEGER,"
        " verdict_delete INTEGER,"        # NULL = no LLM verdict
        " verdict_reason TEXT,"
        " verdict_confidence REAL,"
        " source TEXT,"                   # 'report' | 'run'
        " updated_at TEXT,"
        " PRIMARY KEY (account, address))")
    return conn


def record_from_report(account: str, report: dict, source: str = "report") -> int:
    """Upsert every flagged sender from an AI report. Returns the number stored."""
    account = (account or "").strip().lower()
    if not account:
        return 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    rows = []
    for s in report.get("senders", []):
        if not s.get("flagged"):
            continue
        v = s.get("verdict") or {}
        vd = v.get("delete")
        rows.append((
            account, (s.get("sender") or "").lower(), s.get("score"),
            s.get("count"), s.get("unread"), s.get("unread_ratio"),
            s.get("per_week"), 1 if s.get("list_unsubscribe") else 0,
            1 if s.get("bulk") else 0, 1 if s.get("sender_pattern") else 0,
            (None if vd is None else (1 if vd else 0)),
            v.get("reason") if v else None,
            v.get("confidence") if v else None, source, now))
    if not rows:
        return 0
    conn = _connect()
    try:
        conn.executemany(
            "INSERT INTO spam (account, address, score, messages, unread,"
            " unread_ratio, per_week, list_unsubscribe, bulk, sender_pattern,"
            " verdict_delete, verdict_reason, verdict_confidence, source,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(account, address) DO UPDATE SET score=excluded.score,"
            " messages=excluded.messages, unread=excluded.unread,"
            " unread_ratio=excluded.unread_ratio, per_week=excluded.per_week,"
            " list_unsubscribe=excluded.list_unsubscribe, bulk=excluded.bulk,"
            " sender_pattern=excluded.sender_pattern,"
            " verdict_delete=COALESCE(excluded.verdict_delete, spam.verdict_delete),"
            " verdict_reason=COALESCE(excluded.verdict_reason, spam.verdict_reason),"
            " verdict_confidence=COALESCE(excluded.verdict_confidence,"
            "  spam.verdict_confidence), source=excluded.source,"
            " updated_at=excluded.updated_at", rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def _row_dict(r: sqlite3.Row) -> dict:
    return {"address": r["address"], "score": r["score"],
            "messages": r["messages"], "unread": r["unread"],
            "unread_ratio": r["unread_ratio"], "per_week": r["per_week"],
            "list_unsubscribe": bool(r["list_unsubscribe"]),
            "bulk": bool(r["bulk"]), "sender_pattern": bool(r["sender_pattern"]),
            "verdict_delete": (None if r["verdict_delete"] is None
                               else bool(r["verdict_delete"])),
            "verdict_reason": r["verdict_reason"],
            "verdict_confidence": r["verdict_confidence"],
            "source": r["source"], "updated_at": r["updated_at"]}


# Whitelisted sort columns/expressions (the request maps a key here; never
# interpolate user input directly into SQL).
_SORTS = {
    "score": "score",
    "messages": "messages",
    "unread": "unread_ratio",
    "per_week": "per_week",
    "signals": "(list_unsubscribe + bulk + sender_pattern)",
    "verdict": "verdict_delete",          # keep/delete; then confidence
    "address": "address COLLATE NOCASE",
}


def list_addresses(account: str, *, offset: int = 0, limit: int = 25,
                   search: str = "", sort_by: str = "score",
                   sort_dir: str = "desc") -> dict:
    """Return {items, total, offset, limit, sort_by, sort_dir} for an account.

    Sorting is over the **whole** list (then paginated). ``sort_by`` is one of
    _SORTS; ``sort_dir`` is 'asc' or 'desc'.
    """
    account = (account or "").strip().lower()
    where, params = "account=?", [account]
    if (search or "").strip():
        where += " AND address LIKE ?"
        params.append(f"%{search.strip().lower()}%")
    col = _SORTS.get(sort_by, _SORTS["score"])
    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    order = f"{col} {direction}"
    if sort_by == "verdict":
        order += f", verdict_confidence {direction}"
    order += ", address COLLATE NOCASE ASC"     # stable tiebreaker
    conn = _connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM spam WHERE {where}",
                             params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM spam WHERE {where} ORDER BY {order}"
            " LIMIT ? OFFSET ?", params + [int(limit), int(offset)]).fetchall()
    finally:
        conn.close()
    return {"items": [_row_dict(r) for r in rows], "total": total,
            "offset": int(offset), "limit": int(limit),
            "sort_by": sort_by if sort_by in _SORTS else "score",
            "sort_dir": direction.lower()}


def delete_addresses(account: str, addresses: list) -> int:
    """Remove the given addresses from an account's spam list. Returns count."""
    account = (account or "").strip().lower()
    addrs = [a.strip().lower() for a in (addresses or []) if a and a.strip()]
    if not addrs:
        return 0
    conn = _connect()
    try:
        qs = ",".join("?" * len(addrs))
        cur = conn.execute(
            f"DELETE FROM spam WHERE account=? AND address IN ({qs})",
            [account] + addrs)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def all_addresses(account: str) -> list:
    """Every spam address for an account (used by 'select all' bulk ops)."""
    account = (account or "").strip().lower()
    conn = _connect()
    try:
        rows = conn.execute("SELECT address FROM spam WHERE account=?"
                            " ORDER BY score DESC", (account,)).fetchall()
    finally:
        conn.close()
    return [r["address"] for r in rows]

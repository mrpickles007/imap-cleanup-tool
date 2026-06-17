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
        " unsub_mailto TEXT,"             # List-Unsubscribe mailto: target
        " unsub_http TEXT,"               # List-Unsubscribe https URL
        " unsub_oneclick INTEGER,"        # RFC 8058 one-click POST supported
        " unsub_done_at TEXT,"            # when we unsubscribed (NULL = not yet)
        " unsub_done_method TEXT,"        # 'email' | 'oneclick'
        " unsub_done_result TEXT,"        # human-readable outcome
        " updated_at TEXT,"
        " PRIMARY KEY (account, address))")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(spam)")}
    for col, typ in (("unsub_mailto", "TEXT"), ("unsub_http", "TEXT"),
                     ("unsub_oneclick", "INTEGER"), ("unsub_done_at", "TEXT"),
                     ("unsub_done_method", "TEXT"), ("unsub_done_result", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE spam ADD COLUMN {col} {typ}")
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
            v.get("confidence") if v else None, source,
            s.get("unsub_mailto"), s.get("unsub_http"),
            1 if s.get("unsub_oneclick") else 0, now))
    if not rows:
        return 0
    conn = _connect()
    try:
        conn.executemany(
            "INSERT INTO spam (account, address, score, messages, unread,"
            " unread_ratio, per_week, list_unsubscribe, bulk, sender_pattern,"
            " verdict_delete, verdict_reason, verdict_confidence, source,"
            " unsub_mailto, unsub_http, unsub_oneclick,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(account, address) DO UPDATE SET score=excluded.score,"
            " messages=excluded.messages, unread=excluded.unread,"
            " unread_ratio=excluded.unread_ratio, per_week=excluded.per_week,"
            " list_unsubscribe=excluded.list_unsubscribe, bulk=excluded.bulk,"
            " sender_pattern=excluded.sender_pattern,"
            " verdict_delete=COALESCE(excluded.verdict_delete, spam.verdict_delete),"
            " verdict_reason=COALESCE(excluded.verdict_reason, spam.verdict_reason),"
            " verdict_confidence=COALESCE(excluded.verdict_confidence,"
            "  spam.verdict_confidence), source=excluded.source,"
            " unsub_mailto=COALESCE(excluded.unsub_mailto, spam.unsub_mailto),"
            " unsub_http=COALESCE(excluded.unsub_http, spam.unsub_http),"
            " unsub_oneclick=excluded.unsub_oneclick,"
            " updated_at=excluded.updated_at", rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def _row_dict(r: sqlite3.Row) -> dict:
    mailto = r["unsub_mailto"]
    http = r["unsub_http"]
    oneclick = bool(r["unsub_oneclick"])
    # "auto" = we can unsubscribe without the user (send an email, or one-click POST)
    auto = bool(mailto) or (bool(http) and oneclick)
    # how it would be done (mailto wins, matching the unsubscribe endpoint):
    #   "email"    -> auto, an unsubscribe email sent from the active SMTP profile
    #   "oneclick" -> auto, a one-click HTTPS POST (RFC 8058)
    #   "link"     -> manual, a plain link that opens a confirmation page
    #   ""         -> nothing actionable captured
    if mailto:
        kind = "email"
    elif http and oneclick:
        kind = "oneclick"
    elif http:
        kind = "link"
    else:
        kind = ""
    return {"address": r["address"], "score": r["score"], "unsub_kind": kind,
            "messages": r["messages"], "unread": r["unread"],
            "unread_ratio": r["unread_ratio"], "per_week": r["per_week"],
            "list_unsubscribe": bool(r["list_unsubscribe"]),
            "bulk": bool(r["bulk"]), "sender_pattern": bool(r["sender_pattern"]),
            "verdict_delete": (None if r["verdict_delete"] is None
                               else bool(r["verdict_delete"])),
            "verdict_reason": r["verdict_reason"],
            "verdict_confidence": r["verdict_confidence"],
            "source": r["source"], "updated_at": r["updated_at"],
            # unsubscribe info for the UI: auto badge, or a link to open
            "unsub_auto": auto, "unsub_url": http or None,
            "unsub_can": bool(mailto or http),
            # outcome of a performed unsubscribe (NULL until we do one)
            "unsub_done_at": r["unsub_done_at"],
            "unsub_done_method": r["unsub_done_method"],
            "unsub_done_result": r["unsub_done_result"]}


def unsub_targets(account: str, addresses: list) -> list:
    """Per-address unsubscribe data for the given addresses (for the bulk action).

    Returns ``[{address, mailto, http, oneclick}]`` (only rows that have at least
    one unsubscribe method).
    """
    account = (account or "").strip().lower()
    addrs = [a.strip().lower() for a in (addresses or []) if a and a.strip()]
    if not addrs:
        return []
    conn = _connect()
    try:
        out = []
        for i in range(0, len(addrs), 400):
            chunk = addrs[i:i + 400]
            ph = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT address, unsub_mailto, unsub_http, unsub_oneclick "
                f"FROM spam WHERE account=? AND address IN ({ph})",
                [account] + chunk).fetchall()
            for r in rows:
                if r["unsub_mailto"] or r["unsub_http"]:
                    out.append({"address": r["address"],
                                "mailto": r["unsub_mailto"],
                                "http": r["unsub_http"],
                                "oneclick": bool(r["unsub_oneclick"])})
        return out
    finally:
        conn.close()


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


# Unsubscribe-capability filters for the Spam tab. "auto" = we can do it without
# the user (mailto, or a one-click http link); "manual" = only a plain http link
# that opens a confirmation page; "none" = no List-Unsubscribe at all.
_UNSUB_AUTO = ("((unsub_mailto IS NOT NULL AND unsub_mailto<>'')"
               " OR (unsub_http IS NOT NULL AND unsub_http<>'' AND unsub_oneclick=1))")
_UNSUB_ANY = ("((unsub_mailto IS NOT NULL AND unsub_mailto<>'')"
              " OR (unsub_http IS NOT NULL AND unsub_http<>''))")
_UNSUB_FILTERS = {
    "auto": _UNSUB_AUTO,
    "manual": f"({_UNSUB_ANY} AND NOT {_UNSUB_AUTO})",
    "none": f"NOT {_UNSUB_ANY}",
}


def list_addresses(account: str, *, offset: int = 0, limit: int = 25,
                   search: str = "", unsub: str = "all", sort_by: str = "score",
                   sort_dir: str = "desc") -> dict:
    """Return {items, total, offset, limit, sort_by, sort_dir} for an account.

    Sorting is over the **whole** list (then paginated). ``sort_by`` is one of
    _SORTS; ``sort_dir`` is 'asc' or 'desc'. ``unsub`` filters by unsubscribe
    capability (all / auto / manual / none).
    """
    account = (account or "").strip().lower()
    where, params = "account=?", [account]
    if (search or "").strip():
        where += " AND address LIKE ?"
        params.append(f"%{search.strip().lower()}%")
    uf = _UNSUB_FILTERS.get((unsub or "all").lower())
    if uf:
        where += f" AND {uf}"
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


def count(account: str) -> int:
    """How many spam addresses are saved for an account."""
    account = (account or "").strip().lower()
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM spam WHERE account=?",
                            (account,)).fetchone()[0]
    finally:
        conn.close()


def mark_unsubscribed(account: str, address: str, method: str, result: str,
                      at: str | None = None) -> bool:
    """Record that we unsubscribed ``address`` (method/result/timestamp).

    ``method`` is 'email' or 'oneclick'; ``result`` is a short human-readable
    outcome. ``at`` defaults to now. Returns True if a row was updated.
    """
    account = (account or "").strip().lower()
    address = (address or "").strip().lower()
    if not account or not address:
        return False
    if at is None:
        at = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE spam SET unsub_done_at=?, unsub_done_method=?,"
            " unsub_done_result=? WHERE account=? AND address=?",
            (at, method, result, account, address))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def count_unsub_email(account: str) -> int:
    """Senders whose automatic unsubscribe needs an email (a ``mailto:`` target).

    These are the ones that require an active SMTP profile to unsubscribe
    automatically; used to decide whether to warn the user.
    """
    account = (account or "").strip().lower()
    conn = _connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM spam WHERE account=? AND unsub_mailto IS NOT NULL"
            " AND unsub_mailto<>''", (account,)).fetchone()[0]
    finally:
        conn.close()


def add_address(account: str, address: str, score: float | None = None) -> bool:
    """Manually add (or update the score of) a spam address. Returns True if saved.

    Manual entries carry ``source='manual'``; an existing address keeps its
    richer report data and only takes the new score (when one is given).
    """
    account = (account or "").strip().lower()
    address = (address or "").strip().lower()
    if not account or "@" not in address:
        return False
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO spam (account, address, score, source, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(account, address) DO UPDATE SET"
            " score=COALESCE(excluded.score, spam.score),"
            " source=excluded.source, updated_at=excluded.updated_at",
            (account, address, score, "manual", now))
        conn.commit()
    finally:
        conn.close()
    return True


# Score-filter operators offered to the "Load saved Spam addresses" target box.
_SCORE_OPS = {"is": "=", "le": "<=", "ge": ">=", "lt": "<", "gt": ">"}


def addresses_by_score(account: str, op: str, score: float) -> list:
    """Addresses whose score satisfies ``score <op> value`` (NULL scores skipped).

    ``op`` is one of is / le / ge / lt / gt. Used to load a filtered subset of
    the spam list into the Target list.
    """
    account = (account or "").strip().lower()
    sql_op = _SCORE_OPS.get(op)
    if sql_op is None:
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT address FROM spam WHERE account=? AND score IS NOT NULL"
            f" AND score {sql_op} ? ORDER BY score DESC",
            (account, float(score))).fetchall()
    finally:
        conn.close()
    return [r["address"] for r in rows]

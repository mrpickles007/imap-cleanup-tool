"""Local cache of message header metadata, to speed up repeat AI reports.

Fetching the ``From``/``Date``/``Subject`` headers for every message is the slow
part of building an AI report - on a slow IMAP server it can take seconds per 50
messages. Those header fields **never change** for a given message, so we cache
them locally (SQLite) keyed by the message's IMAP **UID**.

UIDs are only stable within a folder's ``UIDVALIDITY`` (the server bumps it if it
ever renumbers - folder recreated, mailbox migrated, ...), so the key includes it;
a changed ``UIDVALIDITY`` makes the old rows for that folder unusable and they are
purged. The cache is **per account** (host+user), so different mailboxes never mix.

This is **opt-in per connection profile** ("Enable local cache"). The volatile
``\\Seen`` flag is NOT cached - callers re-read it cheaply each time so the unread
count stays accurate. Stdlib only (``sqlite3``); no third-party deps.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .scheduler import config_dir


def db_path() -> Path:
    """Path to the SQLite file holding cached header metadata."""
    return config_dir() / "header_cache.sqlite"


class HeaderCache:
    """Per-(account, folder, uidvalidity, uid) store of immutable header fields."""

    def __init__(self, path: Path | None = None):
        self._path = path or db_path()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS headers ("
                " account TEXT NOT NULL,"
                " folder TEXT NOT NULL,"
                " uidvalidity TEXT NOT NULL,"
                " uid TEXT NOT NULL,"
                " sender TEXT,"
                " date_h TEXT,"
                " unsub INTEGER NOT NULL DEFAULT 0,"
                " bulk INTEGER NOT NULL DEFAULT 0,"
                " subject TEXT,"
                " from_header TEXT,"        # raw From (for list-senders/full-scan)
                " unsub_value TEXT,"        # raw List-Unsubscribe header
                " unsub_post TEXT,"         # raw List-Unsubscribe-Post header
                " message_id TEXT,"         # Message-ID (for import de-duplication)
                " PRIMARY KEY (account, folder, uidvalidity, uid))")
            # Migrate caches created before later columns existed.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(headers)")}
            for col in ("from_header", "unsub_value", "unsub_post", "message_id"):
                if col not in cols:
                    conn.execute(f"ALTER TABLE headers ADD COLUMN {col} TEXT")
            conn.commit()
        finally:
            conn.close()

    def purge_other(self, account: str, folder: str, uidvalidity: str) -> None:
        """Drop cached rows for this folder whose UIDVALIDITY differs (stale)."""
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM headers WHERE account=? AND folder=? "
                "AND uidvalidity<>?", (account, folder, uidvalidity))
            conn.commit()
        finally:
            conn.close()

    def get(self, account: str, folder: str, uidvalidity: str,
            uids: list[str]) -> dict[str, dict]:
        """Return cached rows for the given UIDs as ``{uid: {fields...}}``."""
        if not uids:
            return {}
        conn = self._connect()
        try:
            out: dict[str, dict] = {}
            # Chunk the IN(...) list to stay well under SQLite's variable limit.
            for i in range(0, len(uids), 400):
                chunk = uids[i:i + 400]
                ph = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT uid, sender, date_h, unsub, bulk, subject, "
                    f"unsub_value, unsub_post "
                    f"FROM headers WHERE account=? AND folder=? AND uidvalidity=? "
                    f"AND date_h IS NOT NULL AND uid IN ({ph})",  # AI-complete rows
                    (account, folder, uidvalidity, *chunk)).fetchall()
                for r in rows:
                    out[r["uid"]] = {
                        "sender": r["sender"] or "(no sender)",
                        "date_h": r["date_h"] or "",
                        "unsub": bool(r["unsub"]), "bulk": bool(r["bulk"]),
                        "subject": r["subject"] or "",
                        "unsub_value": r["unsub_value"] or "",
                        "unsub_post": r["unsub_post"] or ""}
            return out
        finally:
            conn.close()

    def put(self, account: str, folder: str, uidvalidity: str,
            rows: dict[str, dict]) -> None:
        """Insert/replace full (AI) cached rows; ``rows`` is ``{uid: {fields}}``.

        Also stores the raw ``from_header`` so these rows serve the From-only
        consumers (list-senders / full-scan) too.
        """
        if not rows:
            return
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO headers"
                " (account, folder, uidvalidity, uid, sender, date_h, unsub,"
                "  bulk, subject, from_header, unsub_value, unsub_post)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(account, folder, uidvalidity, uid) DO UPDATE SET"
                " sender=excluded.sender, date_h=excluded.date_h,"
                " unsub=excluded.unsub, bulk=excluded.bulk,"
                " subject=excluded.subject, from_header=excluded.from_header,"
                " unsub_value=excluded.unsub_value, unsub_post=excluded.unsub_post",
                [(account, folder, uidvalidity, uid, r.get("sender"),
                  r.get("date_h"), 1 if r.get("unsub") else 0,
                  1 if r.get("bulk") else 0, r.get("subject"),
                  r.get("from_header"), r.get("unsub_value"), r.get("unsub_post"))
                 for uid, r in rows.items()])
            conn.commit()
        finally:
            conn.close()

    def get_from(self, account: str, folder: str, uidvalidity: str,
                 uids: list[str]) -> dict[str, str]:
        """Return cached raw ``From`` headers as ``{uid: from_header}``.

        Serves list-senders and ``--scan-mode full``; usable rows are any with a
        stored ``from_header`` (whether populated here or by a full AI fetch).
        """
        if not uids:
            return {}
        conn = self._connect()
        try:
            out: dict[str, str] = {}
            for i in range(0, len(uids), 400):
                chunk = uids[i:i + 400]
                ph = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT uid, from_header FROM headers "
                    f"WHERE account=? AND folder=? AND uidvalidity=? "
                    f"AND from_header IS NOT NULL AND uid IN ({ph})",
                    (account, folder, uidvalidity, *chunk)).fetchall()
                for r in rows:
                    out[r["uid"]] = r["from_header"]
            return out
        finally:
            conn.close()

    def put_from(self, account: str, folder: str, uidvalidity: str,
                 rows: dict[str, str]) -> None:
        """Insert/replace From-only rows; updates only ``from_header`` on conflict
        (so it never wipes the richer fields of an existing AI row)."""
        if not rows:
            return
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO headers"
                " (account, folder, uidvalidity, uid, from_header)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(account, folder, uidvalidity, uid) DO UPDATE SET"
                " from_header=excluded.from_header",
                [(account, folder, uidvalidity, uid, fh)
                 for uid, fh in rows.items()])
            conn.commit()
        finally:
            conn.close()

    def get_message_ids(self, account: str, folder: str, uidvalidity: str,
                        uids: list[str]) -> dict[str, str]:
        """Return cached ``Message-ID`` headers as ``{uid: message_id}`` (used by
        import de-duplication); only rows that have a stored message_id."""
        if not uids:
            return {}
        conn = self._connect()
        try:
            out: dict[str, str] = {}
            for i in range(0, len(uids), 400):
                chunk = uids[i:i + 400]
                ph = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT uid, message_id FROM headers "
                    f"WHERE account=? AND folder=? AND uidvalidity=? "
                    f"AND message_id IS NOT NULL AND uid IN ({ph})",
                    (account, folder, uidvalidity, *chunk)).fetchall()
                for r in rows:
                    out[r["uid"]] = r["message_id"]
            return out
        finally:
            conn.close()

    def put_message_ids(self, account: str, folder: str, uidvalidity: str,
                       rows: dict[str, str]) -> None:
        """Insert/replace Message-ID-only rows; updates only ``message_id`` on
        conflict (never wipes the richer fields of an existing row)."""
        if not rows:
            return
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO headers"
                " (account, folder, uidvalidity, uid, message_id)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(account, folder, uidvalidity, uid) DO UPDATE SET"
                " message_id=excluded.message_id",
                [(account, folder, uidvalidity, uid, mid)
                 for uid, mid in rows.items()])
            conn.commit()
        finally:
            conn.close()

    def has_account(self, account: str) -> bool:
        """True if any cached rows exist for this account."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM headers WHERE account=? LIMIT 1",
                (account,)).fetchone()
            return row is not None
        finally:
            conn.close()

    def count_account(self, account: str) -> int:
        """How many header rows are cached for this account."""
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM headers WHERE account=?",
                (account,)).fetchone()[0]
        finally:
            conn.close()

    def clear(self, account: str | None = None) -> None:
        """Wipe the whole cache, or just one account's rows."""
        conn = self._connect()
        try:
            if account is None:
                conn.execute("DELETE FROM headers")
            else:
                conn.execute("DELETE FROM headers WHERE account=?", (account,))
            conn.commit()
        finally:
            conn.close()

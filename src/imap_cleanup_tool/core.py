"""Core IMAP operations: connect, search, list, delete, empty.

This module contains no UI and no argument parsing; it is imported by both the
CLI and the GUI. All functions accept an optional ``should_stop`` callback so a
long operation can be cancelled cooperatively at the next safe checkpoint.
"""

from __future__ import annotations

import csv
import imaplib
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import parseaddr

from .targets import sender_matches

UID_CHUNK_SIZE = 500
GMAIL_STORE_CAP = 200  # Gmail chokes on large STORE commands.
AI_FETCH_CHUNK = 50    # AI report header fetches: small batches = visible progress.

logger = logging.getLogger("imap_cleanup_tool")

StopCheck = Callable[[], bool]


class StopRequested(Exception):
    """Raised internally when a cooperative stop has been requested."""


def _check_stop(should_stop: StopCheck | None) -> None:
    if should_stop is not None and should_stop():
        raise StopRequested


def _quote_mailbox(name: str) -> str:
    """Quote a mailbox name for IMAP commands so names with spaces work.

    Without quotes, ``SELECT Posta inviata`` is parsed as two arguments and the
    server replies ``BAD Could not parse command``.
    """
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _same_mailbox(a: str, b: str) -> bool:
    """True if two mailbox names refer to the same folder.

    Names are case-sensitive in general, but ``INBOX`` is case-insensitive per
    the IMAP spec, so ``inbox`` and ``INBOX`` are the same folder.
    """
    a, b = (a or "").strip(), (b or "").strip()
    if a.upper() == "INBOX" and b.upper() == "INBOX":
        return True
    return a == b


# --------------------------------------------------------------------------- #
# Header helpers
# --------------------------------------------------------------------------- #
def decode_mime_header(value: str) -> str:
    """Decode an RFC 2047 encoded header into a plain string."""
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError):
        return value or ""


def extract_sender_email(from_header: str) -> str:
    """Return the lowercase email address from a 'From' header value."""
    decoded = decode_mime_header(from_header)
    _, addr = parseaddr(decoded)
    return addr.strip().lower()


def _extract_uid(meta: bytes) -> bytes | None:
    """Pull the UID token out of a FETCH metadata line."""
    tokens = meta.replace(b"(", b" ").replace(b")", b" ").split()
    try:
        return tokens[tokens.index(b"UID") + 1]
    except (ValueError, IndexError):
        return None


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
def connect(host: str, port: int, user: str, password: str,
            timeout: int = 120) -> imaplib.IMAP4_SSL:
    """Open an SSL IMAP connection and log in. Raises on failure."""
    logger.info("Connecting to %s:%d (timeout %ds) ...", host, port, timeout)
    conn = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    conn.login(user, password)
    logger.info("Logged in as %s.", user)
    return conn


def safe_logout(conn: imaplib.IMAP4_SSL | None) -> None:
    """Close and log out, ignoring errors. Accepts None."""
    if conn is None:
        return
    for method in ("close", "logout"):
        try:
            getattr(conn, method)()
        except (OSError, imaplib.IMAP4.error):
            pass


def list_folders(conn: imaplib.IMAP4_SSL) -> list[str]:
    """Return the list of folder names on the server."""
    status, data = conn.list()
    if status != "OK":
        logger.warning("Unable to list folders.")
        return []
    names = []
    for item in data:
        if not item:
            continue
        decoded = item.decode(errors="replace")
        match = re.search(r'"([^"]*)"\s*$', decoded)
        names.append(match.group(1) if match else decoded.split()[-1])
    return names


def folder_message_counts(conn: imaplib.IMAP4_SSL, names: list[str],
                          should_stop: StopCheck | None = None
                          ) -> dict[str, int | None]:
    """Return a {folder: message_count} map using IMAP STATUS (cheap, no fetch).

    The count is ``None`` for folders that cannot be inspected (e.g. \\Noselect
    parents like ``[Gmail]``).
    """
    counts: dict[str, int | None] = {}
    for name in names:
        _check_stop(should_stop)
        try:
            status, data = conn.status(_quote_mailbox(name), "(MESSAGES)")
        except (OSError, imaplib.IMAP4.error):
            counts[name] = None
            continue
        match = (re.search(rb"MESSAGES\s+(\d+)", data[0])
                 if status == "OK" and data and data[0] else None)
        counts[name] = int(match.group(1)) if match else None
    return counts


# --------------------------------------------------------------------------- #
# Fetching headers
# --------------------------------------------------------------------------- #
def _read_uidvalidity(conn: imaplib.IMAP4_SSL) -> str:
    """Read the current folder's UIDVALIDITY (call right after SELECT)."""
    try:
        uv = conn.response("UIDVALIDITY")[1]
        return uv[0].decode() if uv and uv[0] else ""
    except Exception:  # pylint: disable=broad-exception-caught
        return ""


def fetch_from_headers(conn: imaplib.IMAP4_SSL, uids: list[bytes],
                       batch_size: int = UID_CHUNK_SIZE,
                       should_stop: StopCheck | None = None,
                       *, cache=None, account: str = "", folder: str = "",
                       uidvalidity: str = "") -> dict[bytes, str]:
    """Fetch the 'From' header for all UIDs, in batches.

    With a ``cache`` (headercache.HeaderCache) and a known ``uidvalidity``, the
    raw From header is read from the local cache for UIDs we have already seen;
    only **new** UIDs are fetched, and the result is written back. The From header
    never changes, so this is always safe.
    """
    results: dict[bytes, str] = {}
    total = len(uids)

    use_cache = cache is not None and bool(uidvalidity)
    cached: dict[str, str] = {}
    if use_cache:
        try:
            cache.purge_other(account, folder, uidvalidity)
            cached = cache.get_from(account, folder, uidvalidity,
                                    [u.decode() for u in uids])
        except Exception:  # pylint: disable=broad-exception-caught
            cached = {}
    for u in uids:
        us = u.decode()
        if us in cached:
            results[u] = cached[us]
    if cached:
        logger.info("  %d/%d From header(s) from local cache; fetching %d new.",
                    len(cached), total, total - len(cached))

    missing = [u for u in uids if u.decode() not in cached]
    new_rows: dict[str, str] = {}
    done = 0
    logger.info("Fetching headers in batches of %d ...", batch_size)
    for i in range(0, len(missing), batch_size):
        _check_stop(should_stop)
        chunk = missing[i:i + batch_size]
        status, data = conn.uid("FETCH", b",".join(chunk),
                                "(UID BODY.PEEK[HEADER.FIELDS (FROM)])")
        if status != "OK" or not data:
            logger.warning("FETCH failed for a batch of %d messages.", len(chunk))
            continue
        for part in data:
            if not (isinstance(part, tuple) and len(part) >= 2 and part[1]):
                continue
            uid = _extract_uid(part[0])
            if uid is None:
                continue
            text = part[1].decode(errors="replace")
            value = text.split(":", 1)[1].strip() if ":" in text else ""
            results[uid] = value
            if use_cache:
                new_rows[uid.decode()] = value
        done += len(chunk)
        logger.info("  ... fetched headers %d/%d", done, len(missing))

    if use_cache and new_rows:
        try:
            cache.put_from(account, folder, uidvalidity, new_rows)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return results


# --------------------------------------------------------------------------- #
# Sender listing (with optional CSV export)
# --------------------------------------------------------------------------- #
def save_senders_csv(path: str, account: str, folder: str,
                     ranked: list[tuple[str, int]]) -> None:
    """Append sender rows to a CSV, writing a header row if the file is new."""
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    exists = os.path.isfile(path)
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(["timestamp", "account", "folder", "sender", "count"])
        for sender, count in ranked:
            writer.writerow([timestamp, account, folder, sender, count])


def list_senders(conn: imaplib.IMAP4_SSL, folder: str,
                 batch_size: int = UID_CHUNK_SIZE,
                 should_stop: StopCheck | None = None,
                 account: str = "",
                 save_path: str | None = None,
                 cache=None) -> dict[str, int]:
    """Log unique senders in a folder with counts; optionally save to CSV."""
    status, _ = conn.select(_quote_mailbox(folder), readonly=True)
    if status != "OK":
        logger.error("Cannot open folder %r.", folder)
        return {}
    uidvalidity = _read_uidvalidity(conn) if cache is not None else ""
    status, data = conn.uid("SEARCH", None, "ALL")
    if status != "OK" or not data or not data[0]:
        logger.info("Folder %r is empty.", folder)
        return {}

    all_uids = data[0].split()
    logger.info("Folder %r: inspecting %d message(s).", folder, len(all_uids))
    headers = fetch_from_headers(conn, all_uids, batch_size, should_stop,
                                 cache=cache, account=account, folder=folder,
                                 uidvalidity=uidvalidity)

    counts: dict[str, int] = {}
    for value in headers.values():
        sender = extract_sender_email(value) or "(no sender)"
        counts[sender] = counts.get(sender, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    logger.info("Unique senders in %r (count | address):", folder)
    for sender, num in ranked:
        logger.info("  %5d | %s", num, sender)

    if save_path:
        save_senders_csv(save_path, account, folder, ranked)
        logger.info("Saved %d sender(s) to %s", len(ranked), save_path)
    return counts


# --------------------------------------------------------------------------- #
# Searching for messages to act on
# --------------------------------------------------------------------------- #
def search_targets(conn: imaplib.IMAP4_SSL, addresses: set[str],
                   domains: set[str], exact_domains: set[str] | None = None,
                   should_stop: StopCheck | None = None) -> set[bytes]:
    """Find UIDs by sender using one IMAP 'SEARCH FROM' per target term.

    Note: server-side SEARCH FROM is a substring match, so the exact-domain
    (``*@``) distinction is not enforced here - it only applies to ``full`` mode.
    """
    found: set[bytes] = set()
    terms = sorted(addresses | domains | (exact_domains or set()))
    total = len(terms)
    logger.info("Searching server-side for %d sender term(s) ...", total)
    for num, term in enumerate(terms, start=1):
        _check_stop(should_stop)
        logger.info("  [%d/%d] SEARCH FROM %r ...", num, total, term)
        status, data = conn.uid("SEARCH", None, "FROM", f'"{term}"')
        if status != "OK":
            logger.warning("SEARCH FROM %r failed.", term)
            continue
        uids = data[0].split() if data and data[0] else []
        if uids:
            found.update(uids)
            logger.info("      -> %d match(es)", len(uids))
    return found


def search_rule(conn: imaplib.IMAP4_SSL, search_argument: str) -> set[bytes]:
    """Find UIDs matching a compiled IMAP SEARCH argument string."""
    logger.info("Server-side SEARCH: %s", search_argument)
    status, data = conn.uid("SEARCH", None, *search_argument.split(" "))
    if status != "OK":
        logger.warning("SEARCH failed for argument: %s", search_argument)
        return set()
    uids = set(data[0].split()) if data and data[0] else set()
    logger.info("  -> %d match(es)", len(uids))
    return uids


# --------------------------------------------------------------------------- #
# Deleting / emptying
# --------------------------------------------------------------------------- #
def delete_uids(conn: imaplib.IMAP4_SSL, uids: list[bytes],
                gmail_trash: bool = False,
                batch_size: int = UID_CHUNK_SIZE,
                should_stop: StopCheck | None = None) -> int:
    """Mark messages for deletion in chunks; return the count processed.

    Normal mode flags ``\\Deleted``; Gmail mode applies the ``\\Trash`` label
    via X-GM-LABELS (the only way to truly delete on Gmail).
    """
    processed = 0
    total = len(uids)
    step = min(batch_size, GMAIL_STORE_CAP) if gmail_trash else batch_size
    action = "Moving to Trash" if gmail_trash else "Flagging deleted"
    logger.info("%s %d message(s) in batches of %d ...", action, total, step)
    for i in range(0, total, step):
        _check_stop(should_stop)
        chunk = uids[i:i + step]
        if gmail_trash:
            status, _ = conn.uid("STORE", b",".join(chunk),
                                 "+X-GM-LABELS", r"(\Trash)")
        else:
            status, _ = conn.uid("STORE", b",".join(chunk),
                                 "+FLAGS", r"(\Deleted)")
        if status == "OK":
            processed += len(chunk)
        else:
            logger.warning("Failed to process a chunk of %d messages.", len(chunk))
        logger.info("  ... processed %d/%d", min(i + step, total), total)
    return processed


def _supports_uidplus(conn: imaplib.IMAP4_SSL) -> bool:
    """True if the server advertises UIDPLUS (so ``UID EXPUNGE`` is available)."""
    try:
        return "UIDPLUS" in (getattr(conn, "capabilities", ()) or ())
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def flag_and_expunge(conn: imaplib.IMAP4_SSL, uids: list[bytes], *,
                     batch_size: int = UID_CHUNK_SIZE,
                     should_stop: StopCheck | None = None) -> int:
    """Flag ``\\Deleted`` and EXPUNGE in batches; return the count removed.

    A single ``EXPUNGE`` over tens of thousands of messages can make the server
    return a "System Error" or drop the connection, leaving messages behind. So
    we expunge **incrementally**: with UIDPLUS we ``UID EXPUNGE`` each flagged
    batch; without it we flag one batch then ``EXPUNGE`` (which removes only what
    has been flagged so far). Either way no single EXPUNGE has to clear the whole
    folder at once.
    """
    total = len(uids)
    uidplus = _supports_uidplus(conn)
    removed = 0
    logger.info("Deleting %d message(s) in batches of %d (expunge per batch%s) ...",
                total, batch_size, "; UID EXPUNGE" if uidplus else "")
    for i in range(0, total, batch_size):
        _check_stop(should_stop)
        chunk = uids[i:i + batch_size]
        joined = b",".join(chunk)
        status, _ = conn.uid("STORE", joined, "+FLAGS", r"(\Deleted)")
        if status != "OK":
            logger.warning("Failed to flag a batch of %d message(s); skipping it.",
                           len(chunk))
            continue
        try:
            if uidplus:
                estatus, _ = conn.uid("EXPUNGE", joined)
            else:
                estatus, _ = conn.expunge()
            if estatus == "OK":
                removed += len(chunk)
            else:
                logger.warning("EXPUNGE of a batch returned %s; continuing.",
                               estatus)
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error as exc:
            logger.warning("EXPUNGE of a batch failed (%s); continuing.", exc)
        logger.info("  ... removed %d/%d", min(i + batch_size, total), total)
    return removed


def empty_folder(conn: imaplib.IMAP4_SSL, folder: str, dry_run: bool,
                 batch_size: int = UID_CHUNK_SIZE,
                 should_stop: StopCheck | None = None) -> int:
    """Delete ALL messages in a folder (no filtering). Returns count removed."""
    status, _ = conn.select(_quote_mailbox(folder), readonly=dry_run)
    if status != "OK":
        logger.error("Cannot open folder %r - skipping.", folder)
        return 0
    status, data = conn.uid("SEARCH", None, "ALL")
    if status != "OK" or not data or not data[0]:
        logger.info("Folder %r is already empty.", folder)
        return 0
    all_uids = data[0].split()
    if dry_run:
        logger.info("[DRY-RUN] Would empty %r: %d message(s).",
                    folder, len(all_uids))
        return len(all_uids)
    removed = flag_and_expunge(conn, all_uids, batch_size=batch_size,
                               should_stop=should_stop)
    logger.info("Expunged %r - folder emptied (%d).", folder, removed)
    return removed


# --------------------------------------------------------------------------- #
# Creating folders / labels and moving messages
# --------------------------------------------------------------------------- #
def create_folder(conn: imaplib.IMAP4_SSL, name: str) -> str:
    """Create a mailbox on the server. On Gmail this creates a *label*.

    Folders and Gmail labels are the same thing over IMAP, so a single
    ``CREATE`` works for both. Tolerant if the folder already exists.
    """
    name = name.strip()
    if not name:
        raise imaplib.IMAP4.error("Empty folder name.")
    try:
        status, data = conn.create(_quote_mailbox(name))
    except imaplib.IMAP4.error as exc:
        if "alreadyexists" in str(exc).lower() or "exist" in str(exc).lower():
            return f"Folder {name!r} already exists."
        raise
    detail = (data[0].decode(errors="replace") if data and data[0] else "")
    if status == "OK":
        try:
            conn.subscribe(_quote_mailbox(name))   # so mail clients show it
        except (OSError, imaplib.IMAP4.error):
            pass
        logger.info("Created folder/label %r.", name)
        return f"Created folder {name!r}."
    if "exist" in detail.lower():
        return f"Folder {name!r} already exists."
    raise imaplib.IMAP4.error(detail or "CREATE failed")


# Special-use / system flags (RFC 6154) plus \Noselect: folders carrying any of
# these are system folders we refuse to delete.
_PROTECTED_FLAGS = {"\\noselect", "\\all", "\\archive", "\\drafts",
                    "\\flagged", "\\junk", "\\sent", "\\trash", "\\important"}


def folder_attributes(conn: imaplib.IMAP4_SSL) -> dict[str, list[str]]:
    """Return ``{folder_name: [LIST flags]}`` (e.g. ``\\HasNoChildren``, ``\\Trash``)."""
    status, data = conn.list()
    result: dict[str, list[str]] = {}
    if status != "OK":
        return result
    for item in data:
        if not item:
            continue
        line = item.decode(errors="replace")
        flags = re.match(r"\(([^)]*)\)", line)
        name = re.search(r'"([^"]*)"\s*$', line)
        result[name.group(1) if name else line.split()[-1]] = (
            flags.group(1).split() if flags else [])
    return result


def special_folder(conn: imaplib.IMAP4_SSL, flag: str) -> str | None:
    """Return the folder carrying a special-use LIST flag (e.g. ``\\Junk``,
    ``\\Trash``), regardless of its localized name, or None if absent."""
    want = flag.lower()
    for name, flags in folder_attributes(conn).items():
        if want in {f.lower() for f in flags}:
            return name
    return None


def protected_folder_names(conn: imaplib.IMAP4_SSL) -> set[str]:
    """Names of system folders that must not be deleted (INBOX + special-use)."""
    names = {n for n, flags in folder_attributes(conn).items()
             if {f.lower() for f in flags} & _PROTECTED_FLAGS}
    names.add("INBOX")
    return names


def delete_folder(conn: imaplib.IMAP4_SSL, name: str) -> str:
    """Delete a (non-system) folder/label. Raises ValueError for protected ones."""
    name = name.strip()
    if not name:
        raise ValueError("Empty folder name.")
    if name.upper() == "INBOX" or name in protected_folder_names(conn):
        raise ValueError(f"{name!r} is a system folder and cannot be deleted.")
    # Deleting the *currently selected* mailbox makes many servers force a
    # disconnect ("Selected mailbox was deleted, have to disconnect"). Park the
    # selection on INBOX first - read-only, so nothing gets expunged.
    try:
        conn.select("INBOX", readonly=True)
    except (OSError, imaplib.IMAP4.error):
        pass
    try:
        conn.unsubscribe(_quote_mailbox(name))
    except (OSError, imaplib.IMAP4.error):
        pass
    status, data = conn.delete(_quote_mailbox(name))
    if status == "OK":
        logger.info("Deleted folder/label %r.", name)
        return f"Deleted folder {name!r}."
    raise imaplib.IMAP4.error(
        (data[0].decode(errors="replace") if data and data[0] else "DELETE failed"))


def move_uids(conn: imaplib.IMAP4_SSL, uids: list[bytes], dest: str,
              batch_size: int = UID_CHUNK_SIZE,
              should_stop: StopCheck | None = None) -> int:
    """Move messages to ``dest`` in chunks; return the count moved.

    Uses the server's ``MOVE`` command (RFC 6851) when available, otherwise
    falls back to ``COPY`` + flag ``\\Deleted`` + ``EXPUNGE``. On Gmail a move
    relabels the message (removes the source label, adds the destination one).
    """
    processed = 0
    total = len(uids)
    has_move = "MOVE" in getattr(conn, "capabilities", ())
    qdest = _quote_mailbox(dest)
    logger.info("%s %d message(s) to %r in batches of %d ...",
                "Moving" if has_move else "Copying", total, dest, batch_size)
    for i in range(0, total, batch_size):
        _check_stop(should_stop)
        chunk = uids[i:i + batch_size]
        ids = b",".join(chunk)
        if has_move:
            status, _ = conn.uid("MOVE", ids, qdest)
        else:
            status, _ = conn.uid("COPY", ids, qdest)
            if status == "OK":
                conn.uid("STORE", ids, "+FLAGS", r"(\Deleted)")
        if status == "OK":
            processed += len(chunk)
        else:
            logger.warning("Failed to move a chunk of %d message(s).", len(chunk))
        logger.info("  ... moved %d/%d", min(i + batch_size, total), total)
    if not has_move:
        conn.expunge()   # finalize the copy-then-delete move
    return processed


def flag_senders_as_spam(conn: imaplib.IMAP4_SSL, folder: str,
                         addresses: set[str], junk: str,
                         per_sender: int | None = 1, *,
                         dry_run: bool = False,
                         batch_size: int = UID_CHUNK_SIZE,
                         should_stop: StopCheck | None = None) -> tuple[int, set]:
    """Move message(s) from each sender into ``junk`` (the "report spam" signal).

    ``per_sender`` caps how many of each sender's (newest) messages to move;
    ``None`` moves them all. A message in the Junk/Spam folder teaches the
    provider to route that sender's future mail to spam. Returns
    ``(messages_moved, addresses_with_mail)`` - senders with no mail in this
    folder are skipped (the caller can report them; over several folders the
    address sets can be unioned).
    """
    status, _ = conn.select(_quote_mailbox(folder), readonly=dry_run)
    if status != "OK":
        logger.warning("Cannot open %r - skipping spam-flagging.", folder)
        return 0, set()
    moved = 0
    hit: set[str] = set()
    for addr in sorted(a for a in addresses if a):
        _check_stop(should_stop)
        status, data = conn.uid("SEARCH", None, "FROM", f'"{addr}"')
        uids = data[0].split() if status == "OK" and data and data[0] else []
        if not uids:
            continue
        hit.add(addr)
        chosen = sorted(uids, key=int)
        if per_sender and per_sender > 0:
            chosen = chosen[-per_sender:]                       # newest first
        if dry_run:
            moved += len(chosen)
            logger.info("[DRY-RUN] Would report %r as spam (move %d msg to %r).",
                        addr, len(chosen), junk)
            continue
        moved += move_uids(conn, chosen, junk, batch_size, should_stop)
        logger.info("Reported %r as spam: moved %d message(s) to %r.",
                    addr, len(chosen), junk)
    return moved, hit


# --------------------------------------------------------------------------- #
# AI cleanup: heuristic per-sender report
# --------------------------------------------------------------------------- #
# Calibrated default weights (NOT equal) for the 0-1 sub-signals. Tunable from
# the UI; the score is their weighted average, scaled to 0-10.
DEFAULT_WEIGHTS = {
    "list_unsubscribe": 3.5,   # presence of List-Unsubscribe -> bulk/newsletter
    "unread_ratio": 3.0,       # share of unread messages (ignored = likely junk)
    "bulk": 1.5,               # Precedence: bulk / list
    "sender_pattern": 1.0,     # noreply@, newsletter@, notifications@, ...
    "frequency": 1.0,          # how often this sender writes
}
_BULK_LOCALPARTS = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|newsletter|news|notif\w*|mailer|"
    r"marketing|updates?|alerts?|bounce|info|noreply)\b", re.IGNORECASE)


def _seen_from_meta(meta: bytes) -> bool:
    """True if the FETCH metadata line carries the \\Seen flag."""
    match = re.search(rb"FLAGS\s*\(([^)]*)\)", meta)
    return bool(match and b"\\Seen" in match.group(1))


def _uid_from_meta(meta: bytes) -> str:
    """Extract the numeric UID from a FETCH metadata line (\"... UID 42 ...\")."""
    match = re.search(rb"\bUID\s+(\d+)", meta)
    return match.group(1).decode() if match else ""


def _fetch_flags(conn: imaplib.IMAP4_SSL, uids: list[bytes],
                 should_stop: StopCheck | None) -> dict[str, bool]:
    """Fetch just the \\Seen flag per UID (cheap - no header download)."""
    seen: dict[str, bool] = {}
    step = max(1, AI_FETCH_CHUNK * 4)        # flags-only -> larger batches are fine
    for i in range(0, len(uids), step):
        _check_stop(should_stop)
        chunk = uids[i:i + step]
        status, data = conn.uid("FETCH", b",".join(chunk), "(UID FLAGS)")
        if status != "OK" or not data:
            continue
        for part in data:
            meta = part[0] if isinstance(part, tuple) else part
            if not meta:
                continue
            uid = _uid_from_meta(meta)
            if uid:
                seen[uid] = _seen_from_meta(meta)
    return seen


def _fetch_sender_meta(conn: imaplib.IMAP4_SSL, uids: list[bytes],
                       batch_size: int, should_stop: StopCheck | None,
                       *, cache=None, account: str = "", folder: str = "",
                       uidvalidity: str = ""):
    """Yield (sender, date_header, seen, has_unsub, is_bulk, subject) per message.

    When ``cache`` is given (a headercache.HeaderCache) and ``uidvalidity`` is
    known, the immutable header fields are read from the local cache for UIDs we
    have already seen; only **new** UIDs are fetched in full. The volatile
    ``\\Seen`` flag is always re-read (a cheap FLAGS fetch) so the unread count
    stays accurate. Newly fetched headers are written back to the cache.
    """
    from email import message_from_string
    fields = "(UID FLAGS BODY.PEEK[HEADER.FIELDS " \
             "(FROM DATE SUBJECT LIST-UNSUBSCRIBE PRECEDENCE)])"
    # Cap the per-request size: a single FETCH of hundreds of comma-listed UIDs
    # blocks with no feedback (and some servers stall on it). Smaller batches let
    # us log progress and stay cancellable.
    batch_size = max(1, min(batch_size, AI_FETCH_CHUNK))
    total = len(uids)

    use_cache = cache is not None and bool(uidvalidity)
    cached: dict[str, dict] = {}
    if use_cache:
        try:
            cached = cache.get(account, folder, uidvalidity,
                               [u.decode() for u in uids])
        except Exception:  # pylint: disable=broad-exception-caught
            cached = {}
    if cached:
        logger.info("  %d/%d header(s) from local cache; fetching %d new.",
                    len(cached), total, total - len(cached))

    missing = [u for u in uids if u.decode() not in cached]
    new_rows: dict[str, dict] = {}
    done = 0
    for i in range(0, len(missing), batch_size):
        _check_stop(should_stop)
        chunk = missing[i:i + batch_size]
        status, data = conn.uid("FETCH", b",".join(chunk), fields)
        done += len(chunk)
        logger.info("  fetched headers %d/%d ...", done, len(missing))
        if status != "OK" or not data:
            continue
        for part in data:
            if not (isinstance(part, tuple) and len(part) >= 2 and part[1]):
                continue
            seen = _seen_from_meta(part[0])
            msg = message_from_string(part[1].decode(errors="replace"))
            from_h = msg.get("From", "")
            sender = extract_sender_email(from_h) or "(no sender)"
            date_h = msg.get("Date", "")
            unsub = bool(msg.get("List-Unsubscribe"))
            bulk = "bulk" in (msg.get("Precedence", "").lower())
            subject = decode_mime_header(msg.get("Subject", ""))
            uid = _uid_from_meta(part[0])
            if use_cache and uid:
                new_rows[uid] = {"sender": sender, "date_h": date_h,
                                 "unsub": unsub, "bulk": bulk, "subject": subject,
                                 "from_header": from_h}
            yield (sender, date_h, seen, unsub, bulk, subject)

    if use_cache and new_rows:
        try:
            cache.put(account, folder, uidvalidity, new_rows)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    if cached:
        cached_uids = [u for u in uids if u.decode() in cached]
        seen_map = _fetch_flags(conn, cached_uids, should_stop)
        for u in cached_uids:
            uid = u.decode()
            c = cached[uid]
            yield (c["sender"], c["date_h"], seen_map.get(uid, False),
                   c["unsub"], c["bulk"], c["subject"])


def build_ai_report(conn: imaplib.IMAP4_SSL, folders: list[str], *,
                    threshold: float = 6.0, sample_size: int = 5,
                    exclude: set[str] | None = None,
                    weights: dict | None = None,
                    addresses: set[str] | None = None,
                    domains: set[str] | None = None,
                    exact_domains: set[str] | None = None,
                    search_argument: str | None = None,
                    batch_size: int = UID_CHUNK_SIZE,
                    should_stop: StopCheck | None = None,
                    cache=None, account: str = "") -> dict:
    """Aggregate messages by sender and compute a 0-10 heuristic spam score.

    Scope: if a ``search_argument`` (rule) or ``addresses``/``domains`` (target
    list) is given, only those messages are analysed; otherwise the **whole
    folder** is analysed (like move-all). Returns a JSON-friendly report; senders
    scoring >= ``threshold`` are flagged and carry a small sample of subjects.
    Local-only: no network, no LLM.
    """
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    exclude = {e.strip().lower() for e in (exclude or set()) if e.strip()}
    has_filter = bool(search_argument or addresses or domains or exact_domains)
    agg: dict[str, dict] = {}
    for folder in folders:
        status, _ = conn.select(_quote_mailbox(folder), readonly=True)
        if status != "OK":
            logger.warning("Cannot open %r - skipping.", folder)
            continue
        # UIDVALIDITY pins the cache to this exact UID numbering; if the server
        # ever renumbers, the old cached rows for this folder are stale -> purge.
        uidvalidity = ""
        if cache is not None:
            try:
                uv = conn.response("UIDVALIDITY")[1]
                uidvalidity = (uv[0].decode() if uv and uv[0] else "")
                if uidvalidity:
                    cache.purge_other(account, folder, uidvalidity)
            except Exception:  # pylint: disable=broad-exception-caught
                uidvalidity = ""
        if search_argument:
            uids = sorted(search_rule(conn, search_argument), key=int)
        elif addresses or domains or exact_domains:
            uids = sorted(search_targets(conn, addresses or set(),
                                         domains or set(), exact_domains or set(),
                                         should_stop), key=int)
        else:
            status, data = conn.uid("SEARCH", None, "ALL")
            uids = data[0].split() if status == "OK" and data and data[0] else []
        logger.info("AI report: scanning %d message(s) in %r (%s) ...",
                    len(uids), folder, "filtered" if has_filter else "whole folder")
        for sender, date_h, seen, unsub, bulk, subject in _fetch_sender_meta(
                conn, uids, batch_size, should_stop, cache=cache,
                account=account, folder=folder, uidvalidity=uidvalidity):
            if sender.lower() in exclude:
                continue
            s = agg.setdefault(sender, {"count": 0, "unread": 0, "unsub": False,
                                        "bulk": False, "dates": [], "samples": []})
            s["count"] += 1
            if not seen:
                s["unread"] += 1
            s["unsub"] = s["unsub"] or unsub
            s["bulk"] = s["bulk"] or bulk
            if date_h:
                s["dates"].append(date_h)
            if len(s["samples"]) < sample_size:
                s["samples"].append({"subject": subject or "(no subject)",
                                     "date": date_h, "read": seen})

    senders = []
    wsum = sum(weights.values()) or 1.0
    for sender, s in agg.items():
        unread_ratio = s["unread"] / s["count"] if s["count"] else 0.0
        per_week = _per_week(s["dates"], s["count"])
        pattern = bool(_BULK_LOCALPARTS.match(sender.split("@", 1)[0]))
        sig = {
            "list_unsubscribe": 1.0 if s["unsub"] else 0.0,
            "unread_ratio": round(unread_ratio, 3),
            "bulk": 1.0 if s["bulk"] else 0.0,
            "sender_pattern": 1.0 if pattern else 0.0,
            "frequency": min(1.0, per_week / 5.0),
        }
        score = round(sum(weights[k] * sig[k] for k in weights) / wsum * 10, 1)
        senders.append({
            "sender": sender, "count": s["count"], "unread": s["unread"],
            "unread_ratio": round(unread_ratio, 3),
            "per_week": round(per_week, 2),
            "list_unsubscribe": s["unsub"], "bulk": s["bulk"],
            "sender_pattern": pattern, "score": score,
            "flagged": score >= threshold,
            "samples": s["samples"] if score >= threshold else [],
        })
    senders.sort(key=lambda x: x["score"], reverse=True)
    flagged = [s for s in senders if s["flagged"]]
    flagged_messages = sum(s["count"] for s in flagged)
    logger.info("AI report: %d sender(s), %d above threshold %.1f.",
                len(senders), len(flagged), threshold)
    logger.info("=> %d email(s) from %d flagged sender(s) are potentially "
                "deletable.", flagged_messages, len(flagged))
    return {"folders": folders, "threshold": threshold, "sample_size": sample_size,
            "weights": weights, "total_senders": len(senders),
            "flagged_count": len(flagged), "flagged_messages": flagged_messages,
            "senders": senders}


def ai_report_csv(report: dict) -> str:
    """Render an AI report as CSV (Excel-friendly), one row per sender.

    Verdict columns are filled only when the report was run with an LLM.
    """
    import csv
    import io
    buf = io.StringIO()
    cols = ["sender", "score", "flagged", "messages", "unread", "unread_pct",
            "per_week", "list_unsubscribe", "bulk", "sender_pattern",
            "verdict_delete", "verdict_reason", "verdict_confidence",
            "sample_subjects"]
    writer = csv.writer(buf)
    writer.writerow(cols)
    for s in report.get("senders", []):
        v = s.get("verdict") or {}
        # unread as an integer percentage: avoids a 3-decimal value like "0.667"
        # being misread as 667 by Excel in locales where "." groups thousands.
        ur = s.get("unread_ratio")
        unread_pct = "" if ur is None else f"{round(ur * 100)}%"
        writer.writerow([
            s.get("sender", ""), s.get("score", ""),
            "yes" if s.get("flagged") else "no",
            s.get("count", ""), s.get("unread", ""), unread_pct,
            s.get("per_week", ""),
            "yes" if s.get("list_unsubscribe") else "no",
            "yes" if s.get("bulk") else "no",
            "yes" if s.get("sender_pattern") else "no",
            ("yes" if v.get("delete") else "no") if v else "",
            v.get("reason", "") if v else "",
            v.get("confidence", "") if v else "",
            " | ".join(x.get("subject", "") for x in s.get("samples", [])),
        ])
    return buf.getvalue()


def _per_week(date_headers: list[str], count: int) -> float:
    """Estimate messages-per-week from a list of Date headers."""
    from datetime import timezone
    from email.utils import parsedate_to_datetime
    parsed = []
    for d in date_headers:
        try:
            dt = parsedate_to_datetime(d)
        except (TypeError, ValueError):
            continue
        if dt is None:
            continue
        # Mix of tz-aware and naive Date headers can't be compared - normalize
        # naive ones to UTC so max()/min() below never raise.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        parsed.append(dt)
    if len(parsed) < 2:
        return float(count)            # treat as within a single week
    span_days = (max(parsed) - min(parsed)).days
    weeks = max(span_days / 7.0, 1 / 7.0)
    return count / weeks


# --------------------------------------------------------------------------- #
# High-level per-folder operation
# --------------------------------------------------------------------------- #
def process_folder(conn: imaplib.IMAP4_SSL, folder: str, *,
                   addresses: set[str] | None = None,
                   domains: set[str] | None = None,
                   exact_domains: set[str] | None = None,
                   search_argument: str | None = None,
                   dry_run: bool = True, expunge: bool = False,
                   include_subdomains: bool = False,
                   batch_size: int = UID_CHUNK_SIZE,
                   scan_mode: str = "search",
                   gmail_trash: bool = False,
                   move: bool = False,
                   dest_folder: str | None = None,
                   count_only: bool = False,
                   should_stop: StopCheck | None = None,
                   cache=None, account: str = "") -> int:
    """Scan one folder and act on matching messages. Returns count acted on.

    The action is delete (default), Gmail-trash (``gmail_trash``), or **move**
    to ``dest_folder`` (``move``). Two matching sources, mutually exclusive:
      * ``search_argument`` - a compiled IMAP SEARCH string from rules.py.
      * ``addresses``/``domains`` - the classic target lists.
    ``scan_mode='full'`` (target mode only) downloads headers and matches
    locally with exact-domain / subdomain rules; 'search' filters server-side.
    """
    if move and not (dest_folder and dest_folder.strip()):
        logger.error("Move requested but no destination folder given - skipping.")
        return 0
    if move and _same_mailbox(dest_folder, folder):
        logger.warning("Skipping %r: cannot move a folder into itself.", folder)
        return 0
    status, _ = conn.select(_quote_mailbox(folder), readonly=dry_run)
    if status != "OK":
        logger.error("Cannot open folder %r - skipping.", folder)
        return 0
    uidvalidity = _read_uidvalidity(conn) if cache is not None else ""

    if search_argument:
        matched = sorted(search_rule(conn, search_argument), key=int)
    elif scan_mode == "search":
        matched = sorted(search_targets(conn, addresses or set(),
                                        domains or set(), exact_domains or set(),
                                        should_stop),
                         key=int)
    else:
        status, data = conn.uid("SEARCH", None, "ALL")
        if status != "OK" or not data or not data[0]:
            logger.info("Folder %r is empty.", folder)
            return 0
        all_uids = data[0].split()
        logger.info("Folder %r: scanning %d message(s).", folder, len(all_uids))
        headers = fetch_from_headers(conn, all_uids, batch_size, should_stop,
                                     cache=cache, account=account, folder=folder,
                                     uidvalidity=uidvalidity)
        matched = []
        for uid, value in headers.items():
            sender = extract_sender_email(value)
            if sender_matches(sender, addresses or set(), domains or set(),
                              exact_domains or set(), include_subdomains):
                matched.append(uid)
                logger.info("  MATCH  uid=%s  <%s>", uid.decode(), sender)

    if not matched:
        logger.info("No matching messages in %r.", folder)
        return 0

    if count_only:
        logger.info("Matched %d message(s) in %r.", len(matched), folder)
        return len(matched)

    if dry_run:
        if move:
            what = f"move to {dest_folder!r}"
        elif gmail_trash:
            what = "move to Gmail Trash"
        else:
            what = "delete"
        logger.info("[DRY-RUN] Would %s %d message(s) from %r.",
                    what, len(matched), folder)
        return len(matched)

    if move:
        processed = move_uids(conn, matched, dest_folder, batch_size, should_stop)
        logger.info("Moved %d message(s) from %r to %r.",
                    processed, folder, dest_folder)
        return processed

    if gmail_trash:
        processed = delete_uids(conn, matched, gmail_trash=True,
                                batch_size=batch_size, should_stop=should_stop)
        logger.info("Moved %d message(s) to Gmail Trash from %r.",
                    processed, folder)
    elif expunge:
        # Flag + expunge in batches so a huge single EXPUNGE can't time out.
        processed = flag_and_expunge(conn, matched, batch_size=batch_size,
                                     should_stop=should_stop)
        logger.info("Flagged + expunged %d message(s) in %r (permanent removal).",
                    processed, folder)
    else:
        processed = delete_uids(conn, matched, batch_size=batch_size,
                                should_stop=should_stop)
        logger.info("Flagged %d message(s) as deleted in %r.", processed, folder)
    return processed

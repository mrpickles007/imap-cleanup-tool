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
def fetch_from_headers(conn: imaplib.IMAP4_SSL, uids: list[bytes],
                       batch_size: int = UID_CHUNK_SIZE,
                       should_stop: StopCheck | None = None) -> dict[bytes, str]:
    """Fetch the 'From' header for all UIDs, in batches."""
    results: dict[bytes, str] = {}
    total = len(uids)
    done = 0
    logger.info("Fetching headers in batches of %d ...", batch_size)
    for i in range(0, total, batch_size):
        _check_stop(should_stop)
        chunk = uids[i:i + batch_size]
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
            results[uid] = text.split(":", 1)[1].strip() if ":" in text else ""
        done += len(chunk)
        logger.info("  ... fetched headers %d/%d", done, total)
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
                 save_path: str | None = None) -> dict[str, int]:
    """Log unique senders in a folder with counts; optionally save to CSV."""
    status, _ = conn.select(_quote_mailbox(folder), readonly=True)
    if status != "OK":
        logger.error("Cannot open folder %r.", folder)
        return {}
    status, data = conn.uid("SEARCH", None, "ALL")
    if status != "OK" or not data or not data[0]:
        logger.info("Folder %r is empty.", folder)
        return {}

    all_uids = data[0].split()
    logger.info("Folder %r: inspecting %d message(s).", folder, len(all_uids))
    headers = fetch_from_headers(conn, all_uids, batch_size, should_stop)

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


def empty_folder(conn: imaplib.IMAP4_SSL, folder: str, dry_run: bool,
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
    flagged = delete_uids(conn, all_uids, should_stop=should_stop)
    conn.expunge()
    logger.info("Expunged %r - folder emptied (%d).", folder, flagged)
    return flagged


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
                   should_stop: StopCheck | None = None) -> int:
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
    status, _ = conn.select(_quote_mailbox(folder), readonly=dry_run)
    if status != "OK":
        logger.error("Cannot open folder %r - skipping.", folder)
        return 0

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
        headers = fetch_from_headers(conn, all_uids, batch_size, should_stop)
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

    processed = delete_uids(conn, matched, gmail_trash=gmail_trash,
                            batch_size=batch_size, should_stop=should_stop)
    if gmail_trash:
        logger.info("Moved %d message(s) to Gmail Trash from %r.",
                    processed, folder)
    else:
        logger.info("Flagged %d message(s) as deleted in %r.", processed, folder)
        if expunge:
            conn.expunge()
            logger.info("Expunged folder %r (permanent removal).", folder)
    return processed

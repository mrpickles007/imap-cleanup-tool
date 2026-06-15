"""Email notifications via SMTP, stored in a local SQLite database.

An *SMTP profile* is a saved outgoing-mail server (host, port, security, user +
the password). The password is stored either **plain** or **encrypted** with a
user-chosen secret (PBKDF2-HMAC-SHA256 200k + Fernet), exactly like connection
profiles (so an encrypted profile cannot run unattended in scheduled jobs).

Sending uses the standard-library ``smtplib`` only, so notifications work from the
CLI too (the optional ``cryptography`` package is needed only to encrypt a
profile). One profile is marked **active**; cleanup runs/jobs send a summary to
the configured recipient when notifications are enabled.

Settings (in a ``meta`` table): ``active`` profile name, ``notify_to`` recipient,
``notify_jobs`` and ``notify_runs`` toggles.
"""

from __future__ import annotations

import base64
import os
import smtplib
import sqlite3
import ssl
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from .scheduler import config_dir


class NotifyError(Exception):
    """A problem worth showing the user (bad password, SMTP failure, …)."""


def db_path() -> Path:
    """Path to the SQLite file holding SMTP profiles + settings."""
    return config_dir() / "smtp.sqlite"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS smtp_profiles ("
        " name TEXT PRIMARY KEY,"
        " host TEXT NOT NULL,"
        " port INTEGER NOT NULL DEFAULT 587,"
        " security TEXT NOT NULL DEFAULT 'starttls',"   # ssl | starttls | none
        " user TEXT,"
        " from_addr TEXT,"
        " encrypted INTEGER NOT NULL DEFAULT 0,"
        " salt BLOB,"
        " secret BLOB,"                 # Fernet token, or UTF-8 password bytes
        " created_at TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _derive_key(secret: str, salt: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on extra
        raise NotifyError("Encryption needs the 'cryptography' package "
                          "(install the [web] extra).") from exc
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #
def list_profiles() -> list[dict]:
    """Return saved SMTP profiles (metadata only - never the password)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name, host, port, security, user, from_addr, encrypted"
            " FROM smtp_profiles ORDER BY name COLLATE NOCASE").fetchall()
    finally:
        conn.close()
    return [{"name": r["name"], "host": r["host"], "port": r["port"],
             "security": r["security"], "user": r["user"],
             "from_addr": r["from_addr"], "encrypted": bool(r["encrypted"])}
            for r in rows]


def save_profile(name: str, host: str, port: int, user: str, password: str,
                 from_addr: str = "", security: str = "starttls",
                 encrypt: bool = False, secret: str = "",
                 update_password: bool = True) -> str:
    """Create or replace an SMTP profile. Returns the (trimmed) name.

    With ``update_password`` False (and the profile already exists) only the
    metadata is updated and the stored password + encryption are kept - so
    editing does not require retyping the password (it is never returned).
    """
    name = (name or "").strip()
    if not name:
        raise NotifyError("Profile name is required.")
    if not (host or "").strip():
        raise NotifyError("SMTP host is required.")
    if security not in ("ssl", "starttls", "none"):
        raise NotifyError("Security must be ssl, starttls or none.")

    if not update_password:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE smtp_profiles SET host=?, port=?, security=?, user=?,"
                " from_addr=? WHERE name=?",
                (host.strip(), int(port), security, (user or "").strip(),
                 (from_addr or "").strip(), name))
            if cur.rowcount == 0:
                raise NotifyError(f"No SMTP profile named {name!r} to update.")
            conn.commit()
        finally:
            conn.close()
        return name

    if encrypt:
        if not secret:
            raise NotifyError("An encryption password is required.")
        salt = os.urandom(16)
        key = _derive_key(secret, salt)
        from cryptography.fernet import Fernet
        enc, salt_b = 1, salt
        secret_b = Fernet(key).encrypt((password or "").encode("utf-8"))
    else:
        enc, salt_b, secret_b = 0, None, (password or "").encode("utf-8")

    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO smtp_profiles"
            " (name, host, port, security, user, from_addr, encrypted, salt,"
            "  secret, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET host=excluded.host,"
            " port=excluded.port, security=excluded.security,"
            " user=excluded.user, from_addr=excluded.from_addr,"
            " encrypted=excluded.encrypted, salt=excluded.salt,"
            " secret=excluded.secret",
            (name, host.strip(), int(port), security, (user or "").strip(),
             (from_addr or "").strip(), enc, salt_b, secret_b,
             datetime.now().astimezone().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()
    return name


def load_profile(name: str, secret: str = "") -> dict:
    """Return an SMTP profile including the (decrypted) password."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM smtp_profiles WHERE name=?",
                           (name,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise NotifyError(f"No SMTP profile named {name!r}.")

    if row["encrypted"]:
        if not secret:
            raise NotifyError("This profile is encrypted - enter its password.")
        key = _derive_key(secret, bytes(row["salt"]))
        from cryptography.fernet import Fernet, InvalidToken
        try:
            password = Fernet(key).decrypt(bytes(row["secret"])).decode("utf-8")
        except InvalidToken as exc:
            raise NotifyError("Wrong password.") from exc
    else:
        password = bytes(row["secret"] or b"").decode("utf-8")

    return {"name": row["name"], "host": row["host"], "port": row["port"],
            "security": row["security"], "user": row["user"],
            "from_addr": row["from_addr"], "password": password,
            "encrypted": bool(row["encrypted"])}


def delete_profile(name: str) -> None:
    """Remove an SMTP profile; clear the active flag if it pointed here."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM smtp_profiles WHERE name=?", (name,))
        cur = conn.execute("SELECT value FROM meta WHERE key='active'").fetchone()
        if cur and cur["value"] == name:
            conn.execute("DELETE FROM meta WHERE key='active'")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Settings (active profile + recipient + toggles)
# --------------------------------------------------------------------------- #
def get_settings() -> dict:
    """Return {active, notify_to, notify_jobs, notify_runs}."""
    conn = _connect()
    try:
        rows = {r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM meta").fetchall()}
    finally:
        conn.close()
    return {"active": rows.get("active", ""),
            "notify_to": rows.get("notify_to", ""),
            "notify_jobs": rows.get("notify_jobs", "0") == "1",
            "notify_runs": rows.get("notify_runs", "0") == "1"}


def set_settings(active: str | None = None, notify_to: str | None = None,
                 notify_jobs: bool | None = None,
                 notify_runs: bool | None = None) -> None:
    """Update any subset of the notification settings."""
    pairs: list[tuple[str, str]] = []
    if active is not None:
        pairs.append(("active", active.strip()))
    if notify_to is not None:
        pairs.append(("notify_to", notify_to.strip()))
    if notify_jobs is not None:
        pairs.append(("notify_jobs", "1" if notify_jobs else "0"))
    if notify_runs is not None:
        pairs.append(("notify_runs", "1" if notify_runs else "0"))
    if not pairs:
        return
    conn = _connect()
    try:
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", pairs)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def _server(cfg: dict) -> smtplib.SMTP:
    """Open and authenticate an SMTP connection from a loaded profile dict."""
    host, port = cfg["host"], int(cfg["port"])
    security = cfg.get("security", "starttls")
    try:
        if security == "ssl":
            srv = smtplib.SMTP_SSL(host, port, timeout=30,
                                   context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(host, port, timeout=30)
            if security == "starttls":
                srv.starttls(context=ssl.create_default_context())
        if cfg.get("user"):
            srv.login(cfg["user"], cfg.get("password", ""))
        return srv
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise NotifyError(f"SMTP connection failed: {exc}") from exc


def test_connection(name: str, secret: str = "") -> dict:
    """Connect + authenticate (no send). Returns {ok, message}."""
    cfg = load_profile(name, secret)
    try:
        srv = _server(cfg)
        try:
            srv.noop()
        finally:
            srv.quit()
    except NotifyError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"Connected to {cfg['host']} as "
            f"{cfg['user'] or '(no auth)'}."}


def send_email(cfg: dict, to_addr: str, subject: str, body: str) -> None:
    """Send one plain-text email using a loaded profile dict."""
    to_addr = (to_addr or "").strip()
    if not to_addr:
        raise NotifyError("No recipient address set (Notifications tab).")
    msg = EmailMessage()
    msg["From"] = (cfg.get("from_addr") or cfg.get("user") or "").strip()
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    if not msg["From"]:
        raise NotifyError("No From address (set it on the SMTP profile).")
    srv = _server(cfg)
    try:
        srv.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise NotifyError(f"Could not send email: {exc}") from exc
    finally:
        srv.quit()


def send_notification(subject: str, body: str, *, when: str,
                      secret: str = "") -> bool:
    """Send a notification for a run/job if enabled and configured.

    ``when`` is 'job' or 'run' - the matching toggle must be on. Returns True if
    an email was sent, False if notifications are off / not configured. Raises
    NotifyError on an actual send failure. Encrypted active profiles can only be
    used when ``secret`` is supplied (so scheduled jobs need a plain profile).
    """
    s = get_settings()
    enabled = s["notify_jobs"] if when == "job" else s["notify_runs"]
    if not enabled or not s["active"] or not s["notify_to"]:
        return False
    cfg = load_profile(s["active"], secret)
    if cfg["encrypted"] and not secret:
        raise NotifyError("The active SMTP profile is encrypted and cannot send "
                          "unattended - use a non-encrypted profile for jobs.")
    send_email(cfg, s["notify_to"], subject, body)
    return True


def cleanup_summary(account: str, folders: list[str], total: int, *,
                    dry_run: bool, gmail: bool, kind: str = "Cleanup") -> tuple:
    """Build (subject, body) for a finished cleanup run/job."""
    verb = "would be deleted" if dry_run else "deleted"
    subject = f"[imap-cleanup-tool] {kind} on {account}: {total} message(s)"
    lines = [
        f"{kind} finished on account: {account}",
        f"Folders: {', '.join(folders)}",
        f"Messages {verb}: {total}",
    ]
    if gmail and total and not dry_run:
        lines += [
            "",
            "NOTE (Gmail): the messages were moved to the Trash, NOT permanently "
            "deleted. To remove them for good, empty the Trash - e.g. schedule "
            "an 'Empty folder' job on [Gmail]/Trash, or let Gmail auto-purge it "
            "after 30 days.",
        ]
    lines += ["", "- imap-cleanup-tool"]
    return subject, "\n".join(lines)

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
import re
import smtplib
import sqlite3
import ssl
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from .scheduler import config_dir


class NotifyError(Exception):
    """A problem worth showing the user (bad password, SMTP failure, …)."""


class RateLimitError(NotifyError):
    """The SMTP server is throttling / over quota (a transient 4xx)."""


# SMTP reply codes that mean "temporary - back off and retry" (RFC 5321 4xx),
# typically rate limiting / greylisting / over quota for this window.
_TRANSIENT_SMTP_CODES = {421, 450, 451, 452, 454, 471}
_RATE_LIMIT_HINTS = ("rate", "too many", "quota", "limit", "throttl",
                     "try again", "temporarily")


def _classify_smtp_error(exc: Exception) -> tuple[bool, bool]:
    """Return (is_transient, is_rate_limit) for an SMTP/connection error."""
    if isinstance(exc, smtplib.SMTPResponseException):
        code = int(getattr(exc, "smtp_code", 0) or 0)
        text = str(getattr(exc, "smtp_error", b"") or b"").lower()
        transient = 400 <= code < 500
        rate = (code in _TRANSIENT_SMTP_CODES
                or any(h in text for h in _RATE_LIMIT_HINTS))
        return transient, rate
    # A dropped/timed-out connection is transient (worth one reconnect+retry).
    if isinstance(exc, (smtplib.SMTPServerDisconnected, ConnectionError,
                        TimeoutError, OSError)):
        return True, False
    return False, False


class BatchSender:
    """Send several emails over ONE authenticated SMTP connection.

    Reusing the connection (instead of connect+login+quit per message) is far
    gentler on provider rate limits. Each :meth:`send` retries transient (4xx /
    dropped-connection) failures with backoff, reconnecting as needed, and raises
    :class:`RateLimitError` when the server keeps throttling.
    """

    def __init__(self, cfg: dict, *, retries: int = 3, backoff: float = 2.0,
                 sleep=time.sleep):
        self.cfg = cfg
        self.retries = max(1, retries)
        self.backoff = backoff
        self._sleep = sleep
        self.srv: smtplib.SMTP | None = None

    def _ensure(self) -> None:
        if self.srv is None:
            self.srv = _server(self.cfg)      # NotifyError on connect/login failure

    def close(self) -> None:
        if self.srv is not None:
            try:
                self.srv.quit()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            self.srv = None

    def send(self, to_addr: str, subject: str, body: str) -> None:
        """Send one message, retrying transient failures. Raises NotifyError /
        RateLimitError on a final failure."""
        from_addr = (self.cfg.get("from_addr") or self.cfg.get("user") or "").strip()
        if not from_addr:
            raise NotifyError("No From address (set it on the SMTP profile).")
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = (to_addr or "").strip()
        msg["Subject"] = subject
        msg.set_content(body)
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                self._ensure()
                self.srv.send_message(msg)
                return
            except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
                last = exc
                self.close()                  # drop a possibly-broken connection
                transient, rate = _classify_smtp_error(exc)
                if transient and attempt < self.retries - 1:
                    self._sleep(self.backoff * (attempt + 1))
                    continue
                if rate:
                    raise RateLimitError(
                        f"SMTP rate limit / quota: {exc}") from exc
                raise NotifyError(f"Could not send email: {exc}") from exc
        raise NotifyError(f"Could not send email: {last}")


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
        " secret BLOB,"                 # Fernet token, or UTF-8 password/token bytes
        " auth_method TEXT NOT NULL DEFAULT 'password',"  # 'password' | 'oauth'
        " provider TEXT NOT NULL DEFAULT '',"             # '' | 'microsoft' | …
        " created_at TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    # Migrate older DBs that predate the OAuth columns.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(smtp_profiles)")}
    if "auth_method" not in cols:
        conn.execute("ALTER TABLE smtp_profiles ADD COLUMN "
                     "auth_method TEXT NOT NULL DEFAULT 'password'")
    if "provider" not in cols:
        conn.execute("ALTER TABLE smtp_profiles ADD COLUMN "
                     "provider TEXT NOT NULL DEFAULT ''")
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


def _seal(value: str, encrypt: bool, secret: str):
    """Turn a secret value (password or refresh token) into stored columns:
    ``(encrypted_flag, salt_or_None, secret_bytes)``."""
    if encrypt:
        if not secret:
            raise NotifyError("An encryption password is required.")
        salt = os.urandom(16)
        key = _derive_key(secret, salt)
        from cryptography.fernet import Fernet
        return 1, salt, Fernet(key).encrypt((value or "").encode("utf-8"))
    return 0, None, (value or "").encode("utf-8")


def _unseal(row, secret: str) -> str:
    """Recover the secret value stored for ``row`` (decrypting if needed)."""
    if row["encrypted"]:
        if not secret:
            raise NotifyError("This profile is encrypted - enter its password.")
        key = _derive_key(secret, bytes(row["salt"]))
        from cryptography.fernet import Fernet, InvalidToken
        try:
            return Fernet(key).decrypt(bytes(row["secret"])).decode("utf-8")
        except InvalidToken as exc:
            raise NotifyError("Wrong password.") from exc
    return bytes(row["secret"] or b"").decode("utf-8")


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #
def list_profiles() -> list[dict]:
    """Return saved SMTP profiles (metadata only - never the password)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name, host, port, security, user, from_addr, encrypted,"
            " auth_method, provider"
            " FROM smtp_profiles ORDER BY name COLLATE NOCASE").fetchall()
    finally:
        conn.close()
    return [{"name": r["name"], "host": r["host"], "port": r["port"],
             "security": r["security"], "user": r["user"],
             "from_addr": r["from_addr"], "encrypted": bool(r["encrypted"]),
             "auth_method": r["auth_method"] or "password",
             "provider": r["provider"] or ""}
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

    enc, salt_b, secret_b = _seal(password, encrypt, secret)
    _write_profile(name, host, port, security, user, from_addr, enc, salt_b,
                   secret_b, auth_method="password", provider="")
    return name


def _write_profile(name, host, port, security, user, from_addr, enc, salt_b,
                   secret_b, *, auth_method, provider) -> None:
    """Insert or replace a full SMTP profile row (shared by password + OAuth)."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO smtp_profiles"
            " (name, host, port, security, user, from_addr, encrypted, salt,"
            "  secret, auth_method, provider, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET host=excluded.host,"
            " port=excluded.port, security=excluded.security,"
            " user=excluded.user, from_addr=excluded.from_addr,"
            " encrypted=excluded.encrypted, salt=excluded.salt,"
            " secret=excluded.secret, auth_method=excluded.auth_method,"
            " provider=excluded.provider",
            (name, host.strip(), int(port), security, (user or "").strip(),
             (from_addr or "").strip(), enc, salt_b, secret_b, auth_method,
             provider, datetime.now().astimezone().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()


def save_oauth_profile(name: str, host: str, user: str, refresh_token: str,
                       provider: str, from_addr: str = "",
                       security: str = "starttls", port: int = 587,
                       encrypt: bool = False, secret: str = "") -> str:
    """Create or replace an OAuth2 (XOAUTH2) SMTP profile. The stored secret is
    the refresh token; access tokens are minted from it at send time."""
    name = (name or "").strip()
    if not name:
        raise NotifyError("Profile name is required.")
    if not (host or "").strip():
        raise NotifyError("SMTP host is required.")
    if not (refresh_token or "").strip():
        raise NotifyError("A refresh token is required for an OAuth profile.")
    if not (provider or "").strip():
        raise NotifyError("An OAuth provider is required.")
    enc, salt_b, secret_b = _seal(refresh_token, encrypt, secret)
    _write_profile(name, host, port, security, user, from_addr, enc, salt_b,
                   secret_b, auth_method="oauth", provider=provider.strip().lower())
    return name


def update_refresh_token(name: str, refresh_token: str, secret: str = "") -> None:
    """Persist a rotated refresh token for an OAuth SMTP profile, preserving
    whether the profile is encrypted (needs the encryption password if it is)."""
    conn = _connect()
    try:
        row = conn.execute("SELECT encrypted FROM smtp_profiles WHERE name=?",
                           (name,)).fetchone()
        if row is None:
            raise NotifyError(f"No SMTP profile named {name!r}.")
        _, salt_b, secret_b = _seal(refresh_token, bool(row["encrypted"]), secret)
        conn.execute("UPDATE smtp_profiles SET salt=?, secret=? WHERE name=?",
                     (salt_b, secret_b, name))
        conn.commit()
    finally:
        conn.close()


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

    auth_method = (row["auth_method"] or "password")
    value = _unseal(row, secret)
    is_oauth = auth_method == "oauth"

    return {"name": row["name"], "host": row["host"], "port": row["port"],
            "security": row["security"], "user": row["user"],
            "from_addr": row["from_addr"],
            "password": "" if is_oauth else value,
            "refresh_token": value if is_oauth else "",
            "auth_method": auth_method, "provider": row["provider"] or "",
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
def _oauth_login(srv: smtplib.SMTP, cfg: dict) -> None:
    """Authenticate an open SMTP connection with XOAUTH2 from an OAuth profile."""
    from . import oauth
    # Persist a rotated refresh token so unattended (scheduled) sends keep working.
    # Encrypted profiles can't be re-sealed without the passphrase, so skip then.
    def _persist(new_token: str) -> None:
        if cfg.get("encrypted") or not cfg.get("name"):
            return
        try:
            update_refresh_token(cfg["name"], new_token)
        except NotifyError:
            pass
    try:
        token = oauth.access_token_for(cfg, persist=_persist)
    except oauth.OAuthError as exc:
        raise NotifyError(f"OAuth sign-in failed: {exc}") from exc
    # We AUTH by hand (smtplib has no XOAUTH2 helper), so we must greet first - just
    # like SMTP.login() does. STARTTLS resets the EHLO state, and on a fresh SSL
    # connection none was sent yet; without this the server replies "send hello first".
    srv.ehlo_or_helo_if_needed()
    code, resp = srv.docmd("AUTH", "XOAUTH2 " + oauth.xoauth2_b64(cfg["user"], token))
    if code == 334:                       # server returned a base64 error challenge
        code, resp = srv.docmd("")        # send empty line to surface the real error
    if code != 235:
        detail = resp.decode("utf-8", "replace") if isinstance(resp, bytes) else resp
        raise NotifyError(f"SMTP OAuth login was rejected: {detail}")


def _quiet_close(srv) -> None:
    """Close a half-open SMTP connection without sending QUIT (which could hang on
    an already-broken/throttled link) and without raising."""
    if srv is None:
        return
    try:
        srv.close()
    except Exception:  # pylint: disable=broad-exception-caught
        pass


def _server(cfg: dict) -> smtplib.SMTP:
    """Open and authenticate an SMTP connection from a loaded profile dict."""
    host, port = cfg["host"], int(cfg["port"])
    security = cfg.get("security", "starttls")
    srv = None
    try:
        if security == "ssl":
            srv = smtplib.SMTP_SSL(host, port, timeout=60,
                                   context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(host, port, timeout=60)
            if security == "starttls":
                srv.starttls(context=ssl.create_default_context())
        if cfg.get("auth_method") == "oauth":
            _oauth_login(srv, cfg)
        elif cfg.get("user"):
            srv.login(cfg["user"], cfg.get("password", ""))
        return srv
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        _quiet_close(srv)                     # don't leak the socket on failure
        raise NotifyError(f"SMTP connection failed: {exc}") from exc
    except NotifyError:                       # auth helper (_oauth_login) failed
        _quiet_close(srv)
        raise


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


# Errors that are an actual auth/rejection problem (don't retry - they'll just keep
# failing). Everything else (timeouts, dropped connections, 4xx, throttling) is
# treated as transient and worth a retry - this is what makes notifications survive
# the occasional Outlook SMTP read-timeout on a scheduled (unattended) run.
_AUTH_FAIL_RE = re.compile(r"rejected|authenticat|5\.7|\b535\b|invalid_grant|sign.?in",
                           re.IGNORECASE)


def _is_transient_send_error(message: str) -> bool:
    return not _AUTH_FAIL_RE.search(message or "")


def send_email(cfg: dict, to_addr: str, subject: str, body: str,
               attachments: list | None = None, *, retries: int = 3,
               backoff: float = 3.0, sleep=time.sleep) -> None:
    """Send one plain-text email using a loaded profile dict.

    ``attachments`` is an optional list of ``(filename, text_content)`` pairs,
    attached as ``text/csv`` (used to email an AI report). Transient failures
    (timeouts, dropped connections, throttling) are retried with backoff;
    auth/rejection failures fail fast.
    """
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
    for filename, content in (attachments or []):
        msg.add_attachment((content or "").encode("utf-8"), maintype="text",
                           subtype="csv", filename=filename)

    last: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            srv = _server(cfg)                # may raise NotifyError (connect/auth)
            try:
                srv.send_message(msg)
                return
            finally:
                try:
                    srv.quit()
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
        except (NotifyError, smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            last = exc
            if attempt < retries - 1 and _is_transient_send_error(str(exc)):
                sleep(backoff * (attempt + 1))
                continue
            if isinstance(exc, NotifyError):
                raise
            raise NotifyError(f"Could not send email: {exc}") from exc
    raise NotifyError(f"Could not send email: {last}")


def send_notification(subject: str, body: str, *, when: str,
                      secret: str = "", profile: str = "",
                      attachments: list | None = None) -> bool:
    """Send a notification for a run/job if enabled and configured.

    ``when`` is 'job' or 'run' - the matching toggle must be on. ``attachments``
    is an optional list of ``(filename, text_content)`` (e.g. an AI report CSV).
    Returns True if an email was sent, False if notifications are off / not
    configured. Raises NotifyError on an actual send failure. Encrypted active
    profiles can only be used when ``secret`` is supplied (so scheduled jobs need
    a plain profile).
    """
    s = get_settings()
    enabled = s["notify_jobs"] if when == "job" else s["notify_runs"]
    active = (profile or "").strip() or s["active"]   # per-job profile override
    if not enabled or not active or not s["notify_to"]:
        return False
    cfg = load_profile(active, secret)
    if cfg["encrypted"] and not secret:
        raise NotifyError("The active SMTP profile is encrypted and cannot send "
                          "unattended - use a non-encrypted profile for jobs.")
    send_email(cfg, s["notify_to"], subject, body, attachments=attachments)
    return True


def send_from_active(to_addr: str, subject: str, body: str,
                     secret: str = "") -> None:
    """Send a one-off email from the **active** SMTP profile (e.g. an unsubscribe
    mailto). Raises NotifyError if no active profile is set or it's encrypted
    without a secret."""
    s = get_settings()
    if not s["active"]:
        raise NotifyError("No active SMTP profile - set one in the Notifications "
                          "tab to send unsubscribe emails.")
    cfg = load_profile(s["active"], secret)
    if cfg["encrypted"] and not secret:
        raise NotifyError("The active SMTP profile is encrypted - unlock it first.")
    send_email(cfg, to_addr, subject, body)


def open_active_sender(secret: str = "") -> "BatchSender":
    """A :class:`BatchSender` bound to the active SMTP profile - for sending many
    one-off emails (e.g. bulk unsubscribe) over a single connection, with retry."""
    s = get_settings()
    if not s["active"]:
        raise NotifyError("No active SMTP profile - set one in the Notifications "
                          "tab to send unsubscribe emails.")
    cfg = load_profile(s["active"], secret)
    if cfg["encrypted"] and not secret:
        raise NotifyError("The active SMTP profile is encrypted - unlock it first.")
    return BatchSender(cfg)


def has_active_profile() -> bool:
    """True if an active SMTP profile is configured (for mailto unsubscribes)."""
    try:
        return bool(get_settings()["active"])
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def cleanup_summary(account: str, folders: list[str], total: int, *,
                    dry_run: bool, gmail: bool, kind: str = "Cleanup",
                    dest: str = "") -> tuple:
    """Build (subject, body) for a finished run/job, worded for the operation."""
    if kind == "Move":
        verb = "would be moved" if dry_run else "moved"
    else:
        verb = "would be deleted" if dry_run else "deleted"
    subject = f"[imap-cleanup-tool] {kind} on {account}: {total} message(s)"
    lines = [
        f"{kind} finished on account: {account}",
        f"Folders: {', '.join(folders)}",
    ]
    if kind == "Move" and dest:
        lines.append(f"Destination: {dest}")
    lines.append(f"Messages {verb}: {total}")
    if kind != "Move" and gmail and total and not dry_run:
        lines += [
            "",
            "NOTE (Gmail): the messages were moved to the Trash, NOT permanently "
            "deleted. To remove them for good, empty the Trash - e.g. schedule "
            "an 'Empty folder' job on [Gmail]/Trash, or let Gmail auto-purge it "
            "after 30 days.",
        ]
    lines += ["", "- imap-cleanup-tool"]
    return subject, "\n".join(lines)

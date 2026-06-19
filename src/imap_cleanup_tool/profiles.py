"""Connection profiles stored in a local SQLite database.

A *profile* is a saved IMAP connection (host, port, user, timeout + a secret).
Profiles authenticate one of two ways (``auth_method``):

* **password** - classic IMAP login; the stored secret is the password.
* **oauth** - OAuth2 / XOAUTH2 (Microsoft, later Google); the stored secret is
  the **refresh token** obtained once at sign-in, used to mint access tokens
  silently afterwards. ``provider`` records which OAuth provider issued it.

The secret (password or refresh token) can be stored either:

* **plain** - written as-is in the local DB (the user's explicit choice), or
* **encrypted** - with a user-chosen secret: a key is derived with PBKDF2-HMAC
  (SHA-256, 200k iterations) over a random per-profile salt, and the value is
  sealed with Fernet (AES-128-CBC + HMAC). The secret itself is never stored, so
  loading an encrypted profile requires re-entering it (which means encrypted
  profiles cannot run unattended in scheduled jobs - true for both auth methods).

Encryption needs the ``cryptography`` package (installed with the ``[web]``
extra). The DB lives next to the scheduler's jobs file in the per-user config
directory.
"""

from __future__ import annotations

import base64
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .scheduler import config_dir


class ProfileError(Exception):
    """A problem worth showing the user (bad password, missing crypto, …)."""


def db_path() -> Path:
    """Path to the SQLite file holding connection profiles."""
    return config_dir() / "profiles.sqlite"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS profiles ("
        " name TEXT PRIMARY KEY,"
        " host TEXT NOT NULL,"
        " port INTEGER NOT NULL DEFAULT 993,"
        " user TEXT NOT NULL,"
        " timeout INTEGER NOT NULL DEFAULT 120,"
        " encrypted INTEGER NOT NULL DEFAULT 0,"
        " salt BLOB,"
        " secret BLOB,"               # Fernet token, or UTF-8 password/token bytes
        " local_cache INTEGER NOT NULL DEFAULT 0,"
        " auth_method TEXT NOT NULL DEFAULT 'password',"  # 'password' | 'oauth'
        " provider TEXT NOT NULL DEFAULT '',"             # '' | 'microsoft' | …
        " created_at TEXT)")
    # Migrate older DBs that predate later columns.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(profiles)")}
    if "local_cache" not in cols:
        conn.execute("ALTER TABLE profiles ADD COLUMN "
                     "local_cache INTEGER NOT NULL DEFAULT 0")
    if "auth_method" not in cols:
        conn.execute("ALTER TABLE profiles ADD COLUMN "
                     "auth_method TEXT NOT NULL DEFAULT 'password'")
    if "provider" not in cols:
        conn.execute("ALTER TABLE profiles ADD COLUMN "
                     "provider TEXT NOT NULL DEFAULT ''")
    return conn


def _derive_key(secret: str, salt: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on extra
        raise ProfileError("Encryption needs the 'cryptography' package "
                           "(install the [web] extra).") from exc
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))


def _seal(value: str, encrypt: bool, secret: str):
    """Turn a secret value (password or refresh token) into the columns we store:
    ``(encrypted_flag, salt_or_None, secret_bytes)``."""
    if encrypt:
        if not secret:
            raise ProfileError("An encryption password is required.")
        salt = os.urandom(16)
        key = _derive_key(secret, salt)            # ProfileError if no crypto
        from cryptography.fernet import Fernet
        return 1, salt, Fernet(key).encrypt((value or "").encode("utf-8"))
    return 0, None, (value or "").encode("utf-8")


def _unseal(row, secret: str) -> str:
    """Recover the secret value stored for ``row`` (decrypting if needed)."""
    if row["encrypted"]:
        if not secret:
            raise ProfileError("This profile is encrypted - enter its password.")
        key = _derive_key(secret, bytes(row["salt"]))
        from cryptography.fernet import Fernet, InvalidToken
        try:
            return Fernet(key).decrypt(bytes(row["secret"])).decode("utf-8")
        except InvalidToken as exc:
            raise ProfileError("Wrong password.") from exc
    return bytes(row["secret"] or b"").decode("utf-8")


def list_profiles() -> list[dict]:
    """Return saved profiles (metadata only - never the secret)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name, host, user, port, encrypted, local_cache,"
            " auth_method, provider FROM profiles "
            "ORDER BY name COLLATE NOCASE").fetchall()
    finally:
        conn.close()
    return [{"name": r["name"], "host": r["host"], "user": r["user"],
             "port": r["port"], "encrypted": bool(r["encrypted"]),
             "local_cache": bool(r["local_cache"]),
             "auth_method": r["auth_method"] or "password",
             "provider": r["provider"] or ""}
            for r in rows]


def _save(name: str, host: str, port: int, user: str, secret_value: str,
          *, auth_method: str, provider: str, timeout: int, encrypt: bool,
          secret: str, local_cache: bool) -> str:
    """Create or replace a profile (shared by password and OAuth profiles)."""
    name = (name or "").strip()
    if not name:
        raise ProfileError("Profile name is required.")
    if not (host or "").strip() or not (user or "").strip():
        raise ProfileError("Host and user are required.")

    enc, salt_b, secret_b = _seal(secret_value, encrypt, secret)

    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO profiles"
            " (name, host, port, user, timeout, encrypted, salt, secret,"
            "  local_cache, auth_method, provider, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET host=excluded.host,"
            " port=excluded.port, user=excluded.user, timeout=excluded.timeout,"
            " encrypted=excluded.encrypted, salt=excluded.salt,"
            " secret=excluded.secret, local_cache=excluded.local_cache,"
            " auth_method=excluded.auth_method, provider=excluded.provider",
            (name, host.strip(), int(port), user.strip(), int(timeout), enc,
             salt_b, secret_b, 1 if local_cache else 0, auth_method, provider,
             datetime.now().astimezone().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()
    return name


def save_profile(name: str, host: str, port: int, user: str, password: str,
                 timeout: int = 120, encrypt: bool = False,
                 secret: str = "", local_cache: bool = False) -> str:
    """Create or replace a password (classic IMAP login) profile."""
    return _save(name, host, port, user, password, auth_method="password",
                 provider="", timeout=timeout, encrypt=encrypt, secret=secret,
                 local_cache=local_cache)


def save_oauth_profile(name: str, host: str, port: int, user: str,
                       refresh_token: str, provider: str, timeout: int = 120,
                       encrypt: bool = False, secret: str = "",
                       local_cache: bool = False) -> str:
    """Create or replace an OAuth2 (XOAUTH2) profile. The stored secret is the
    refresh token; access tokens are minted from it silently at connect time."""
    if not (refresh_token or "").strip():
        raise ProfileError("A refresh token is required for an OAuth profile.")
    if not (provider or "").strip():
        raise ProfileError("An OAuth provider is required.")
    return _save(name, host, port, user, refresh_token, auth_method="oauth",
                 provider=provider.strip().lower(), timeout=timeout,
                 encrypt=encrypt, secret=secret, local_cache=local_cache)


def update_refresh_token(name: str, refresh_token: str, secret: str = "") -> None:
    """Persist a rotated refresh token for an OAuth profile, preserving whether
    the profile is encrypted (needs the encryption password if it is)."""
    conn = _connect()
    try:
        row = conn.execute("SELECT encrypted FROM profiles WHERE name=?",
                           (name,)).fetchone()
        if row is None:
            raise ProfileError(f"No profile named {name!r}.")
        _, salt_b, secret_b = _seal(refresh_token, bool(row["encrypted"]), secret)
        conn.execute("UPDATE profiles SET salt=?, secret=? WHERE name=?",
                     (salt_b, secret_b, name))
        conn.commit()
    finally:
        conn.close()


def load_profile(name: str, secret: str = "") -> dict:
    """Return a profile's fields including the (decrypted) secret. For password
    profiles the secret is in ``password``; for OAuth profiles it is the
    ``refresh_token`` (``password`` stays empty)."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE name=?",
                           (name,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ProfileError(f"No profile named {name!r}.")

    auth_method = (row["auth_method"] or "password")
    value = _unseal(row, secret)
    is_oauth = auth_method == "oauth"

    return {"name": row["name"], "host": row["host"], "port": row["port"],
            "user": row["user"], "timeout": row["timeout"],
            "password": "" if is_oauth else value,
            "refresh_token": value if is_oauth else "",
            "auth_method": auth_method, "provider": row["provider"] or "",
            "local_cache": bool(row["local_cache"])}


def delete_profile(name: str) -> None:
    """Remove a profile by name (no error if it does not exist)."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM profiles WHERE name=?", (name,))
        conn.commit()
    finally:
        conn.close()

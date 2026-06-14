"""Connection profiles stored in a local SQLite database.

A *profile* is a saved IMAP connection (host, port, user, timeout + the
password). The password can be stored either:

* **plain** - written as-is in the local DB (the user's explicit choice), or
* **encrypted** - with a user-chosen secret: a key is derived with PBKDF2-HMAC
  (SHA-256, 200k iterations) over a random per-profile salt, and the password is
  sealed with Fernet (AES-128-CBC + HMAC). The secret itself is never stored, so
  loading an encrypted profile requires re-entering it.

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
        " secret BLOB,"               # Fernet token, or UTF-8 password bytes
        " created_at TEXT)")
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


def list_profiles() -> list[dict]:
    """Return saved profiles (metadata only - never the password)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name, host, user, port, encrypted FROM profiles "
            "ORDER BY name COLLATE NOCASE").fetchall()
    finally:
        conn.close()
    return [{"name": r["name"], "host": r["host"], "user": r["user"],
             "port": r["port"], "encrypted": bool(r["encrypted"])}
            for r in rows]


def save_profile(name: str, host: str, port: int, user: str, password: str,
                 timeout: int = 120, encrypt: bool = False,
                 secret: str = "") -> str:
    """Create or replace a profile. Returns the (trimmed) profile name."""
    name = (name or "").strip()
    if not name:
        raise ProfileError("Profile name is required.")
    if not (host or "").strip() or not (user or "").strip():
        raise ProfileError("Host and user are required.")

    if encrypt:
        if not secret:
            raise ProfileError("An encryption password is required.")
        salt = os.urandom(16)
        key = _derive_key(secret, salt)           # ProfileError if no crypto
        from cryptography.fernet import Fernet
        enc, salt_b = 1, salt
        secret_b = Fernet(key).encrypt((password or "").encode("utf-8"))
    else:
        enc, salt_b, secret_b = 0, None, (password or "").encode("utf-8")

    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO profiles"
            " (name, host, port, user, timeout, encrypted, salt, secret,"
            "  created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET host=excluded.host,"
            " port=excluded.port, user=excluded.user, timeout=excluded.timeout,"
            " encrypted=excluded.encrypted, salt=excluded.salt,"
            " secret=excluded.secret",
            (name, host.strip(), int(port), user.strip(), int(timeout), enc,
             salt_b, secret_b,
             datetime.now().astimezone().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()
    return name


def load_profile(name: str, secret: str = "") -> dict:
    """Return a profile's fields including the (decrypted) password."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE name=?",
                           (name,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ProfileError(f"No profile named {name!r}.")

    if row["encrypted"]:
        if not secret:
            raise ProfileError("This profile is encrypted - enter its password.")
        key = _derive_key(secret, bytes(row["salt"]))
        from cryptography.fernet import Fernet, InvalidToken
        try:
            password = Fernet(key).decrypt(bytes(row["secret"])).decode("utf-8")
        except InvalidToken as exc:
            raise ProfileError("Wrong password.") from exc
    else:
        password = bytes(row["secret"] or b"").decode("utf-8")

    return {"name": row["name"], "host": row["host"], "port": row["port"],
            "user": row["user"], "timeout": row["timeout"], "password": password}


def delete_profile(name: str) -> None:
    """Remove a profile by name (no error if it does not exist)."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM profiles WHERE name=?", (name,))
        conn.commit()
    finally:
        conn.close()

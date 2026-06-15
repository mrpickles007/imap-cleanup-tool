"""LLM model configurations stored in a local SQLite database.

A *model* is a saved LLM configuration used by the AI Cleanup feature:

* ``model``      - a litellm model string (e.g. ``gpt-4o-mini``,
  ``ollama/llama3``, ``openrouter/meta-llama/llama-3.1-8b-instruct``),
* ``api_base``   - optional custom endpoint (Ollama / self-hosted / proxy),
* the **API key**, stored either plain or **encrypted** with a user secret
  (PBKDF2-HMAC-SHA256 + Fernet, exactly like connection profiles - the secret is
  never stored, so an encrypted model can't be used unattended in scheduling),
* optional **cost tracking**: a flag plus input/output price per million tokens.

The actual LLM calls live elsewhere and import ``litellm`` lazily (the ``[ai]``
extra); this module is pure stdlib apart from the optional ``cryptography`` used
only when encrypting.
"""

from __future__ import annotations

import base64
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .scheduler import config_dir


class LLMError(Exception):
    """A problem worth showing the user (missing crypto, wrong secret, …)."""


def db_path() -> Path:
    """Path to the SQLite file holding LLM model configs."""
    return config_dir() / "llm.sqlite"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS models ("
        " name TEXT PRIMARY KEY,"
        " model TEXT NOT NULL,"
        " api_base TEXT,"
        " encrypted INTEGER NOT NULL DEFAULT 0,"
        " salt BLOB,"
        " secret BLOB,"                 # API key: Fernet token or UTF-8 bytes
        " track_costs INTEGER NOT NULL DEFAULT 0,"
        " cost_input REAL NOT NULL DEFAULT 0,"
        " cost_output REAL NOT NULL DEFAULT 0,"
        " created_at TEXT)")
    return conn


def _derive_key(secret: str, salt: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on extra
        raise LLMError("Encryption needs the 'cryptography' package "
                       "(install the [web] extra).") from exc
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))


def list_models() -> list[dict]:
    """Return saved models (metadata + cost settings, never the API key)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name, model, api_base, encrypted, track_costs, cost_input,"
            " cost_output FROM models ORDER BY name COLLATE NOCASE").fetchall()
    finally:
        conn.close()
    return [{"name": r["name"], "model": r["model"], "api_base": r["api_base"],
             "encrypted": bool(r["encrypted"]),
             "track_costs": bool(r["track_costs"]),
             "cost_input": r["cost_input"], "cost_output": r["cost_output"],
             "has_key": True}
            for r in rows]


def save_model(name: str, model: str, api_key: str = "", api_base: str = "",
               encrypt: bool = False, secret: str = "",
               track_costs: bool = False, cost_input: float = 0.0,
               cost_output: float = 0.0) -> str:
    """Create or replace a model config. Returns the (trimmed) name."""
    name = (name or "").strip()
    model = (model or "").strip()
    if not name:
        raise LLMError("Model config name is required.")
    if not model:
        raise LLMError("A model string is required (e.g. gpt-4o-mini).")

    if encrypt:
        if not secret:
            raise LLMError("An encryption password is required.")
        salt = os.urandom(16)
        key = _derive_key(secret, salt)
        from cryptography.fernet import Fernet
        enc, salt_b = 1, salt
        secret_b = Fernet(key).encrypt((api_key or "").encode("utf-8"))
    else:
        enc, salt_b, secret_b = 0, None, (api_key or "").encode("utf-8")

    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO models (name, model, api_base, encrypted, salt, secret,"
            " track_costs, cost_input, cost_output, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET model=excluded.model,"
            " api_base=excluded.api_base, encrypted=excluded.encrypted,"
            " salt=excluded.salt, secret=excluded.secret,"
            " track_costs=excluded.track_costs, cost_input=excluded.cost_input,"
            " cost_output=excluded.cost_output",
            (name, model, (api_base or "").strip() or None, enc, salt_b, secret_b,
             1 if track_costs else 0, float(cost_input or 0),
             float(cost_output or 0),
             datetime.now().astimezone().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()
    return name


def load_model(name: str, secret: str = "") -> dict:
    """Return a model config including the (decrypted) API key."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM models WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise LLMError(f"No model config named {name!r}.")

    if row["encrypted"]:
        if not secret:
            raise LLMError("This model config is encrypted - enter its password.")
        key = _derive_key(secret, bytes(row["salt"]))
        from cryptography.fernet import Fernet, InvalidToken
        try:
            api_key = Fernet(key).decrypt(bytes(row["secret"])).decode("utf-8")
        except InvalidToken as exc:
            raise LLMError("Wrong password.") from exc
    else:
        api_key = bytes(row["secret"] or b"").decode("utf-8")

    return {"name": row["name"], "model": row["model"],
            "api_base": row["api_base"], "api_key": api_key,
            "encrypted": bool(row["encrypted"]),
            "track_costs": bool(row["track_costs"]),
            "cost_input": row["cost_input"], "cost_output": row["cost_output"]}


def delete_model(name: str) -> None:
    """Remove a model config by name (no error if it does not exist)."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM models WHERE name=?", (name,))
        conn.commit()
    finally:
        conn.close()

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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS costs ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " model_name TEXT NOT NULL,"
        " ts TEXT,"
        " prompt_tokens INTEGER NOT NULL DEFAULT 0,"
        " completion_tokens INTEGER NOT NULL DEFAULT 0,"
        " cost REAL NOT NULL DEFAULT 0)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
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


# Models seeded on first run so AI Cleanup works out of the box: a popular cloud
# model (no key stored - litellm reads OPENAI_API_KEY from the environment) and a
# free local one (Ollama). Users can edit or delete them; once seeded they are
# not recreated (a deleted default stays deleted).
_DEFAULT_MODELS = [
    # name, model, api_base, track_costs, cost_input, cost_output
    ("gpt-4o-mini", "gpt-4o-mini", None, True, 0.15, 0.60),
    ("ollama-llama3", "ollama/llama3", "http://localhost:11434", False, 0.0, 0.0),
]


def ensure_default_models() -> None:
    """Seed the default models once (idempotent). Safe to call on every startup."""
    conn = _connect()
    try:
        seeded = conn.execute(
            "SELECT value FROM meta WHERE key='models_seeded'").fetchone()
        if seeded:
            return
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        for name, model, api_base, tc, ci, co in _DEFAULT_MODELS:
            conn.execute(
                "INSERT OR IGNORE INTO models (name, model, api_base, encrypted,"
                " salt, secret, track_costs, cost_input, cost_output, created_at)"
                " VALUES (?, ?, ?, 0, NULL, ?, ?, ?, ?, ?)",
                (name, model, api_base, b"", 1 if tc else 0,
                 float(ci), float(co), now))
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES "
            "('models_seeded', ?)", (now,))
        conn.commit()
    finally:
        conn.close()


def list_models() -> list[dict]:
    """Return saved models (metadata + cost settings, never the API key)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name, model, api_base, encrypted, track_costs, cost_input,"
            " cost_output, secret FROM models"
            " ORDER BY name COLLATE NOCASE").fetchall()
    finally:
        conn.close()
    return [{"name": r["name"], "model": r["model"], "api_base": r["api_base"],
             "encrypted": bool(r["encrypted"]),
             "track_costs": bool(r["track_costs"]),
             "cost_input": r["cost_input"], "cost_output": r["cost_output"],
             "has_key": bool(r["secret"])}
            for r in rows]


def save_model(name: str, model: str, api_key: str = "", api_base: str = "",
               encrypt: bool = False, secret: str = "",
               track_costs: bool = False, cost_input: float = 0.0,
               cost_output: float = 0.0, update_key: bool = True) -> str:
    """Create or replace a model config. Returns the (trimmed) name.

    When ``update_key`` is False and the model already exists, only its metadata
    (model string, api_base, cost settings) is updated and the stored API key +
    encryption are left untouched - so editing a model does not require retyping
    its key (the key is never returned to the UI).
    """
    name = (name or "").strip()
    model = (model or "").strip()
    if not name:
        raise LLMError("Model config name is required.")
    if not model:
        raise LLMError("A model string is required (e.g. gpt-4o-mini).")

    if not update_key:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE models SET model=?, api_base=?, track_costs=?,"
                " cost_input=?, cost_output=? WHERE name=?",
                (model, (api_base or "").strip() or None,
                 1 if track_costs else 0, float(cost_input or 0),
                 float(cost_output or 0), name))
            if cur.rowcount == 0:
                raise LLMError(f"No model config named {name!r} to update.")
            conn.commit()
        finally:
            conn.close()
        return name

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


def log_cost(model_name: str, prompt_tokens: int, completion_tokens: int,
             cost: float) -> None:
    """Append one LLM call to the persistent per-model cost log."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO costs (model_name, ts, prompt_tokens, completion_tokens,"
            " cost) VALUES (?, ?, ?, ?, ?)",
            (model_name, datetime.now().astimezone().isoformat(timespec="seconds"),
             int(prompt_tokens or 0), int(completion_tokens or 0),
             float(cost or 0)))
        conn.commit()
    finally:
        conn.close()


def cost_log(model_name: str, limit: int = 200) -> dict:
    """Return recent cost entries for a model plus an all-time total."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT ts, prompt_tokens, completion_tokens, cost FROM costs "
            "WHERE model_name=? ORDER BY id DESC LIMIT ?",
            (model_name, int(limit))).fetchall()
        tot = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(prompt_tokens),0) pt,"
            " COALESCE(SUM(completion_tokens),0) ct, COALESCE(SUM(cost),0) cost"
            " FROM costs WHERE model_name=?", (model_name,)).fetchone()
    finally:
        conn.close()
    return {"model": model_name,
            "entries": [{"ts": r["ts"], "prompt_tokens": r["prompt_tokens"],
                         "completion_tokens": r["completion_tokens"],
                         "cost": r["cost"]} for r in rows],
            "total": {"calls": tot["c"], "prompt_tokens": tot["pt"],
                      "completion_tokens": tot["ct"],
                      "cost": round(tot["cost"], 6)}}

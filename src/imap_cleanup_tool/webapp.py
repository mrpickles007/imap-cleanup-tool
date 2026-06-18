"""Local web UI for imap-cleanup-tool (optional ``[web]`` extra).

A small FastAPI app that serves a single-page interface and a JSON API.

Unlike a plain request/response wrapper, the server keeps a **persistent
session** per connected client: the IMAP connection is opened once and reused,
so a page refresh does not drop it. Long operations (cleanup, listing senders)
run in a **background thread** that can be **stopped**, and the page polls for
new log lines and status - the UI never freezes.

Run it with the installed command::

    imap-cleanup-tool-web              # opens the browser on http://127.0.0.1:8765

Install the dependencies with::

    pip install "imap-cleanup-tool[web]"
"""

# NOTE: deliberately no ``from __future__ import annotations`` - FastAPI must
# resolve the Pydantic request models (defined locally in create_app) from real
# annotation objects, not strings.

import csv
import io
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (__version__, ai, core, llm, notifications, profiles, scheduler,
               spamstore)
from .rules import RuleError, compile_search, node_from_dict
from .targets import parse_targets_text

STATIC_DIR = Path(__file__).parent / "web" / "static"
ASSETS_DIR = Path(__file__).parent / "assets"
# Extensible config presets live as JSON next to the assets (IMAP + SMTP providers
# and the LLM model picker list).
PROVIDERS_FILE = ASSETS_DIR / "providers.json"

# Fallback if providers.json is missing/corrupt.
PROVIDER_PRESETS = [
    {"name": "Custom", "host": "", "port": 993},
    {"name": "Gmail", "host": "imap.gmail.com", "port": 993},
    {"name": "Outlook / Office 365", "host": "outlook.office365.com", "port": 993},
    {"name": "iCloud Mail", "host": "imap.mail.me.com", "port": 993},
]


SMTP_PROVIDERS_FILE = ASSETS_DIR / "smtp_providers.json"
MODELS_FILE = ASSETS_DIR / "llm_models.json"


def _load_providers() -> list:
    """Load the IMAP provider presets from the (extensible) JSON config file."""
    try:
        data = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return data
    except (OSError, ValueError):
        pass
    return PROVIDER_PRESETS


def _ai_extra_available() -> bool:
    """True if the optional ``[ai]`` extra (litellm) is importable."""
    import importlib.util
    return importlib.util.find_spec("litellm") is not None


def _load_smtp_providers() -> list:
    """Load the SMTP provider presets from the (extensible) JSON config file."""
    try:
        data = json.loads(SMTP_PROVIDERS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return data
    except (OSError, ValueError):
        pass
    return [{"name": "Custom", "host": "", "port": 587, "security": "starttls"}]


def _load_llm_models() -> dict:
    """Load the LLM model presets (``{"remote": [...], "local": [...]}``) used by
    the model-string picker: the bundled shortlist plus the user's own saved presets
    (which persist locally). Free text is still allowed, so this is just a shortlist."""
    bundled = {"remote": ["gpt-4o-mini", "gpt-4o"], "local": ["ollama/llama3"]}
    try:
        data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and (data.get("remote") or data.get("local")):
            bundled = {"remote": list(data.get("remote") or []),
                       "local": list(data.get("local") or [])}
    except (OSError, ValueError):
        pass
    try:
        from . import llm
        user = llm.user_presets()
    except Exception:  # pylint: disable=broad-exception-caught
        user = {"remote": [], "local": []}
    out = {}
    for kind in ("remote", "local"):
        seen = []
        for m in list(bundled.get(kind, [])) + list(user.get(kind, [])):
            if m and m not in seen:
                seen.append(m)
        out[kind] = seen
    return out


def _llm_user_presets() -> dict:
    """Just the user-added presets (so the UI knows which dropdown items are
    removable). Bundled presets are not deletable."""
    try:
        from . import llm
        return llm.user_presets()
    except Exception:  # pylint: disable=broad-exception-caught
        return {"remote": [], "local": []}


def _safe_job_name(name: str) -> str:
    """Sanitise a job name so it is safe in a scheduler command line / task id."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "job").strip()) or "job"


def _folder_dicts(conn) -> list[dict]:
    """Folder rows for the UI: name, message count, a ``protected`` flag (system
    folders the user must not delete), and ``special`` use (e.g. "trash") read
    from the LIST flags so it works regardless of localized folder names."""
    names = core.list_folders(conn)
    counts = core.folder_message_counts(conn, names)
    attrs = core.folder_attributes(conn)
    protected = core.protected_folder_names(conn)

    def special(name: str) -> str:
        flags = {f.lower() for f in attrs.get(name, [])}
        if "\\trash" in flags:
            return "trash"
        if "\\junk" in flags:
            return "junk"
        return ""

    return [{"name": n, "count": counts.get(n),
             "protected": n in protected or n.upper() == "INBOX",
             "special": special(n)}
            for n in names]

FIELD_OPERATORS = {
    "sender": [["contains", "contains"], ["is exactly", "is"]],
    "subject": [["contains", "contains"], ["is exactly", "is"]],
    "date": [["on", "is"], ["on/after", "starts"], ["before", "ends"]],
}

# --------------------------------------------------------------------------- #
# Server-side session + background run state
# --------------------------------------------------------------------------- #
_SESSIONS: dict[str, "Session"] = {}
_RUN_BY_THREAD: dict[int, "RunState"] = {}


SESSION_LOG_CAP = 50000     # keep at most this many log lines per session
SESSION_IDLE_LIMIT = 600    # seconds: idle sessions are logged out and dropped


class RunState:
    """In-memory state of one background operation (cleanup or sender listing)."""

    def __init__(self, run_id: str, kind: str, session: "Session") -> None:
        self.run_id = run_id
        self.kind = kind                 # "run" | "senders"
        self.session = session
        self.status = "running"          # running | done | stopped | error
        self.stop = threading.Event()
        self.result: dict[str, Any] = {}
        self.error: str | None = None


class Session:
    """A live, reusable IMAP connection, its folder listing, and a rolling log."""

    def __init__(self, sid: str, conn, host: str, port: int, user: str) -> None:
        self.sid = sid
        self.conn = conn
        self.host = host
        self.port = port
        self.user = user
        self.local_cache = False         # opt-in header cache (per profile)
        self.folders: list[dict] = []    # [{name, count}]
        self.lock = threading.Lock()     # serialises IMAP use
        self.run: RunState | None = None
        self.log: list[str] = []         # rolling log buffer (persists refresh)
        self.log_base = 0                # absolute index of self.log[0]
        self.last_seen = time.monotonic()
        self.export: tuple[str, bytes] | None = None   # last (filename, mbox bytes)

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def add_log(self, line: str) -> None:
        self.log.append(line)
        if len(self.log) > SESSION_LOG_CAP:
            drop = len(self.log) - SESSION_LOG_CAP
            del self.log[:drop]
            self.log_base += drop

    def log_since(self, cursor: int) -> tuple[list[str], int]:
        start = max(0, cursor - self.log_base)
        return self.log[start:], self.log_base + len(self.log)


def _session_cache(sess: "Session"):
    """A HeaderCache when this session's profile enabled local caching, else None.

    Shared by every header-fetching operation (AI report/run, list senders,
    full-scan match) so the cache applies wherever headers are downloaded.
    """
    if not getattr(sess, "local_cache", False):
        return None
    try:
        from .headercache import HeaderCache
        return HeaderCache()
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def _install_log_dispatch() -> None:
    """Attach (once) a handler that routes core log records to the running job
    of the current thread, so each background run captures only its own log."""
    if getattr(_install_log_dispatch, "_done", False):
        return

    class _Dispatch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            run = _RUN_BY_THREAD.get(threading.get_ident())
            if run is not None:
                run.session.add_log(self.format(record))

    handler = _Dispatch()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
    core.logger.handlers.clear()
    core.logger.addHandler(handler)
    core.logger.setLevel(logging.INFO)
    _install_log_dispatch._done = True   # type: ignore[attr-defined]


def _start_run(session: "Session", kind: str, work) -> "RunState":
    """Spawn ``work(rs)`` in a background thread bound to a new RunState."""
    run = RunState(uuid.uuid4().hex[:12], kind, session)
    session.run = run

    def worker() -> None:
        _RUN_BY_THREAD[threading.get_ident()] = run
        try:
            with session.lock:
                work(run)
            run.status = "stopped" if run.stop.is_set() else "done"
        except core.StopRequested:
            run.status = "stopped"
            session.add_log("⏹  Operation stopped by the user.")
        except (OSError, core.imaplib.IMAP4.error) as exc:
            run.status = "error"
            run.error = str(exc)
            session.add_log(f"[NETWORK ERROR] {exc}")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            run.status = "error"
            run.error = str(exc)
            session.add_log(f"[ERROR] {exc}")
        finally:
            _RUN_BY_THREAD.pop(threading.get_ident(), None)

    threading.Thread(target=worker, daemon=True).start()
    return run


def _start_reaper() -> None:
    """Start (once) a daemon that logs out IMAP sessions left idle.

    A page kept open sends a heartbeat that refreshes ``last_seen``; when the page
    is closed abruptly the heartbeat stops and the connection is reaped instead of
    hanging. Running operations are never reaped.
    """
    if getattr(_start_reaper, "_done", False):
        return

    def loop() -> None:
        while True:
            time.sleep(60)
            now = time.monotonic()
            for sid, sess in list(_SESSIONS.items()):
                running = bool(sess.run and sess.run.status == "running")
                if not running and now - sess.last_seen > SESSION_IDLE_LIMIT:
                    core.safe_logout(sess.conn)
                    _SESSIONS.pop(sid, None)

    threading.Thread(target=loop, daemon=True).start()
    _start_reaper._done = True   # type: ignore[attr-defined]


def create_app():
    """Build and return the FastAPI application (lazy fastapi import)."""
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, Response
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field

    _install_log_dispatch()
    _start_reaper()
    llm.ensure_default_models()      # seed gpt-4o-mini + Ollama on first run

    # ----- request models -------------------------------------------------- #
    class ConnIn(BaseModel):
        host: str = Field(..., min_length=1)
        port: int = 993
        user: str = Field(..., min_length=1)
        password: str = ""
        timeout: int = 120
        local_cache: bool = False

    class Match(BaseModel):
        match_mode: str = "targets"          # "targets" | "rule"
        targets_text: str = ""
        rule_tree: dict | None = None

    class Options(BaseModel):
        folders: list[str] = Field(default_factory=lambda: ["INBOX"])
        scan_mode: str = "search"
        include_subdomains: bool = False
        batch_size: int = core.UID_CHUNK_SIZE
        gmail_trash: bool = False
        expunge: bool = False
        empty_folder: bool = False
        move: bool = False                   # move matches to dest_folder
        dest_folder: str = ""                # destination for move
        dry_run: bool = True

    class RunIn(Match, Options):
        sid: str
        notify_secret: str = ""          # SMTP passphrase for the completion email

    class CreateFolderIn(BaseModel):
        sid: str
        name: str

    class AIReportIn(Match):
        sid: str
        folders: list[str] = Field(default_factory=lambda: ["INBOX"])
        threshold: float = 6.0
        sample_size: int = 5
        exclude: str = ""                    # one address per line
        weights: dict | None = None
        batch_size: int = core.UID_CHUNK_SIZE
        model: str = ""                      # LLM config name (optional)
        secret: str = ""                     # password for an encrypted model
        dry_run: bool = True                 # used by /api/ai-run
        expunge: bool = False                # used by /api/ai-run
        flag_spam: bool = False              # move 1 msg/confirmed sender to Junk
        check_spam: bool = True              # skip already-saved spam from the LLM
        notify_secret: str = ""              # SMTP passphrase for the report/run email

    class LLMModelIn(BaseModel):
        name: str
        model: str = "gpt-4o-mini"
        api_key: str = ""
        api_base: str = ""
        encrypt: bool = False
        secret: str = ""
        track_costs: bool = False
        cost_input: float = 0.0
        cost_output: float = 0.0
        update_key: bool = True          # False = keep the stored key when editing

    class LlmPresetIn(BaseModel):
        kind: str = "remote"             # 'remote' | 'local'
        value: str
        remove: bool = False             # True = delete this user preset

    class SmtpProfileIn(BaseModel):
        name: str
        host: str
        port: int = 587
        security: str = "starttls"       # ssl | starttls | none
        user: str = ""
        password: str = ""
        from_addr: str = ""
        encrypt: bool = False
        secret: str = ""
        update_password: bool = True     # False = keep the stored password on edit

    class SmtpActionIn(BaseModel):
        name: str
        secret: str = ""

    class NotifySettingsIn(BaseModel):
        active: str | None = None
        notify_to: str | None = None
        notify_jobs: bool | None = None
        notify_runs: bool | None = None

    class SpamActionIn(BaseModel):
        sid: str
        addresses: list[str] = Field(default_factory=list)
        mode: str = "move"        # spam-flag: "delete" (move 1 + delete rest) | "move"
        folders: list[str] = Field(default_factory=list)   # spam-flag scope
        skip_done: bool = False   # spam-unsubscribe: skip already-unsubscribed ones

    class SpamAddIn(BaseModel):
        sid: str
        address: str
        score: float | None = None

    class SpamTargetsIn(BaseModel):
        sid: str
        op: str = "ge"            # is | le | ge | lt | gt
        score: float = 6.0

    class SendersIn(Match):
        sid: str
        folders: list[str] = Field(default_factory=lambda: ["INBOX"])
        batch_size: int = core.UID_CHUNK_SIZE
        save_path: str | None = None
        scan_mode: str = "search"
        include_subdomains: bool = False

    class RuleIn(BaseModel):
        tree: dict

    class SidIn(BaseModel):
        sid: str

    class JobNameIn(BaseModel):
        name: str

    class ProfileSaveIn(BaseModel):
        name: str
        host: str
        port: int = 993
        user: str
        password: str = ""
        timeout: int = 120
        encrypt: bool = False
        secret: str = ""
        local_cache: bool = False

    class ProfileLoadIn(BaseModel):
        name: str
        secret: str = ""

    class JobIn(Match, Options):
        name: str = "job"
        profile: str = ""                    # connection profile (non-encrypted)
        notify_profile: str = ""             # SMTP profile for the email (non-encrypted)
        kind: str = "daily"   # once|interval|hourly|daily|weekly|monthly
        time: str = "03:00"   # HH:MM (once/hourly/daily/weekly/monthly)
        date: str = ""        # YYYY-MM-DD (once only)
        minutes: int = 60     # interval only
        day: str = ""         # weekly: MON..SUN; monthly: day-of-month 1..31
        ai_cleanup: bool = False
        ai_model: str = ""    # non-encrypted LLM model config name
        ai_threshold: float = 6.0
        ai_sample: int = 5
        ai_skip_llm: bool = False     # heuristic only (no model)
        ai_report_only: bool = False  # build report, delete nothing (emailed if on)
        ai_flag_spam: bool = False    # report confirmed senders as spam (1 msg -> Junk)
        ai_check_spam: bool = True    # skip already-saved spam from the LLM

    # ----- helpers --------------------------------------------------------- #
    def _session(sid: str) -> "Session":
        sess = _SESSIONS.get(sid)
        if sess is None:
            raise HTTPException(440, "Not connected. Click Connect.")
        sess.touch()
        return sess

    def _resolve_match(match: "Match", allow_empty: bool = False):
        """Return (addresses, domains, exact_domains, search_argument).

        With ``allow_empty`` an empty filter is **not** an error - it yields no
        criteria (callers like Export / List senders then act on the whole
        folder), instead of raising.
        """
        if match.match_mode == "rule":
            if not match.rule_tree:
                if allow_empty:
                    return set(), set(), set(), None
                raise HTTPException(400, "No rule provided.")
            try:
                arg = compile_search(node_from_dict(match.rule_tree))
                return set(), set(), set(), arg
            except (RuleError, KeyError, TypeError) as exc:
                raise HTTPException(400, f"Invalid rule: {exc}") from exc
        try:
            addresses, domains, exact_domains = parse_targets_text(
                match.targets_text)
        except ValueError as exc:
            if allow_empty:
                return set(), set(), set(), None
            raise HTTPException(400, str(exc)) from exc
        return addresses, domains, exact_domains, None

    # ----- app ------------------------------------------------------------- #
    app = FastAPI(title="imap-cleanup-tool", docs_url="/api/docs")

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {
            "version": __version__,
            "providers": _load_providers(),
            "smtp_providers": _load_smtp_providers(),
            "models": _load_llm_models(),
            "models_user": _llm_user_presets(),
            "fields": list(FIELD_OPERATORS),
            "operators": FIELD_OPERATORS,
            "gmail_store_cap": core.GMAIL_STORE_CAP,
            "default_batch": core.UID_CHUNK_SIZE,
            "ai_available": _ai_extra_available(),
        }

    @app.post("/api/connect")
    def connect(body: ConnIn) -> dict[str, Any]:
        try:
            conn = core.connect(body.host, body.port, body.user,
                                body.password, body.timeout)
        except (OSError, core.imaplib.IMAP4.error) as exc:
            raise HTTPException(502, f"Connection/login failed: {exc}") from exc
        sid = uuid.uuid4().hex
        sess = Session(sid, conn, body.host, body.port, body.user)
        sess.local_cache = bool(body.local_cache)
        try:
            sess.folders = _folder_dicts(conn)
        except (OSError, core.imaplib.IMAP4.error):
            sess.folders = []
        _SESSIONS[sid] = sess
        return {"sid": sid, "host": sess.host, "user": sess.user,
                "folders": sess.folders}

    @app.get("/api/session/{sid}")
    def session_info(sid: str) -> dict[str, Any]:
        sess = _SESSIONS.get(sid)
        if sess is None:
            return {"connected": False}
        sess.touch()
        running = bool(sess.run and sess.run.status == "running")
        return {"connected": True, "host": sess.host, "user": sess.user,
                "folders": sess.folders,
                "log_cursor": sess.log_base + len(sess.log),
                "run_id": sess.run.run_id if running else None}

    @app.post("/api/refresh-folders")
    def refresh_folders(body: SidIn) -> dict[str, Any]:
        sess = _session(body.sid)
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is running; try again after "
                                     "it finishes.")
        with sess.lock:
            try:
                sess.folders = _folder_dicts(sess.conn)
            except (OSError, core.imaplib.IMAP4.error) as exc:
                raise HTTPException(502, f"IMAP error: {exc}") from exc
        return {"folders": sess.folders}

    @app.post("/api/create-folder")
    def create_folder(body: CreateFolderIn) -> dict[str, Any]:
        """Create a folder (a label on Gmail) on the server, then reload the list."""
        sess = _session(body.sid)
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(400, "Provide a folder/label name.")
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is running; try again after "
                                     "it finishes.")
        with sess.lock:
            try:
                message = core.create_folder(sess.conn, name)
                sess.folders = _folder_dicts(sess.conn)
            except (OSError, core.imaplib.IMAP4.error) as exc:
                raise HTTPException(502, f"IMAP error: {exc}") from exc
        return {"message": message, "created": name, "folders": sess.folders}

    @app.post("/api/delete-folder")
    def delete_folder(body: CreateFolderIn) -> dict[str, Any]:
        """Delete a non-system folder/label on the server, then reload the list."""
        sess = _session(body.sid)
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(400, "Provide a folder/label name.")
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is running; try again after "
                                     "it finishes.")
        with sess.lock:
            try:
                message = core.delete_folder(sess.conn, name)
                sess.folders = _folder_dicts(sess.conn)
            except ValueError as exc:                # protected / system folder
                raise HTTPException(400, str(exc)) from exc
            except (OSError, core.imaplib.IMAP4.error) as exc:
                raise HTTPException(502, f"IMAP error: {exc}") from exc
        return {"message": message, "deleted": name, "folders": sess.folders}

    @app.post("/api/disconnect/{sid}")
    def disconnect(sid: str) -> dict[str, Any]:
        sess = _SESSIONS.pop(sid, None)
        if sess is not None:
            core.safe_logout(sess.conn)
        return {"ok": True}

    @app.post("/api/validate-rule")
    def validate_rule(body: RuleIn) -> dict[str, Any]:
        try:
            return {"search": compile_search(node_from_dict(body.tree))}
        except (RuleError, KeyError, TypeError) as exc:
            raise HTTPException(400, f"Invalid rule: {exc}") from exc

    # ----- connection profiles (local SQLite, optionally encrypted) -------- #
    @app.get("/api/profiles")
    def get_profiles() -> dict[str, Any]:
        return {"profiles": profiles.list_profiles()}

    @app.post("/api/profiles")
    def save_profile(body: ProfileSaveIn) -> dict[str, Any]:
        try:
            name = profiles.save_profile(
                body.name, body.host, body.port, body.user, body.password,
                body.timeout, body.encrypt, body.secret, body.local_cache)
        except profiles.ProfileError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"saved": name}

    @app.post("/api/profiles/load")
    def load_profile(body: ProfileLoadIn) -> dict[str, Any]:
        try:
            return profiles.load_profile(body.name, body.secret)
        except profiles.ProfileError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/profiles/{name}")
    def delete_profile(name: str) -> dict[str, Any]:
        profiles.delete_profile(name)
        return {"deleted": name}

    # ----- SMTP profiles + email notifications ----------------------------- #
    @app.get("/api/smtp-profiles")
    def get_smtp_profiles() -> dict[str, Any]:
        return {"profiles": notifications.list_profiles(),
                "settings": notifications.get_settings()}

    @app.post("/api/smtp-profiles")
    def save_smtp_profile(body: SmtpProfileIn) -> dict[str, Any]:
        try:
            name = notifications.save_profile(
                body.name, body.host, body.port, body.user, body.password,
                from_addr=body.from_addr, security=body.security,
                encrypt=body.encrypt, secret=body.secret,
                update_password=body.update_password)
        except notifications.NotifyError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"saved": name}

    @app.delete("/api/smtp-profiles/{name}")
    def delete_smtp_profile(name: str) -> dict[str, Any]:
        notifications.delete_profile(name)
        return {"deleted": name}

    @app.post("/api/smtp-test")
    def smtp_test(body: SmtpActionIn) -> dict[str, Any]:
        try:
            return notifications.test_connection(body.name, body.secret)
        except notifications.NotifyError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/smtp-send-test")
    def smtp_send_test(body: SmtpActionIn) -> dict[str, Any]:
        """Send a test email to the configured recipient via the named profile."""
        s = notifications.get_settings()
        if not s["notify_to"]:
            raise HTTPException(400, "Set a recipient address first.")
        try:
            cfg = notifications.load_profile(body.name, body.secret)
            notifications.send_email(
                cfg, s["notify_to"], "[imap-cleanup-tool] Test email",
                "This is a test email from imap-cleanup-tool. "
                "Your SMTP notifications are working.")
        except notifications.NotifyError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"sent": s["notify_to"]}

    @app.post("/api/smtp-verify")
    def smtp_verify(body: SmtpActionIn) -> dict[str, Any]:
        """Check an SMTP profile's passphrase (decrypt-test only, sends nothing)."""
        try:
            notifications.load_profile(body.name, body.secret)
        except notifications.NotifyError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/notify-settings")
    def save_notify_settings(body: NotifySettingsIn) -> dict[str, Any]:
        notifications.set_settings(
            active=body.active, notify_to=body.notify_to,
            notify_jobs=body.notify_jobs, notify_runs=body.notify_runs)
        return notifications.get_settings()

    def _notify_run(account, folders, total, *, dry_run, gmail,
                    kind="Cleanup", dest="", secret="") -> None:
        """Send a run-completion email if 'notify on runs' is enabled (best effort).

        ``secret`` is the SMTP passphrase when the active profile is encrypted
        (collected up-front by the UI); without it an encrypted profile is skipped.
        """
        try:
            subj, body = notifications.cleanup_summary(
                account, folders, total, dry_run=dry_run, gmail=gmail, kind=kind,
                dest=dest)
            if notifications.send_notification(subj, body, when="run",
                                               secret=secret):
                core.logger.info("Notification email sent to the configured "
                                 "recipient.")
        except notifications.NotifyError as exc:
            core.logger.warning("Notification email not sent: %s", exc)

    def _notify_report(account, report, filename: str = "ai_report.csv",
                       secret: str = "") -> None:
        """Email the AI report as a CSV attachment if 'notify on runs' is on.

        ``filename`` should match the report's saved-on-disk name so the emailed
        attachment and the downloadable file line up.
        """
        try:
            flagged = report.get("flagged_count", 0)
            deletable = report.get("flagged_messages", 0)
            subject = (f"[imap-cleanup-tool] AI report on {account}: "
                       f"{flagged} sender(s) flagged")
            body = (f"AI Cleanup report (report only - nothing deleted) for "
                    f"account: {account}\nFlagged senders: {flagged}\n"
                    f"Emails potentially deletable: {deletable}\n\n"
                    f"The full report is attached as CSV.\n\n- imap-cleanup-tool")
            csv_text = core.ai_report_csv(report)
            if notifications.send_notification(
                    subject, body, when="run", secret=secret,
                    attachments=[(filename, csv_text)]):
                core.logger.info("Report emailed to the configured recipient.")
        except notifications.NotifyError as exc:
            core.logger.warning("Report email not sent: %s", exc)

    # ----- LLM model configs (for AI Cleanup) ------------------------------ #
    @app.get("/api/llm-models")
    def get_llm_models() -> dict[str, Any]:
        return {"models": llm.list_models()}

    @app.post("/api/llm-models")
    def save_llm_model(body: LLMModelIn) -> dict[str, Any]:
        try:
            name = llm.save_model(
                body.name, body.model, body.api_key, body.api_base,
                body.encrypt, body.secret, body.track_costs,
                body.cost_input, body.cost_output, update_key=body.update_key)
        except llm.LLMError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"saved": name}

    @app.delete("/api/llm-models/{name}")
    def delete_llm_model(name: str) -> dict[str, Any]:
        llm.delete_model(name)
        return {"deleted": name}

    @app.post("/api/llm-presets")
    def add_llm_preset(body: LlmPresetIn) -> dict[str, Any]:
        """Add or remove a custom model id in the user's presets; returns the
        merged picker list plus the user-only presets (which are removable)."""
        if body.remove:
            llm.remove_user_preset(body.kind, body.value)
        else:
            llm.add_user_preset(body.kind, body.value)
        return {"models": _load_llm_models(), "models_user": _llm_user_presets()}

    @app.post("/api/senders")
    def senders(body: SendersIn) -> dict[str, Any]:
        sess = _session(body.sid)
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is already running.")
        folders = body.folders or ["INBOX"]
        save_path = (body.save_path or "").strip() or None
        addresses, domains, exact_domains, search_argument = _resolve_match(
            body, allow_empty=True)        # empty filter = list every sender

        def work(rs: RunState) -> None:
            # list_senders logs each sender (count | address) into the session
            # log, which the client streams. We also keep a structured copy in
            # the run result (server-side only, not sent on every poll) so it can
            # be exported as CSV on demand via /api/senders.csv. When a filter is
            # set it only lists senders among the matching messages.
            ranked: list[dict[str, Any]] = []
            cache = _session_cache(sess)
            for folder in folders:
                counts = core.list_senders(
                    sess.conn, folder, body.batch_size,
                    should_stop=rs.stop.is_set, account=sess.user,
                    save_path=save_path, cache=cache,
                    addresses=addresses, domains=domains,
                    exact_domains=exact_domains, search_argument=search_argument,
                    include_subdomains=body.include_subdomains,
                    scan_mode=body.scan_mode)
                for sender, count in sorted(counts.items(),
                                            key=lambda kv: kv[1], reverse=True):
                    ranked.append({"folder": folder, "sender": sender,
                                   "count": count})
            rs.result = {"senders": ranked, "saved_to": save_path}

        run = _start_run(sess, "senders", work)
        return {"run_id": run.run_id}

    @app.post("/api/run")
    def run(body: RunIn) -> dict[str, Any]:
        sess = _session(body.sid)
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is already running.")
        addresses: set[str] = set()
        domains: set[str] = set()
        exact_domains: set[str] = set()
        search_argument = None
        if not body.empty_folder:
            has_rule = body.match_mode == "rule" and bool(body.rule_tree)
            has_targets = (body.match_mode != "rule"
                           and bool((body.targets_text or "").strip()))
            if body.move and not has_rule and not has_targets:
                search_argument = "ALL"   # move every message (no filter)
            else:
                addresses, domains, exact_domains, search_argument = \
                    _resolve_match(body)
        dest = (body.dest_folder or "").strip()
        if body.move and not body.empty_folder and not dest:
            raise HTTPException(400, "Choose a destination folder for the move.")
        folders = body.folders or ["INBOX"]

        def work(rs: RunState) -> None:
            total = 0
            if body.empty_folder:
                for folder in folders:
                    total += core.empty_folder(sess.conn, folder, body.dry_run,
                                                batch_size=body.batch_size,
                                                should_stop=rs.stop.is_set)
            else:
                cache = _session_cache(sess)   # used by --scan-mode full
                for folder in folders:
                    total += core.process_folder(
                        sess.conn, folder, addresses=addresses, domains=domains,
                        exact_domains=exact_domains,
                        search_argument=search_argument, dry_run=body.dry_run,
                        expunge=body.expunge,
                        include_subdomains=body.include_subdomains,
                        batch_size=body.batch_size, scan_mode=body.scan_mode,
                        gmail_trash=body.gmail_trash, move=body.move,
                        dest_folder=dest, should_stop=rs.stop.is_set,
                        cache=cache, account=sess.user)
            verb = "would be processed" if body.dry_run else "processed"
            core.logger.info("Done. %d message(s) %s.", total, verb)
            rs.result = {"processed": total, "dry_run": body.dry_run}
            if body.empty_folder:
                kind = "Empty folder"
            elif body.move:
                kind = "Move"
            else:
                kind = "Cleanup"
            _notify_run(sess.user, folders, total, dry_run=body.dry_run,
                        gmail=body.gmail_trash and not body.move, kind=kind,
                        dest=dest, secret=body.notify_secret)

        run_state = _start_run(sess, "run", work)
        return {"run_id": run_state.run_id}

    @app.post("/api/count")
    def count_matches(body: RunIn) -> dict[str, Any]:
        """Count how many messages the current filter matches (no changes)."""
        sess = _session(body.sid)
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is already running.")
        addresses, domains, exact_domains, search_argument = _resolve_match(body)
        folders = body.folders or ["INBOX"]

        def work(rs: RunState) -> None:
            cache = _session_cache(sess)   # used by --scan-mode full
            total = 0
            for folder in folders:
                total += core.process_folder(
                    sess.conn, folder, addresses=addresses, domains=domains,
                    exact_domains=exact_domains, search_argument=search_argument,
                    dry_run=True, count_only=True,
                    include_subdomains=body.include_subdomains,
                    batch_size=body.batch_size, scan_mode=body.scan_mode,
                    should_stop=rs.stop.is_set, cache=cache, account=sess.user)
            core.logger.info("=> %d matching message(s) across %d folder(s).",
                             total, len(folders))
            rs.result = {"matched": total}

        run_state = _start_run(sess, "count", work)
        return {"run_id": run_state.run_id}

    @app.post("/api/export-messages")
    def export_messages(body: RunIn) -> dict[str, Any]:
        """Download the full matching messages (bodies included) as one ``.mbox``.

        Same matching as Count (rule / targets, search or full) - or the **whole**
        folder when no filter is set. Read-only: ``BODY.PEEK[]`` never marks mail
        read. The blob is held on the session; the browser then GETs it.
        """
        sess = _session(body.sid)
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is already running.")
        addresses, domains, exact_domains, search_argument = _resolve_match(
            body, allow_empty=True)        # empty filter = export the whole folder
        folders = body.folders or ["INBOX"]

        def work(rs: RunState) -> None:
            cache = _session_cache(sess)
            raws: list[bytes] = []
            for folder in folders:
                uids = core.matched_uids(
                    sess.conn, folder, addresses=addresses, domains=domains,
                    exact_domains=exact_domains, search_argument=search_argument,
                    include_subdomains=body.include_subdomains,
                    scan_mode=body.scan_mode, batch_size=body.batch_size,
                    should_stop=rs.stop.is_set, cache=cache, account=sess.user,
                    match_all_if_empty=True)
                core.logger.info("Export: %d message(s) matched in %r.",
                                 len(uids), folder)
                if uids:
                    raws.extend(core.fetch_messages(
                        sess.conn, uids, body.batch_size, rs.stop.is_set))
            mbox = core.build_mbox(raws)
            stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M")
            fname = f"messages_{scheduler.account_slug(sess.user)}_{stamp}.mbox"
            sess.export = (fname, mbox)
            core.logger.info("=> exported %d message(s) (%d KB) -> %s",
                             len(raws), len(mbox) // 1024, fname)
            rs.result = {"messages": len(raws), "filename": fname}

        run_state = _start_run(sess, "export", work)
        return {"run_id": run_state.run_id}

    def _load_ai_model(body: "AIReportIn"):
        """Load the chosen LLM config (raising 400 on problems), or None."""
        name = (body.model or "").strip()
        if not name:
            return None
        try:
            return llm.load_model(name, body.secret)
        except llm.LLMError as exc:
            raise HTTPException(400, str(exc)) from exc

    def _ai_scope(body: "AIReportIn"):
        """Resolve the optional match scope for AI (filter, or whole folder)."""
        if body.match_mode == "rule" and body.rule_tree:
            try:
                return set(), set(), set(), compile_search(
                    node_from_dict(body.rule_tree))
            except (RuleError, KeyError, TypeError) as exc:
                raise HTTPException(400, f"Invalid rule: {exc}") from exc
        if (body.targets_text or "").strip():
            try:
                a, d, e = parse_targets_text(body.targets_text)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return a, d, e, None
        return set(), set(), set(), None      # no filter -> whole folder

    def _ai_build(sess: "Session", body: "AIReportIn", folders, exclude,
                  rs: "RunState", model_cfg, scope):
        """Heuristic report + optional LLM evaluation (attaches verdicts)."""
        addresses, domains, exact_domains, search_argument = scope
        cache = _session_cache(sess)
        if cache is not None:
            core.logger.info("Local header cache is ON for this profile.")
        report = core.build_ai_report(
            sess.conn, folders, threshold=body.threshold,
            sample_size=body.sample_size, exclude=exclude,
            weights=body.weights, addresses=addresses, domains=domains,
            exact_domains=exact_domains, search_argument=search_argument,
            batch_size=body.batch_size, should_stop=rs.stop.is_set,
            cache=cache, account=sess.user)
        if model_cfg:
            core.logger.info("Asking %s to evaluate %d flagged sender(s) ...",
                             model_cfg["model"], report["flagged_count"])
            # Log cost per batch so even a stopped/failed run records what the API
            # already billed (the final summary below covers a clean finish).
            recorder = None
            if model_cfg.get("track_costs"):
                recorder = lambda p, c, co: llm.log_cost(body.model, p, c, co)
            known_spam = (set(spamstore.all_addresses(sess.user))
                          if getattr(body, "check_spam", True) else None)
            try:
                ev = ai.evaluate(report, model_cfg, should_stop=rs.stop.is_set,
                                 record_cost=recorder, known_spam=known_spam)
            except core.StopRequested:
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                raise RuntimeError(f"LLM call failed: {exc}") from exc
            for s in report["senders"]:
                if s.get("flagged"):
                    s["verdict"] = ev["verdicts"].get(s["sender"].lower())
            report["llm"] = {"model": model_cfg["model"],
                             "prompt_tokens": ev["prompt_tokens"],
                             "completion_tokens": ev["completion_tokens"],
                             "cost": ev["cost"]}
            core.logger.info("LLM done: %d to delete · %d/%d tokens · cost %s",
                             sum(1 for v in ev["verdicts"].values() if v["delete"]),
                             ev["prompt_tokens"], ev["completion_tokens"],
                             ev["cost"])
        return report

    def _log_llm_total(report) -> None:
        """Log the aggregated LLM cost for this report (across all batches)."""
        info = report.get("llm")
        if not info:
            return
        cost = info.get("cost")
        cost_str = f"${cost:.6f}" if isinstance(cost, (int, float)) else \
            "not tracked (enable cost tracking on the model)"
        core.logger.info("=> LLM cost for this report: %s "
                         "(%d input + %d output tokens, model %s).",
                         cost_str, info.get("prompt_tokens", 0),
                         info.get("completion_tokens", 0), info.get("model", "?"))

    def _ai_reports_dir():
        return scheduler.ai_reports_dir()

    def _save_ai_report(report, account: str):
        """Persist the report as a per-account, timestamped CSV; return its Path."""
        try:
            path = scheduler.save_ai_report(core.ai_report_csv(report), account)
            core.logger.info("Report saved to disk as %s (pick it from the "
                             "download list).", path.name)
            return path
        except OSError as exc:
            core.logger.warning("Could not save the report to disk: %s", exc)
            return None

    def _record_spam(sess: "Session", report, source: str) -> None:
        """Save the flagged senders to this account's Spam addresses list."""
        try:
            n = spamstore.record_from_report(sess.user, report, source)
            if n:
                core.logger.info("Saved %d sender(s) to the Spam addresses list "
                                 "(see the Spam addresses tab).", n)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            core.logger.warning("Could not save spam addresses: %s", exc)

    @app.get("/api/header-cache/{sid}")
    def header_cache_status(sid: str) -> dict[str, Any]:
        """How many cached headers exist for the connected account (0 = none)."""
        sess = _session(sid)
        try:
            from .headercache import HeaderCache
            n = HeaderCache().count_account(sess.user)
            return {"exists": n > 0, "count": n}
        except Exception:  # pylint: disable=broad-exception-caught
            return {"exists": False, "count": 0}

    @app.delete("/api/header-cache/{sid}")
    def header_cache_clear(sid: str) -> dict[str, Any]:
        """Wipe this account's cached headers (used when caching is disabled)."""
        sess = _session(sid)
        try:
            from .headercache import HeaderCache
            HeaderCache().clear(sess.user)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise HTTPException(500, f"Could not clear cache: {exc}") from exc
        return {"cleared": True}

    @app.post("/api/ai-report")
    def ai_report(body: AIReportIn) -> dict[str, Any]:
        """Heuristic per-sender report; if a model is chosen, also LLM verdicts.
        Report only - never deletes."""
        sess = _session(body.sid)
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is already running.")
        folders = body.folders or ["INBOX"]
        # Exclusions come solely from the box (the UI pre-fills the user's own
        # address on connect, so it is excluded by default but can be removed).
        exclude = set((body.exclude or "").splitlines())
        model_cfg = _load_ai_model(body)
        scope = _ai_scope(body)        # resolve here so 400s reach the client

        def work(rs: RunState) -> None:
            report = _ai_build(sess, body, folders, exclude, rs, model_cfg, scope)
            rs.result = {"report": report}
            core.logger.info("=> AI report ready: %d of %d sender(s) flagged "
                             "(threshold %.1f); %d email(s) potentially deletable. "
                             "Use 'Download report (CSV)'.",
                             report["flagged_count"], report["total_senders"],
                             body.threshold, report.get("flagged_messages", 0))
            _log_llm_total(report)
            saved = _save_ai_report(report, sess.user)
            _record_spam(sess, report, "report")
            _notify_report(sess.user, report,
                           saved.name if saved else "ai_report.csv",
                           secret=body.notify_secret)

        run_state = _start_run(sess, "ai-report", work)
        return {"run_id": run_state.run_id}

    @app.post("/api/ai-run")
    def ai_run(body: AIReportIn) -> dict[str, Any]:
        """Heuristic + LLM evaluation, then DELETE the senders the LLM confirms."""
        sess = _session(body.sid)
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is already running.")
        if not (body.model or "").strip():
            raise HTTPException(400, "Choose a model for AI Cleanup (LLM tab).")
        folders = body.folders or ["INBOX"]
        exclude = set((body.exclude or "").splitlines())   # box-only (see ai-report)
        model_cfg = _load_ai_model(body)
        gmail = "gmail" in (sess.host or "").lower()
        scope = _ai_scope(body)        # resolve here so 400s reach the client

        def work(rs: RunState) -> None:
            report = _ai_build(sess, body, folders, exclude, rs, model_cfg, scope)
            confirmed = {s["sender"].lower() for s in report["senders"]
                         if s.get("flagged") and (s.get("verdict") or {}).get("delete")}
            rs.result = {"report": report, "to_delete": sorted(confirmed)}
            _log_llm_total(report)
            _save_ai_report(report, sess.user)
            _record_spam(sess, report, "run")
            if not confirmed:
                core.logger.info("=> AI confirmed nothing to delete.")
                return
            core.logger.info("AI confirmed %d sender(s) to delete.", len(confirmed))
            junk = core.special_folder(sess.conn, "\\Junk") if body.flag_spam \
                else None
            if body.flag_spam and not junk:
                core.logger.warning("Flag-as-spam requested but no Junk/Spam "
                                    "folder found - skipping that step.")
            total = 0
            for folder in folders:
                if junk:
                    m, _h = core.flag_senders_as_spam(
                        sess.conn, folder, confirmed, junk, per_sender=1,
                        dry_run=body.dry_run, batch_size=body.batch_size,
                        should_stop=rs.stop.is_set)
                    core.logger.info("Reported senders as spam in %r: %d "
                                     "message(s) moved to %r.", folder, m, junk)
                total += core.process_folder(
                    sess.conn, folder, addresses=confirmed, dry_run=body.dry_run,
                    expunge=body.expunge, gmail_trash=gmail,
                    batch_size=body.batch_size, scan_mode="search",
                    should_stop=rs.stop.is_set)
            verb = "would be deleted" if body.dry_run else "deleted"
            core.logger.info("=> AI Cleanup: %d message(s) %s.", total, verb)
            _notify_run(sess.user, folders, total, dry_run=body.dry_run,
                        gmail=gmail, kind="AI Cleanup", secret=body.notify_secret)

        run_state = _start_run(sess, "ai-run", work)
        return {"run_id": run_state.run_id}

    @app.get("/api/llm-costs/{name}")
    def llm_costs(name: str) -> dict[str, Any]:
        return llm.cost_log(name)

    @app.get("/api/ai-report.json/{sid}")
    def ai_report_json(sid: str) -> Response:
        sess = _session(sid)
        report = (sess.run.result or {}).get("report") if sess.run else None
        if not report:
            raise HTTPException(404, "No report yet - generate one first.")
        return Response(content=json.dumps(report, indent=2, ensure_ascii=False),
                        media_type="application/json")

    @app.get("/api/ai-report.csv/{sid}")
    def ai_report_csv(sid: str) -> Response:
        sess = _session(sid)
        report = (sess.run.result or {}).get("report") if sess.run else None
        if not report:
            raise HTTPException(404, "No report yet - generate one first.")
        return Response(
            content=core.ai_report_csv(report), media_type="text/csv",
            headers={"Content-Disposition":
                     "attachment; filename=ai-report.csv"})

    def _valid_report_name(name: str) -> bool:
        return not ("/" in name or "\\" in name) and \
            name.startswith("ai_report_") and name.endswith(".csv")

    @app.get("/api/ai-reports/list/{sid}")
    def ai_reports_list(sid: str) -> dict[str, Any]:
        """List this account's saved report CSVs (newest first).

        Returns {name, label} where label is the timestamp (the account prefix is
        stripped for display; the dropdown shows the timestamp)."""
        sess = _session(sid)
        prefix = f"ai_report_{scheduler.account_slug(sess.user)}_"
        items = []
        for p in _ai_reports_dir().glob(prefix + "*.csv"):
            items.append({"name": p.name,
                          "label": p.name[len(prefix):-len(".csv")]})
        items.sort(key=lambda x: x["label"], reverse=True)
        return {"reports": items}

    @app.get("/api/ai-reports/{name}")
    def ai_reports_get(name: str) -> Response:
        """Download a previously saved report CSV by file name."""
        if not _valid_report_name(name):
            raise HTTPException(400, "Invalid report name.")
        path = _ai_reports_dir() / name
        if not path.is_file():
            raise HTTPException(404, "No such saved report.")
        return Response(
            content=path.read_text(encoding="utf-8"), media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.delete("/api/ai-reports/{name}")
    def ai_reports_delete(name: str) -> dict[str, Any]:
        """Delete a saved report CSV."""
        if not _valid_report_name(name):
            raise HTTPException(400, "Invalid report name.")
        path = _ai_reports_dir() / name
        if path.is_file():
            path.unlink()
        return {"deleted": name}

    # ----- Spam addresses (per-account list from AI Cleanup) --------------- #
    @app.get("/api/spam/{sid}")
    def spam_list(sid: str, offset: int = 0, limit: int = 25, q: str = "",
                  unsub: str = "all", sort: str = "score",
                  dir: str = "desc") -> dict[str, Any]:
        sess = _session(sid)
        res = spamstore.list_addresses(sess.user, offset=offset, limit=limit,
                                       search=q, unsub=unsub, sort_by=sort,
                                       sort_dir=dir)
        # so the UI can warn when mailto-unsubscribes can't be sent (no SMTP)
        res["smtp_active"] = notifications.has_active_profile()
        res["unsub_email_total"] = spamstore.count_unsub_email(sess.user)
        return res

    @app.post("/api/spam-delete")
    def spam_delete(body: SpamActionIn) -> dict[str, Any]:
        sess = _session(body.sid)
        n = spamstore.delete_addresses(sess.user, body.addresses)
        return {"removed": n}

    @app.post("/api/spam-add")
    def spam_add(body: SpamAddIn) -> dict[str, Any]:
        """Manually add an address to this account's spam list."""
        sess = _session(body.sid)
        if not spamstore.add_address(sess.user, body.address, body.score):
            raise HTTPException(400, "Enter a valid email address.")
        return {"added": body.address.strip().lower(),
                "total": spamstore.count(sess.user)}

    @app.post("/api/spam-load-targets")
    def spam_load_targets(body: SpamTargetsIn) -> dict[str, Any]:
        """Spam addresses whose score matches the filter, to load into targets."""
        sess = _session(body.sid)
        return {"addresses": spamstore.addresses_by_score(
            sess.user, body.op, body.score)}

    @app.post("/api/spam-unsubscribe")
    def spam_unsubscribe(body: SpamActionIn) -> dict[str, Any]:
        """Unsubscribe from the selected senders via their List-Unsubscribe.

        Automatic for **mailto:** (sent from the active SMTP profile) and for
        RFC 8058 **one-click** HTTPS POSTs. Senders whose only method is a plain
        https link are returned as ``manual`` (the UI opens them).
        """
        sess = _session(body.sid)
        from . import unsubscribe as unsub
        from datetime import datetime
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        targets = spamstore.unsub_targets(sess.user, body.addresses)
        # optionally skip senders we already unsubscribed (the UI asks when the
        # selection includes some) - leaves the door open to re-do them otherwise
        skipped = 0
        if body.skip_done:
            already = set(spamstore.done_addresses(sess.user, body.addresses))
            before = len(targets)
            targets = [t for t in targets if t["address"] not in already]
            skipped = before - len(targets)
        smtp_ok = notifications.has_active_profile()
        done, manual, failed = [], [], []
        for t in targets:
            addr = t["address"]
            done_it = False
            reason = ""
            # 1) prefer the mailto path (most senders identify you by the To token)
            if t["mailto"]:
                if not smtp_ok:
                    # missing SMTP is already flagged upstream by the spam-tab
                    # banner, so just report it - no silent fall back to a link
                    failed.append({"address": addr, "reason":
                                   "no active SMTP profile (set one in "
                                   "Notifications to send unsubscribe emails)"})
                    continue
                try:
                    to, subj, bod = unsub.parse_mailto(t["mailto"])
                    notifications.send_from_active(to, subj, bod)
                    done.append(addr)
                    spamstore.mark_unsubscribed(sess.user, addr, "email",
                                                "unsubscribe email sent", now)
                    done_it = True
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    reason = str(exc)     # the send errored -> try the link below
            # 2) no mailto, or the email send errored: use the https link if any
            #    (RFC 8058 one-click if advertised, else a manual confirmation page)
            if not done_it and t["http"]:
                if t["oneclick"] and unsub.http_one_click(t["http"]):
                    done.append(addr)
                    spamstore.mark_unsubscribed(
                        sess.user, addr, "oneclick",
                        "one-click request confirmed (HTTP 2xx)", now)
                else:
                    manual.append({"address": addr, "url": t["http"]})
                done_it = True
            # 3) nothing usable left - report why (with any link, for the UI)
            if not done_it:
                failed.append({"address": addr,
                               "reason": reason or "no usable unsubscribe method",
                               "url": t["http"] or ""})
        core.logger.info("Unsubscribe: %d auto, %d to open by hand, %d failed, "
                         "%d skipped (already done).",
                         len(done), len(manual), len(failed), skipped)
        return {"unsubscribed": done, "manual": manual, "failed": failed,
                "skipped": skipped, "requested": len(body.addresses or []),
                "with_method": len(targets) + skipped}

    @app.post("/api/spam-unsub-precheck")
    def spam_unsub_precheck(body: SpamActionIn) -> dict[str, Any]:
        """How many of the selected senders are already unsubscribed.

        The UI calls this before unsubscribing so it can ask whether to re-do or
        skip the already-done ones.
        """
        sess = _session(body.sid)
        return {"done": len(spamstore.done_addresses(sess.user, body.addresses)),
                "selected": len(body.addresses or [])}

    @app.post("/api/spam-select-all")
    def spam_select_all(body: SpamActionIn) -> dict[str, Any]:
        """All spam addresses for the account (used by 'select all' bulk ops)."""
        sess = _session(body.sid)
        return {"addresses": spamstore.all_addresses(sess.user)}

    @app.post("/api/spam-flag")
    def spam_flag(body: SpamActionIn) -> dict[str, Any]:
        """Flag senders as spam on the server.

        ``mode="delete"`` works like Run: move **one** of each sender's messages
        to Junk/Spam (the training signal), then **delete** the rest. ``mode="move"``
        moves **all** their inbox mail to Junk/Spam and deletes nothing. Reports
        how many senders actually had mail.
        """
        sess = _session(body.sid)
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is already running.")
        addrs = {a.strip().lower() for a in (body.addresses or []) if a.strip()}
        if not addrs:
            raise HTTPException(400, "No addresses selected.")
        delete = body.mode == "delete"
        gmail = "gmail" in (sess.host or "").lower()
        # Scope = the folders selected in the Cleanup tab (same as a run).
        folders = body.folders or ["INBOX"]
        moved = deleted = 0
        hit: set[str] = set()
        with sess.lock:
            junk = core.special_folder(sess.conn, "\\Junk")
            if not junk:
                raise HTTPException(
                    400, "No Spam/Junk folder found on this server.")
            if junk in folders:
                raise HTTPException(
                    400, "The Junk/Spam folder is in the selected folders - "
                         "deselect it in the Cleanup tab first.")
            try:
                for folder in folders:
                    m, h = core.flag_senders_as_spam(
                        sess.conn, folder, addrs, junk,
                        per_sender=1 if delete else None,
                        batch_size=core.UID_CHUNK_SIZE)
                    moved += m
                    hit |= h
                    if delete:
                        deleted += core.process_folder(
                            sess.conn, folder, addresses=addrs, dry_run=False,
                            expunge=not gmail, gmail_trash=gmail,
                            batch_size=core.UID_CHUNK_SIZE, scan_mode="search")
            except (OSError, core.imaplib.IMAP4.error) as exc:
                raise HTTPException(400, f"Flag-as-spam failed: {exc}") from exc
        no_mail = len(addrs) - len(hit)
        core.logger.info("Flagged %d sender(s) as spam (mode=%s) in %s: %d moved "
                         "to %r, %d deleted; %d had no mail.", len(addrs),
                         body.mode, ", ".join(folders), moved, junk, deleted,
                         no_mail)
        return {"flagged": len(hit), "moved": moved, "deleted": deleted,
                "folder": junk, "no_mail": no_mail, "selected": len(addrs),
                "mode": body.mode, "folders": folders}

    @app.get("/api/log/{sid}")
    def get_log(sid: str, cursor: int = 0) -> dict[str, Any]:
        """Return new session-log lines since ``cursor`` plus the run status.

        The log is per-session and survives a page refresh (the client also
        keeps a copy). One unified stream covers cleanup and sender listing.
        """
        sess = _session(sid)
        rs = sess.run
        lines, new_cursor = sess.log_since(cursor)
        return {"lines": lines, "cursor": new_cursor,
                "running": bool(rs and rs.status == "running"),
                "status": rs.status if rs else None,
                "run_id": rs.run_id if rs else None,
                "error": rs.error if rs else None}

    @app.get("/api/senders.csv/{sid}")
    def senders_csv(sid: str):
        """Return the last 'List senders' result as CSV (the browser downloads it)."""
        sess = _session(sid)
        rs = sess.run
        rows = rs.result.get("senders") if rs and rs.result else None
        if not rows:
            raise HTTPException(404, "No sender listing available yet. "
                                     "Run 'List senders' first.")
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["timestamp", "account", "folder", "sender", "count"])
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        for row in rows:
            writer.writerow([timestamp, sess.user, row["folder"],
                             row["sender"], row["count"]])
        return Response(content=buf.getvalue(), media_type="text/csv")

    @app.get("/api/export-messages.mbox/{sid}")
    def export_messages_download(sid: str):
        """Return the last Export-messages result as a downloadable ``.mbox``."""
        sess = _session(sid)
        if not sess.export:
            raise HTTPException(404, "No export available yet. Run "
                                     "'Export messages' first.")
        fname, blob = sess.export
        return Response(content=blob, media_type="application/mbox",
                        headers={"Content-Disposition":
                                 f"attachment; filename={fname}"})

    @app.post("/api/import-messages/{sid}")
    async def import_messages(sid: str, folder: str,
                              request: Request) -> dict[str, Any]:
        """Import messages from an uploaded ``.mbox`` (or ``.eml``) into ``folder``.

        The file is the raw request body (no multipart dep); ``folder`` is a
        query param. Each message is APPENDed to the destination folder.
        """
        sess = _session(sid)
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is already running.")
        dest = (folder or "").strip()
        if not dest:
            raise HTTPException(400, "Pick a destination folder for the import.")
        data = await request.body()      # the uploaded file is the raw body
        if not data:
            raise HTTPException(400, "The uploaded file is empty.")
        messages = core.read_messages(data)
        if not messages:
            raise HTTPException(400, "No messages found in the uploaded file.")

        def work(rs: RunState) -> None:
            core.logger.info("Importing %d message(s) into %r ...",
                             len(messages), dest)
            n = core.append_messages(sess.conn, dest, messages, rs.stop.is_set)
            core.logger.info("=> imported %d/%d message(s) into %r.",
                             n, len(messages), dest)
            rs.result = {"imported": n, "total": len(messages)}

        run_state = _start_run(sess, "import", work)
        return {"run_id": run_state.run_id}

    @app.post("/api/stop/{sid}/{run_id}")
    def stop(sid: str, run_id: str) -> dict[str, Any]:
        sess = _session(sid)
        if sess.run and sess.run.run_id == run_id:
            sess.run.stop.set()
        return {"ok": True}

    # ----- scheduling ------------------------------------------------------ #
    def _job_from(body: JobIn) -> scheduler.Job:
        name = _safe_job_name(body.name)
        prof = (body.profile or "").strip()
        if not prof:
            raise HTTPException(400, "Choose a (non-encrypted) connection "
                                     "profile for the job.")
        info = next((p for p in profiles.list_profiles()
                     if p["name"] == prof), None)
        if info is None:
            raise HTTPException(400, f"Profile {prof!r} not found.")
        if info["encrypted"]:
            raise HTTPException(400, "Encrypted profiles can't run unattended - "
                                     "use a non-encrypted profile for scheduled "
                                     "jobs.")
        args: list[str] = ["--profile", prof]
        nprof = (body.notify_profile or "").strip()
        if nprof:
            sm = next((p for p in notifications.list_profiles()
                       if p["name"] == nprof), None)
            if sm is None:
                raise HTTPException(400, f"SMTP profile {nprof!r} not found.")
            if sm["encrypted"]:
                raise HTTPException(400, "Encrypted SMTP profiles can't send from "
                                         "scheduled jobs - pick a non-encrypted one.")
            args += ["--notify-profile", nprof]
        for folder in (body.folders or ["INBOX"]):
            args += ["--folder", folder]
        if body.ai_cleanup:
            args += ["--ai-cleanup",
                     "--ai-threshold", str(body.ai_threshold),
                     "--ai-sample", str(int(body.ai_sample)), "--yes"]
            if body.ai_report_only:
                args += ["--ai-report-only"]
            if body.ai_flag_spam:
                args += ["--ai-flag-spam"]
            if not body.ai_check_spam:
                args += ["--ai-no-check-spam"]
            # Skip LLM = heuristic only -> no model. Otherwise a non-encrypted
            # model is required (for the run, or for the LLM verdicts in a report).
            if not body.ai_skip_llm:
                mname = (body.ai_model or "").strip()
                minfo = next((m for m in llm.list_models()
                              if m["name"] == mname), None)
                if minfo is None:
                    raise HTTPException(
                        400, "Choose a saved LLM model for the AI job "
                             "(or tick Skip LLM).")
                if minfo["encrypted"]:
                    raise HTTPException(
                        400, "Encrypted model configs can't run unattended - "
                             "use a non-encrypted one.")
                args += ["--ai-model", mname]
            elif not body.ai_report_only:
                raise HTTPException(
                    400, "Skip LLM only makes sense with Report only for a job "
                         "(heuristic alone can't decide what to delete).")
            try:
                sched = scheduler.build_schedule(
                    body.kind, time=body.time, date=body.date,
                    minutes=body.minutes, day=body.day)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return scheduler.Job(name=name, args=args, schedule=sched)
        if body.empty_folder:
            args.append("--empty-folder")
        elif body.match_mode == "rule" and body.rule_tree:
            try:
                args += ["--rule", node_from_dict(body.rule_tree).to_expression()]
            except (RuleError, KeyError, TypeError) as exc:
                raise HTTPException(400, f"Invalid rule: {exc}") from exc
        elif (body.targets_text or "").strip():
            # Persist the pasted target list to a file so the scheduled CLI can read it.
            try:
                parse_targets_text(body.targets_text)  # validate
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            tpath = scheduler.config_dir() / f"{name}.targets.txt"
            tpath.write_text(body.targets_text, encoding="utf-8")
            args += ["--targets", str(tpath)]
        elif body.move:
            pass   # move-all: no target list / rule -> the CLI moves every message
        else:
            raise HTTPException(400, "The job has no match. In the Cleanup tab "
                "fill the Target list or a Rule, enable Empty folder, or enable "
                "Move (which, with no filter, moves every message) - then save.")
        dest = (body.dest_folder or "").strip()
        if body.move and not body.empty_folder:
            if not dest:
                raise HTTPException(400, "Choose a destination folder for the "
                                         "move job.")
            args += ["--move", "--dest-folder", dest]
        else:
            if body.gmail_trash:
                args.append("--gmail-trash")
            if body.expunge:
                args.append("--expunge")
        args += ["--scan-mode", body.scan_mode, "--yes"]
        try:
            sched = scheduler.build_schedule(
                body.kind, time=body.time, date=body.date,
                minutes=body.minutes, day=body.day)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return scheduler.Job(name=name, args=args, schedule=sched)

    @app.get("/api/jobs")
    def list_jobs() -> dict[str, Any]:
        installed = scheduler.installed_job_names()
        return {"jobs": [{"name": j.name, "schedule": j.schedule,
                          "args": j.args, "last_run": j.last_run,
                          "installed": j.name in installed,
                          "command": scheduler.export_system(j)}
                         for j in scheduler.load_jobs()]}

    @app.post("/api/jobs")
    def save_job(body: JobIn) -> dict[str, Any]:
        job = _job_from(body)
        scheduler.upsert_job(job)
        return {"saved": job.name, "command": scheduler.export_system(job)}

    @app.delete("/api/jobs/{name}")
    def delete_job(name: str) -> dict[str, Any]:
        # Removing the saved job also removes its system task (it would fail
        # anyway once the job is gone).
        try:
            scheduler.uninstall_system(scheduler.Job(name=name, args=[],
                                                     schedule={}))
        except (RuntimeError, OSError):
            pass
        scheduler.delete_job(name)
        return {"deleted": name}

    @app.post("/api/jobs/uninstall")
    def uninstall_job(body: JobNameIn) -> dict[str, Any]:
        """Deregister the job from the OS scheduler, keeping the saved job."""
        try:
            message = scheduler.uninstall_system(
                scheduler.Job(name=body.name, args=[], schedule={}))
        except (RuntimeError, OSError) as exc:
            raise HTTPException(500, f"Could not remove the system task: {exc}") \
                from exc
        return {"message": message}

    @app.post("/api/jobs/install-saved")
    def install_saved_job(body: JobNameIn) -> dict[str, Any]:
        """Install an already-saved job into the OS scheduler."""
        job = next((j for j in scheduler.load_jobs() if j.name == body.name),
                   None)
        if job is None:
            raise HTTPException(404, f"No saved job named {body.name!r}.")
        try:
            message = scheduler.install_system(job)
        except (RuntimeError, OSError) as exc:
            raise HTTPException(500, f"Could not install the job: {exc}") from exc
        return {"message": message, "command": scheduler.export_system(job)}

    @app.post("/api/jobs/export")
    def export_job(body: JobIn) -> dict[str, Any]:
        # The OS command runs the job by name, so it must be saved first.
        job = _job_from(body)
        scheduler.upsert_job(job)
        return {"name": job.name, "command": scheduler.export_system(job)}

    @app.post("/api/jobs/install")
    def install_job(body: JobIn) -> dict[str, Any]:
        job = _job_from(body)
        scheduler.upsert_job(job)
        try:
            message = scheduler.install_system(job)
        except (RuntimeError, OSError) as exc:
            raise HTTPException(500, f"Could not install the job: {exc}") from exc
        return {"name": job.name, "message": message,
                "command": scheduler.export_system(job)}

    @app.get("/api/jobs/{name}/log")
    def job_log(name: str) -> dict[str, Any]:
        if not any(j.name == name for j in scheduler.load_jobs()):
            raise HTTPException(404, f"No saved job named {name!r}.")
        return {"name": name, "log": scheduler.read_job_log(name)}

    # ----- static ---------------------------------------------------------- #
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/logo.png")
    def logo() -> FileResponse:
        return FileResponse(ASSETS_DIR / "logo.png")

    @app.get("/favicon.ico")
    def favicon() -> FileResponse:
        return FileResponse(ASSETS_DIR / "favicon.ico")

    @app.get("/apple-touch-icon.png")
    def apple_touch_icon() -> FileResponse:
        return FileResponse(ASSETS_DIR / "apple-touch-icon.png")

    @app.get("/favicon-32.png")
    def favicon_32() -> FileResponse:
        return FileResponse(ASSETS_DIR / "favicon-32.png")

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``imap-cleanup-tool-web``: launch the local server."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="imap-cleanup-tool-web",
        description="Launch the imap-cleanup-tool local web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not open the browser automatically.")
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ModuleNotFoundError:
        print('[ERROR] The web UI needs the optional [web] extra, which is not '
              'installed. Install it with:\n'
              '    pip install "imap-cleanup-tool[web]"')
        return 2

    app = create_app()
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"imap-cleanup-tool web UI running on {url}  (press Ctrl+C to stop)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

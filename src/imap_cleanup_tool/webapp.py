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

from . import __version__, ai, core, llm, notifications, profiles, scheduler
from .rules import RuleError, compile_search, node_from_dict
from .targets import parse_targets_text

STATIC_DIR = Path(__file__).parent / "web" / "static"
ASSETS_DIR = Path(__file__).parent / "assets"
PROVIDERS_FILE = Path(__file__).parent / "web" / "providers.json"

# Fallback if providers.json is missing/corrupt.
PROVIDER_PRESETS = [
    {"name": "Custom", "host": "", "port": 993},
    {"name": "Gmail", "host": "imap.gmail.com", "port": 993},
    {"name": "Outlook / Office 365", "host": "outlook.office365.com", "port": 993},
    {"name": "iCloud Mail", "host": "imap.mail.me.com", "port": 993},
]


SMTP_PROVIDERS_FILE = Path(__file__).parent / "web" / "smtp_providers.json"


def _load_providers() -> list:
    """Load the IMAP provider presets from the (extensible) JSON config file."""
    try:
        data = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return data
    except (OSError, ValueError):
        pass
    return PROVIDER_PRESETS


def _load_smtp_providers() -> list:
    """Load the SMTP provider presets from the (extensible) JSON config file."""
    try:
        data = json.loads(SMTP_PROVIDERS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return data
    except (OSError, ValueError):
        pass
    return [{"name": "Custom", "host": "", "port": 587, "security": "starttls"}]


def _safe_job_name(name: str) -> str:
    """Sanitise a job name so it is safe in a scheduler command line / task id."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "job").strip()) or "job"


def _folder_dicts(conn) -> list[dict]:
    """Folder rows for the UI: name, message count, and a ``protected`` flag
    (system folders the user must not delete)."""
    names = core.list_folders(conn)
    counts = core.folder_message_counts(conn, names)
    protected = core.protected_folder_names(conn)
    return [{"name": n, "count": counts.get(n),
             "protected": n in protected or n.upper() == "INBOX"}
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
        self.folders: list[dict] = []    # [{name, count}]
        self.lock = threading.Lock()     # serialises IMAP use
        self.run: RunState | None = None
        self.log: list[str] = []         # rolling log buffer (persists refresh)
        self.log_base = 0                # absolute index of self.log[0]
        self.last_seen = time.monotonic()

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
    from fastapi import FastAPI, HTTPException
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

    class SendersIn(BaseModel):
        sid: str
        folders: list[str] = Field(default_factory=lambda: ["INBOX"])
        batch_size: int = core.UID_CHUNK_SIZE
        save_path: str | None = None

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

    class ProfileLoadIn(BaseModel):
        name: str
        secret: str = ""

    class JobIn(Match, Options):
        name: str = "job"
        profile: str = ""                    # connection profile (non-encrypted)
        kind: str = "daily"   # once|interval|hourly|daily|weekly|monthly
        time: str = "03:00"   # HH:MM (once/hourly/daily/weekly/monthly)
        date: str = ""        # YYYY-MM-DD (once only)
        minutes: int = 60     # interval only
        day: str = ""         # weekly: MON..SUN; monthly: day-of-month 1..31
        ai_cleanup: bool = False
        ai_model: str = ""    # non-encrypted LLM model config name
        ai_threshold: float = 6.0
        ai_sample: int = 5

    # ----- helpers --------------------------------------------------------- #
    def _session(sid: str) -> "Session":
        sess = _SESSIONS.get(sid)
        if sess is None:
            raise HTTPException(440, "Not connected. Click Connect.")
        sess.touch()
        return sess

    def _resolve_match(match: "Match"):
        """Return (addresses, domains, exact_domains, search_argument)."""
        if match.match_mode == "rule":
            if not match.rule_tree:
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
            "fields": list(FIELD_OPERATORS),
            "operators": FIELD_OPERATORS,
            "gmail_store_cap": core.GMAIL_STORE_CAP,
            "default_batch": core.UID_CHUNK_SIZE,
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
                body.timeout, body.encrypt, body.secret)
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

    @app.post("/api/notify-settings")
    def save_notify_settings(body: NotifySettingsIn) -> dict[str, Any]:
        notifications.set_settings(
            active=body.active, notify_to=body.notify_to,
            notify_jobs=body.notify_jobs, notify_runs=body.notify_runs)
        return notifications.get_settings()

    def _notify_run(account, folders, total, *, dry_run, gmail,
                    kind="Cleanup") -> None:
        """Send a run-completion email if 'notify on runs' is enabled (best effort)."""
        try:
            subj, body = notifications.cleanup_summary(
                account, folders, total, dry_run=dry_run, gmail=gmail, kind=kind)
            if notifications.send_notification(subj, body, when="run"):
                core.logger.info("Notification email sent to the configured "
                                 "recipient.")
        except notifications.NotifyError as exc:
            core.logger.warning("Notification email not sent: %s", exc)

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

    @app.post("/api/senders")
    def senders(body: SendersIn) -> dict[str, Any]:
        sess = _session(body.sid)
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is already running.")
        folders = body.folders or ["INBOX"]
        save_path = (body.save_path or "").strip() or None

        def work(rs: RunState) -> None:
            # list_senders logs each sender (count | address) into the session
            # log, which the client streams. We also keep a structured copy in
            # the run result (server-side only, not sent on every poll) so it can
            # be exported as CSV on demand via /api/senders.csv.
            ranked: list[dict[str, Any]] = []
            for folder in folders:
                counts = core.list_senders(
                    sess.conn, folder, body.batch_size,
                    should_stop=rs.stop.is_set, account=sess.user,
                    save_path=save_path)
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
                                                should_stop=rs.stop.is_set)
            else:
                for folder in folders:
                    total += core.process_folder(
                        sess.conn, folder, addresses=addresses, domains=domains,
                        exact_domains=exact_domains,
                        search_argument=search_argument, dry_run=body.dry_run,
                        expunge=body.expunge,
                        include_subdomains=body.include_subdomains,
                        batch_size=body.batch_size, scan_mode=body.scan_mode,
                        gmail_trash=body.gmail_trash, move=body.move,
                        dest_folder=dest, should_stop=rs.stop.is_set)
            verb = "would be processed" if body.dry_run else "processed"
            core.logger.info("Done. %d message(s) %s.", total, verb)
            rs.result = {"processed": total, "dry_run": body.dry_run}
            kind = "Empty folder" if body.empty_folder else "Cleanup"
            _notify_run(sess.user, folders, total, dry_run=body.dry_run,
                        gmail=body.gmail_trash, kind=kind)

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
            total = 0
            for folder in folders:
                total += core.process_folder(
                    sess.conn, folder, addresses=addresses, domains=domains,
                    exact_domains=exact_domains, search_argument=search_argument,
                    dry_run=True, count_only=True,
                    include_subdomains=body.include_subdomains,
                    batch_size=body.batch_size, scan_mode=body.scan_mode,
                    should_stop=rs.stop.is_set)
            core.logger.info("=> %d matching message(s) across %d folder(s).",
                             total, len(folders))
            rs.result = {"matched": total}

        run_state = _start_run(sess, "count", work)
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
        report = core.build_ai_report(
            sess.conn, folders, threshold=body.threshold,
            sample_size=body.sample_size, exclude=exclude,
            weights=body.weights, addresses=addresses, domains=domains,
            exact_domains=exact_domains, search_argument=search_argument,
            batch_size=body.batch_size, should_stop=rs.stop.is_set)
        if model_cfg:
            core.logger.info("Asking %s to evaluate %d flagged sender(s) ...",
                             model_cfg["model"], report["flagged_count"])
            # Log cost per batch so even a stopped/failed run records what the API
            # already billed (the final summary below covers a clean finish).
            recorder = None
            if model_cfg.get("track_costs"):
                recorder = lambda p, c, co: llm.log_cost(body.model, p, c, co)
            try:
                ev = ai.evaluate(report, model_cfg, should_stop=rs.stop.is_set,
                                 record_cost=recorder)
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
        d = scheduler.config_dir() / "ai_reports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_ai_report(report) -> None:
        """Persist the report as a timestamped CSV so it survives later runs."""
        from datetime import datetime
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base, n = f"ai_report_{stamp}", 1
        path = _ai_reports_dir() / f"{base}.csv"
        while path.exists():                      # avoid same-second collisions
            n += 1
            path = _ai_reports_dir() / f"{base}_{n}.csv"
        try:
            # newline="" so the csv's own \r\n is not re-translated to \r\r\n
            # on Windows (which shows a blank row between every record).
            path.write_text(core.ai_report_csv(report), encoding="utf-8",
                            newline="")
            core.logger.info("Report saved to disk as %s (pick it from the "
                             "download list).", path.name)
        except OSError as exc:
            core.logger.warning("Could not save the report to disk: %s", exc)

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
            _save_ai_report(report)

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
            _save_ai_report(report)
            if not confirmed:
                core.logger.info("=> AI confirmed nothing to delete.")
                return
            core.logger.info("AI confirmed %d sender(s) to delete.", len(confirmed))
            total = 0
            for folder in folders:
                total += core.process_folder(
                    sess.conn, folder, addresses=confirmed, dry_run=body.dry_run,
                    expunge=body.expunge, gmail_trash=gmail,
                    batch_size=body.batch_size, scan_mode="search",
                    should_stop=rs.stop.is_set)
            verb = "would be deleted" if body.dry_run else "deleted"
            core.logger.info("=> AI Cleanup: %d message(s) %s.", total, verb)
            _notify_run(sess.user, folders, total, dry_run=body.dry_run,
                        gmail=gmail, kind="AI Cleanup")

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

    @app.get("/api/ai-reports")
    def ai_reports_list() -> dict[str, Any]:
        """List the timestamped report CSVs saved on disk (newest first)."""
        files = sorted((p.name for p in _ai_reports_dir().glob("ai_report_*.csv")),
                       reverse=True)
        return {"reports": files}

    @app.get("/api/ai-reports/{name}")
    def ai_reports_get(name: str) -> Response:
        """Download a previously saved report CSV by file name."""
        if ("/" in name or "\\" in name or not name.startswith("ai_report_")
                or not name.endswith(".csv")):
            raise HTTPException(400, "Invalid report name.")
        path = _ai_reports_dir() / name
        if not path.is_file():
            raise HTTPException(404, "No such saved report.")
        return Response(
            content=path.read_text(encoding="utf-8"), media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={name}"})

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
        for folder in (body.folders or ["INBOX"]):
            args += ["--folder", folder]
        if body.ai_cleanup:
            mname = (body.ai_model or "").strip()
            minfo = next((m for m in llm.list_models() if m["name"] == mname),
                         None)
            if minfo is None:
                raise HTTPException(400, "Choose a saved LLM model for the AI job.")
            if minfo["encrypted"]:
                raise HTTPException(400, "Encrypted model configs can't run "
                                         "unattended - use a non-encrypted one.")
            args += ["--ai-cleanup", "--ai-model", mname,
                     "--ai-threshold", str(body.ai_threshold),
                     "--ai-sample", str(int(body.ai_sample)), "--yes"]
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
                          "installed": j.name in installed}
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
        print('The web UI needs extra packages. Install them with:\n'
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

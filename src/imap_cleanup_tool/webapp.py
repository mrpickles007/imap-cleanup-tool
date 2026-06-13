"""Local web UI for imap-cleanup-tool (optional ``[web]`` extra).

A small FastAPI app that serves a single-page interface and a JSON API.

Unlike a plain request/response wrapper, the server keeps a **persistent
session** per connected client: the IMAP connection is opened once and reused,
so a page refresh does not drop it. Long operations (cleanup, listing senders)
run in a **background thread** that can be **stopped**, and the page polls for
new log lines and status — the UI never freezes.

Run it with the installed command::

    imap-cleanup-tool-web              # opens the browser on http://127.0.0.1:8765

Install the dependencies with::

    pip install "imap-cleanup-tool[web]"
"""

# NOTE: deliberately no ``from __future__ import annotations`` — FastAPI must
# resolve the Pydantic request models (defined locally in create_app) from real
# annotation objects, not strings.

import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from . import __version__, core, scheduler
from .rules import RuleError, compile_search, node_from_dict
from .targets import parse_targets_text

STATIC_DIR = Path(__file__).parent / "web" / "static"
ASSETS_DIR = Path(__file__).parent / "assets"

PROVIDER_PRESETS = {
    "Custom": "",
    "Gmail": "imap.gmail.com",
    "iCloud": "imap.mail.me.com",
    "Outlook / Office365": "outlook.office365.com",
    "Aruba": "imaps.aruba.it",
    "Libero": "imapmail.libero.it",
}

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


class RunState:
    """In-memory state of one background operation (cleanup or sender listing)."""

    def __init__(self, run_id: str, kind: str) -> None:
        self.run_id = run_id
        self.kind = kind                 # "run" | "senders"
        self.status = "running"          # running | done | stopped | error
        self.log: list[str] = []
        self.stop = threading.Event()
        self.result: dict[str, Any] = {}
        self.error: str | None = None


class Session:
    """A live, reusable IMAP connection plus the last folder listing."""

    def __init__(self, sid: str, conn, host: str, port: int, user: str) -> None:
        self.sid = sid
        self.conn = conn
        self.host = host
        self.port = port
        self.user = user
        self.folders: list[str] = []
        self.lock = threading.Lock()     # serialises IMAP use
        self.run: RunState | None = None


def _install_log_dispatch() -> None:
    """Attach (once) a handler that routes core log records to the running job
    of the current thread, so each background run captures only its own log."""
    if getattr(_install_log_dispatch, "_done", False):
        return

    class _Dispatch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            run = _RUN_BY_THREAD.get(threading.get_ident())
            if run is not None:
                run.log.append(self.format(record))

    handler = _Dispatch()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
    core.logger.handlers.clear()
    core.logger.addHandler(handler)
    core.logger.setLevel(logging.INFO)
    _install_log_dispatch._done = True   # type: ignore[attr-defined]


def _start_run(session: "Session", kind: str, work) -> "RunState":
    """Spawn ``work(rs)`` in a background thread bound to a new RunState."""
    run = RunState(uuid.uuid4().hex[:12], kind)
    session.run = run

    def worker() -> None:
        _RUN_BY_THREAD[threading.get_ident()] = run
        try:
            with session.lock:
                work(run)
            run.status = "stopped" if run.stop.is_set() else "done"
        except core.StopRequested:
            run.status = "stopped"
            run.log.append("⏹  Operation stopped by the user.")
        except (OSError, core.imaplib.IMAP4.error) as exc:
            run.status = "error"
            run.error = str(exc)
            run.log.append(f"[NETWORK ERROR] {exc}")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            run.status = "error"
            run.error = str(exc)
            run.log.append(f"[ERROR] {exc}")
        finally:
            _RUN_BY_THREAD.pop(threading.get_ident(), None)

    threading.Thread(target=worker, daemon=True).start()
    return run


def create_app():
    """Build and return the FastAPI application (lazy fastapi import)."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field

    _install_log_dispatch()

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
        dry_run: bool = True

    class RunIn(Match, Options):
        sid: str

    class SendersIn(BaseModel):
        sid: str
        folders: list[str] = Field(default_factory=lambda: ["INBOX"])
        batch_size: int = core.UID_CHUNK_SIZE
        save_path: str | None = None

    class RuleIn(BaseModel):
        tree: dict

    class JobIn(Match, Options):
        name: str = "job"
        host: str = ""
        port: int = 993
        user: str = ""
        kind: str = "daily"                  # "daily" | "interval"
        time: str = "03:00"
        minutes: int = 60

    class SchedulerIn(BaseModel):
        enabled: bool

    # ----- helpers --------------------------------------------------------- #
    def _session(sid: str) -> "Session":
        sess = _SESSIONS.get(sid)
        if sess is None:
            raise HTTPException(440, "Not connected. Click Connect.")
        return sess

    def _resolve_match(match: "Match"):
        """Return (addresses, domains, search_argument) or raise HTTPException."""
        if match.match_mode == "rule":
            if not match.rule_tree:
                raise HTTPException(400, "No rule provided.")
            try:
                return set(), set(), compile_search(node_from_dict(match.rule_tree))
            except (RuleError, KeyError, TypeError) as exc:
                raise HTTPException(400, f"Invalid rule: {exc}") from exc
        try:
            addresses, domains = parse_targets_text(match.targets_text)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return addresses, domains, None

    # ----- app ------------------------------------------------------------- #
    app = FastAPI(title="imap-cleanup-tool", docs_url="/api/docs")
    internal = scheduler.InternalScheduler(
        lambda job: __import__("imap_cleanup_tool.cli", fromlist=["main"]).main(job.args))

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {
            "version": __version__,
            "providers": PROVIDER_PRESETS,
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
            sess.folders = core.list_folders(conn)
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
        running = bool(sess.run and sess.run.status == "running")
        return {"connected": True, "host": sess.host, "user": sess.user,
                "folders": sess.folders,
                "run_id": sess.run.run_id if running else None}

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

    @app.post("/api/senders")
    def senders(body: SendersIn) -> dict[str, Any]:
        sess = _session(body.sid)
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is already running.")
        folders = body.folders or ["INBOX"]
        save_path = (body.save_path or "").strip() or None

        def work(rs: RunState) -> None:
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
        search_argument = None
        if not body.empty_folder:
            addresses, domains, search_argument = _resolve_match(body)
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
                        search_argument=search_argument, dry_run=body.dry_run,
                        expunge=body.expunge,
                        include_subdomains=body.include_subdomains,
                        batch_size=body.batch_size, scan_mode=body.scan_mode,
                        gmail_trash=body.gmail_trash, should_stop=rs.stop.is_set)
            verb = "would be processed" if body.dry_run else "processed"
            core.logger.info("Done. %d message(s) %s.", total, verb)
            rs.result = {"processed": total, "dry_run": body.dry_run}

        run_state = _start_run(sess, "run", work)
        return {"run_id": run_state.run_id}

    @app.get("/api/progress/{sid}/{run_id}")
    def progress(sid: str, run_id: str, cursor: int = 0) -> dict[str, Any]:
        sess = _session(sid)
        rs = sess.run
        if rs is None or rs.run_id != run_id:
            raise HTTPException(404, "Unknown run.")
        return {"status": rs.status, "kind": rs.kind, "error": rs.error,
                "log": rs.log[cursor:], "cursor": len(rs.log),
                "result": rs.result if rs.status != "running" else {}}

    @app.post("/api/stop/{sid}/{run_id}")
    def stop(sid: str, run_id: str) -> dict[str, Any]:
        sess = _session(sid)
        if sess.run and sess.run.run_id == run_id:
            sess.run.stop.set()
        return {"ok": True}

    # ----- scheduling ------------------------------------------------------ #
    def _job_from(body: JobIn) -> scheduler.Job:
        args: list[str] = []
        if body.host:
            args += ["--host", body.host, "--port", str(body.port)]
        if body.user:
            args += ["--user", body.user]
        for folder in (body.folders or ["INBOX"]):
            args += ["--folder", folder]
        if body.empty_folder:
            args.append("--empty-folder")
        elif body.match_mode == "rule":
            if not body.rule_tree:
                raise HTTPException(400, "No rule provided.")
            try:
                args += ["--rule", node_from_dict(body.rule_tree).to_expression()]
            except (RuleError, KeyError, TypeError) as exc:
                raise HTTPException(400, f"Invalid rule: {exc}") from exc
        else:
            # Persist the pasted target list to a file so the scheduled CLI can read it.
            try:
                parse_targets_text(body.targets_text)  # validate
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            tpath = scheduler.config_dir() / f"{body.name}.targets.txt"
            tpath.write_text(body.targets_text, encoding="utf-8")
            args += ["--targets", str(tpath)]
        if body.gmail_trash:
            args.append("--gmail-trash")
        if body.expunge:
            args.append("--expunge")
        args += ["--scan-mode", body.scan_mode, "--yes"]
        sched = ({"kind": "daily", "time": body.time} if body.kind == "daily"
                 else {"kind": "interval", "minutes": body.minutes})
        return scheduler.Job(name=body.name or "job", args=args, schedule=sched)

    @app.get("/api/jobs")
    def list_jobs() -> dict[str, Any]:
        return {"jobs": [{"name": j.name, "schedule": j.schedule,
                          "args": j.args, "last_run": j.last_run}
                         for j in scheduler.load_jobs()],
                "scheduler_running": internal.is_running()
                if hasattr(internal, "is_running") else False}

    @app.post("/api/jobs")
    def save_job(body: JobIn) -> dict[str, Any]:
        job = _job_from(body)
        scheduler.upsert_job(job)
        return {"saved": job.name, "command": scheduler.export_system(job)}

    @app.delete("/api/jobs/{name}")
    def delete_job(name: str) -> dict[str, Any]:
        scheduler.delete_job(name)
        return {"deleted": name}

    @app.post("/api/jobs/export")
    def export_job(body: JobIn) -> dict[str, Any]:
        return {"command": scheduler.export_system(_job_from(body))}

    @app.post("/api/scheduler")
    def set_scheduler(body: SchedulerIn) -> dict[str, Any]:
        if body.enabled:
            internal.start()
        else:
            internal.stop()
        return {"enabled": body.enabled}

    # ----- static ---------------------------------------------------------- #
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/logo.png")
    def logo() -> FileResponse:
        return FileResponse(ASSETS_DIR / "logo.png")

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

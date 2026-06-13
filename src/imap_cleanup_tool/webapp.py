"""Local web UI for imap-cleanup-tool (optional ``[web]`` extra).

A small FastAPI app that serves a single-page interface and a JSON API. It is
**stateless**: every request carries the IMAP connection parameters; the server
opens a fresh SSL connection, performs the operation while capturing the log
output, and closes. This keeps the server simple and safe to run on localhost.

Run it with the installed command::

    imap-cleanup-tool-web              # opens the browser on http://127.0.0.1:8765

or directly::

    python -m imap_cleanup_tool.webapp --port 8765 --no-browser

Install the dependencies with::

    pip install "imap-cleanup-tool[web]"
"""

# NOTE: deliberately no ``from __future__ import annotations`` here — FastAPI must
# resolve the Pydantic request models (defined locally in create_app) from real
# annotation objects, not strings.

import contextlib
import logging
from pathlib import Path
from typing import Any

from . import __version__, core
from .rules import RuleError, compile_search, node_from_dict
from .targets import parse_targets_text

STATIC_DIR = Path(__file__).parent / "web" / "static"
ASSETS_DIR = Path(__file__).parent / "assets"

# IMAP host -> friendly provider name, and the reverse presets for the UI.
PROVIDER_PRESETS = {
    "Custom": "",
    "Gmail": "imap.gmail.com",
    "iCloud": "imap.mail.me.com",
    "Outlook / Office365": "outlook.office365.com",
    "Aruba": "imaps.aruba.it",
    "Libero": "imapmail.libero.it",
}

# Per-field operators for the visual query builder: (label, rules.py key).
FIELD_OPERATORS = {
    "sender": [["contains", "contains"], ["is exactly", "is"]],
    "subject": [["contains", "contains"], ["is exactly", "is"]],
    "date": [["on", "is"], ["on/after", "starts"], ["before", "ends"]],
}


@contextlib.contextmanager
def _capture_logs():
    """Collect ``core.logger`` records emitted inside the block as text lines."""
    lines: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = _Collector()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                            datefmt="%H:%M:%S"))
    previous_level = core.logger.level
    core.logger.addHandler(handler)
    core.logger.setLevel(logging.INFO)
    try:
        yield lines
    finally:
        core.logger.removeHandler(handler)
        core.logger.setLevel(previous_level)


def create_app():
    """Build and return the FastAPI application (lazy fastapi import)."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field

    class ConnIn(BaseModel):
        host: str = Field(..., min_length=1)
        port: int = 993
        user: str = Field(..., min_length=1)
        password: str = ""
        timeout: int = 120

    class FoldersIn(BaseModel):
        conn: ConnIn

    class SendersIn(BaseModel):
        conn: ConnIn
        folders: list[str] = Field(default_factory=lambda: ["INBOX"])
        batch_size: int = core.UID_CHUNK_SIZE

    class RuleIn(BaseModel):
        tree: dict

    class RunIn(BaseModel):
        conn: ConnIn
        folders: list[str] = Field(default_factory=lambda: ["INBOX"])
        match_mode: str = "targets"          # "targets" | "rule"
        targets_text: str = ""
        rule_tree: dict | None = None
        scan_mode: str = "search"            # "search" | "full"
        include_subdomains: bool = False
        batch_size: int = core.UID_CHUNK_SIZE
        gmail_trash: bool = False
        expunge: bool = False
        empty_folder: bool = False
        dry_run: bool = True

    def _open(conn: ConnIn):
        try:
            return core.connect(conn.host, conn.port, conn.user,
                                 conn.password, conn.timeout)
        except (OSError, core.imaplib.IMAP4.error) as exc:
            raise HTTPException(502, f"Connection/login failed: {exc}") from exc

    app = FastAPI(title="imap-cleanup-tool", docs_url="/api/docs")

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {
            "version": __version__,
            "providers": PROVIDER_PRESETS,
            "fields": list(FIELD_OPERATORS),
            "operators": FIELD_OPERATORS,
            "defaults": {"port": 993, "timeout": 120,
                         "batch_size": core.UID_CHUNK_SIZE},
        }

    @app.post("/api/folders")
    def folders(body: FoldersIn) -> dict[str, Any]:
        conn = _open(body.conn)
        try:
            return {"folders": core.list_folders(conn)}
        finally:
            core.safe_logout(conn)

    @app.post("/api/senders")
    def senders(body: SendersIn) -> dict[str, Any]:
        conn = _open(body.conn)
        try:
            with _capture_logs() as log:
                ranked: list[dict[str, Any]] = []
                for folder in body.folders:
                    counts = core.list_senders(conn, folder, body.batch_size,
                                               account=body.conn.user)
                    for sender, count in sorted(counts.items(),
                                                key=lambda kv: kv[1], reverse=True):
                        ranked.append({"folder": folder, "sender": sender,
                                       "count": count})
            return {"senders": ranked, "log": log}
        finally:
            core.safe_logout(conn)

    @app.post("/api/validate-rule")
    def validate_rule(body: RuleIn) -> dict[str, Any]:
        try:
            return {"search": compile_search(node_from_dict(body.tree))}
        except (RuleError, KeyError, TypeError) as exc:
            raise HTTPException(400, f"Invalid rule: {exc}") from exc

    @app.post("/api/run")
    def run(body: RunIn) -> dict[str, Any]:
        # Resolve the matching source up front so bad input fails fast (400).
        addresses: set[str] = set()
        domains: set[str] = set()
        search_argument: str | None = None
        if not body.empty_folder:
            if body.match_mode == "rule":
                if not body.rule_tree:
                    raise HTTPException(400, "No rule provided.")
                try:
                    search_argument = compile_search(node_from_dict(body.rule_tree))
                except (RuleError, KeyError, TypeError) as exc:
                    raise HTTPException(400, f"Invalid rule: {exc}") from exc
            else:
                try:
                    addresses, domains = parse_targets_text(body.targets_text)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc

        conn = _open(body.conn)
        try:
            with _capture_logs() as log:
                total = 0
                if body.empty_folder:
                    for folder in body.folders:
                        total += core.empty_folder(conn, folder, body.dry_run)
                else:
                    for folder in body.folders:
                        total += core.process_folder(
                            conn, folder, addresses=addresses, domains=domains,
                            search_argument=search_argument, dry_run=body.dry_run,
                            expunge=body.expunge,
                            include_subdomains=body.include_subdomains,
                            batch_size=body.batch_size, scan_mode=body.scan_mode,
                            gmail_trash=body.gmail_trash)
            return {"processed": total, "dry_run": body.dry_run, "log": log}
        except core.imaplib.IMAP4.error as exc:
            raise HTTPException(502, f"IMAP error: {exc}") from exc
        finally:
            core.safe_logout(conn)

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
        import threading
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"imap-cleanup-tool web UI running on {url}  (press Ctrl+C to stop)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

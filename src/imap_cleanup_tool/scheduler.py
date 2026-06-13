"""Scheduling: persist named jobs, run them internally, or export to the OS.

A *job* is a saved scan/clean operation plus a schedule. Jobs are stored as
JSON under a config directory so both the CLI and GUI can see them.

Two execution paths:

* Internal  - a lightweight background thread (APScheduler-free) that wakes up
              every minute and runs jobs whose time has come. Works only while
              the app is running.
* System    - export a job to the OS scheduler: a ``schtasks`` command on
              Windows or a crontab line on Linux/macOS, invoking the package
              CLI. Runs even when the app is closed.

This module does not perform IMAP work itself; it shells out to the installed
``imap-cleanup-tool`` CLI so that system tasks are self-contained.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("imap_cleanup_tool")


def config_dir() -> Path:
    """Return (and create) the per-user config directory for jobs."""
    if sys.platform.startswith("win"):
        base = Path(os.getenv("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = base / "imap-cleanup-tool"
    path.mkdir(parents=True, exist_ok=True)
    return path


def jobs_file() -> Path:
    """Path to the JSON file holding all saved jobs."""
    return config_dir() / "jobs.json"


@dataclass
class Job:
    """A saved, schedulable operation.

    ``args`` is the list of CLI arguments to run (everything after the program
    name), e.g. ``["--host", "imap.gmail.com", "--targets", "t.txt",
    "--expunge"]``. ``schedule`` is a simple spec: ``{"kind": "daily",
    "time": "03:00"}`` or ``{"kind": "interval", "minutes": 60}``.
    """

    name: str
    args: list[str] = field(default_factory=list)
    schedule: dict = field(default_factory=dict)
    enabled: bool = True
    last_run: str | None = None

    def to_dict(self) -> dict:
        """Return the job as a plain dict."""
        return asdict(self)


def load_jobs() -> list[Job]:
    """Load all saved jobs (empty list if none)."""
    path = jobs_file()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read jobs file: %s", exc)
        return []
    return [Job(**item) for item in data]


def save_jobs(jobs: list[Job]) -> None:
    """Persist the given jobs to disk."""
    jobs_file().write_text(
        json.dumps([j.to_dict() for j in jobs], indent=2, ensure_ascii=False),
        encoding="utf-8")


def upsert_job(job: Job) -> None:
    """Add a job or replace an existing one with the same name."""
    jobs = [j for j in load_jobs() if j.name != job.name]
    jobs.append(job)
    save_jobs(jobs)


def delete_job(name: str) -> None:
    """Remove a job by name."""
    save_jobs([j for j in load_jobs() if j.name != name])


# --------------------------------------------------------------------------- #
# Schedule evaluation
# --------------------------------------------------------------------------- #
def _due(job: Job, now: datetime, last: datetime | None) -> bool:
    """Return True if the job should run at ``now``."""
    kind = job.schedule.get("kind")
    if kind == "interval":
        minutes = int(job.schedule.get("minutes", 60))
        if last is None:
            return True
        return (now - last).total_seconds() >= minutes * 60
    if kind == "daily":
        target = job.schedule.get("time", "03:00")
        hh, mm = (int(x) for x in target.split(":"))
        if now.hour != hh or now.minute != mm:
            return False
        return last is None or last.date() != now.date()
    return False


# --------------------------------------------------------------------------- #
# Internal runner
# --------------------------------------------------------------------------- #
class InternalScheduler:
    """A minute-resolution background scheduler thread."""

    def __init__(self, runner) -> None:
        """``runner`` is a callable taking a Job and executing it."""
        self._runner = runner
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Start the background scheduler thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Internal scheduler started.")

    def stop(self) -> None:
        """Signal the scheduler thread to stop."""
        self._stop.set()
        logger.info("Internal scheduler stopped.")

    def is_running(self) -> bool:
        """True if the background scheduler thread is alive."""
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.wait(timeout=20):
            now = datetime.now()
            for job in load_jobs():
                if not job.enabled:
                    continue
                last = (datetime.fromisoformat(job.last_run)
                        if job.last_run else None)
                if _due(job, now, last):
                    logger.info("Running scheduled job %r ...", job.name)
                    try:
                        self._runner(job)
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        logger.error("Job %r failed: %s", job.name, exc)
                    job.last_run = now.isoformat(timespec="seconds")
                    upsert_job(job)


# --------------------------------------------------------------------------- #
# System export
# --------------------------------------------------------------------------- #
def cli_invocation(args: list[str]) -> str:
    """Build the shell command that runs the CLI with the given args.

    Uses the current interpreter and ``-m imap_cleanup_tool.cli`` so it works inside
    a virtualenv without depending on PATH.
    """
    python = sys.executable
    parts = [python, "-m", "imap_cleanup_tool.cli", *args]
    return " ".join(shlex.quote(p) for p in parts)


def export_windows(job: Job) -> str:
    """Return a ``schtasks`` command that registers this job on Windows."""
    command = cli_invocation(job.args)
    when = job.schedule
    if when.get("kind") == "daily":
        sched = f'/SC DAILY /ST {when.get("time", "03:00")}'
    else:
        minutes = int(when.get("minutes", 60))
        sched = f"/SC MINUTE /MO {minutes}"
    task_name = f"ImapCleanupTool_{job.name}"
    return (f'schtasks /Create /TN "{task_name}" '
            f'/TR "{command}" {sched} /F')


def export_cron(job: Job) -> str:
    """Return a crontab line that runs this job on Linux/macOS."""
    command = cli_invocation(job.args)
    when = job.schedule
    if when.get("kind") == "daily":
        hh, mm = (int(x) for x in when.get("time", "03:00").split(":"))
        spec = f"{mm} {hh} * * *"
    else:
        minutes = int(when.get("minutes", 60))
        spec = f"*/{minutes} * * * *"
    return f"{spec} {command}  # imap-cleanup-tool job: {job.name}"


def export_system(job: Job) -> str:
    """Return the OS-appropriate scheduling command for the job."""
    if sys.platform.startswith("win"):
        return export_windows(job)
    return export_cron(job)

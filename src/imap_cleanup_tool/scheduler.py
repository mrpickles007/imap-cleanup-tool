"""Scheduling: persist named jobs and install them into the OS scheduler.

A *job* is a saved scan/clean operation plus a schedule. Jobs are stored as
JSON under a config directory so both the CLI and GUI can see them.

Jobs run via the **operating system scheduler** only (there is no internal
background scheduler): a job is exported/installed as a ``schtasks`` task on
Windows, a crontab line on Linux/macOS (recurring), or an ``at`` job on
Linux/macOS (one-shot), each invoking the package CLI. They run even when the
app is closed.

This module does not perform IMAP work itself; it shells out to the installed
``imap-cleanup-tool`` CLI so that system tasks are self-contained.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
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
    at_id: int | None = None   # POSIX `at` job number, set when a one-shot is installed

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
# Job run logs
# --------------------------------------------------------------------------- #
def logs_dir() -> Path:
    """Directory holding per-job run logs (created on demand)."""
    path = config_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_log_path(name: str) -> Path:
    """Path to the rolling log file for a job."""
    return logs_dir() / f"{name}.log"


def ai_reports_dir() -> Path:
    """Directory holding saved AI Cleanup report CSVs (shared by web + CLI)."""
    path = config_dir() / "ai_reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_ai_report(csv_text: str, stamp: str | None = None) -> Path:
    """Write an AI report CSV to a timestamped file; returns the path.

    ``newline=""`` avoids the CSV's \\r\\n being re-translated to \\r\\r\\n on
    Windows (which shows a blank row between records).
    """
    from datetime import datetime
    stamp = stamp or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base, n = f"ai_report_{stamp}", 1
    path = ai_reports_dir() / f"{base}.csv"
    while path.exists():
        n += 1
        path = ai_reports_dir() / f"{base}_{n}.csv"
    path.write_text(csv_text, encoding="utf-8", newline="")
    return path


def read_job_log(name: str, max_bytes: int = 200_000) -> str:
    """Return the tail of a job's log file (empty string if it never ran)."""
    path = job_log_path(name)
    if not path.exists():
        return ""
    data = path.read_text(encoding="utf-8", errors="replace")
    if len(data) > max_bytes:
        data = "...(truncated)...\n" + data[-max_bytes:]
    return data


# --------------------------------------------------------------------------- #
# Schedule construction
# --------------------------------------------------------------------------- #
# Weekday codes shared by the UI and schtasks (/D MON..SUN); cron uses numbers
# (0 = Sunday).
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_CRON_DOW = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3,
             "THU": 4, "FRI": 5, "SAT": 6}


def _check_time(value: str) -> str:
    """Validate/normalise an ``HH:MM`` string."""
    try:
        hh, mm = (int(x) for x in str(value).split(":"))
    except ValueError as exc:
        raise ValueError(f"Invalid time {value!r} (use HH:MM).") from exc
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"Time out of range: {value!r}.")
    return f"{hh:02d}:{mm:02d}"


def build_schedule(kind: str, *, time: str = "03:00", date: str = "",
                   minutes: int = 60, day: str = "") -> dict:
    """Validate the UI's scheduling inputs and return a normalised dict.

    Kinds: ``once`` (date+time), ``interval`` (every N minutes), ``hourly``
    (every hour at minute MM of ``time``), ``daily`` (HH:MM), ``weekly``
    (weekday + HH:MM), ``monthly`` (day-of-month 1-28 + HH:MM).
    """
    if kind == "once":
        if not date:
            raise ValueError("Choose a date for a one-time job.")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"Invalid date {date!r} (use YYYY-MM-DD).") from exc
        return {"kind": "once", "date": date, "time": _check_time(time)}
    if kind == "interval":
        num = int(minutes)
        if num < 1:
            raise ValueError("Interval minutes must be at least 1.")
        return {"kind": "interval", "minutes": num}
    if kind == "hourly":
        minute = int(_check_time(time).split(":")[1])
        return {"kind": "hourly", "minute": minute}
    if kind == "daily":
        return {"kind": "daily", "time": _check_time(time)}
    if kind == "weekly":
        code = str(day).upper()[:3]
        if code not in WEEKDAYS:
            raise ValueError("Choose a weekday for a weekly job.")
        return {"kind": "weekly", "day": code, "time": _check_time(time)}
    if kind == "monthly":
        try:
            dom = int(day)
        except (ValueError, TypeError) as exc:
            raise ValueError("Choose a day of month (1-28) for a monthly job.") \
                from exc
        if not 1 <= dom <= 28:
            # Capped at 28 so the job fires every month (29-31 skip short ones).
            raise ValueError("Day of month must be between 1 and 28.")
        return {"kind": "monthly", "day": dom, "time": _check_time(time)}
    raise ValueError(f"Unknown schedule kind: {kind!r}.")


def describe_schedule(when: dict) -> str:
    """Human-readable one-line summary of a schedule dict."""
    kind = when.get("kind")
    if kind == "once":
        return f"Once on {when.get('date', '?')} at {when.get('time', '?')}"
    if kind == "interval":
        return f"Every {int(when.get('minutes', 60))} minute(s)"
    if kind == "hourly":
        return f"Hourly at minute {int(when.get('minute', 0)):02d}"
    if kind == "daily":
        return f"Daily at {when.get('time', '03:00')}"
    if kind == "weekly":
        return f"Weekly on {when.get('day', 'MON')} at {when.get('time', '03:00')}"
    if kind == "monthly":
        return (f"Monthly on day {int(when.get('day', 1))} "
                f"at {when.get('time', '03:00')}")
    return "Unknown schedule"


# --------------------------------------------------------------------------- #
# System export
# --------------------------------------------------------------------------- #
# The scheduled task runs the job *by name* (``--run-job NAME``) so the command
# line stays free of the spaces and quotes a rule expression would contain. This
# requires the job to be saved (see upsert_job) before it is scheduled.
def _task_name(job: Job) -> str:
    return f"ImapCleanupTool_{job.name}"


def _runjob_posix(name: str) -> str:
    return " ".join([shlex.quote(sys.executable), "-m",
                     "imap_cleanup_tool.cli", "--run-job", shlex.quote(name)])


def _runjob_windows(name: str) -> str:
    # The interpreter path is quoted; the job name is sanitised by the caller.
    return f'"{sys.executable}" -m imap_cleanup_tool.cli --run-job {name}'


def _win_date(iso: str) -> str:
    """Format an ISO date (YYYY-MM-DD) for ``schtasks /SD`` in system locale."""
    parsed = datetime.strptime(iso, "%Y-%m-%d")
    pattern = "MM/dd/yyyy"
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Culture).DateTimeFormat.ShortDatePattern"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            pattern = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    tokens = {"dddd": "%A", "ddd": "%a", "dd": "%d", "d": "%d",
              "MMMM": "%B", "MMM": "%b", "MM": "%m", "M": "%m",
              "yyyy": "%Y", "yy": "%y", "y": "%Y"}
    strf = re.sub(r"dddd|ddd|dd|d|MMMM|MMM|MM|M|yyyy|yy|y",
                  lambda m: tokens[m.group(0)], pattern)
    return parsed.strftime(strf)


def _windows_schedule(job: Job) -> list[str]:
    when = job.schedule
    kind = when.get("kind")
    time = str(when.get("time", "03:00"))
    if kind == "once":
        return ["/SC", "ONCE", "/SD", _win_date(when["date"]), "/ST", time]
    if kind == "interval":
        return ["/SC", "MINUTE", "/MO", str(int(when.get("minutes", 60)))]
    if kind == "hourly":
        return ["/SC", "HOURLY", "/ST", f"00:{int(when.get('minute', 0)):02d}"]
    if kind == "weekly":
        return ["/SC", "WEEKLY", "/D", str(when.get("day", "MON")), "/ST", time]
    if kind == "monthly":
        return ["/SC", "MONTHLY", "/D", str(int(when.get("day", 1))), "/ST", time]
    return ["/SC", "DAILY", "/ST", time]


def export_windows(job: Job) -> str:
    """Return a ``schtasks`` command that registers this job on Windows."""
    sched = " ".join(_windows_schedule(job))
    return (f'schtasks /Create /TN "{_task_name(job)}" '
            f'/TR "{_runjob_windows(job.name)}" {sched} /F')


def _cron_spec(when: dict) -> str:
    """Return the five-field cron timing spec for a recurring schedule."""
    kind = when.get("kind")
    if kind == "interval":
        return f"*/{int(when.get('minutes', 60))} * * * *"
    if kind == "hourly":
        return f"{int(when.get('minute', 0))} * * * *"
    hh, mm = (int(x) for x in str(when.get("time", "03:00")).split(":"))
    if kind == "weekly":
        return f"{mm} {hh} * * {_CRON_DOW.get(when.get('day', 'MON'), 1)}"
    if kind == "monthly":
        return f"{mm} {hh} {int(when.get('day', 1))} * *"
    return f"{mm} {hh} * * *"  # daily


def export_cron(job: Job) -> str:
    """Return the crontab line (recurring) or ``at`` command (one-shot)."""
    when = job.schedule
    if when.get("kind") == "once":
        hh, mm = (int(x) for x in str(when.get("time", "03:00")).split(":"))
        piped = f"echo {shlex.quote(_runjob_posix(job.name))} | "
        return f"{piped}at {hh:02d}:{mm:02d} {when.get('date', '')}".strip()
    spec = _cron_spec(when)
    return f"{spec} {_runjob_posix(job.name)}  # imap-cleanup-tool job: {job.name}"


def export_system(job: Job) -> str:
    """Return the OS-appropriate scheduling command for the job (for display)."""
    if sys.platform.startswith("win"):
        return export_windows(job)
    return export_cron(job)


def install_windows(job: Job) -> str:
    """Register the job with the Windows Task Scheduler. Returns a status line."""
    cmd = ["schtasks", "/Create", "/TN", _task_name(job),
           "/TR", _runjob_windows(job.name), *_windows_schedule(job), "/F"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                           or "schtasks failed")
    return f'Registered Windows task "{_task_name(job)}".'


def _install_at(job: Job) -> str:
    """Schedule a one-time job via the POSIX ``at`` command.

    Records the ``at`` job number on the job (so it can be listed via ``atq``
    and removed via ``atrm`` from the panel, like recurring cron jobs).
    """
    when = job.schedule
    hh, mm = (int(x) for x in str(when.get("time", "03:00")).split(":"))
    try:
        result = subprocess.run(
            ["at", f"{hh:02d}:{mm:02d}", when.get("date", "")],
            input=_runjob_posix(job.name) + "\n",
            text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError("The `at` command is required for one-time jobs but "
                           "was not found. Install 'at' or pick a recurring "
                           "schedule.") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "at failed")
    # `at` prints e.g. "job 42 at Wed Jul  1 09:05:00 2026" (usually on stderr).
    match = re.search(r"job\s+(\d+)\s+at",
                      (result.stderr or "") + "\n" + (result.stdout or ""))
    if match:
        job.at_id = int(match.group(1))
        upsert_job(job)
        return f"Scheduled one-time job '{job.name}' with `at` (job #{job.at_id})."
    return (f"Scheduled one-time job '{job.name}' with `at` (its id could not "
            f"be read - manage it with 'atq'/'atrm').")


def install_cron(job: Job) -> str:
    """Add (or replace) this job's line in the user's crontab. Returns a status."""
    if job.schedule.get("kind") == "once":
        return _install_at(job)
    marker = f"# imap-cleanup-tool job: {job.name}"
    try:
        current = subprocess.run(["crontab", "-l"], capture_output=True,
                                 text=True).stdout
    except FileNotFoundError as exc:
        raise RuntimeError("crontab command not found") from exc
    lines = [ln for ln in current.splitlines() if marker not in ln]
    lines.append(export_cron(job))
    result = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                            text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "crontab failed")
    return f"Installed cron job '{job.name}'."


def install_system(job: Job) -> str:
    """Install the job into the OS scheduler (Task Scheduler or cron)."""
    if sys.platform.startswith("win"):
        return install_windows(job)
    return install_cron(job)


def uninstall_windows(job: Job) -> str:
    """Remove this job's Windows Task Scheduler task. Tolerant if it's absent."""
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", _task_name(job), "/F"],
        capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").lower()
        if "cannot find" in err or "does not exist" in err:
            return f'No system task for "{job.name}" (already removed).'
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                           or "schtasks delete failed")
    return f'Removed Windows task "{_task_name(job)}".'


def _uninstall_at(job: Job) -> str:
    """Cancel a one-time ``at`` job via ``atrm`` and clear its recorded id."""
    if job.at_id is None:
        return (f"No `at` job recorded for '{job.name}' "
                f"(already ran, already removed, or never installed).")
    try:
        result = subprocess.run(["atrm", str(job.at_id)],
                                capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("The `atrm` command was not found.") from exc
    err = (result.stderr or "").lower()
    tolerated = "cannot find" in err or "no atjob" in err or "no such" in err
    if result.returncode != 0 and not tolerated:
        raise RuntimeError(result.stderr.strip() or "atrm failed")
    # Clear the stored id on the persisted job (the at-job is gone either way).
    saved = next((j for j in load_jobs() if j.name == job.name), None)
    if saved is not None:
        saved.at_id = None
        upsert_job(saved)
    return f"Removed one-time `at` job '{job.name}' (#{job.at_id})."


def uninstall_cron(job: Job) -> str:
    """Remove this job from cron (recurring) or ``at`` (one-shot)."""
    # The caller may pass a bare Job (no schedule); consult the saved copy to
    # tell a one-shot `at` job from a recurring cron line.
    saved = next((j for j in load_jobs() if j.name == job.name), None)
    if saved is not None and saved.schedule.get("kind") == "once":
        return _uninstall_at(saved)
    marker = f"# imap-cleanup-tool job: {job.name}"
    try:
        current = subprocess.run(["crontab", "-l"], capture_output=True,
                                 text=True).stdout
    except FileNotFoundError as exc:
        raise RuntimeError("crontab command not found") from exc
    kept = [ln for ln in current.splitlines() if marker not in ln]
    if len(kept) == len(current.splitlines()):
        return f"No cron entry for '{job.name}' (already removed)."
    result = subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n",
                            text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "crontab failed")
    return f"Removed cron job '{job.name}'."


def uninstall_system(job: Job) -> str:
    """Remove the job from the OS scheduler (Task Scheduler or cron)."""
    if sys.platform.startswith("win"):
        return uninstall_windows(job)
    return uninstall_cron(job)


def installed_job_names() -> set[str]:
    """Return the set of job names currently registered in the OS scheduler."""
    names: set[str] = set()
    if sys.platform.startswith("win"):
        result = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            return names
        for line in result.stdout.splitlines():
            match = re.match(r'"\\?ImapCleanupTool_(.+?)"', line)
            if match:
                names.add(match.group(1))
        return names
    try:
        current = subprocess.run(["crontab", "-l"], capture_output=True,
                                 text=True).stdout
        marker = "# imap-cleanup-tool job: "
        for line in current.splitlines():
            if marker in line:
                names.add(line.split(marker, 1)[1].strip())
    except FileNotFoundError:
        pass  # no cron - still check one-shot `at` jobs below
    # One-shot jobs scheduled via `at`: a recorded at_id still in the queue.
    queued = _queued_at_ids()
    if queued:
        for job in load_jobs():
            if job.schedule.get("kind") == "once" and job.at_id in queued:
                names.add(job.name)
    return names


def _queued_at_ids() -> set[int]:
    """Return the set of job numbers currently queued in ``at`` (via ``atq``)."""
    try:
        result = subprocess.run(["atq"], capture_output=True, text=True)
    except FileNotFoundError:
        return set()
    if result.returncode != 0:
        return set()
    ids: set[int] = set()
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)", line)
        if match:
            ids.add(int(match.group(1)))
    return ids

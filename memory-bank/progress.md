# Progress

_Last updated: 2026-06-13_

## What works

- **Core IMAP ops** (`core.py`): connect/login over SSL, list folders, fetch
  `From` headers in batches, list senders with counts + CSV export, search by
  targets or compiled rule, delete (flag / Gmail-label), expunge, empty folder.
  Cooperative cancellation via `should_stop`.
- **CLI** (`cli.py`): full argparse interface, credential resolution
  (flags → env → prompt), confirmation prompts, dry-run, list modes.
- **GUI** (`gui.py`): Tkinter dark-themed app — connection, folder picker,
  target/rule matching, options, threaded run with live log, Stop button,
  scheduler tab. Fully in English.
- **Rule engine** (`rules.py` + `rule_parser.py`): text expression → nested
  Condition/Group tree → IMAP `SEARCH` string. Verified end-to-end.
- **Scheduler** (`scheduler.py`): save/load/delete jobs, internal minute-loop
  scheduler, export to `schtasks`/`crontab`.
- **Packaging**: `pyproject.toml` (hatchling), two entry points, CI workflow for
  exe build + PyPI publish on `v*` tags.

## Verified this session

- Imports of all modules succeed under the `ict` venv (Python 3.13).
- Rule parsing: `sender contains amazon.com OR (subject is Invoice AND date
  starts 2025-01-01)` → `OR FROM "amazon.com" SUBJECT "Invoice" SINCE 01-Jan-2025`.

## What's left

- [ ] Push to GitHub (`mrpickles007/imap-cleanup-tool`).
- [ ] First PyPI release (`twine upload` or CI on tag) + `PYPI_API_TOKEN` secret.
- [ ] Automated tests (none yet) — add pytest + a few unit tests for
      `rules`/`rule_parser`/`targets`.
- [ ] Tag `v0.1.0` to trigger the release workflow once the repo is on GitHub.
- [ ] Optional: GUI app icon for the PyInstaller build.

## Known issues / limitations

- No test suite yet.
- IMAP has no exact-header match — `is` == `contains` server-side; strict
  matching only via `--scan-mode full`.
- Live IMAP behavior not yet validated end-to-end in this session (no account
  was used).
- `gh` CLI is not installed locally, so the GitHub repo must be created via the
  web UI or after installing `gh`.

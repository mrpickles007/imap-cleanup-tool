# Active Context

_Last updated: 2026-06-13_

## Current focus

Preparing the project for its first public open-source release on GitHub + PyPI.
The codebase was initially drafted (by a non–Claude-Code Claude) partly in
Italian; the immediate task was to get it release-ready in English with proper
project scaffolding.

## Recent changes (this session)

- **Full Italian → English translation:**
  - `gui.py` — all UI labels, tabs (`Cleanup`, `Scheduling`), buttons, status
    text, dialogs, and log messages.
  - `rules.py` and `rule_parser.py` — all `RuleError` messages and parser token
    labels (`field`/`operator`/`value`).
  - `README.md` — GUI tab references updated to the English names.
  - (`core.py`, `cli.py`, `targets.py`, `scheduler.py` were already English.)
- **Metadata:** `pyproject.toml` author set to Giulio Alberello + email; all
  `USERNAME` placeholders → `mrpickles007`; `LICENSE` copyright → Giulio
  Alberello.
- **Added** `CLAUDE.md` and this `memory-bank/`.
- **Git:** repository initialized in `imap-cleanup-tool/` with an initial commit.
- **Verified** imports + rule parsing run clean under the `ict` venv (Python 3.13).

## Next steps (not yet done)

- Create the GitHub repo `mrpickles007/imap-cleanup-tool` and push (commands
  given to the user; `gh` CLI is not installed on this machine).
- Register on PyPI, add the `PYPI_API_TOKEN` repo secret for CI publishing.
- Consider adding automated tests (currently none) — pytest in the dev extra.
- Optional: real end-to-end test against a live IMAP account (manual).

## Open questions / decisions pending

- Whether to add a test suite before or after the first tagged release.
- App icon for the PyInstaller GUI build (none yet).

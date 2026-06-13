<p align="center">
  <img src="assets/logo.png" alt="IMAP Cleanup Tool logo" width="360">
</p>

<h1 align="center">IMAP Cleaner</h1>

Delete or move IMAP emails in bulk — by **sender**, by **domain**, or by
**nested rules** (a query builder). Works from the **command line** and from a
**graphical interface** (Tkinter). No third-party runtime dependencies: it uses
only the Python standard library.

- Match by a target file (one sender/domain per line) **or** by a rule
  expression like `sender contains amazon.com OR (subject is Fattura AND date starts 2025-01-01)`.
- Fast **server-side search** for huge folders, or strict **local matching**.
- **Gmail mode**: moves matches to Trash (the only way to truly delete on Gmail).
- **Empty a whole folder** (e.g. Trash) without scanning.
- **List senders** with counts and export them to CSV (with timestamp).
- **Stop** button / cooperative cancellation for long runs.
- **Scheduler**: save jobs and run them internally, or export a Windows Task
  Scheduler / cron command to run them even when the app is closed.

> ⚠️ Deleting email is destructive. Always do a `--dry-run` first. Without
> `--expunge`, messages are only flagged deleted (often hidden by the client
> but recoverable until expunged).

---

## Table of contents

- [Install](#install)
- [Quick start](#quick-start)
- [Command-line usage](#command-line-usage)
- [Rule expressions](#rule-expressions)
- [Target file format](#target-file-format)
- [Graphical interface](#graphical-interface)
- [Scheduling](#scheduling)
- [Gmail notes](#gmail-notes)
- [Building a Windows .exe](#building-a-windows-exe)
- [Publishing to PyPI](#publishing-to-pypi)

---

## Install

### From PyPI (recommended)

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install imap-cleanup-tool
```

This installs two commands: `imap-cleanup-tool` (CLI) and `imap-cleanup-tool-gui` (GUI).

### From source

```bash
git clone https://github.com/mrpickles007/imap-cleanup-tool.git
cd imap-cleanup-tool

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -e ".[dev]"          # editable install + dev tools
```

### Running the tests

The test suite uses only the standard library (`unittest`) — nothing extra to
install:

```bash
python -m unittest discover -s tests -v
```

### Tkinter (GUI) on Linux

Tkinter ships with Python, but some Linux distros split it into a system
package:

```bash
sudo apt install python3-tk      # Debian/Ubuntu
```

Windows and macOS Python installers include Tkinter already.

---

## Quick start

```bash
# 1. See your folders (find the real Trash/Sent names)
imap-cleanup-tool --host imap.gmail.com --user you@gmail.com --list-folders

# 2. Preview what would be deleted (changes nothing)
imap-cleanup-tool --host imap.gmail.com --user you@gmail.com \
    --targets targets.txt --dry-run

# 3. Do it for real (Gmail: move to Trash)
imap-cleanup-tool --host imap.gmail.com --user you@gmail.com \
    --targets targets.txt --gmail-trash
```

Credentials are read from flags, then environment variables
(`IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_PORT`), then an interactive
prompt. Prefer the prompt or env vars over `--password` so the secret does not
land in your shell history.

---

## Command-line usage

| Option | Meaning |
| --- | --- |
| `--host`, `--port`, `--user`, `--password` | Connection (port default 993). |
| `--timeout N` | Socket timeout in seconds (default 120). |
| `--folder NAME` | Folder to scan; repeat for several. Default `INBOX`. |
| `--targets FILE` | Match by a target list file. |
| `--rule "EXPR"` | Match by a rule expression (see below). |
| `--scan-mode search\|full` | Server-side search (fast) or local match (strict). |
| `--include-subdomains` | In `full` mode, also match subdomains. |
| `--batch-size N` | Messages per IMAP request (default 500). |
| `--list-folders` | Print folders and exit. |
| `--list-senders` | Print unique senders with counts and exit. |
| `--save-senders CSV` | With `--list-senders`, append to a CSV. |
| `--empty-folder` | Delete ALL messages in the folder(s); no filtering. |
| `--gmail-trash` | Move matches to Gmail Trash via labels. |
| `--dry-run` | Report only; make no changes. |
| `--expunge` | Permanently remove after flagging. |
| `--yes` | Skip the confirmation prompt (for scripts/cron). |
| `--verbose`, `-v` | Debug logging with per-batch progress. |

Examples:

```bash
# Save a sender report (timestamp, account, folder, sender, count)
imap-cleanup-tool --host HOST --user USER --list-senders --save-senders senders.csv

# Empty the Trash, fast, no scan
imap-cleanup-tool --host HOST --user USER --folder Trash --empty-folder --dry-run
imap-cleanup-tool --host HOST --user USER --folder Trash --empty-folder

# Strict local matching including subdomains
imap-cleanup-tool --host HOST --user USER --targets targets.txt \
    --scan-mode full --include-subdomains --dry-run
```

---

## Rule expressions

Rules are an alternative to target files, evaluated server-side via IMAP
`SEARCH`. Fields and operators:

| Field | Operators | Maps to |
| --- | --- | --- |
| `sender` | `is`, `contains` | `FROM` |
| `subject` | `is`, `contains` | `SUBJECT` |
| `date` | `is`, `starts`, `ends` | `ON`, `SINCE`, `BEFORE` |

Combine conditions with `AND` / `OR`, and group with parentheses for nesting.
Dates accept `YYYY-MM-DD`. Quote values with spaces.

```bash
imap-cleanup-tool --host HOST --user USER --dry-run \
    --rule 'sender contains amazon.com OR (subject is "Black Friday" AND date starts 2025-11-01)'
```

> `is` and `contains` both map to IMAP substring matching on the header; IMAP
> has no exact-header match, so treat `contains` as the reliable operator and
> use the target-file `--scan-mode full` path when you need strict exactness.

---

## Target file format

One entry per line; `#` starts a comment.

```text
spam@example.com        # exact sender address
*@newsletter.com        # whole domain (wildcard form)
annoying.com            # whole domain (bare form)
mail.annoying.com       # a specific subdomain only
```

---

## Graphical interface

```bash
imap-cleanup-tool-gui
```

The window has two tabs:

**Cleanup** — connect once with the **Connect** button (the connection stays
open and is reused). Pick folders by moving them from *Available* to *Selected*
(each click **adds**; double-click or the **←** button **removes** one —
selections no longer overwrite each other). Choose a target file or a rule, set
options, and press **Run**. **Stop** cancels a running operation at the next
safe checkpoint.

**Scheduling** — save the current form as a named job, toggle the internal
scheduler, or export an OS command (see below).

---

## Scheduling

Jobs are stored as JSON in your user config directory
(`%APPDATA%\imap-cleanup-tool` on Windows, `~/.config/imap-cleanup-tool` elsewhere).

- **Internal scheduler**: enable it in the GUI; jobs run while the app is open.
- **System scheduler**: click *Export system command* to get a ready-to-run
  line:
  - **Windows** — a `schtasks /Create ...` command (Task Scheduler).
  - **Linux/macOS** — a `crontab` line.

Exported commands invoke the CLI through the current interpreter
(`python -m imap_cleanup_tool.cli ...`), so they work inside your virtualenv without
relying on `PATH`. Use `--yes` in scheduled jobs to skip the prompt.

---

## Gmail notes

1. Enable 2-Step Verification, then create an **App Password** and use it
   instead of your normal password.
2. Enable IMAP in Gmail settings.
3. Host is `imap.gmail.com`. Folder names are special: `[Gmail]/Trash`,
   `[Gmail]/All Mail`, `[Gmail]/Spam` (localised, e.g. `[Gmail]/Cestino`).
4. Use `--gmail-trash`: a plain delete in `INBOX` only removes the label, not
   the message. Target `[Gmail]/All Mail` to catch archived mail too.

---

## Building a Windows .exe

Locally:

```bash
pip install pyinstaller .
pyinstaller --onefile --windowed --name imap-cleanup-tool-gui \
    --collect-submodules imap_cleanup_tool src/imap_cleanup_tool/gui.py
pyinstaller --onefile --name imap-cleanup-tool \
    --collect-submodules imap_cleanup_tool src/imap_cleanup_tool/cli.py
```

The executables appear in `dist/`. The included GitHub Actions workflow
(`.github/workflows/build-and-release.yml`) builds them automatically and
attaches them to a GitHub Release whenever you push a `v*` tag.

---

## Publishing to PyPI

```bash
pip install build twine
python -m build                 # creates dist/*.whl and dist/*.tar.gz
twine upload dist/*             # needs a PyPI account + API token
```

Or let the workflow publish on tag push, with a `PYPI_API_TOKEN` repository
secret. Remember to bump `version` in `pyproject.toml` and
`src/imap_cleanup_tool/__init__.py` for each release.

---

## License

**GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)** — see
[LICENSE](LICENSE).

This is free, open-source software with a strong copyleft: you may use, study,
modify, and redistribute it, but **any derivative work — including software that
reuses any part of this code, and modified versions offered over a network as a
service — must also be released as open source under the AGPL-3.0**. You cannot
incorporate this code into a closed-source or proprietary product.

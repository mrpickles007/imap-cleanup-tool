<p align="center">
  <img src="https://raw.githubusercontent.com/mrpickles007/imap-cleanup-tool/main/src/imap_cleanup_tool/assets/logo.png" alt="IMAP Cleanup Tool logo" width="360">
</p>

<h1 align="center">IMAP Cleaner</h1>

Delete or move IMAP emails in bulk — by **sender**, by **domain**, or by
**nested rules** (a query builder). Works from the **command line** and from a
local **web interface**. The CLI uses only the Python standard library; the web
UI is an optional extra (FastAPI).

- Match by a target file (one sender/domain per line) **or** by a rule
  expression like `sender contains amazon.com OR (subject is Invoice AND date starts 2025-01-01)`.
- Fast **server-side search** for huge folders, or strict **local matching**.
- **Count** how many emails a filter matches before deleting anything.
- **Gmail mode**: moves matches to Trash (the only way to truly delete on Gmail).
- **Empty a whole folder** (e.g. Trash) without scanning.
- **List senders** with counts and export them to CSV (with timestamp).
- **Stop** button / cooperative cancellation for long runs.
- **Scheduler**: save jobs and run them while the app is open, or **install**
  them into Windows Task Scheduler / cron to run even when it is closed.

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
- [Web interface](#web-interface)
- [Scheduling](#scheduling)
- [Gmail notes](#gmail-notes)

---

## Install

### From PyPI (recommended)

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install imap-cleanup-tool
```

This installs the `imap-cleanup-tool` CLI. For the **web interface**
(recommended for most users), install the extra:

```bash
pip install "imap-cleanup-tool[web]"     # adds the imap-cleanup-tool-web command
```

The CLI stays dependency-free; only the web UI pulls in FastAPI + uvicorn.

### From source

```bash
git clone https://github.com/mrpickles007/imap-cleanup-tool.git
cd imap-cleanup-tool

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -e ".[dev,web]"      # editable install + dev tools + web UI
```

### Running the tests

The test suite uses only the standard library (`unittest`) — nothing extra to
install:

```bash
python -m unittest discover -s tests -v
```

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
| `--run-job NAME` | Run a saved scheduled job by name (used by the OS scheduler). |

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

In the **web UI** you build rules visually with the query builder (no typing).
The text grammar below is what the **CLI** `--rule` flag accepts and what
scheduled jobs store — the visual builder produces exactly these expressions
under the hood.

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
*@newsletter.com        # that domain EXACTLY — never subdomains
annoying.com            # that domain, plus subdomains if --include-subdomains
mail.annoying.com       # that specific (sub)domain
```

The `*@domain` form always matches the domain exactly; the bare `domain` form
also matches subdomains when `--include-subdomains` is given. This distinction
applies to local `--scan-mode full`; server-side `search` is a substring match
either way.

So in `full` mode `*@paypal.com` is the same as `paypal.com` *without*
`--include-subdomains`. The useful part is mixing them: with
`--include-subdomains` **on**, `*@paypal.com` stays exact while bare domains
expand to their subdomains — per-entry control in a single list. Example:

```text
*@paypal.com      # exact, even with --include-subdomains
newsletter.com    # this one DOES include its subdomains
```

---

## Web interface

A local web UI (FastAPI) is the tool's graphical interface. Install the extra
and run:

```bash
pip install "imap-cleanup-tool[web]"
imap-cleanup-tool-web        # serves http://127.0.0.1:8765 and opens your browser
```

Options: `--host`, `--port`, `--no-browser`. It runs only on your machine
(`127.0.0.1`) by default. The IMAP connection lives on the local server and is
reused across actions, surviving a page refresh; it is dropped automatically
after a period of inactivity. Your password is never stored.

Highlights:

- Many provider presets, connect-and-load-folders (with per-folder message
  counts), multi-folder selection, Select all / Deselect all.
- Match by a **target list** (paste or load from a file, with inline format
  help) or a **visual nested query builder** (field ▸ operator ▸ value, AND/OR
  groups).
- **Count matching emails** before deleting; **dry-run** is on by default.
- Context-aware options with tooltips (e.g. *Include subdomains* only in
  `"full"` scan mode; *Gmail: move to Trash* only for Gmail).
- Background runs with a **Stop** button and a persistent, live log panel.
- **List senders** with counts (export to CSV), and a **Scheduling** tab to
  create jobs and install them into the OS scheduler.

---

## Scheduling

Jobs are stored as JSON in your user config directory
(`%APPDATA%\imap-cleanup-tool` on Windows, `~/.config/imap-cleanup-tool` elsewhere).

- **Internal scheduler**: toggle it in the web UI's *Scheduling* tab; jobs run
  while the app is open.
- **System scheduler**: click *Install to system scheduler* to register the job
  directly (a `schtasks` task on Windows, a `crontab` line on Linux/macOS) so it
  runs even when the app is closed. *Export command* shows the equivalent line.

Scheduled tasks run a saved job by name (`imap-cleanup-tool --run-job NAME`) via
the current interpreter, so they work inside your virtualenv without relying on
`PATH`. For unattended jobs the password is read from the `IMAP_PASSWORD`
environment variable (it is never stored in the job).

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

## License

**GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)** — see
[LICENSE](LICENSE).

This is free, open-source software with a strong copyleft: you may use, study,
modify, and redistribute it, but **any derivative work — including software that
reuses any part of this code, and modified versions offered over a network as a
service — must also be released as open source under the AGPL-3.0**. You cannot
incorporate this code into a closed-source or proprietary product.

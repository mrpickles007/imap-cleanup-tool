# Project Brief

## What

`imap-cleanup-tool` is an open-source Python utility to **delete or move IMAP
emails in bulk**. Matching is by:

- a **target file** (one sender address or domain per line), or
- a **rule expression** — a nested query builder (`sender contains x OR
  (subject is y AND date starts 2025-01-01)`) compiled to IMAP `SEARCH`.

It can also list senders with counts (export to CSV) and empty an entire folder.

## Why

Webmail UIs make bulk cleanup (years of newsletters, a noisy domain, a full
Trash folder) slow and painful. This tool does it from the command line or a
simple desktop GUI, fast, with a safety-first model.

## Distribution goals

- Installable **Python package** on **PyPI** (`pip install imap-cleanup-tool`).
- Standalone **Windows `.exe`** (CLI + GUI) via PyInstaller, attached to GitHub
  Releases.
- Open source on **GitHub** under the **MIT** license.

## Hard constraints

- **Stdlib only at runtime** — no third-party runtime dependencies.
- **Python ≥ 3.10**, cross-platform (Windows / macOS / Linux).
- **English** for all code, UI, and docs.
- **Safety first** — deletion is destructive; dry-run and confirmation paths are
  non-negotiable.

## Author / ownership

- Author: Giulio Alberello (giulioalberello@gmail.com)
- GitHub owner: `mrpickles007`
- Repo: `https://github.com/mrpickles007/imap-cleanup-tool`

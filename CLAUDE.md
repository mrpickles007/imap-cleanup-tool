# CLAUDE.md

Guidance for Claude Code (and any AI assistant) working in this repository.

## Project in one line

`imap-cleanup-tool` — an open-source, stdlib-only Python tool that deletes or
moves IMAP emails in bulk (by sender, domain, or nested query-builder rules).
It ships as a **CLI**, a **Tkinter GUI**, and a standalone **Windows .exe**, and
is published to **PyPI** and **GitHub**.

## Memory bank — read this first

This project uses a **memory bank** under [`memory-bank/`](memory-bank/). At the
start of any non-trivial task, read these files before doing anything else —
they are the source of truth for context that is not obvious from the code:

- [`memory-bank/projectbrief.md`](memory-bank/projectbrief.md) — what we are building and why.
- [`memory-bank/productContext.md`](memory-bank/productContext.md) — users, use cases, UX intent.
- [`memory-bank/systemPatterns.md`](memory-bank/systemPatterns.md) — architecture and key design decisions.
- [`memory-bank/techContext.md`](memory-bank/techContext.md) — stack, env, build/release tooling, constraints.
- [`memory-bank/activeContext.md`](memory-bank/activeContext.md) — current focus and recent changes.
- [`memory-bank/progress.md`](memory-bank/progress.md) — what works, what's left, known issues.

**Keep the memory bank current.** After a meaningful change, update
`activeContext.md` and `progress.md` (and any other file the change touches). If
the user says **"update memory bank"**, review and refresh all six files.

## Golden rules for this codebase

1. **No third-party runtime dependencies.** The package must run on a clean
   Python ≥3.10 with only the standard library (`imaplib`, `argparse`,
   `tkinter`, `threading`, `csv`, `json`, `email`). Dev/build tools
   (`pyinstaller`, `build`, `twine`, `pylint`) live in the `dev` optional extra
   only. Do **not** add a runtime dependency without explicit approval.
2. **English only.** All code, comments, docstrings, UI strings, log messages,
   and docs are in English. The project was originally drafted in Italian and
   has been fully translated — do not reintroduce Italian.
3. **Deleting email is destructive.** Preserve the safety model: `--dry-run`
   default in the GUI, confirmation prompts in the CLI, and the
   flag-then-`--expunge` two-step. Never make a change that silently deletes
   without a dry-run path.
4. **`core.py` is UI-agnostic.** It contains no argument parsing and no UI. Both
   `cli.py` and `gui.py` import it. Keep IMAP logic there; keep presentation out.
5. **Cooperative cancellation.** Long-running core functions accept a
   `should_stop` callback and raise `StopRequested`. Honor this in new code.
6. **Bump version in two places** for every release: `pyproject.toml`
   (`version`) and `src/imap_cleanup_tool/__init__.py` (`__version__`). Keep them
   in sync.

## Layout

```
imap-cleanup-tool/
├── pyproject.toml              # hatchling build, entry points, metadata
├── README.md                  # user-facing docs (English)
├── LICENSE                    # MIT
├── CLAUDE.md                  # this file
├── memory-bank/               # AI memory bank (read first)
├── .github/workflows/         # build-and-release.yml (exe + PyPI on v* tag)
└── src/imap_cleanup_tool/
    ├── __init__.py            # public API re-exports + __version__
    ├── core.py                # IMAP ops: connect/search/list/delete/empty
    ├── cli.py                 # argparse CLI (entry: imap-cleanup-tool)
    ├── gui.py                 # Tkinter GUI (entry: imap-cleanup-tool-gui)
    ├── targets.py             # target-file parsing + sender matching
    ├── rules.py               # rule tree (Condition/Group) -> IMAP SEARCH
    ├── rule_parser.py         # text expression -> rule tree
    └── scheduler.py           # saved jobs + internal/OS scheduling
```

Entry points (from `pyproject.toml`):
- `imap-cleanup-tool` → `imap_cleanup_tool.cli:main`
- `imap-cleanup-tool-gui` → `imap_cleanup_tool.gui:main`

## Environment & commands (Windows, this machine)

This machine uses **virtualenvwrapper-win**. The project's virtualenv is named
**`ict`** (Python 3.13), located at `C:\Users\Personal\Envs\ict`.

- Activate interactively: `workon ict`
- Python directly: `C:\Users\Personal\Envs\ict\Scripts\python.exe`

Common commands (run from the `imap-cleanup-tool/` directory):

```powershell
# Editable install with dev tools
pip install -e ".[dev]"

# Lint
pylint src/imap_cleanup_tool

# Run the CLI / GUI from source
python -m imap_cleanup_tool.cli --help
python -m imap_cleanup_tool.gui

# Build sdist + wheel
python -m build

# Build the Windows executables
pyinstaller --onefile --windowed --name imap-cleanup-tool-gui --collect-submodules imap_cleanup_tool src/imap_cleanup_tool/gui.py
pyinstaller --onefile --name imap-cleanup-tool --collect-submodules imap_cleanup_tool src/imap_cleanup_tool/cli.py
```

> Note: when invoking Python via the Bash tool, `C:/Envs` does not resolve — the
> envs live at `C:\Users\Personal\Envs`. Use the full path to the interpreter.

## Release flow (summary; details in techContext.md)

1. Bump `version` + `__version__`.
2. Commit, then tag `vX.Y.Z` and push the tag.
3. The `build-and-release` workflow builds the Windows `.exe` files, attaches
   them to the GitHub Release, and publishes to PyPI (needs the
   `PYPI_API_TOKEN` repo secret).

## Repository

GitHub: `https://github.com/mrpickles007/imap-cleanup-tool` (owner: mrpickles007).

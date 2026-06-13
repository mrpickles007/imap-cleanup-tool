# System Patterns

## Architecture

UI-agnostic core, two front-ends, plus pluggable matching and scheduling:

```
            ┌─────────────┐        ┌─────────────┐
            │   cli.py    │        │   gui.py    │
            │ (argparse)  │        │  (Tkinter)  │
            └──────┬──────┘        └──────┬──────┘
                   └───────────┬──────────┘
                          ┌────▼────┐
                          │ core.py │  IMAP ops only, no UI/argparse
                          └────┬────┘
        ┌──────────────┬───────┼───────────┬──────────────┐
   targets.py      rules.py  rule_parser.py            scheduler.py
 (file matching) (tree→SEARCH) (text→tree)        (jobs + OS/internal)
```

## Key decisions

- **`core.py` has no UI and no argument parsing.** Both front-ends import it.
  All IMAP logic (connect, search, fetch headers, list senders, delete, empty,
  `process_folder`) lives here. Keep it that way.
- **Two matching sources, mutually exclusive** in `process_folder`:
  `search_argument` (compiled from rules) **or** `addresses`/`domains` (target
  file). Rules are server-side `SEARCH`; targets can be server-side (`search`)
  or strict local header matching (`full`).
- **Rule engine is a serializable tree.** `Condition` and `Group` (AND/OR,
  arbitrarily nestable) in `rules.py`, JSON-friendly via `to_dict`/
  `node_from_dict` so rules can be stored in scheduled jobs. `compile_search`
  emits an IMAP `SEARCH` string. IMAP `OR` is a 2-arg prefix operator, so
  multi-child OR is folded right-to-left.
- **`rule_parser.py`** is a small recursive-descent parser: text expression →
  rule tree. Fields: `sender|subject|date`; operators: `is|contains|starts|ends`;
  `AND`/`OR` with parentheses for grouping.
- **Cooperative cancellation.** Core functions take a `should_stop` callback and
  raise `StopRequested` at safe checkpoints. The GUI runs work on a daemon
  thread, sets a `threading.Event`, and surfaces it through `should_stop`.
- **GUI threading + logging.** Work runs off the Tk main thread; log records are
  pushed to a `queue.Queue` via a custom `logging.Handler` and drained by a
  periodic `root.after` poll. Never touch Tk widgets from the worker thread —
  marshal back with `root.after(0, ...)`.
- **Batching.** UID operations are chunked (`UID_CHUNK_SIZE = 500`); Gmail STORE
  is capped lower (`GMAIL_STORE_CAP = 200`) because Gmail rejects large STOREs.

## Safety model

- GUI default: **dry-run on**. CLI: confirmation prompt unless `--yes`.
- Deletion flags `\Deleted`; messages are only **expunged** with `--expunge`
  (or when emptying a folder). Gmail mode applies the `\Trash` label instead.

## Scheduling

- `scheduler.Job` = CLI args + a schedule dict (`daily` time or `interval`
  minutes), persisted as JSON in the per-user config dir.
- **Internal scheduler**: a minute-resolution daemon thread (no APScheduler).
- **System export**: builds a `schtasks` (Windows) or `crontab` (Unix) line that
  invokes `python -m imap_cleanup_tool.cli ...` using `sys.executable` so it
  works inside a virtualenv without relying on `PATH`.

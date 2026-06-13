# Product Context

## Who it's for

People with large, messy mailboxes who want bulk control that webmail doesn't
offer well:

- Power users cleaning years of newsletters / a specific noisy domain.
- Anyone who wants a repeatable, scheduled cleanup (e.g. empty Trash nightly).
- Technical users who prefer a CLI; non-technical users who prefer a GUI.

## Core use cases

1. **Delete by sender/domain** using a target file.
2. **Delete by rule** (sender/subject/date with AND/OR nesting).
3. **List senders** in a folder with counts; export to timestamped CSV to decide
   what to clean.
4. **Empty a folder** wholesale (e.g. Trash) without scanning headers.
5. **Gmail mode** — move matches to Trash via `X-GM-LABELS` (the only real
   delete on Gmail).
6. **Schedule** jobs — internally (while the GUI runs) or by exporting a Windows
   Task Scheduler / cron command that runs the CLI even when the app is closed.

## UX intent

- **Safe by default**: GUI defaults to dry-run; CLI confirms before destructive
  actions; nothing is permanently removed unless `--expunge` is given.
- **Transparent**: verbose, per-batch logging so the user sees exactly what
  happens.
- **No surprises with folder selection**: the GUI folder picker *adds* to a set
  and removes individually — selections never silently overwrite each other.
- **Interruptible**: a Stop button / cooperative cancellation for long runs.

## Provider notes

- Presets for Gmail, iCloud, Outlook/Office365, Aruba, Libero.
- Gmail requires an App Password + IMAP enabled; special folder names like
  `[Gmail]/All Mail`, `[Gmail]/Trash` (may be localized).

# Contributing to imap-cleanup-tool

Thanks for your interest in improving imap-cleanup-tool! Bug reports, fixes, and
features are welcome.

## Licensing of contributions (important)

This project is published under the **AGPL-3.0**. So that its licensing can be
managed consistently, **all contributions are accepted under the
[Contributor License Agreement (CLA)](CLA.md)** - the same approach used by many
open-source projects.

In short, by submitting a pull request you:

1. **Agree to the [CLA](CLA.md)** - you keep ownership of your work and grant the
   project owner a broad license to manage the project's licensing.
2. **Sign off your commits** with the Developer Certificate of Origin:

   ```bash
   git commit -s -m "your message"      # adds a Signed-off-by line
   ```

The first time you open a PR, please add a comment: *"I have read the CLA and I
agree to it."*

## Development setup

```bash
git clone https://github.com/mrpickles007/imap-cleanup-tool.git
cd imap-cleanup-tool

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1

pip install -e ".[dev,web]"        # editable install + dev tools + web UI
```

## Before you open a PR

- **Run the tests** (standard library only, nothing extra to install):

  ```bash
  python -m unittest discover -s tests -v
  ```

- **Add or update tests** for any behavior you change.
- **Match the codebase conventions** (see [CLAUDE.md](CLAUDE.md) for the golden
  rules). The most important ones:
  - **English only** in code, comments, UI strings, and docs.
  - **CLI and `core.py` stay standard-library only.** Third-party dependencies
    belong exclusively in the optional `[web]` extra.
  - **`core.py` is UI-agnostic** - no argument parsing, no presentation.
  - Preserve the **safety model**: dry-run by default in the web UI,
    confirmation in the CLI, flag-then-`--expunge`.
  - Honor **cooperative cancellation** (`should_stop` / `StopRequested`) in
    long-running code.
- Keep PRs focused; describe the change and how you tested it.

## Reporting bugs / requesting features

Open a GitHub issue with steps to reproduce (for bugs) or a clear use case (for
features). Never include real passwords or app passwords in issues or logs.

For anything else, you can reach support at **support@imapcleanuptool.com**.

## Code of conduct

Be respectful and constructive. Harassment or abuse will not be tolerated.

# Tech Context

## Stack

- **Language:** Python ≥ 3.10 (developed/tested with 3.13 on this machine).
- **Runtime deps:** none — standard library only (`imaplib`, `argparse`,
  `tkinter`, `threading`, `queue`, `csv`, `json`, `email`, `pathlib`, `shlex`).
- **Build backend:** `hatchling` (src layout, `packages = ["src/imap_cleanup_tool"]`).
- **Dev/build extra (`.[dev]`):** `pylint`, `pyinstaller`, `build`, `twine`.

## Local environment (Windows, this machine)

- **virtualenvwrapper-win**; project env is **`ict`** (Python 3.13).
- Location: `C:\Users\Personal\Envs\ict`.
- Activate: `workon ict`. Direct interpreter:
  `C:\Users\Personal\Envs\ict\Scripts\python.exe`.
- Caveat: the Bash tool cannot see `C:/Envs`; the real path is
  `C:\Users\Personal\Envs`. Prefer PowerShell or the full interpreter path.

## Common commands

```powershell
pip install -e ".[dev]"                 # editable install + dev tools
pylint src/imap_cleanup_tool            # lint
python -m imap_cleanup_tool.cli --help  # run CLI from source
python -m imap_cleanup_tool.gui         # run GUI from source
python -m build                         # sdist + wheel into dist/
```

## Entry points

- `imap-cleanup-tool` → `imap_cleanup_tool.cli:main`
- `imap-cleanup-tool-gui` → `imap_cleanup_tool.gui:main` (GUI script)

## Building the Windows .exe (PyInstaller)

```powershell
pyinstaller --onefile --windowed --name imap-cleanup-tool-gui --collect-submodules imap_cleanup_tool src/imap_cleanup_tool/gui.py
pyinstaller --onefile --name imap-cleanup-tool --collect-submodules imap_cleanup_tool src/imap_cleanup_tool/cli.py
```

Outputs land in `dist/`. `--collect-submodules imap_cleanup_tool` ensures all
package modules are bundled.

## CI/CD — `.github/workflows/build-and-release.yml`

Triggered on pushing a tag matching `v*`:

- **windows-exe** job (windows-latest): builds GUI + CLI executables and attaches
  them to the GitHub Release (`softprops/action-gh-release@v2`).
- **pypi** job (ubuntu-latest): `python -m build` then
  `pypa/gh-action-pypi-publish`, authenticated with the **`PYPI_API_TOKEN`**
  repository secret.

## Release checklist

1. Bump `version` in `pyproject.toml` **and** `__version__` in
   `src/imap_cleanup_tool/__init__.py` (keep in sync).
2. Update `README.md` / memory bank if behavior changed.
3. Commit; `git tag vX.Y.Z`; `git push origin main --tags`.
4. CI builds exes + publishes to PyPI.

## Config / data locations

Per-user config dir for scheduled jobs (`jobs.json`):
- Windows: `%APPDATA%\imap-cleanup-tool`
- macOS: `~/Library/Application Support/imap-cleanup-tool`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/imap-cleanup-tool`

Locally-created files (gitignored): `senders.csv`, `targets.txt`, `jobs.json`.

## Known constraints / gotchas

- IMAP `SEARCH FROM/SUBJECT` is substring matching; there is no exact-header
  match — `is` and `contains` map to the same token. Use `--scan-mode full` for
  strict local matching.
- Gmail: needs App Password + IMAP enabled; plain delete in `INBOX` only removes
  a label — use `--gmail-trash` and consider targeting `[Gmail]/All Mail`.
- `tkinter` is stdlib but some Linux distros need `sudo apt install python3-tk`.

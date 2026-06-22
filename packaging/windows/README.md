# Windows installer

Online installer built with [Inno Setup](https://jrsoftware.org/isinfo.php). It
embeds a private, relocatable Python and pip-installs the app at install time
using the pinned versions in [`../constraints.txt`](../constraints.txt). AI
Cleanup is an optional task (off by default).

## Files

| File | Purpose |
| --- | --- |
| `installer.iss` | Inno Setup script (components, pip step, shortcuts, PATH, uninstall). |
| `launcher.py` | What the shortcut runs: opens the web UI, or falls back to the CLI. |
| `build.ps1` | Downloads python-build-standalone, then compiles the installer. |
| `python/` | The bundled Python (created by `build.ps1`; **not** committed). |
| `dist/` | Output `.exe` (**not** committed). |

## Build (by hand)

1. Install Inno Setup (make sure `ISCC.exe` is on `PATH`).
2. Pick a [python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases)
   **install_only** Windows x86_64 `.tar.gz` and pin its URL.
3. Run:

   ```powershell
   cd packaging\windows
   .\build.ps1 -Version 0.36.8 -PbsUrl "https://github.com/astral-sh/python-build-standalone/releases/download/<TAG>/cpython-3.13.x+<DATE>-x86_64-pc-windows-msvc-install_only.tar.gz"
   ```

4. Test `dist\imap-cleanup-tool-0.36.8-windows-setup.exe` on a clean Windows VM
   (with and without the AI task; confirm the Start-menu icon opens the web UI).
5. Upload it to the GitHub Release, then we commit the README install tabs.

## Notes

- **Internet** is needed during installation (the pip step). This is the agreed
  online-installer model.
- **Code signing:** unsigned for now (SmartScreen shows "Run anyway"). Free Store
  signing would require a self-contained MSIX instead - see [`../README.md`](../README.md).
- Keep the embedded **PBS release tag** pinned and recorded so builds are
  reproducible.

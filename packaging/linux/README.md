# Linux AppImage

A single-file **AppImage** that bundles its own Python and the app (CLI + web UI,
and AI Cleanup when built with the `web,ai` extras). Runs on most distributions,
no install and no root. Double-click or run from a terminal: it starts the local
web UI and opens the browser.

## Files

| File | Purpose |
| --- | --- |
| `build.sh` | Downloads python-build-standalone, installs the app, packs the AppImage. |
| `AppRun` | AppImage entry point (`python3 -m imap_cleanup_tool.webapp`). |
| `imap-cleanup-tool.desktop` | Desktop entry (icon, name, runs in a terminal). |
| `dist/` | Output `.AppImage` (**not** committed - goes to GitHub Releases). |

## Build (by hand, on Linux or WSL)

```bash
cd packaging/linux
./build.sh 0.36.8 web,ai        # or "web" for a smaller, AI-less AppImage
```

Heavy work happens in a fast temp dir; only the finished
`dist/imap-cleanup-tool-<version>-x86_64.AppImage` is kept. The app icon is taken
from `../../../imapcleanuptool-site/logo.png` if present.

- **Internet** is needed during the build (downloads Python + dependencies).
- No signing/certificate is required on Linux.
- `appimagetool` is run with `--appimage-extract-and-run` so it works without FUSE
  (e.g. inside WSL).

## Pinned

- **python-build-standalone**: release tag `20260610`, CPython **3.13.14**
  (`...-x86_64-unknown-linux-gnu-install_only.tar.gz`). Override with `PBS_URL=...`.
  (3.14 is intentionally avoided for now: litellm/AI requires Python < 3.14.)
- Dependency versions: see [`../constraints.txt`](../constraints.txt).

A `web,ai` build is ~144 MB (litellm and its deps are large); a `web`-only build
is much smaller.

## Verified

Built and smoke-tested on WSL Ubuntu: the AppImage starts the bundled server and
serves the UI (HTTP 200, `<title>IMAP Cleanup Tool</title>`).

# Desktop installers / packaging

This folder holds everything needed to ship **native installers** so a
non-technical user never has to touch Python or `pip`. The runtime is unchanged:
the installed app still **starts the local web server and opens the browser**
(the `imap-cleanup-tool-web` entry point already does this).

> **Builds are produced by hand**, not by CI. The finished artifacts (the Windows
> `.exe`, the Linux `.AppImage`) are uploaded to **GitHub Releases**, and the
> website + README link to them from there.

## How it works (all platforms)

1. The installer **embeds its own Python** (a relocatable
   [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
   build) inside the app folder. It does **not** use, touch, or conflict with any
   Python already on the user's system.
2. At install time it runs an **online** step:
   `pip install "imap-cleanup-tool[web]" -c constraints.txt` (and `[web,ai]` if
   the user ticks the AI component). Needs internet **once**, during setup.
3. The versions of everything pulled in are frozen in
   [`constraints.txt`](constraints.txt) - the versions we test and control.
4. It creates a launcher (Start-menu / desktop icon on Windows, a `.desktop`
   entry on Linux) that runs `imap-cleanup-tool-web`, plus the CLI on `PATH`.

The **AI Cleanup** component (`[ai]`, which pulls in the heavy `litellm`) is an
**optional checkbox** in the installer, off by default to keep the install light.

## Distribution matrix

| Platform | Artifact | Where to get it | Signing / store | Status |
| --- | --- | --- | --- | --- |
| **Windows** | Online `.exe` installer (Inno Setup) | GitHub Releases **+** Microsoft Store listing | See "Windows signing" below | Planned |
| **Linux** | `.AppImage` (single file, most distros) | GitHub Releases | None needed | Planned |
| **macOS** | *(none yet)* - install from source | Source tab / PyPI | Apple Dev ID needed later | **Future** |
| **Source** | `pip install` from PyPI | PyPI | n/a | **Available now** |

The README and the website both present these as **four tabs**:
**Windows / Linux / macOS / Source**. The macOS tab tells the user to install
from source and links to the Source tab (native macOS app is future work).

## Asset naming (GitHub Releases)

Release assets use **stable, unversioned** names so the website and README can link
to the always-latest build via GitHub's `releases/latest/download/<name>` redirect
(no per-release link edits, ever):

```
imap-cleanup-tool-windows-setup.exe
imap-cleanup-tool-x86_64.AppImage
```

Download links (do not change these per release):

```
https://github.com/mrpickles007/imap-cleanup-tool/releases/latest/download/imap-cleanup-tool-windows-setup.exe
https://github.com/mrpickles007/imap-cleanup-tool/releases/latest/download/imap-cleanup-tool-x86_64.AppImage
```

The version still lives in the release tag/title and in the app (`--version`). When
cutting a release, upload each asset under the exact stable name above.

---

## Windows

**Tooling:** [Inno Setup](https://jrsoftware.org/isinfo.php) (free) for the `.exe`.

**Build steps (by hand):**

1. Download a python-build-standalone Windows x86_64 build (pin the exact release
   tag we standardise on) and extract it into `packaging/windows/python/`.
2. `python\python.exe -m pip install --upgrade pip`.
3. Compile `packaging/windows/installer.iss` with Inno Setup's `ISCC.exe`. The
   script:
   - bundles `packaging/windows/python/`,
   - shows a **component page**: *Web UI* (default) and *AI Cleanup* (optional),
   - on install runs `python\python.exe -m pip install "imap-cleanup-tool[web]"`
     (or `[web,ai]`) `-c constraints.txt`,
   - creates Start-menu + desktop shortcuts to `python\Scripts\imap-cleanup-tool-web.exe`,
   - adds the `Scripts` dir to `PATH` for the CLI (optional checkbox).
4. Upload the resulting `imap-cleanup-tool-<version>-windows-setup.exe` to the
   GitHub Release.

**Windows signing (how to spend zero, and what it costs to remove warnings):**

- **Unsigned (free):** the installer runs, but **SmartScreen** shows
  "Windows protected your PC / unknown publisher" - the user clicks
  *More info -> Run anyway*. Fine to launch with; document the click-through.
- **Microsoft Store (cheap, removes the warning):** free signing only applies to
  **MSIX/packaged** apps, which must be **self-contained** (no online pip step).
  So the Store path is a *separate, bundled* build, not this online installer.
  Either: (a) list this Win32 installer on the Store for discoverability (still
  unsigned -> SmartScreen still applies), or (b) later build a self-contained
  **MSIX** for true free Store signing. Decision deferred.
- **Code-signing cert:** Certum "Open Source" (~100 EUR/yr, OV, reputation builds
  over time) or EV (~300-500 EUR/yr, instant trust + hardware token).

> Tip: pointing the Start-menu shortcut at `pythonw.exe` + a tiny launcher hides
> the console window for a cleaner feel; the plain `imap-cleanup-tool-web.exe`
> shows a console with the server log + URL, which is also acceptable.

---

## Linux

**Tooling:** [`appimagetool`](https://github.com/AppImage/AppImageKit) (free).
AppImage is the pragmatic single-format choice: one file that runs on most
distributions without per-distro packaging.

**Build steps (by hand):**

1. Download a python-build-standalone Linux x86_64 build and extract into the
   AppDir, e.g. `AppDir/usr/python/`.
2. `AppDir/usr/python/bin/python3 -m pip install "imap-cleanup-tool[web]" -c constraints.txt`
   (add `,ai` for an AI-included build, or ship one AppImage and let users add AI
   from source).
3. Add `AppDir/AppRun` (launches `.../imap-cleanup-tool-web`), an
   `imap-cleanup-tool.desktop`, and an icon.
4. `appimagetool AppDir imap-cleanup-tool-<version>-x86_64.AppImage` and upload to
   the GitHub Release.

No signing or certificate is required on Linux.

---

## macOS (future)

Not shipped as a native app yet. The macOS install tab points users to the
**Source** instructions (`pip install`). When we do a native build later it will
need an **Apple Developer ID** ($99/yr) for signing + notarization, otherwise
Gatekeeper blocks the app. Building also requires a Mac or a macOS CI runner.

---

## Source (available now)

Plain `pip install` from PyPI - see the project README. This is the macOS path
for now and the universal fallback for any platform.

---

## Keeping versions under our control

[`constraints.txt`](constraints.txt) pins the dependency versions every installer
ships. Treat it as the source of truth: bump it deliberately, re-run the test
suite, then build. The python-build-standalone release tag we embed should also
be pinned (record it here when chosen) so builds are reproducible.

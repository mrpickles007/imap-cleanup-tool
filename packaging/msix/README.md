# Microsoft Store (MSIX)

Goal: publish IMAP Cleanup Tool on the **Microsoft Store** as an **MSIX**, so the
Store signs it (no SmartScreen), users get a trusted install + auto-updates. This
is **in addition** to the direct `.exe` on GitHub - both are kept in sync per
release; we do **not** replace the `.exe`.

> The website Windows tab already says "Coming soon to the Microsoft Store".

## What's here

- `assets/` - the Store/MSIX visual assets generated from the site logo
  (`StoreLogo` 50, `Square44/71/150/310`, `Wide310x150`). Ready to use.
- `AppxManifest.xml` - a manifest **template**. The `Identity` values are
  placeholders: copy the real ones from Partner Center after reserving the name.

## What only you can do (account + identity)

1. **Partner Center developer account** -
   [partner.microsoft.com/dashboard](https://partner.microsoft.com/dashboard).
   One-time fee (~$19 individual / ~$99 company). Identity + payment = yours.
2. **Reserve the app name** (e.g. "IMAP Cleanup Tool"). Partner Center then gives
   you the **product identity**: `Package/Identity/Name`, `Publisher` (CN=...),
   and `Publisher display name`. These must go into the package.
3. A **Privacy policy URL** (host one on imapcleanuptool.com) and store listing
   text + screenshots (we already have marketing copy + screenshots).

## Two ways to build the MSIX

### Route A - MSIX Packaging Tool (recommended, no SDK)

The free **MSIX Packaging Tool** (install from the Microsoft Store) captures an
existing install and produces an MSIX - perfect for our app, because it turns the
online `.exe` into a **self-contained snapshot** (Python + all deps baked in):

1. Install the **MSIX Packaging Tool** from the Store.
2. Run our `imap-cleanup-tool-windows-setup.exe` once on a clean Windows VM with
   the **AI component ticked** (so the snapshot includes everything).
3. In the MSIX Packaging Tool choose **"Application package" -> create from an
   installer**, point it at our `.exe`, and let it monitor the install.
4. When asked, set the **entry point** to the Start-menu shortcut (it runs the
   web UI) and the **package identity** to the Partner Center values from above.
5. It outputs an `.msix` (signed with a test cert for local testing). For the
   Store you upload it **unsigned-by-you**; the Store re-signs it.
6. Upload in Partner Center, fill the listing, submit for certification.

### Route B - Windows SDK + makeappx (scriptable)

If you install the **Windows SDK** (gives `makeappx.exe` + `signtool.exe`), the
package can be built from a manifest, which is repeatable per release:

1. Build a self-contained payload: a relocatable Python with the app installed,
   e.g. `python -m pip install "imap-cleanup-tool[web,ai]" -c ../constraints.txt`
   into `payload/app/python`, plus a small **launcher.exe** entry point that runs
   `python\python.exe -m imap_cleanup_tool.webapp` (MSIX can't pass args to the
   main executable, so a launcher exe is required).
2. Put `AppxManifest.xml` (with the real Identity) + `assets/` next to the payload.
3. `makeappx pack /d <root> /p imap-cleanup-tool.msix`
4. For local testing only: self-sign with `signtool` + a test cert and sideload.
   For the Store: upload to Partner Center (it re-signs).

> Tooling note: as of now this machine has **no** Windows SDK / makeappx /
> signtool / MSIX Packaging Tool installed - one of them must be installed first.

## Caveats (MSIX vs the .exe)

- **Bigger**: self-contained (no install-time pip), so all deps are baked in.
- **No install-time choices**: MSIX has no "tick AI" step - ship **web+AI
  included** (simplest) or a web-only package.
- **Sandbox**: runs in the MSIX container. The local web server on 127.0.0.1
  works (packaged apps get a loopback exemption for themselves); app data lives
  in the virtualized AppData. `runFullTrust` + `internetClient` capabilities.
- **Version**: MSIX version is 4-part, last digit 0 (e.g. `0.36.8.0`).

## Keep in sync

Each release: rebuild the `.exe`, the AppImage **and** the MSIX at the same app
version, and upload the `.exe`/AppImage to GitHub Releases + submit the MSIX
update to the Store.

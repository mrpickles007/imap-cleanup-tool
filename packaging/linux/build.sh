#!/usr/bin/env bash
# Build the IMAP Cleanup Tool Linux AppImage (bundled: Python + the app baked in).
#
#   ./build.sh [VERSION] [web|web,ai]
#
# Heavy lifting happens in a fast native temp dir (not /mnt/c), and only the
# finished .AppImage is copied back to packaging/linux/dist.
#
# Env overrides:
#   PBS_URL   pin a python-build-standalone "install_only" linux x86_64 .tar.gz
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
VERSION="${1:-0.36.8}"
EXTRAS="${2:-web,ai}"
ARCH="x86_64"
DIST="$HERE/dist"
mkdir -p "$DIST"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
APPDIR="$WORK/AppDir"
mkdir -p "$APPDIR/usr"

echo "== Resolving python-build-standalone URL =="
# Note: GitHub's browser_download_url encodes the '+' in the version as %2B.
if [ -z "${PBS_URL:-}" ]; then
  curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest -o "$WORK/rel.json"
  PBS_URL="$(grep -oE 'https://github.com/[^"]*cpython-3\.12\.[0-9]+(%2B|\+)[0-9]+-x86_64-unknown-linux-gnu-install_only\.tar\.gz' "$WORK/rel.json" | head -n1 || true)"
fi
[ -n "$PBS_URL" ] || { echo "Could not resolve PBS URL; set PBS_URL"; exit 1; }
echo "   $PBS_URL"

echo "== Downloading + extracting Python =="
curl -fsSL "$PBS_URL" -o "$WORK/pbs.tar.gz"
tar -xzf "$WORK/pbs.tar.gz" -C "$APPDIR/usr"     # -> AppDir/usr/python
PY="$APPDIR/usr/python/bin/python3"

echo "== Installing the app ($EXTRAS) from local source =="
"$PY" -m pip install --upgrade pip
"$PY" -m pip install "$REPO_ROOT[$EXTRAS]" -c "$HERE/../constraints.txt"

echo "== AppDir metadata =="
sed 's/\r$//' "$HERE/AppRun" > "$APPDIR/AppRun"; chmod +x "$APPDIR/AppRun"
sed 's/\r$//' "$HERE/imap-cleanup-tool.desktop" > "$APPDIR/imap-cleanup-tool.desktop"
ICON_SRC="$REPO_ROOT/../imapcleanuptool-site/logo.png"
if [ -f "$ICON_SRC" ]; then
  cp "$ICON_SRC" "$APPDIR/imap-cleanup-tool.png"
else
  # 1x1 transparent PNG fallback so appimagetool has an icon
  printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82' > "$APPDIR/imap-cleanup-tool.png"
fi

echo "== Fetching appimagetool =="
AIT="$WORK/appimagetool"
curl -fsSL -o "$AIT" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
chmod +x "$AIT"

echo "== Building AppImage =="
OUT="$DIST/imap-cleanup-tool-$VERSION-$ARCH.AppImage"
ARCH="$ARCH" "$AIT" --appimage-extract-and-run "$APPDIR" "$OUT"

echo "== Done =="
ls -lh "$OUT"

#!/usr/bin/env python3
"""Create/update a GitHub Release and upload the desktop installers.

The GitHub token is read from the local git credential helper (the same one
`git push` uses) - nothing is stored or printed. Stdlib only.

    python packaging/publish-release.py <tag> [--title TITLE] [--notes NOTES]

Uploads (replacing any existing same-named asset), so the website's
`releases/latest/download/<name>` links always resolve:
    packaging/windows/dist/imap-cleanup-tool-windows-setup.exe
    packaging/linux/dist/imap-cleanup-tool-x86_64.AppImage
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "mrpickles007/imap-cleanup-tool"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = [
    os.path.join(ROOT, "packaging", "windows", "dist",
                 "imap-cleanup-tool-windows-setup.exe"),
    os.path.join(ROOT, "packaging", "linux", "dist",
                 "imap-cleanup-tool-x86_64.AppImage"),
]


def get_token():
    out = subprocess.run(["git", "-C", ROOT, "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, check=False)
    for line in out.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):]
    sys.exit("No GitHub credential found (git credential fill).")


def api(token, method, url, data=None, headers=None):
    h = {"Authorization": "Bearer " + token,
         "Accept": "application/vnd.github+json",
         "User-Agent": "imap-cleanup-tool-release"}
    if headers:
        h.update(headers)
    body = None
    if isinstance(data, (bytes, bytearray)):
        body = data
    elif data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            ct = r.headers.get("Content-Type", "")
            return r.status, (json.loads(raw) if raw and "json" in ct else raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--title")
    ap.add_argument("--notes", default="")
    args = ap.parse_args()
    token = get_token()

    st, rel = api(token, "GET",
                  "https://api.github.com/repos/%s/releases/tags/%s" % (REPO, args.tag))
    if st == 200:
        rid = rel["id"]
        print("Using existing release %s (id %s)" % (args.tag, rid))
    else:
        st, rel = api(token, "POST",
                      "https://api.github.com/repos/%s/releases" % REPO,
                      data={"tag_name": args.tag,
                            "name": args.title or args.tag, "body": args.notes})
        if st not in (200, 201):
            sys.exit("Create release failed (%s): %r" % (st, rel))
        rid = rel["id"]
        print("Created release %s (id %s)" % (args.tag, rid))

    st, assets = api(token, "GET",
                     "https://api.github.com/repos/%s/releases/%s/assets" % (REPO, rid))
    existing = {a["name"]: a["id"] for a in assets} if st == 200 else {}

    for path in ASSETS:
        name = os.path.basename(path)
        if not os.path.isfile(path):
            print("  SKIP missing: %s" % path)
            continue
        if name in existing:
            api(token, "DELETE",
                "https://api.github.com/repos/%s/releases/assets/%s" % (REPO, existing[name]))
            print("  replaced existing %s" % name)
        with open(path, "rb") as f:
            data = f.read()
        print("  uploading %s (%.0f MB)..." % (name, len(data) / 1048576))
        st, res = api(token, "POST",
                      "https://uploads.github.com/repos/%s/releases/%s/assets?name=%s"
                      % (REPO, rid, name),
                      data=data, headers={"Content-Type": "application/octet-stream"})
        if st in (200, 201):
            print("    -> %s" % res["browser_download_url"])
        else:
            snippet = res[:300] if isinstance(res, bytes) else res
            print("    FAILED (%s): %r" % (st, snippet))
    print("done")


if __name__ == "__main__":
    main()

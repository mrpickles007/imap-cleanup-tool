"""IMAP Cleanup Tool launcher (Windows desktop build).

Run by the Start-menu / desktop shortcut. It opens the local web UI in the
browser if the web extra is installed; otherwise it falls back to the CLI in a
console. Launched with the bundled ``python.exe`` (not ``pythonw.exe``) on
purpose: the visible console doubles as the "app is running" indicator and the
way to stop the local server (close the window).
"""

import importlib.util
import os
import subprocess
import sys


def _have(*modules):
    return all(importlib.util.find_spec(m) is not None for m in modules)


def main():
    if _have("fastapi", "uvicorn"):
        # Web UI present: start it. `imap-cleanup-tool-web` defaults to
        # 127.0.0.1 and opens the browser itself.
        from imap_cleanup_tool.webapp import main as web_main
        sys.argv = ["imap-cleanup-tool-web"]
        web_main()
    else:
        # Web extra not installed (CLI-only install): show the CLI help.
        base = os.path.dirname(sys.executable)
        cli = os.path.join(base, "Scripts", "imap-cleanup-tool.exe")
        if not os.path.exists(cli):
            cli = os.path.join(base, "imap-cleanup-tool.exe")
        print("The web UI is not installed in this build; showing the "
              "command-line tool.\n")
        if os.path.exists(cli):
            subprocess.run([cli, "--help"], check=False)
        else:
            subprocess.run([sys.executable, "-m", "imap_cleanup_tool.cli",
                            "--help"], check=False)
        input("\nPress Enter to close...")


if __name__ == "__main__":
    main()

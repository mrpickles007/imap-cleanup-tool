"""Command-line interface for imap-cleanup-tool.

Examples
--------
List folders / senders::

    imap-cleanup-tool --host imap.gmail.com --user you@gmail.com --list-folders
    imap-cleanup-tool --host HOST --user USER --list-senders --save-senders out.csv

Delete by target file (classic)::

    imap-cleanup-tool --host HOST --user USER --targets targets.txt --dry-run
    imap-cleanup-tool --host HOST --user USER --targets targets.txt --expunge

Delete by a rule expression::

    imap-cleanup-tool --host HOST --user USER \\
        --rule 'sender contains amazon.com OR subject contains fattura' --dry-run

Gmail (move matches to Trash)::

    imap-cleanup-tool --host imap.gmail.com --user you@gmail.com \\
        --targets targets.txt --gmail-trash

Credentials come from flags, then env (IMAP_HOST/IMAP_USER/IMAP_PASSWORD/
IMAP_PORT), then an interactive prompt.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys

from . import core
from .rules import RuleError, compile_search
from .rule_parser import parse_rule_expression
from .targets import load_targets


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=os.getenv("IMAP_HOST"))
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("IMAP_PORT", "993")))
    parser.add_argument("--user", default=os.getenv("IMAP_USER"))
    parser.add_argument("--password", default=os.getenv("IMAP_PASSWORD"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--folder", action="append",
                        help="Folder to scan; repeat for several. Default INBOX.")
    parser.add_argument("--targets", help="Path to the target list file.")
    parser.add_argument("--rule",
                        help="Rule expression, e.g. "
                             "'sender contains x AND subject is y'.")
    parser.add_argument("--scan-mode", choices=["search", "full"],
                        default="search")
    parser.add_argument("--include-subdomains", action="store_true")
    parser.add_argument("--batch-size", type=int, default=core.UID_CHUNK_SIZE)
    parser.add_argument("--list-folders", action="store_true")
    parser.add_argument("--list-senders", action="store_true")
    parser.add_argument("--save-senders", metavar="CSV")
    parser.add_argument("--empty-folder", action="store_true")
    parser.add_argument("--gmail-trash", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--expunge", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--run-job", metavar="NAME",
                        help="Run a saved scheduled job by name (used by the "
                             "OS scheduler / cron).")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="imap-cleanup-tool",
        description="Delete or move IMAP emails by sender, domain or rule.")
    _add_arguments(parser)
    return parser.parse_args(argv)


def _resolve_credentials(args: argparse.Namespace) -> tuple[str, str, str]:
    host = args.host or input("IMAP host: ").strip()
    user = args.user or input("Username: ").strip()
    password = args.password or getpass.getpass("Password: ")
    return host, user, password


def _confirm(folders: list[str], empty: bool, gmail: bool,
             expunge: bool) -> bool:
    where = ", ".join(folders)
    if empty:
        print(f"About to DELETE EVERYTHING in: {where}")
    else:
        if gmail:
            action = "moved to Gmail Trash"
        elif expunge:
            action = "permanently removed"
        else:
            action = "flagged deleted"
        print(f"Matching messages will be {action} in: {where}")
    return input("Proceed? [y/N] ").strip().lower() in ("y", "yes")


def _run_operation(conn, args: argparse.Namespace, folders: list[str]) -> None:
    search_argument = None
    addresses: set[str] = set()
    domains: set[str] = set()

    if args.rule:
        node = parse_rule_expression(args.rule)
        search_argument = compile_search(node)
    elif args.targets:
        addresses, domains = load_targets(args.targets)
    else:
        sys.exit("[ERROR] Provide --targets or --rule (or use --empty-folder).")

    total = 0
    for folder in folders:
        total += core.process_folder(
            conn, folder, addresses=addresses, domains=domains,
            search_argument=search_argument, dry_run=args.dry_run,
            expunge=args.expunge, include_subdomains=args.include_subdomains,
            batch_size=args.batch_size, scan_mode=args.scan_mode,
            gmail_trash=args.gmail_trash)
    verb = "would be acted on" if args.dry_run else "acted on"
    core.logger.info("Done. %d message(s) %s in total.", total, verb)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    # pylint: disable=too-many-return-statements
    args = parse_args(argv)

    if args.run_job:
        from .scheduler import load_jobs
        job = next((j for j in load_jobs() if j.name == args.run_job), None)
        if job is None:
            print(f"[ERROR] No saved job named {args.run_job!r}.")
            return 4
        return main(job.args)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    host, user, password = _resolve_credentials(args)
    try:
        conn = core.connect(host, args.port, user, password, args.timeout)
    except (OSError, core.imaplib.IMAP4.error) as exc:
        print(f"[ERROR] Connection/login failed: {exc}")
        return 2

    folders = args.folder or ["INBOX"]
    try:
        if args.list_folders:
            for name in core.list_folders(conn):
                print("  ", name)
            return 0
        if args.list_senders:
            for folder in folders:
                core.list_senders(conn, folder, args.batch_size,
                                  account=user, save_path=args.save_senders)
            return 0
        if args.empty_folder:
            if not args.dry_run and not args.yes and not _confirm(
                    folders, True, False, False):
                print("Aborted.")
                return 0
            total = sum(core.empty_folder(conn, f, args.dry_run)
                        for f in folders)
            core.logger.info("Done. %d message(s) processed.", total)
            return 0

        if not args.dry_run and not args.yes and not _confirm(
                folders, False, args.gmail_trash, args.expunge):
            print("Aborted.")
            return 0
        _run_operation(conn, args, folders)
        return 0
    except RuleError as exc:
        print(f"[ERROR] Bad rule: {exc}")
        return 3
    finally:
        core.safe_logout(conn)


if __name__ == "__main__":
    sys.exit(main())

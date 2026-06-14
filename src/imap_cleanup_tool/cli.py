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
    parser.add_argument("--move", action="store_true",
                        help="Move matching messages to --dest-folder instead "
                             "of deleting them.")
    parser.add_argument("--dest-folder", metavar="NAME",
                        help="Destination folder/label for --move.")
    parser.add_argument("--create-folder", metavar="NAME",
                        help="Create a folder (a label on Gmail) on the server "
                             "and exit.")
    parser.add_argument("--delete-folder", metavar="NAME",
                        help="Delete a non-system folder/label on the server "
                             "and exit.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--expunge", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--run-job", metavar="NAME",
                        help="Run a saved scheduled job by name (used by the "
                             "OS scheduler / cron).")
    parser.add_argument("--profile", metavar="NAME",
                        help="Load the connection (host/user/password) from a "
                             "saved, non-encrypted profile.")


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
             expunge: bool, move_to: str | None = None) -> bool:
    where = ", ".join(folders)
    if empty:
        print(f"About to DELETE EVERYTHING in: {where}")
    else:
        if move_to:
            action = f"moved to {move_to!r}"
        elif gmail:
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
    exact_domains: set[str] = set()

    if args.rule:
        node = parse_rule_expression(args.rule)
        search_argument = compile_search(node)
    elif args.targets:
        addresses, domains, exact_domains = load_targets(args.targets)
    elif args.move:
        # Move with no target list / rule = move EVERY message in the folder.
        search_argument = "ALL"
        core.logger.info("No --targets/--rule with --move: moving ALL messages.")
    else:
        sys.exit("[ERROR] Provide --targets or --rule (or use --empty-folder).")

    total = 0
    for folder in folders:
        total += core.process_folder(
            conn, folder, addresses=addresses, domains=domains,
            exact_domains=exact_domains, search_argument=search_argument,
            dry_run=args.dry_run, expunge=args.expunge,
            include_subdomains=args.include_subdomains,
            batch_size=args.batch_size, scan_mode=args.scan_mode,
            gmail_trash=args.gmail_trash, move=args.move,
            dest_folder=args.dest_folder)
    verb = "would be acted on" if args.dry_run else "acted on"
    core.logger.info("Done. %d message(s) %s in total.", total, verb)


def _run_saved_job(job) -> int:
    """Execute a saved job, mirroring all output to its rolling log file."""
    from logging.handlers import RotatingFileHandler
    from .scheduler import job_log_path

    handler = RotatingFileHandler(job_log_path(job.name), maxBytes=512_000,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # A file handler plus a console handler (the latter is harmless when the OS
    # scheduler runs us with no terminal attached). Their presence makes the
    # nested basicConfig() call a no-op, so output is not duplicated.
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())
    root.info("=== Job %r started ===", job.name)
    try:
        code = main(job.args)
        root.info("=== Job %r finished (exit code %s) ===", job.name, code)
        return code
    except Exception as exc:  # pylint: disable=broad-exception-caught
        root.exception("Job %r crashed: %s", job.name, exc)
        return 1
    finally:
        root.removeHandler(handler)
        handler.close()


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
        return _run_saved_job(job)

    if args.profile:
        from .profiles import ProfileError, load_profile
        try:
            prof = load_profile(args.profile)
        except ProfileError as exc:
            print(f"[ERROR] {exc}")
            return 2
        args.host, args.port = prof["host"], prof["port"]
        args.user, args.password = prof["user"], prof["password"]
        args.timeout = prof["timeout"]

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
        if args.create_folder:
            print(core.create_folder(conn, args.create_folder))
            return 0
        if args.delete_folder:
            try:
                print(core.delete_folder(conn, args.delete_folder))
            except ValueError as exc:
                print(f"[ERROR] {exc}")
                return 2
            return 0
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

        if args.move and not (args.dest_folder and args.dest_folder.strip()):
            print("[ERROR] --move requires --dest-folder NAME.")
            return 2
        if not args.dry_run and not args.yes and not _confirm(
                folders, False, args.gmail_trash, args.expunge,
                move_to=args.dest_folder if args.move else None):
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

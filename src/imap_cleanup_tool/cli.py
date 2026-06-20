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
import importlib.util
import logging
import os
import sys

from . import core
from . import __version__
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
    parser.add_argument("--local-cache", action="store_true",
                        help="Cache message headers locally so repeat AI reports "
                             "are faster (also enabled by a profile's setting).")
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
    parser.add_argument("--ai-cleanup", action="store_true",
                        help="AI cleanup: score senders heuristically, ask an "
                             "LLM to judge those above --ai-threshold, then "
                             "delete the confirmed ones. Needs the [ai] extra.")
    parser.add_argument("--ai-model", metavar="NAME",
                        help="Name of a saved (non-encrypted) LLM model config.")
    parser.add_argument("--ai-threshold", type=float, default=6.0,
                        help="Heuristic spam-score threshold 0-10 (default 6).")
    parser.add_argument("--ai-sample", type=int, default=5,
                        help="Sample emails per flagged sender (default 5).")
    parser.add_argument("--ai-exclude", metavar="ADDR", action="append",
                        default=[],
                        help="Extra sender to exclude from the AI report "
                             "(repeatable). Your own address is excluded by "
                             "default unless --ai-include-self is given.")
    parser.add_argument("--ai-include-self", action="store_true",
                        help="Include your own mailbox address in the AI report "
                             "(by default it is excluded).")
    parser.add_argument("--ai-weight", metavar="KEY=VALUE", action="append",
                        default=[],
                        help="Override a heuristic weight (repeatable). Keys: "
                             "list_unsubscribe, unread_ratio, bulk, "
                             "sender_pattern, frequency. E.g. "
                             "--ai-weight unread_ratio=4.")
    parser.add_argument("--ai-report-only", action="store_true",
                        help="With --ai-cleanup: build the report (heuristic, "
                             "plus LLM verdicts if --ai-model is given) but "
                             "DELETE nothing. A model is optional in this mode.")
    parser.add_argument("--ai-report-csv", metavar="PATH",
                        help="Write the AI report as CSV to PATH "
                             "(Excel-friendly).")
    parser.add_argument("--ai-flag-spam", action="store_true",
                        help="Report confirmed senders as spam: move one of each "
                             "sender's messages to the Junk/Spam folder (trains "
                             "the server filter) before deleting the rest.")
    parser.add_argument("--ai-no-check-spam", action="store_false",
                        dest="ai_check_spam", default=True,
                        help="Do NOT skip already-saved spam senders from the LLM "
                             "(by default they are accepted as spam without asking "
                             "the model again, to save tokens).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--expunge", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--run-job", metavar="NAME",
                        help="Run a saved scheduled job by name (used by the "
                             "OS scheduler / cron).")
    parser.add_argument("--profile", metavar="NAME",
                        help="Load the connection (host/user/password or OAuth "
                             "token) from a saved, non-encrypted profile.")
    parser.add_argument("--oauth-login", metavar="PROVIDER",
                        help="Sign in with OAuth2 (e.g. 'microsoft') via the "
                             "device-code flow and save the result as a connection "
                             "profile, then exit. Works headless (prints a URL + "
                             "code to open on any device). Use with --user and "
                             "--oauth-profile.")
    parser.add_argument("--oauth-profile", metavar="NAME", default="",
                        help="Name to save the --oauth-login profile under "
                             "(defaults to the email address).")
    parser.add_argument("--notify-profile", metavar="NAME", default="",
                        help="Send the notification email from this saved SMTP "
                             "profile (must be non-encrypted) instead of the active "
                             "one. Used by scheduled jobs.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="imap-cleanup-tool",
        description="Delete or move IMAP emails by sender, domain or rule.")
    parser.add_argument(
        "-V", "--version", action="version",
        version=f"%(prog)s {__version__}",
        help="Show the installed version and exit. "
             "Update with: pip install -U imap-cleanup-tool")
    _add_arguments(parser)
    return parser.parse_args(argv)


def _resolve_credentials(args: argparse.Namespace) -> tuple[str, str, str]:
    host = args.host or input("IMAP host: ").strip()
    user = args.user or input("Username: ").strip()
    password = args.password or getpass.getpass("Password: ")
    return host, user, password


def _oauth_login_cli(args: argparse.Namespace) -> int:
    """Run the device-code OAuth sign-in and save a connection profile."""
    from . import oauth
    from .profiles import ProfileError, save_oauth_profile

    provider = args.oauth_login.strip().lower()
    try:
        cfg = oauth.get_provider(provider)
    except oauth.OAuthError as exc:
        print(f"[ERROR] {exc}")
        return 2

    # Host/port: explicit flags win, else the provider's default IMAP host.
    imap = cfg.get("imap") or {}
    host = args.host or imap.get("host", "")
    port = args.port if args.host else int(imap.get("port", args.port))
    user = args.user or input("Mailbox email: ").strip()
    if not host or not user:
        print("[ERROR] An IMAP host and a mailbox email are required.")
        return 2
    name = (args.oauth_profile or user).strip()

    try:
        flow = oauth.start_device_code(provider)
    except oauth.OAuthError as exc:
        print(f"[ERROR] {exc}")
        return 2
    # Microsoft/Google send a ready-made instruction string; fall back to our own.
    print("\n" + (flow["message"] or
                  f"To sign in, open {flow['verification_uri']} and enter the "
                  f"code: {flow['user_code']}"))
    print("\nWaiting for you to finish signing in in the browser ...")
    try:
        tok = oauth.poll_device_code(
            flow["device_code"], provider=provider, interval=flow["interval"],
            timeout=flow["expires_in"])
    except oauth.OAuthError as exc:
        print(f"[ERROR] {exc}")
        return 2

    refresh = tok.get("refresh_token") or ""
    if not refresh:
        print("[ERROR] The provider returned no refresh token (the "
              "'offline_access' scope may be missing).")
        return 2
    try:
        save_oauth_profile(name, host, port, user, refresh, provider)
    except ProfileError as exc:
        print(f"[ERROR] {exc}")
        return 2
    print(f"\nSigned in. Saved profile {name!r}. Connect with: "
          f"--profile {name}")
    return 0


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


# "run" for a direct CLI invocation, "job" when executed via --run-job (a
# scheduled job). Gates which notification toggle applies.
_NOTIFY_WHEN = "run"


def _notify_cli(args, folders: list[str], total: int, *, gmail: bool,
                kind: str, attachments=None, subject=None, body=None,
                dest: str = "") -> None:
    """Best-effort email notification after a CLI cleanup (never fatal)."""
    try:
        from . import notifications as nt
        account = getattr(args, "user", None) or getattr(args, "host", "")
        if subject is None or body is None:
            subject, body = nt.cleanup_summary(
                account, folders, total, dry_run=args.dry_run, gmail=gmail,
                kind=kind, dest=dest)
        if nt.send_notification(subject, body, when=_NOTIFY_WHEN,
                                profile=getattr(args, "notify_profile", ""),
                                attachments=attachments):
            core.logger.info("Notification email sent to the configured "
                             "recipient%s.",
                             " (with the report attached)" if attachments else "")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        core.logger.warning("Notification email not sent: %s", exc)


def _notify_job_failure(args, reason: str, *, oauth: bool) -> None:
    """A scheduled (unattended) job failed to connect. The reason is already logged;
    here we also try to email a 'job failed - re-authenticate' alert.

    Best-effort: if SMTP notifications are off, or SMTP itself is broken (e.g. its
    OAuth token also expired - rare, but IMAP+SMTP can fail together), the email
    can't go out. That's fine: ``_notify_cli`` logs that too, so the full story is
    always written to the job log regardless of whether any email is sent.
    """
    if _NOTIFY_WHEN != "job":
        return                      # interactive run: the user is right here
    account = getattr(args, "user", None) or getattr(args, "host", "")
    hint = ("This is an OAuth2 (modern auth) account: the sign-in has likely expired "
            "or been revoked. Re-authenticate in the web UI ('Sign in with Microsoft') "
            "or via the CLI ('imap-cleanup-tool --oauth-login <provider> --user "
            "<email> --oauth-profile <name>'). Until then this job keeps failing."
            if oauth else
            "Check the account credentials / mail server, then run the job again.")
    subject = f"[imap-cleanup-tool] Scheduled job FAILED on {account}"
    body = (f"A scheduled cleanup job could not run.\n\nAccount: {account}\n"
            f"Reason: {reason}\n\n{hint}\n\n- imap-cleanup-tool")
    _notify_cli(args, [], 0, gmail=False, kind="job failure",
                subject=subject, body=body)


def _record_spam_cli(account: str, report: dict, source: str) -> None:
    """Best-effort: save the report's flagged senders to the per-account spam list.
    The web UI already did this; the CLI / scheduled-job path was missing it, so
    scheduled AI reports/runs never populated the Spam addresses tab."""
    try:
        from . import spamstore
        n = spamstore.record_from_report(account, report, source)
        if n:
            core.logger.info("Saved %d sender(s) to the spam addresses list.", n)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        core.logger.warning("Could not save spam addresses: %s", exc)


def _cli_cache(args):
    """A HeaderCache when --local-cache (or a profile) enabled it, else None."""
    if not getattr(args, "local_cache", False):
        return None
    try:
        from .headercache import HeaderCache
        return HeaderCache()
    except Exception:  # pylint: disable=broad-exception-caught
        return None


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

    cache = _cli_cache(args)              # used by --scan-mode full
    total = 0
    for folder in folders:
        total += core.process_folder(
            conn, folder, addresses=addresses, domains=domains,
            exact_domains=exact_domains, search_argument=search_argument,
            dry_run=args.dry_run, expunge=args.expunge,
            include_subdomains=args.include_subdomains,
            batch_size=args.batch_size, scan_mode=args.scan_mode,
            gmail_trash=args.gmail_trash, move=args.move,
            dest_folder=args.dest_folder, cache=cache,
            account=getattr(args, "user", "") or "")
    verb = "would be acted on" if args.dry_run else "acted on"
    core.logger.info("Done. %d message(s) %s in total.", total, verb)
    kind = "Move" if args.move else "Cleanup"
    _notify_cli(args, folders, total,
                gmail=args.gmail_trash and not args.move, kind=kind,
                dest=(args.dest_folder or "") if args.move else "")


def _parse_ai_weights(items: list[str]) -> dict:
    """Parse --ai-weight KEY=VALUE pairs into a weights dict (validates keys)."""
    weights: dict[str, float] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"[ERROR] --ai-weight must be KEY=VALUE, got {item!r}.")
        key, _, value = item.partition("=")
        key = key.strip()
        if key not in core.DEFAULT_WEIGHTS:
            valid = ", ".join(sorted(core.DEFAULT_WEIGHTS))
            raise SystemExit(f"[ERROR] Unknown weight {key!r}. Valid: {valid}.")
        try:
            weights[key] = float(value)
        except ValueError:
            raise SystemExit(f"[ERROR] Weight {key!r} must be a number, "
                             f"got {value!r}.")
    return weights


def _run_ai(conn, args: argparse.Namespace, folders: list[str],
            user: str) -> int:
    """AI cleanup: heuristic report -> LLM verdict -> delete confirmed senders.

    With --ai-report-only the LLM step is optional and nothing is deleted.
    """
    from . import ai
    from .llm import LLMError, ensure_default_models, load_model, log_cost
    ensure_default_models()        # so gpt-4o-mini / Ollama exist out of the box

    cfg = None
    if args.ai_model:
        try:
            cfg = load_model(args.ai_model)
        except LLMError as exc:
            print(f"[ERROR] {exc}")
            return 2
        if cfg.get("encrypted"):
            print("[ERROR] Encrypted model configs can't run unattended.")
            return 2
    elif not args.ai_report_only:
        print("[ERROR] --ai-cleanup requires --ai-model NAME "
              "(or use --ai-report-only).")
        return 2

    weights = _parse_ai_weights(args.ai_weight) or None
    exclude = {a.strip() for a in args.ai_exclude if a.strip()}
    if not args.ai_include_self:
        exclude.add(user)

    # Scope like the Move feature: filter by --rule/--targets, else whole folder.
    search_argument = None
    addresses: set[str] = set()
    domains: set[str] = set()
    exact_domains: set[str] = set()
    if args.rule:
        search_argument = compile_search(parse_rule_expression(args.rule))
    elif args.targets:
        addresses, domains, exact_domains = load_targets(args.targets)
    cache = None
    if getattr(args, "local_cache", False):
        try:
            from .headercache import HeaderCache
            cache = HeaderCache()
            core.logger.info("Local header cache is ON.")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            core.logger.warning("Local cache unavailable (%s); continuing.", exc)
    else:
        try:
            from .headercache import HeaderCache
            if HeaderCache().has_account(user):
                core.logger.warning(
                    "A local header cache exists for this account but "
                    "--local-cache is off: it is ignored (and left untouched). "
                    "Re-run with --local-cache to reuse it for a fast report.")
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    report = core.build_ai_report(conn, folders, threshold=args.ai_threshold,
                                  sample_size=args.ai_sample, exclude=exclude,
                                  weights=weights,
                                  addresses=addresses, domains=domains,
                                  exact_domains=exact_domains,
                                  search_argument=search_argument,
                                  batch_size=args.batch_size,
                                  cache=cache, account=user)

    ev = None
    if cfg is not None:
        # Record cost per batch so a failed/interrupted run still tracks usage.
        recorder = None
        if cfg.get("track_costs"):
            recorder = lambda p, c, co: log_cost(args.ai_model, p, c, co)
        known_spam = None
        if getattr(args, "ai_check_spam", True):
            from . import spamstore
            known_spam = set(spamstore.all_addresses(user))
        try:
            ev = ai.evaluate(report, cfg, record_cost=recorder,
                             known_spam=known_spam)
        except RuntimeError as exc:
            print(f"[ERROR] {exc}")
            return 5
        for s in report["senders"]:
            if s.get("flagged"):
                s["verdict"] = ev["verdicts"].get(s["sender"].lower())
        cost = ev["cost"]
        cost_str = (f"${cost:.6f}" if isinstance(cost, (int, float))
                    else "not tracked")
        core.logger.info("=> LLM cost for this report: %s "
                         "(%d input + %d output tokens, model %s).",
                         cost_str, ev["prompt_tokens"],
                         ev["completion_tokens"], cfg["model"])

    csv_text = core.ai_report_csv(report)
    if args.ai_report_csv:
        try:
            with open(args.ai_report_csv, "w", encoding="utf-8", newline="") as fh:
                fh.write(csv_text)
            core.logger.info("AI report written to %s", args.ai_report_csv)
        except OSError as exc:
            print(f"[ERROR] Could not write {args.ai_report_csv}: {exc}")
            return 2

    if args.ai_report_only or ev is None:
        # Report-only: skip deletion, save the report + its spam addresses, and (if
        # email notifications are on) send the report as an attachment.
        _record_spam_cli(user, report, "report")
        from .scheduler import save_ai_report
        try:
            saved = save_ai_report(csv_text, user)   # tag the file with the account
            core.logger.info("Report saved to %s", saved)
        except OSError as exc:
            core.logger.warning("Could not save the report: %s", exc)
            saved = None
        account = getattr(args, "user", None) or getattr(args, "host", "")
        flagged = report.get("flagged_count", 0)
        deletable = report.get("flagged_messages", 0)
        subject = (f"[imap-cleanup-tool] AI report on {account}: "
                   f"{flagged} sender(s) flagged")
        body = (f"AI Cleanup report (report only - nothing was deleted) for "
                f"account: {account}\nFolders: {', '.join(folders)}\n"
                f"Flagged senders: {flagged}\n"
                f"Emails potentially deletable: {deletable}\n\n"
                f"The full report is attached as a CSV.\n\n- imap-cleanup-tool")
        fname = (saved.name if saved else "ai_report.csv")
        _notify_cli(args, folders, deletable, gmail=False, kind="AI report",
                    subject=subject, body=body,
                    attachments=[(fname, csv_text)])
        core.logger.info("Report only - nothing deleted.")
        return 0

    _record_spam_cli(user, report, "run")   # save flagged senders to the spam list
    confirmed = {s["sender"].lower() for s in report["senders"]
                 if s.get("flagged")
                 and (s.get("verdict") or {}).get("delete")}
    core.logger.info("AI confirmed %d sender(s) to delete.", len(confirmed))
    if not confirmed:
        return 0
    gmail = "gmail" in (args.host or "").lower()
    junk = None
    if getattr(args, "ai_flag_spam", False):
        junk = core.special_folder(conn, "\\Junk")
        if not junk:
            core.logger.warning("--ai-flag-spam: no Junk/Spam folder found - "
                                "skipping that step.")
    total = 0
    for folder in folders:
        if junk:
            m, _h = core.flag_senders_as_spam(
                conn, folder, confirmed, junk, per_sender=1,
                dry_run=args.dry_run, batch_size=args.batch_size)
            core.logger.info("Reported senders as spam in %r: %d message(s) "
                             "moved to %r.", folder, m, junk)
        total += core.process_folder(
            conn, folder, addresses=confirmed, dry_run=args.dry_run,
            expunge=args.expunge, gmail_trash=gmail,
            batch_size=args.batch_size, scan_mode="search")
    verb = "would be deleted" if args.dry_run else "deleted"
    core.logger.info("Done. %d message(s) %s.", total, verb)
    _notify_cli(args, folders, total, gmail=gmail, kind="AI Cleanup")
    return 0


def _run_saved_job(job) -> int:
    """Execute a saved job, mirroring all output to its rolling log file."""
    from logging.handlers import RotatingFileHandler
    from .scheduler import job_log_path, _job_profile

    log_path = job_log_path(job.name, _job_profile(job.args))
    log_path.parent.mkdir(parents=True, exist_ok=True)   # ensure the profile subfolder
    handler = RotatingFileHandler(log_path, maxBytes=512_000,
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
    global _NOTIFY_WHEN
    _NOTIFY_WHEN = "job"        # scheduled run -> use the 'notify on jobs' toggle
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

    # Gate AI Cleanup behind the optional [ai] extra (verify what's installed),
    # mirroring the web UI which disables the whole AI Cleanup option when the
    # extra is missing. Checked up front so it fails fast, before connecting.
    if args.ai_cleanup and importlib.util.find_spec("litellm") is None:
        print('[ERROR] AI Cleanup needs the optional [ai] extra, which is not '
              'installed. Install it with:\n'
              '    pip install "imap-cleanup-tool[ai]"')
        return 3

    if args.oauth_login:
        return _oauth_login_cli(args)

    if args.run_job:
        from .scheduler import load_jobs
        job = next((j for j in load_jobs() if j.name == args.run_job), None)
        if job is None:
            print(f"[ERROR] No saved job named {args.run_job!r}.")
            return 4
        return _run_saved_job(job)

    oauth_prof = None
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
        # A profile can carry the "enable local cache" setting; the flag can also
        # force it on for an ad-hoc connection.
        args.local_cache = args.local_cache or prof.get("local_cache", False)
        if prof.get("auth_method") == "oauth":
            oauth_prof = prof

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    if oauth_prof is not None:
        # OAuth profile: mint a fresh access token from the stored refresh token
        # (persisting any rotation) and authenticate with XOAUTH2. No password
        # prompt - this is what lets scheduled jobs run unattended.
        from . import oauth as _oauth
        from .profiles import ProfileError, update_refresh_token
        host, user = oauth_prof["host"], oauth_prof["user"]
        args.user = user

        def _persist(new_token: str) -> None:
            try:
                update_refresh_token(args.profile, new_token)
            except ProfileError:
                pass
        try:
            token = _oauth.access_token_for(oauth_prof, persist=_persist)
            conn = core.connect_oauth(host, args.port, user, token, args.timeout)
        except _oauth.OAuthError as exc:
            # logger (not print) so the reason lands in the scheduled-job log file.
            core.logger.error("OAuth sign-in failed for profile %r: %s",
                              args.profile, exc)
            _notify_job_failure(args, f"OAuth sign-in failed: {exc}", oauth=True)
            return 2
        except (OSError, core.imaplib.IMAP4.error) as exc:
            core.logger.error("Connection failed for profile %r: %s",
                              args.profile, exc)
            _notify_job_failure(args, f"Connection failed: {exc}", oauth=True)
            return 2
    else:
        host, user, password = _resolve_credentials(args)
        args.user = user             # the resolved account (used for the cache key)
        try:
            conn = core.connect(host, args.port, user, password, args.timeout)
        except (OSError, core.imaplib.IMAP4.error) as exc:
            core.logger.error("Connection/login failed: %s", exc)
            _notify_job_failure(args, f"Connection/login failed: {exc}", oauth=False)
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
            cache = _cli_cache(args)
            for folder in folders:
                core.list_senders(conn, folder, args.batch_size,
                                  account=user, save_path=args.save_senders,
                                  cache=cache)
            return 0
        if args.ai_cleanup:
            if (not args.ai_report_only and not args.dry_run and not args.yes
                    and not _confirm(folders, False, False, False)):
                print("Aborted.")
                return 0
            return _run_ai(conn, args, folders, user)
        if args.empty_folder:
            if not args.dry_run and not args.yes and not _confirm(
                    folders, True, False, False):
                print("Aborted.")
                return 0
            total = sum(core.empty_folder(conn, f, args.dry_run,
                                          batch_size=args.batch_size)
                        for f in folders)
            core.logger.info("Done. %d message(s) processed.", total)
            _notify_cli(args, folders, total, gmail=False, kind="Empty folder")
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

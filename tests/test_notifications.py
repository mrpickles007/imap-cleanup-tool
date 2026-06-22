"""Unit tests for email notifications (SMTP profiles, settings, sending)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import notifications as nt


class _FakeSMTP:
    """Records login + sent messages; stands in for smtplib.SMTP/SMTP_SSL."""

    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        self.logged_in = None
        self.sent = []
        self.started_tls = False
        self.greeted = False
        self.closed = False
        self.docmds = []
        _FakeSMTP.instances.append(self)

    def close(self):
        self.closed = True

    def starttls(self, context=None):
        self.started_tls = True

    def ehlo_or_helo_if_needed(self):
        self.greeted = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def docmd(self, cmd, arg=None):
        self.docmds.append((cmd, arg))
        if cmd == "AUTH":
            return (235, b"2.7.0 Accepted")   # XOAUTH2 accepted
        return (250, b"OK")

    def noop(self):
        return (250, b"OK")

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


class SMTPProfileTests(unittest.TestCase):
    def setUp(self):
        _FakeSMTP.instances = []

    def test_plain_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(nt, "config_dir", return_value=Path(tmp)):
                self.assertEqual(nt.list_profiles(), [])
                nt.save_profile("ses", "email-smtp.x.amazonaws.com", 587,
                                "AKIA", "secretpw", from_addr="me@x.com")
                p = nt.list_profiles()[0]
                self.assertEqual(p["name"], "ses")
                self.assertEqual(p["host"], "email-smtp.x.amazonaws.com")
                self.assertFalse(p["encrypted"])
                loaded = nt.load_profile("ses")
                self.assertEqual(loaded["password"], "secretpw")
                self.assertEqual(loaded["from_addr"], "me@x.com")

    def test_encrypted_needs_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(nt, "config_dir", return_value=Path(tmp)):
                nt.save_profile("e", "smtp.x.com", 587, "u", "pw",
                                encrypt=True, secret="pass")
                self.assertTrue(nt.list_profiles()[0]["encrypted"])
                with self.assertRaises(nt.NotifyError):
                    nt.load_profile("e")
                with self.assertRaises(nt.NotifyError):
                    nt.load_profile("e", secret="wrong")
                self.assertEqual(nt.load_profile("e", secret="pass")["password"],
                                 "pw")

    def test_bad_security_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(nt, "config_dir", return_value=Path(tmp)):
                with self.assertRaises(nt.NotifyError):
                    nt.save_profile("x", "smtp.x.com", 587, "u", "pw",
                                    security="bogus")

    def test_settings_and_delete_clears_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(nt, "config_dir", return_value=Path(tmp)):
                nt.save_profile("p", "smtp.x.com", 587, "u", "pw")
                nt.set_settings(active="p", notify_to="you@x.com",
                                notify_jobs=True, notify_runs=False)
                s = nt.get_settings()
                self.assertEqual(s["active"], "p")
                self.assertEqual(s["notify_to"], "you@x.com")
                self.assertTrue(s["notify_jobs"])
                self.assertFalse(s["notify_runs"])
                nt.delete_profile("p")
                self.assertEqual(nt.get_settings()["active"], "")

    def test_send_email_uses_starttls_and_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(nt, "config_dir", return_value=Path(tmp)):
                cfg = {"host": "smtp.x.com", "port": 587, "security": "starttls",
                       "user": "u@x.com", "password": "pw",
                       "from_addr": "u@x.com"}
                with mock.patch.object(nt.smtplib, "SMTP", _FakeSMTP):
                    nt.send_email(cfg, "to@y.com", "Hi", "Body")
                srv = _FakeSMTP.instances[-1]
                self.assertTrue(srv.started_tls)
                self.assertEqual(srv.logged_in, ("u@x.com", "pw"))
                self.assertEqual(srv.sent[0]["To"], "to@y.com")
                self.assertEqual(srv.sent[0]["Subject"], "Hi")

    def test_send_notification_respects_toggle(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(nt, "config_dir", return_value=Path(tmp)):
                nt.save_profile("p", "smtp.x.com", 587, "u@x.com", "pw",
                                from_addr="u@x.com")
                nt.set_settings(active="p", notify_to="you@x.com",
                                notify_jobs=False, notify_runs=True)
                with mock.patch.object(nt.smtplib, "SMTP", _FakeSMTP):
                    # jobs disabled -> no send
                    self.assertFalse(nt.send_notification("s", "b", when="job"))
                    # runs enabled -> sends
                    self.assertTrue(nt.send_notification("s", "b", when="run"))
                self.assertEqual(len(_FakeSMTP.instances), 1)

    def test_cleanup_summary_gmail_note(self):
        subj, body = nt.cleanup_summary(
            "me@gmail.com", ["INBOX"], 12, dry_run=False, gmail=True)
        self.assertIn("12", subj)
        self.assertIn("Trash", body)
        self.assertIn("empty", body.lower())
        # non-gmail has no trash note
        _, body2 = nt.cleanup_summary(
            "me@x.com", ["INBOX"], 3, dry_run=False, gmail=False)
        self.assertNotIn("Trash", body2)


class BatchSenderTests(unittest.TestCase):
    """The reused-connection sender used by bulk unsubscribe (retry + rate limit)."""

    cfg = {"from_addr": "me@x.com", "host": "smtp.x.com", "port": 587,
           "security": "starttls", "user": "me@x.com", "password": "pw"}

    def _sender(self, behaviors):
        """A BatchSender whose every send_message follows `behaviors` in order.
        Each item is 'ok', a transient exc, or a permanent/rate exc."""
        import smtplib
        seq = iter(behaviors)
        servers = []

        class Srv:
            def __init__(self):
                servers.append(self)

            def send_message(self, msg):
                item = next(seq)
                if isinstance(item, Exception):
                    raise item

            def quit(self):
                pass

        s = nt.BatchSender(self.cfg, sleep=lambda _x: None)
        return s, Srv, servers

    def test_retry_then_success(self):
        import smtplib
        s, Srv, servers = self._sender(
            [smtplib.SMTPServerDisconnected("lost"), "ok"])
        with mock.patch.object(nt, "_server", side_effect=lambda cfg: Srv()):
            s.send("to@y.com", "s", "b")        # succeeds on the 2nd attempt
        self.assertEqual(len(servers), 2)       # reconnected once

    def test_persistent_rate_limit_raises(self):
        import smtplib
        rate = smtplib.SMTPResponseException(451, b"4.7.0 too many messages")
        s, Srv, _ = self._sender([rate, rate, rate])
        with mock.patch.object(nt, "_server", side_effect=lambda cfg: Srv()):
            with self.assertRaises(nt.RateLimitError):
                s.send("to@y.com", "s", "b")

    def test_permanent_error_is_not_rate_limit(self):
        import smtplib
        perm = smtplib.SMTPResponseException(550, b"mailbox unavailable")
        s, Srv, _ = self._sender([perm])
        with mock.patch.object(nt, "_server", side_effect=lambda cfg: Srv()):
            with self.assertRaises(nt.NotifyError) as ctx:
                s.send("to@y.com", "s", "b")
            self.assertNotIsInstance(ctx.exception, nt.RateLimitError)

    def test_connection_reused_across_sends(self):
        s, Srv, servers = self._sender(["ok", "ok", "ok"])
        with mock.patch.object(nt, "_server", side_effect=lambda cfg: Srv()):
            s.send("a@y.com", "s", "b")
            s.send("b@y.com", "s", "b")
            s.send("c@y.com", "s", "b")
        self.assertEqual(len(servers), 1)       # one connection for all three

    def test_cleanup_summary_wording_per_operation(self):
        # Move is worded "moved" (not "deleted") and shows the destination.
        subj, body = nt.cleanup_summary(
            "me@x.com", ["INBOX"], 5, dry_run=False, gmail=False,
            kind="Move", dest="Archive")
        self.assertIn("Move", subj)
        self.assertIn("moved", body)
        self.assertNotIn("deleted", body)
        self.assertIn("Destination: Archive", body)
        # Dry-run move says "would be moved".
        _, body_dry = nt.cleanup_summary(
            "me@x.com", ["INBOX"], 5, dry_run=True, gmail=False, kind="Move")
        self.assertIn("would be moved", body_dry)
        # A delete-style run still says "deleted".
        _, body_del = nt.cleanup_summary(
            "me@x.com", ["INBOX"], 5, dry_run=False, gmail=False, kind="Cleanup")
        self.assertIn("deleted", body_del)
        # A Move never carries the Gmail-Trash note.
        _, body_move_gmail = nt.cleanup_summary(
            "me@gmail.com", ["INBOX"], 5, dry_run=False, gmail=True, kind="Move")
        self.assertNotIn("Trash", body_move_gmail)


class SMTPOAuthTests(unittest.TestCase):
    """OAuth2 (XOAUTH2) SMTP profiles + sending."""

    def setUp(self):
        _FakeSMTP.instances = []

    def test_oauth_profile_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(nt, "config_dir", return_value=Path(tmp)):
                nt.save_oauth_profile("ms", "smtp-mail.outlook.com",
                                      "u@outlook.com", "REFRESH", "microsoft",
                                      from_addr="u@outlook.com")
                p = nt.list_profiles()[0]
                self.assertEqual(p["auth_method"], "oauth")
                self.assertEqual(p["provider"], "microsoft")
                loaded = nt.load_profile("ms")
                self.assertEqual(loaded["auth_method"], "oauth")
                self.assertEqual(loaded["refresh_token"], "REFRESH")
                self.assertEqual(loaded["password"], "")

    def test_oauth_requires_token_and_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(nt, "config_dir", return_value=Path(tmp)):
                with self.assertRaises(nt.NotifyError):
                    nt.save_oauth_profile("x", "h", "u", "", "microsoft")
                with self.assertRaises(nt.NotifyError):
                    nt.save_oauth_profile("x", "h", "u", "tok", "")

    def test_update_refresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(nt, "config_dir", return_value=Path(tmp)):
                nt.save_oauth_profile("ms", "h", "u", "OLD", "microsoft")
                nt.update_refresh_token("ms", "NEW")
                self.assertEqual(nt.load_profile("ms")["refresh_token"], "NEW")

    def test_failed_oauth_login_closes_connection(self):
        """A rejected XOAUTH2 auth must not leak the open socket (else repeated
        tests pile up connections and the provider starts throttling)."""
        from imap_cleanup_tool import oauth

        class _AuthFail(_FakeSMTP):
            def docmd(self, cmd, arg=None):
                self.docmds.append((cmd, arg))
                return (535, b"5.7.3 Authentication unsuccessful")

        cfg = {"host": "smtp-mail.outlook.com", "port": 587,
               "security": "starttls", "user": "u@outlook.com",
               "from_addr": "u@outlook.com", "auth_method": "oauth",
               "provider": "microsoft", "refresh_token": "RT", "encrypted": False}
        with mock.patch.object(nt.smtplib, "SMTP", _AuthFail):
            with mock.patch.object(oauth, "access_token_for", return_value="ATK"):
                with self.assertRaises(nt.NotifyError):
                    nt._server(cfg)
        self.assertTrue(_FakeSMTP.instances[-1].closed)   # socket was closed

    def test_send_uses_xoauth2(self):
        from imap_cleanup_tool import oauth
        cfg = {"host": "smtp-mail.outlook.com", "port": 587,
               "security": "starttls", "user": "u@outlook.com",
               "from_addr": "u@outlook.com", "auth_method": "oauth",
               "provider": "microsoft", "refresh_token": "RT", "encrypted": False}
        with mock.patch.object(nt.smtplib, "SMTP", _FakeSMTP):
            with mock.patch.object(oauth, "access_token_for", return_value="ATKN"):
                nt.send_email(cfg, "to@y.com", "Hi", "Body")
        srv = _FakeSMTP.instances[-1]
        self.assertIsNone(srv.logged_in)              # no password login
        self.assertTrue(srv.greeted)                  # EHLO sent before AUTH
        auth = next(c for c in srv.docmds if c[0] == "AUTH")
        self.assertTrue(auth[1].startswith("XOAUTH2 "))
        self.assertEqual(auth[1].split(" ", 1)[1],
                         oauth.xoauth2_b64("u@outlook.com", "ATKN"))
        self.assertEqual(srv.sent[0]["To"], "to@y.com")


class SendEmailRetryTests(unittest.TestCase):
    """Notifications must survive a transient SMTP failure (e.g. an Outlook read
    timeout on a scheduled run), but fail fast on an auth rejection."""

    cfg = {"host": "smtp-mail.outlook.com", "port": 587, "security": "starttls",
           "user": "u@outlook.com", "from_addr": "u@outlook.com"}

    def test_retries_transient_then_succeeds(self):
        srv = _FakeSMTP("h", 587)
        calls = {"n": 0}

        def fake_server(_cfg):
            calls["n"] += 1
            if calls["n"] == 1:
                raise nt.NotifyError("SMTP connection failed: Connection "
                                     "unexpectedly closed: The read operation timed out")
            return srv

        with mock.patch.object(nt, "_server", side_effect=fake_server):
            nt.send_email(self.cfg, "to@y.com", "Hi", "Body",
                          sleep=lambda _x: None)
        self.assertEqual(calls["n"], 2)          # retried once after the timeout
        self.assertEqual(len(srv.sent), 1)       # then delivered

    def test_does_not_retry_auth_failure(self):
        calls = {"n": 0}

        def fake_server(_cfg):
            calls["n"] += 1
            raise nt.NotifyError("SMTP OAuth login was rejected: "
                                 "5.7.3 Authentication unsuccessful")

        with mock.patch.object(nt, "_server", side_effect=fake_server):
            with self.assertRaises(nt.NotifyError):
                nt.send_email(self.cfg, "to@y.com", "Hi", "Body",
                              sleep=lambda _x: None)
        self.assertEqual(calls["n"], 1)          # failed fast, no retry


if __name__ == "__main__":
    unittest.main()

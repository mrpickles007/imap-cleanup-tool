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
        _FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

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


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the OAuth2 (XOAUTH2) helper module.

The network calls (device-code, token, refresh) go through one private function,
``oauth._post`` - so the whole flow is testable by patching that single seam.
"""

import base64
import unittest
from unittest import mock

from imap_cleanup_tool import oauth


class XOAuth2StringTests(unittest.TestCase):
    def test_bytes_and_b64(self):
        raw = oauth.xoauth2_bytes("u@x.com", "TOK")
        self.assertEqual(raw, b"user=u@x.com\x01auth=Bearer TOK\x01\x01")
        self.assertEqual(base64.b64decode(oauth.xoauth2_b64("u@x.com", "TOK")),
                         raw)


class ProviderConfigTests(unittest.TestCase):
    def test_microsoft_built_in(self):
        cfg = oauth.get_provider("microsoft")
        self.assertTrue(cfg["client_id"])
        self.assertIn("devicecode_endpoint", cfg)
        self.assertIn("offline_access", cfg["scope"])

    def test_unknown_provider_raises(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.get_provider("nope")

    def test_available_providers_flags_configured(self):
        provs = {p["name"]: p for p in oauth.available_providers()}
        self.assertTrue(provs["microsoft"]["configured"])
        # Google ships as a stub with a blank client id until credentials are set.
        if "google" in provs:
            self.assertFalse(provs["google"]["configured"])

    def test_unconfigured_provider_raises(self):
        with mock.patch.object(oauth, "load_providers",
                               return_value={"x": {"client_id": ""}}):
            with self.assertRaises(oauth.OAuthError):
                oauth.get_provider("x")


class DeviceCodeFlowTests(unittest.TestCase):
    def test_start_device_code_normalizes(self):
        resp = {"device_code": "DC", "user_code": "ABCD-EFGH",
                "verification_uri": "https://example/device", "interval": 7,
                "expires_in": 600, "message": "go"}
        # A provider with no pinned URL passes the API's values straight through.
        with mock.patch.object(oauth, "load_providers",
                  return_value={"p": {"client_id": "id", "devicecode_endpoint": "d",
                                      "token_endpoint": "t", "scope": "s"}}):
            with mock.patch.object(oauth, "_post", return_value=resp) as p:
                out = oauth.start_device_code("p")
        self.assertEqual(out["device_code"], "DC")
        self.assertEqual(out["user_code"], "ABCD-EFGH")
        self.assertEqual(out["verification_uri"], "https://example/device")
        self.assertEqual(out["interval"], 7)
        self.assertEqual(out["provider"], "p")
        self.assertTrue(p.called)

    def test_microsoft_pins_canonical_devicelogin(self):
        # Microsoft pins microsoft.com/devicelogin in place of whatever the API
        # returns (the newer login.microsoft.com/device portal is flaky), and drops
        # the API's complete-URL / message so callers show the pinned URL.
        resp = {"device_code": "DC", "user_code": "X",
                "verification_uri": "https://login.microsoft.com/device",
                "verification_uri_complete": "https://login.microsoft.com/device?code=X",
                "message": "open https://login.microsoft.com/device"}
        with mock.patch.object(oauth, "_post", return_value=resp):
            out = oauth.start_device_code("microsoft")
        self.assertEqual(out["verification_uri"], "https://microsoft.com/devicelogin")
        self.assertEqual(out["verification_uri_complete"], "")
        self.assertEqual(out["message"], "")

    def test_start_device_code_uses_verification_url_fallback(self):
        # Google returns 'verification_url' rather than 'verification_uri'.
        resp = {"device_code": "DC", "user_code": "X",
                "verification_url": "https://g/device"}
        with mock.patch.object(oauth, "load_providers",
                               return_value={"g": {"client_id": "id",
                                   "devicecode_endpoint": "d",
                                   "token_endpoint": "t", "scope": "s"}}):
            with mock.patch.object(oauth, "_post", return_value=resp):
                out = oauth.start_device_code("g")
        self.assertEqual(out["verification_uri"], "https://g/device")

    def test_start_device_code_error(self):
        with mock.patch.object(oauth, "_post",
                               return_value={"error": "bad",
                                             "error_description": "nope"}):
            with self.assertRaises(oauth.OAuthError):
                oauth.start_device_code("microsoft")

    def test_poll_pending_then_success(self):
        seq = [{"error": "authorization_pending"},
               {"error": "slow_down"},
               {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}]
        with mock.patch.object(oauth, "_post", side_effect=seq):
            tok = oauth.poll_device_code("DC", provider="microsoft", interval=1,
                                         sleep=lambda _s: None)
        self.assertEqual(tok["access_token"], "AT")
        self.assertEqual(tok["refresh_token"], "RT")

    def test_poll_declined_raises(self):
        with mock.patch.object(oauth, "_post",
                               return_value={"error": "authorization_declined",
                                             "error_description": "user said no"}):
            with self.assertRaises(oauth.OAuthError):
                oauth.poll_device_code("DC", provider="microsoft", interval=1,
                                       sleep=lambda _s: None)

    def test_poll_should_stop(self):
        with mock.patch.object(oauth, "_post",
                               return_value={"error": "authorization_pending"}):
            with self.assertRaises(oauth.OAuthError):
                oauth.poll_device_code("DC", provider="microsoft", interval=1,
                                       should_stop=lambda: True,
                                       sleep=lambda _s: None)


class RefreshTests(unittest.TestCase):
    def test_refresh_returns_token(self):
        with mock.patch.object(oauth, "_post",
                               return_value={"access_token": "AT2"}):
            self.assertEqual(
                oauth.refresh_access_token("RT", "microsoft")["access_token"],
                "AT2")

    def test_refresh_error(self):
        with mock.patch.object(oauth, "_post",
                               return_value={"error": "invalid_grant"}):
            with self.assertRaises(oauth.OAuthError):
                oauth.refresh_access_token("RT", "microsoft")

    def test_access_token_for_persists_rotation(self):
        profile = {"provider": "microsoft", "refresh_token": "OLD"}
        saved = []
        with mock.patch.object(oauth, "_post",
                               return_value={"access_token": "AT",
                                             "refresh_token": "NEW"}):
            token = oauth.access_token_for(profile, persist=saved.append)
        self.assertEqual(token, "AT")
        self.assertEqual(saved, ["NEW"])         # rotated token persisted

    def test_access_token_for_no_rotation(self):
        profile = {"provider": "microsoft", "refresh_token": "SAME"}
        saved = []
        with mock.patch.object(oauth, "_post",
                               return_value={"access_token": "AT",
                                             "refresh_token": "SAME"}):
            oauth.access_token_for(profile, persist=saved.append)
        self.assertEqual(saved, [])              # unchanged -> no persist call

    def test_access_token_for_missing_token(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.access_token_for({"provider": "microsoft", "refresh_token": ""})


class CliOAuthLoginTests(unittest.TestCase):
    def test_oauth_login_saves_profile(self):
        from imap_cleanup_tool import cli, profiles
        with mock.patch.object(oauth, "get_provider",
                  return_value={"client_id": "x",
                                "imap": {"host": "outlook.office365.com",
                                         "port": 993}}), \
             mock.patch.object(oauth, "start_device_code",
                  return_value={"message": "go", "verification_uri": "u",
                                "user_code": "C", "device_code": "DC",
                                "interval": 1, "expires_in": 10}), \
             mock.patch.object(oauth, "poll_device_code",
                  return_value={"refresh_token": "RT", "access_token": "AT"}), \
             mock.patch.object(profiles, "save_oauth_profile",
                  return_value="p") as save:
            rc = cli.main(["--oauth-login", "microsoft", "--user", "u@x.com",
                           "--oauth-profile", "p"])
        self.assertEqual(rc, 0)
        save.assert_called_once()
        args, kwargs = save.call_args
        self.assertIn("RT", args)                     # the refresh token is stored
        self.assertIn("microsoft", args)

    def test_oauth_login_no_refresh_token_fails(self):
        from imap_cleanup_tool import cli
        with mock.patch.object(oauth, "get_provider",
                  return_value={"client_id": "x",
                                "imap": {"host": "h", "port": 993}}), \
             mock.patch.object(oauth, "start_device_code",
                  return_value={"message": "go", "verification_uri": "u",
                                "user_code": "C", "device_code": "DC",
                                "interval": 1, "expires_in": 10}), \
             mock.patch.object(oauth, "poll_device_code",
                  return_value={"access_token": "AT"}):     # no refresh_token
            rc = cli.main(["--oauth-login", "microsoft", "--user", "u@x.com"])
        self.assertEqual(rc, 2)


class _Args:
    user = "me@x.com"
    host = "h"
    profile = "p"
    notify_profile = ""
    dry_run = True


class CliJobFailureNotifyTests(unittest.TestCase):
    def test_failure_emails_and_hints_reauth_in_job_mode(self):
        from imap_cleanup_tool import cli, notifications
        captured = {}

        def fake_send(subject, body, when=None, profile="", attachments=None):
            captured.update(subject=subject, body=body, when=when)
            return True

        with mock.patch.object(cli, "_NOTIFY_WHEN", "job"):
            with mock.patch.object(notifications, "send_notification",
                                   side_effect=fake_send):
                cli._notify_job_failure(_Args(), "OAuth sign-in failed: bad",
                                        oauth=True)
        self.assertEqual(captured.get("when"), "job")
        self.assertIn("FAILED", captured["subject"])
        self.assertIn("re-authenticate", captured["body"].lower())

    def test_failure_silent_in_interactive_run_mode(self):
        from imap_cleanup_tool import cli, notifications
        calls = []
        with mock.patch.object(cli, "_NOTIFY_WHEN", "run"):
            with mock.patch.object(notifications, "send_notification",
                                   side_effect=lambda *a, **k: calls.append(1)):
                cli._notify_job_failure(_Args(), "x", oauth=True)
        self.assertEqual(calls, [])     # no email when a human is at the keyboard

    def test_failure_email_best_effort_when_smtp_also_down(self):
        # IMAP+SMTP failing together: the email can't go out, but it must not raise
        # (so the job log still records everything and the job exits cleanly).
        from imap_cleanup_tool import cli, notifications
        with mock.patch.object(cli, "_NOTIFY_WHEN", "job"):
            with mock.patch.object(notifications, "send_notification",
                                   side_effect=RuntimeError("SMTP OAuth rejected")):
                cli._notify_job_failure(_Args(), "OAuth sign-in failed", oauth=True)
        # reaching here without an exception is the assertion


if __name__ == "__main__":
    unittest.main()

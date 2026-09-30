"""Non-ASCII credential/address handling (issues #3 and #4).

imaplib/smtplib encode command arguments as ASCII, so a non-ASCII password or
sender address used to crash. These tests cover the UTF-8 paths.
"""

import base64
import imaplib
import unittest

from imap_cleanup_tool import core
from imap_cleanup_tool import notifications as nt
from imap_cleanup_tool.rules import RuleError


class ImapLoginTests(unittest.TestCase):
    def test_ascii_login_uses_login(self):
        seen = []

        class C:
            capabilities = ("IMAP4REV1", "AUTH=PLAIN")

            def login(self, u, p):
                seen.append(("login", u, p))

            def authenticate(self, *a):
                seen.append(("auth",) + a)

        core._login(C(), "user", "pass")
        self.assertEqual(seen, [("login", "user", "pass")])

    def test_nonascii_password_uses_sasl_plain_utf8(self):
        sent = {}

        class C:
            capabilities = ("IMAP4REV1", "AUTH=PLAIN")

            def login(self, u, p):          # must NOT be used for non-ASCII
                raise AssertionError("LOGIN must not be sent")

            def authenticate(self, mech, obj):
                sent["mech"] = mech
                sent["auth"] = obj(b"")   # imaplib base64-encodes this

        core._login(C(), "user", "p\xe6ss")
        self.assertEqual(sent["mech"], "PLAIN")
        self.assertEqual(sent["auth"],
                         b"\x00user\x00" + "p\xe6ss".encode("utf-8"))

    def test_nonascii_without_auth_plain_gives_clear_error(self):
        class C:
            capabilities = ("IMAP4REV1", "AUTH=XOAUTH2")

            def authenticate(self, *a):
                raise AssertionError("must not try PLAIN")

        with self.assertRaises(imaplib.IMAP4.error) as cm:
            core._login(C(), "user", "p\xe6ss")
        self.assertIn("non-ASCII", str(cm.exception))


class ImapSearchFromTests(unittest.TestCase):
    def test_ascii_search_from(self):
        calls = []

        class C:
            literal = None

            def uid(self, *a):
                calls.append(a)
                return ("OK", [b"1 2"])

        r = core._search_from(C(), "a@b.com")
        self.assertEqual(r, ("OK", [b"1 2"]))
        self.assertEqual(calls, [("SEARCH", None, "FROM", '"a@b.com"')])

    def test_nonascii_search_from_uses_utf8_literal(self):
        addr = "danskh\xe5ndbold@x.dk"
        seen = []

        class C:
            literal = None

            def uid(self, *a):
                seen.append((a, self.literal))   # literal at send time
                return ("OK", [b"5"])

        c = C()
        r = core._search_from(c, addr)
        self.assertEqual(r, ("OK", [b"5"]))
        # a single UTF-8 search, the term sent as a literal ...
        self.assertEqual(seen, [(("SEARCH", "CHARSET", "UTF-8", "FROM"),
                                 addr.encode("utf-8"))])
        # ... and the literal is cleared afterwards (no leak into next command)
        self.assertIsNone(c.literal)

    def test_literal_cleared_even_on_error(self):
        class C:
            literal = None

            def uid(self, *a):
                raise imaplib.IMAP4.error("boom")

        c = C()
        with self.assertRaises(imaplib.IMAP4.error):
            core._search_from(c, "\xe5@x.dk")
        self.assertIsNone(c.literal)


class ExcludeFailClosedTests(unittest.TestCase):
    def test_failed_exclude_search_stops_instead_of_acting(self):
        class C:
            literal = None

            def uid(self, *a):
                return ("NO", [b"[BADCHARSET]"])

        with self.assertRaises(RuntimeError):
            core._apply_exclude(C(), [b"1", b"2"], {"keep@x.dk"})

    def test_exclude_drops_matches(self):
        class C:
            literal = None

            def uid(self, *a):
                return ("OK", [b"2"])

        self.assertEqual(core._apply_exclude(C(), [b"1", b"2"], {"k@x.dk"}),
                         [b"1"])


class RuleAndFolderTests(unittest.TestCase):
    def test_nonascii_rule_raises_rule_error(self):
        class C:
            def uid(self, *a):
                for x in a:
                    if isinstance(x, str):
                        x.encode("ascii")   # what imaplib does
                return ("OK", [b""])

        with self.assertRaises(RuleError):
            core.search_rule(C(), 'SUBJECT "p\xe5 tilbud"')

    def test_nonascii_new_folder_name_is_rejected_cleanly(self):
        class C:
            def create(self, *a):
                raise AssertionError("must not send")

        with self.assertRaises(imaplib.IMAP4.error):
            core.create_folder(C(), "Kvitteringer \xe6\xf8\xe5")


class SmtpLoginTests(unittest.TestCase):
    def _server(self, auth, replies):
        class S:
            esmtp_features = {"auth": auth}

            def __init__(self):
                self.cmds = []
                self.replies = list(replies)

            def login(self, u, p):
                raise AssertionError("smtplib login must not be used")

            def ehlo_or_helo_if_needed(self):
                pass

            def docmd(self, cmd, arg=""):
                self.cmds.append((cmd, arg))
                return self.replies.pop(0)

        return S()

    def test_ascii_smtp_login(self):
        class S:
            def login(self, u, p):
                self.creds = (u, p)

        s = S()
        nt._smtp_login(s, "user", "pass")
        self.assertEqual(s.creds, ("user", "pass"))

    def test_nonascii_uses_auth_plain_when_offered(self):
        s = self._server("CRAM-MD5 PLAIN LOGIN", [(235, b"ok")])
        nt._smtp_login(s, "user", "p\xe6ss")
        expect = base64.b64encode(
            b"\x00user\x00" + "p\xe6ss".encode("utf-8")).decode("ascii")
        self.assertEqual(s.cmds, [("AUTH", "PLAIN " + expect)])

    def test_nonascii_uses_auth_login_when_no_plain(self):   # e.g. Office 365
        s = self._server("LOGIN XOAUTH2",
                         [(334, b"VXNlcm5hbWU6"), (334, b"UGFzc3dvcmQ6"),
                          (235, b"ok")])
        nt._smtp_login(s, "user", "p\xe6ss")
        self.assertEqual(s.cmds[0], ("AUTH", "LOGIN"))
        self.assertEqual(s.cmds[1][0],
                         base64.b64encode(b"user").decode("ascii"))
        self.assertEqual(s.cmds[2][0],
                         base64.b64encode("p\xe6ss".encode("utf-8")).decode())

    def test_nonascii_rejected_password_is_an_error(self):
        s = self._server("PLAIN", [(535, b"5.7.8 bad credentials")])
        with self.assertRaises(nt.NotifyError) as cm:
            nt._smtp_login(s, "user", "p\xe6ss")
        self.assertIn("535", str(cm.exception))
        self.assertNotIn("b'", str(cm.exception))      # decoded, not bytes repr

    def test_nonascii_without_plain_or_login(self):
        s = self._server("XOAUTH2", [])
        with self.assertRaises(nt.NotifyError):
            nt._smtp_login(s, "user", "p\xe6ss")


if __name__ == "__main__":
    unittest.main()

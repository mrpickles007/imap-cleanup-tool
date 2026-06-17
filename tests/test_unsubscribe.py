"""Tests for List-Unsubscribe parsing + one-click POST."""

import unittest
from unittest import mock

from imap_cleanup_tool import unsubscribe as u


class ParseTests(unittest.TestCase):
    def test_mailto_only(self):
        r = u.parse_list_unsubscribe("<mailto:unsub@list.example?subject=stop>")
        self.assertEqual(r["mailto"], "mailto:unsub@list.example?subject=stop")
        self.assertIsNone(r["http"])
        self.assertFalse(r["oneclick"])

    def test_http_only_no_oneclick(self):
        r = u.parse_list_unsubscribe("<https://x.example/u?id=1>")
        self.assertEqual(r["http"], "https://x.example/u?id=1")
        self.assertIsNone(r["mailto"])
        self.assertFalse(r["oneclick"])

    def test_both_with_oneclick(self):
        r = u.parse_list_unsubscribe(
            "<mailto:u@x.example>, <https://x.example/u?id=1>",
            "List-Unsubscribe=One-Click")
        self.assertEqual(r["mailto"], "mailto:u@x.example")
        self.assertEqual(r["http"], "https://x.example/u?id=1")
        self.assertTrue(r["oneclick"])

    def test_oneclick_needs_http(self):
        # one-click post with only a mailto -> not a usable one-click
        r = u.parse_list_unsubscribe("<mailto:u@x.example>",
                                     "List-Unsubscribe=One-Click")
        self.assertFalse(r["oneclick"])

    def test_empty(self):
        r = u.parse_list_unsubscribe("")
        self.assertEqual(r, {"mailto": None, "http": None, "oneclick": False})

    def test_mime_encoded_word_mailto(self):
        # some senders MIME-encode the header (RFC 2047), hiding the <...> URIs;
        # we must decode it before parsing, or it looks like "no target" (rescan)
        enc = ("=?us-ascii?Q?=3Cmailto=3Aunsubscribe=40em=2Eexample=2Ecom"
               "=3Fsubject=3Dstop=3E?=")
        r = u.parse_list_unsubscribe(enc)
        self.assertEqual(r["mailto"],
                         "mailto:unsubscribe@em.example.com?subject=stop")

    def test_mime_encoded_word_https_oneclick(self):
        enc = "=?us-ascii?Q?=3Chttps=3A=2F=2Fx=2Eexample=2Fu=3Fid=3D1=3E?="
        r = u.parse_list_unsubscribe(enc, "List-Unsubscribe=One-Click")
        self.assertEqual(r["http"], "https://x.example/u?id=1")
        self.assertTrue(r["oneclick"])

    def test_parse_mailto(self):
        to, subj, body = u.parse_mailto(
            "mailto:unsub@list.example?subject=Unsubscribe%20me&body=stop")
        self.assertEqual(to, "unsub@list.example")
        self.assertEqual(subj, "Unsubscribe me")
        self.assertEqual(body, "stop")

    def test_parse_mailto_defaults(self):
        to, subj, body = u.parse_mailto("mailto:u@x.example")
        self.assertEqual(to, "u@x.example")
        self.assertEqual(subj, "unsubscribe")
        self.assertEqual(body, "unsubscribe")


class OneClickTests(unittest.TestCase):
    def test_post_success(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        opener = mock.MagicMock()
        opener.open.return_value = resp
        with mock.patch.object(u.urllib.request, "build_opener",
                               return_value=opener) as m:
            self.assertTrue(u.http_one_click("https://x.example/u"))
            # it POSTs the RFC 8058 body...
            req = opener.open.call_args[0][0]
            self.assertEqual(req.get_method(), "POST")
            self.assertEqual(req.data, b"List-Unsubscribe=One-Click")
            # ...through the POST-preserving redirect handler
            self.assertIs(m.call_args[0][0], u._RepostRedirect)

    def test_post_failure_is_false(self):
        opener = mock.MagicMock()
        opener.open.side_effect = OSError("boom")
        with mock.patch.object(u.urllib.request, "build_opener",
                               return_value=opener):
            self.assertFalse(u.http_one_click("https://x.example/u"))


class RedirectTests(unittest.TestCase):
    """The one-click POST must survive a redirect without losing its body."""

    def _req(self):
        return u.urllib.request.Request(
            "https://a.example/u", data=b"List-Unsubscribe=One-Click",
            method="POST", headers=dict(u._ONE_CLICK_HEADERS))

    def test_redirect_keeps_post_and_body(self):
        h = u._RepostRedirect()
        for code in (301, 302, 303, 307, 308):
            new = h.redirect_request(self._req(), None, code, "redir", {},
                                     "https://b.example/u2")
            self.assertIsNotNone(new, code)
            self.assertEqual(new.get_method(), "POST", code)
            self.assertEqual(new.data, b"List-Unsubscribe=One-Click", code)
            self.assertEqual(new.full_url, "https://b.example/u2", code)
            self.assertEqual(new.get_header("Content-type"),
                             "application/x-www-form-urlencoded", code)

    def test_redirect_refuses_non_http_scheme(self):
        h = u._RepostRedirect()
        self.assertIsNone(h.redirect_request(
            self._req(), None, 302, "redir", {}, "ftp://evil.example/x"))


if __name__ == "__main__":
    unittest.main()

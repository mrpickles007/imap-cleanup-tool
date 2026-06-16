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
        with mock.patch.object(u.urllib.request, "urlopen", return_value=resp) as m:
            self.assertTrue(u.http_one_click("https://x.example/u"))
            # it POSTs the RFC 8058 body
            req = m.call_args[0][0]
            self.assertEqual(req.get_method(), "POST")
            self.assertEqual(req.data, b"List-Unsubscribe=One-Click")

    def test_post_failure_is_false(self):
        with mock.patch.object(u.urllib.request, "urlopen",
                               side_effect=OSError("boom")):
            self.assertFalse(u.http_one_click("https://x.example/u"))


if __name__ == "__main__":
    unittest.main()

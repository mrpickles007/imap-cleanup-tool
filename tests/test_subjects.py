"""Sender subjects preview (issue #2): headers only, never bodies."""

import imaplib
import unittest

from imap_cleanup_tool import core

try:
    import httpx  # noqa: F401  (required by fastapi TestClient)
    from fastapi.testclient import TestClient

    from imap_cleanup_tool import webapp
    _HAVE_WEB = True
except Exception:  # pragma: no cover - depends on optional deps
    _HAVE_WEB = False


class FakeConn:
    """Minimal IMAP stand-in: one folder, two messages from the sender."""

    literal = None

    def __init__(self, found=b"1 2"):
        self.found = found
        self.calls = []

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return ("OK", [b"2"])

    def uid(self, cmd, *args):
        self.calls.append((cmd,) + args)
        if cmd == "SEARCH":
            return ("OK", [self.found])
        if cmd == "FETCH":
            # real servers answer in ASCENDING UID order
            return ("OK", [
                (b"1 (UID 1 BODY[HEADER.FIELDS (SUBJECT DATE)] {12}",
                 b"Date: Sun, 04 Jan 2026 09:00:00 +0000\r\n\r\n"),
                b")",
                (b"2 (UID 2 BODY[HEADER.FIELDS (SUBJECT DATE)] {70}",
                 b"Subject: =?utf-8?q?Caf=C3=A9_deal?=\r\n"
                 b"Date: Mon, 05 Jan 2026 10:00:00 +0000\r\n\r\n"),
                b")",
            ])
        return ("NO", [])


class RawUtf8Conn(FakeConn):
    """Spam-style message with RAW 8-bit UTF-8 headers (no RFC 2047)."""

    def uid(self, cmd, *args):
        if cmd == "FETCH":
            return ("OK", [
                (b"1 (UID 7 BODY[HEADER.FIELDS (SUBJECT DATE)] {60}",
                 "Subject: Tilbud på håndbold\r\n"
                 "Date: lør, 03 jan 2026\r\n\r\n".encode("utf-8")),
                b")",
            ])
        return super().uid(cmd, *args)


class BrokenConn(FakeConn):
    def select(self, mailbox, readonly=False):
        raise imaplib.IMAP4.abort("socket error: connection reset")


class ListSenderSubjectsTests(unittest.TestCase):
    def test_subjects_decoded_and_headers_only(self):
        conn = FakeConn()
        items = core.list_sender_subjects(conn, "news@shop.com", ["INBOX"])
        self.assertEqual(len(items), 2)
        # newest (UID 2) first even though the server answered ascending
        self.assertEqual(items[0]["subject"], "Caf\xe9 deal")   # RFC 2047 decoded
        self.assertEqual(items[1]["subject"], "(no subject)")
        self.assertEqual(items[0]["folder"], "INBOX")
        # read-only select, and only header fields are fetched (BODY.PEEK)
        self.assertIn(("select", '"INBOX"', True), conn.calls)
        fetch = [c for c in conn.calls if c[0] == "FETCH"][0]
        self.assertIn("BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)]", fetch[-1])

    def test_no_messages(self):
        items = core.list_sender_subjects(FakeConn(found=b""), "x@y.com",
                                          ["INBOX"])
        self.assertEqual(items, [])

    def test_cap(self):
        items = core.list_sender_subjects(FakeConn(), "x@y.com", ["INBOX"],
                                          cap=1)
        self.assertEqual(len(items), 1)

    def test_raw_utf8_headers_do_not_crash_or_garble(self):
        items = core.list_sender_subjects(RawUtf8Conn(), "x@y.com", ["INBOX"])
        self.assertEqual(items[0]["subject"], "Tilbud på håndbold")
        self.assertIsInstance(items[0]["date"], str)

    def test_honors_stop(self):
        with self.assertRaises(core.StopRequested):
            core.list_sender_subjects(FakeConn(), "x@y.com", ["INBOX"],
                                      should_stop=lambda: True)


@unittest.skipUnless(_HAVE_WEB, "web extra (fastapi + httpx) not installed")
class SenderSubjectsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(webapp.create_app())

    def test_endpoint_returns_subjects(self):
        sess = webapp.Session("sid-subj", FakeConn(), "imap.x.com", 993,
                              "u@x.com")
        webapp._SESSIONS["sid-subj"] = sess
        try:
            r = self.client.post("/api/sender-subjects",
                                 json={"sid": "sid-subj",
                                       "sender": "news@shop.com",
                                       "folders": ["INBOX"]})
            self.assertEqual(r.status_code, 200)
            d = r.json()
            self.assertEqual(d["count"], 2)
            self.assertEqual(d["items"][0]["subject"], "Caf\xe9 deal")
        finally:
            webapp._SESSIONS.pop("sid-subj", None)

    def test_endpoint_without_session(self):
        r = self.client.post("/api/sender-subjects",
                             json={"sid": "nope", "sender": "a@b.com"})
        self.assertEqual(r.status_code, 440)

    def _with_session(self, conn, payload):
        sess = webapp.Session("sid-subj2", conn, "imap.x.com", 993, "u@x.com")
        webapp._SESSIONS["sid-subj2"] = sess
        try:
            return self.client.post("/api/sender-subjects",
                                    json=dict(payload, sid="sid-subj2"))
        finally:
            webapp._SESSIONS.pop("sid-subj2", None)

    def test_endpoint_rejects_empty_or_crlf_sender(self):
        for bad in ("", "   ", "a@b.com\r\nA1 DELETE INBOX"):
            r = self._with_session(FakeConn(), {"sender": bad})
            self.assertEqual(r.status_code, 400, bad)

    def test_endpoint_imap_error_is_502_not_500(self):
        r = self._with_session(BrokenConn(), {"sender": "a@b.com"})
        self.assertEqual(r.status_code, 502)


if __name__ == "__main__":
    unittest.main()

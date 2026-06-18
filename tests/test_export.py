"""Tests for the export/import-messages feature (mbox round-trip + IMAP ops)."""

import unittest

from imap_cleanup_tool import core

RAW1 = b"From: a@x.com\r\nSubject: Hello\r\n\r\nBody one\r\n"
RAW2 = b"From: b@y.com\r\nSubject: World\r\n\r\nBody two\r\n"


class FakeConn:
    """Minimal IMAP stand-in for export/import: SEARCH, FETCH, APPEND, SELECT."""

    def __init__(self):
        self.calls = []
        self.appended = []

    def select(self, name, readonly=False):
        self.calls.append(("SELECT", name, readonly))
        return ("OK", [b"3"])

    def uid(self, *args):
        self.calls.append(args)
        if args[0] == "SEARCH":
            return ("OK", [b"1 2 3"])
        if args[0] == "FETCH":
            return ("OK", [(b"1 (BODY[] {7}", RAW1), b")",
                            (b"2 (BODY[] {7}", RAW2), b")"])
        return ("OK", [b"1"])

    def append(self, folder, flags, date, message):
        self.appended.append((folder, message))
        return ("OK", [b"[APPENDUID 1 9] APPEND completed"])


class MboxRoundTripTests(unittest.TestCase):
    def test_build_then_read_preserves_messages(self):
        blob = core.build_mbox([RAW1, RAW2])
        self.assertTrue(blob.lstrip().startswith(b"From "))   # real mbox
        back = core.read_messages(blob)
        self.assertEqual(len(back), 2)
        self.assertIn(b"Subject: Hello", back[0])
        self.assertIn(b"Body one", back[0])
        self.assertIn(b"Subject: World", back[1])

    def test_read_single_eml_without_from_separator(self):
        # an uploaded single .eml (no mbox "From " line) is treated as one message
        self.assertEqual(core.read_messages(RAW1), [RAW1])

    def test_read_empty(self):
        self.assertEqual(core.read_messages(b""), [])


class FetchTests(unittest.TestCase):
    def test_fetch_uses_body_peek_and_returns_raw(self):
        conn = FakeConn()
        out = core.fetch_messages(conn, [b"1", b"2"])
        self.assertEqual(out, [RAW1, RAW2])
        fetches = [c for c in conn.calls if c[0] == "FETCH"]
        self.assertTrue(fetches and "BODY.PEEK[]" in fetches[0][2])


class AppendTests(unittest.TestCase):
    def test_append_counts_and_targets_folder(self):
        conn = FakeConn()
        n = core.append_messages(conn, "Archive", [RAW1, RAW2])
        self.assertEqual(n, 2)
        self.assertEqual(len(conn.appended), 2)
        self.assertIn("Archive", conn.appended[0][0])   # quoted mailbox name
        self.assertEqual(conn.appended[0][1], RAW1)


class MatchedUidsTests(unittest.TestCase):
    def test_no_criteria_no_match_all_returns_empty(self):
        conn = FakeConn()
        self.assertEqual(core.matched_uids(conn, "INBOX"), [])

    def test_no_criteria_match_all_returns_every_uid(self):
        conn = FakeConn()
        uids = core.matched_uids(conn, "INBOX", match_all_if_empty=True)
        self.assertEqual(uids, [b"1", b"2", b"3"])

    def test_selects_readonly(self):
        conn = FakeConn()
        core.matched_uids(conn, "INBOX", match_all_if_empty=True)
        sel = [c for c in conn.calls if c[0] == "SELECT"]
        self.assertTrue(sel and sel[0][2] is True)   # readonly=True


if __name__ == "__main__":
    unittest.main()

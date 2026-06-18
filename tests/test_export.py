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


class ExistingMessageIdsTests(unittest.TestCase):
    def test_collects_message_ids(self):
        class C:
            def select(self, n, readonly=False):
                return ("OK", [b"2"])
            def uid(self, *a):
                if a[0] == "SEARCH":
                    return ("OK", [b"1 2"])
                if a[0] == "FETCH":
                    return ("OK", [(b"1 (UID 1 BODY[] {0}", b"Message-ID: <a@x>\r\n\r\n"),
                                   b")",
                                   (b"2 (UID 2 BODY[] {0}", b"Message-ID: <b@x>\r\n\r\n"),
                                   b")"])
                return ("OK", [b""])
        ids = core.existing_message_ids(C(), "INBOX")   # no cache -> fetch all
        self.assertEqual(ids, {"<a@x>", "<b@x>"})


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


_MID1 = b"Message-ID: <m1@x>\r\nFrom: a@x.com\r\nSubject: 1\r\n\r\nbody\r\n"
_MID2 = b"Message-ID: <m2@x>\r\nFrom: b@y.com\r\nSubject: 2\r\n\r\nbody\r\n"


class AppendTests(unittest.TestCase):
    def test_append_counts_and_targets_folder(self):
        conn = FakeConn()
        appended, skipped = core.append_messages(conn, "Archive", [RAW1, RAW2])
        self.assertEqual((appended, skipped), (2, 0))
        self.assertEqual(len(conn.appended), 2)
        self.assertIn("Archive", conn.appended[0][0])   # quoted mailbox name
        self.assertEqual(conn.appended[0][1], RAW1)

    def test_message_id_extraction(self):
        self.assertEqual(core._message_id(_MID1), "<m1@x>")
        self.assertEqual(core._message_id(b"From: a@x.com\r\n\r\nx"), "")

    def test_append_skips_duplicates_by_message_id(self):
        conn = FakeConn()
        appended, skipped = core.append_messages(
            conn, "Archive", [_MID1, _MID2], skip_ids={"<m1@x>"})
        self.assertEqual((appended, skipped), (1, 1))   # m1 in folder -> skipped
        self.assertEqual(conn.appended[0][1], _MID2)

    def test_append_collapses_in_file_duplicates(self):
        conn = FakeConn()
        appended, skipped = core.append_messages(
            conn, "Archive", [_MID1, _MID1, _MID2])
        self.assertEqual((appended, skipped), (2, 1))   # 2nd copy of m1 collapsed


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

    def test_exclude_drops_matching_senders(self):
        # SendersFake: SEARCH ALL -> 1,2,3; a FROM search -> uid 1. So excluding
        # one address removes uid 1 from the whole-folder match.
        uids = core.matched_uids(SendersFake(), "INBOX",
                                 match_all_if_empty=True, exclude={"me@x.com"})
        self.assertEqual(uids, [b"2", b"3"])


_FROM = {b"1": b"From: a@x.com\r\n\r\n",
         b"2": b"From: b@y.com\r\n\r\n",
         b"3": b"From: a@x.com\r\n\r\n"}


class SendersFake:
    """Fake conn for list_senders: SEARCH ALL = 3 msgs; a filter SEARCH = uid 1."""

    def select(self, name, readonly=False):
        return ("OK", [b"3"])

    def uid(self, *args):
        if args[0] == "SEARCH":
            return ("OK", [b"1 2 3"]) if args[2] == "ALL" else ("OK", [b"1"])
        if args[0] == "FETCH":
            data = []
            for u in args[1].split(b","):
                if u in _FROM:
                    data.append((u + b" (UID " + u + b" {0}", _FROM[u]))
                    data.append(b")")
            return ("OK", data)
        return ("OK", [b""])


class ListSendersFilterTests(unittest.TestCase):
    def test_no_filter_counts_all(self):
        counts = core.list_senders(SendersFake(), "INBOX")
        self.assertEqual(counts, {"a@x.com": 2, "b@y.com": 1})

    def test_rule_restricts_to_matching(self):
        counts = core.list_senders(SendersFake(), "INBOX",
                                   search_argument="FROM a@x.com")
        self.assertEqual(counts, {"a@x.com": 1})   # only uid 1 (the rule's match)

    def test_full_mode_filters_while_counting(self):
        counts = core.list_senders(SendersFake(), "INBOX",
                                   addresses={"a@x.com"}, scan_mode="full")
        self.assertEqual(counts, {"a@x.com": 2})   # b@y.com filtered out


if __name__ == "__main__":
    unittest.main()

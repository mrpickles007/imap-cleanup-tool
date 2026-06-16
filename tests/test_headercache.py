"""Tests for the local header cache (speeds up repeat AI reports)."""

import tempfile
import unittest
from pathlib import Path

from imap_cleanup_tool import core
from imap_cleanup_tool.headercache import HeaderCache


class CachingConn:
    """Fake IMAP that honours FETCH UID subsets and counts header fetches."""

    def __init__(self, messages, uidvalidity="100"):
        self.messages = messages          # list of {from,date,subject,seen,...}
        self.uidvalidity = uidvalidity
        self.header_fetched = 0           # messages whose FULL headers were fetched
        self.flag_fetched = 0             # messages whose flags-only were fetched

    def select(self, name, readonly=False):
        return ("OK", [str(len(self.messages)).encode()])

    def response(self, name):
        if name == "UIDVALIDITY":
            return ("UIDVALIDITY", [self.uidvalidity.encode()])
        return (name, [None])

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            ids = " ".join(str(i + 1) for i in range(len(self.messages)))
            return ("OK", [ids.encode() if ids else None])
        if cmd == "FETCH":
            wanted = [int(x) for x in args[0].split(b",") if x]
            flags_only = "(UID FLAGS)" in args[1]
            out = []
            for uid in wanted:
                m = self.messages[uid - 1]
                flags = b"(\\Seen)" if m.get("seen") else b"()"
                if flags_only:
                    self.flag_fetched += 1
                    out.append(("%d (UID %d FLAGS " % (uid, uid)).encode()
                               + flags + b")")
                else:
                    self.header_fetched += 1
                    meta = (("%d (UID %d FLAGS " % (uid, uid)).encode()
                            + flags + b" BODY[] {1})")
                    hdr = "From: %s\r\nDate: %s\r\nSubject: %s\r\n" % (
                        m.get("from", ""), m.get("date", ""), m.get("subject", ""))
                    out.append((meta, hdr.encode()))
            return ("OK", out)
        return ("OK", [b""])


MSGS = [
    {"from": "a@x.com", "date": "Mon, 1 Jan 2025 00:00:00 +0000", "subject": "1"},
    {"from": "a@x.com", "date": "Mon, 2 Jan 2025 00:00:00 +0000", "subject": "2"},
    {"from": "b@y.com", "date": "Mon, 3 Jan 2025 00:00:00 +0000", "subject": "3"},
]


class HeaderCacheStoreTests(unittest.TestCase):
    def test_put_get_roundtrip_and_purge(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = HeaderCache(Path(tmp) / "hc.sqlite")
            c.put("me", "INBOX", "100", {
                "1": {"sender": "a@x.com", "date_h": "d", "unsub": True,
                      "bulk": False, "subject": "hi"}})
            got = c.get("me", "INBOX", "100", ["1", "2"])
            self.assertIn("1", got)
            self.assertNotIn("2", got)            # only what was stored
            self.assertEqual(got["1"]["sender"], "a@x.com")
            self.assertTrue(got["1"]["unsub"])
            # a different uidvalidity sees nothing, and purge_other drops the old
            self.assertEqual(c.get("me", "INBOX", "200", ["1"]), {})
            c.purge_other("me", "INBOX", "200")
            self.assertEqual(c.get("me", "INBOX", "100", ["1"]), {})

    def test_has_account_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = HeaderCache(Path(tmp) / "hc.sqlite")
            self.assertFalse(c.has_account("me"))
            c.put("me", "INBOX", "100",
                  {"1": {"sender": "a", "date_h": "d", "subject": "s"}})
            self.assertTrue(c.has_account("me"))
            self.assertFalse(c.has_account("other"))
            c.clear("me")
            self.assertFalse(c.has_account("me"))


class ReportCacheTests(unittest.TestCase):
    def test_second_report_uses_cache_only_new_uids_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = HeaderCache(Path(tmp) / "hc.sqlite")
            conn = CachingConn(list(MSGS))

            # 1st run: cold cache -> all 3 headers fetched.
            core.build_ai_report(conn, ["INBOX"], threshold=0, cache=cache,
                                 account="me")
            self.assertEqual(conn.header_fetched, 3)

            # 2nd run: warm cache -> 0 header fetches, flags re-read for all 3.
            conn.header_fetched = 0
            conn.flag_fetched = 0
            rep = core.build_ai_report(conn, ["INBOX"], threshold=0, cache=cache,
                                       account="me")
            self.assertEqual(conn.header_fetched, 0)
            self.assertEqual(conn.flag_fetched, 3)
            self.assertEqual(rep["total_senders"], 2)     # a@x.com, b@y.com

            # 3rd run with one new message -> only the new header is fetched.
            conn.messages.append(
                {"from": "c@z.com", "date": "Mon, 4 Jan 2025 00:00:00 +0000",
                 "subject": "4"})
            conn.header_fetched = 0
            core.build_ai_report(conn, ["INBOX"], threshold=0, cache=cache,
                                 account="me")
            self.assertEqual(conn.header_fetched, 1)

    def test_changed_uidvalidity_refetches_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = HeaderCache(Path(tmp) / "hc.sqlite")
            conn = CachingConn(list(MSGS), uidvalidity="100")
            core.build_ai_report(conn, ["INBOX"], threshold=0, cache=cache,
                                 account="me")
            # Server renumbered -> new UIDVALIDITY -> cold again.
            conn.uidvalidity = "999"
            conn.header_fetched = 0
            core.build_ai_report(conn, ["INBOX"], threshold=0, cache=cache,
                                 account="me")
            self.assertEqual(conn.header_fetched, 3)

    def test_no_cache_is_unchanged(self):
        conn = CachingConn(list(MSGS))
        rep = core.build_ai_report(conn, ["INBOX"], threshold=0)   # cache=None
        self.assertEqual(conn.header_fetched, 3)
        self.assertEqual(rep["total_senders"], 2)


if __name__ == "__main__":
    unittest.main()

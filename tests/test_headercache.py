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
        self.from_fetched = 0             # messages whose From-only header was fetched

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
            fields = args[1]
            flags_only = "(UID FLAGS)" in fields
            from_only = ("(FROM)]" in fields) and ("SUBJECT" not in fields)
            out = []
            for uid in wanted:
                m = self.messages[uid - 1]
                flags = b"(\\Seen)" if m.get("seen") else b"()"
                if flags_only:
                    self.flag_fetched += 1
                    out.append(("%d (UID %d FLAGS " % (uid, uid)).encode()
                               + flags + b")")
                elif from_only:
                    self.from_fetched += 1
                    meta = ("%d (UID %d BODY[] {1})" % (uid, uid)).encode()
                    out.append((meta, ("From: %s\r\n" % m.get("from", "")).encode()))
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


class FromCacheTests(unittest.TestCase):
    """list-senders / full-scan share the same cache for the From header."""

    def test_list_senders_second_run_uses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = HeaderCache(Path(tmp) / "hc.sqlite")
            conn = CachingConn(list(MSGS))
            c1 = core.list_senders(conn, "INBOX", account="me", cache=cache)
            self.assertEqual(conn.from_fetched, 3)
            self.assertEqual(c1["a@x.com"], 2)
            conn.from_fetched = 0
            c2 = core.list_senders(conn, "INBOX", account="me", cache=cache)
            self.assertEqual(conn.from_fetched, 0)        # all from cache
            self.assertEqual(c2, c1)

    def test_ai_report_then_list_senders_shares_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = HeaderCache(Path(tmp) / "hc.sqlite")
            conn = CachingConn(list(MSGS))
            # An AI report populates full rows (incl. raw From) ...
            core.build_ai_report(conn, ["INBOX"], threshold=0, cache=cache,
                                 account="me")
            self.assertEqual(conn.header_fetched, 3)
            # ... so list-senders fetches no From headers at all.
            conn.from_fetched = 0
            core.list_senders(conn, "INBOX", account="me", cache=cache)
            self.assertEqual(conn.from_fetched, 0)

    def test_full_scan_match_uses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = HeaderCache(Path(tmp) / "hc.sqlite")
            conn = CachingConn(list(MSGS))
            n1 = core.process_folder(conn, "INBOX", addresses={"a@x.com"},
                                     scan_mode="full", dry_run=True,
                                     cache=cache, account="me")
            self.assertEqual(n1, 2)
            self.assertEqual(conn.from_fetched, 3)        # scanned all 3
            conn.from_fetched = 0
            n2 = core.process_folder(conn, "INBOX", addresses={"a@x.com"},
                                     scan_mode="full", dry_run=True,
                                     cache=cache, account="me")
            self.assertEqual(n2, 2)
            self.assertEqual(conn.from_fetched, 0)        # all from cache


if __name__ == "__main__":
    unittest.main()

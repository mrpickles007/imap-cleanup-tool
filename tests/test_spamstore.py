"""Unit tests for the per-account spam-address store."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import spamstore as ss


def _report(senders):
    return {"senders": senders}


def _sender(addr, score, flagged=True, verdict=None):
    s = {"sender": addr, "score": score, "flagged": flagged, "count": 5,
         "unread": 4, "unread_ratio": 0.8, "per_week": 3,
         "list_unsubscribe": True, "bulk": True, "sender_pattern": False}
    if verdict is not None:
        s["verdict"] = verdict
    return s


class SpamStoreTests(unittest.TestCase):
    def test_record_only_flagged_and_per_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "config_dir", return_value=Path(tmp)):
                rep = _report([_sender("spam@x.com", 9),
                               _sender("ok@x.com", 2, flagged=False)])
                n = ss.record_from_report("me@gmail.com", rep, "report")
                self.assertEqual(n, 1)
                lst = ss.list_addresses("me@gmail.com")
                self.assertEqual(lst["total"], 1)
                self.assertEqual(lst["items"][0]["address"], "spam@x.com")
                # a different account has its own (empty) list
                self.assertEqual(ss.list_addresses("other@x.com")["total"], 0)

    def test_verdict_stored_and_preserved_on_reupsert(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "config_dir", return_value=Path(tmp)):
                ss.record_from_report("a@x.com", _report([
                    _sender("s@x.com", 8, verdict={"delete": True,
                            "reason": "newsletter", "confidence": 0.9})]), "run")
                item = ss.list_addresses("a@x.com")["items"][0]
                self.assertTrue(item["verdict_delete"])
                self.assertEqual(item["verdict_reason"], "newsletter")
                # a later heuristic-only report keeps the existing verdict
                ss.record_from_report("a@x.com",
                                      _report([_sender("s@x.com", 8.5)]), "report")
                item = ss.list_addresses("a@x.com")["items"][0]
                self.assertTrue(item["verdict_delete"])
                self.assertEqual(item["score"], 8.5)        # score updated

    def test_pagination_and_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "config_dir", return_value=Path(tmp)):
                ss.record_from_report("a@x.com", _report(
                    [_sender(f"s{i}@shop.com", 9 - i*0.1) for i in range(30)]),
                    "report")
                page = ss.list_addresses("a@x.com", offset=0, limit=10)
                self.assertEqual(page["total"], 30)
                self.assertEqual(len(page["items"]), 10)
                self.assertEqual(page["items"][0]["address"], "s0@shop.com")  # top score
                page2 = ss.list_addresses("a@x.com", offset=10, limit=10)
                self.assertEqual(page2["items"][0]["address"], "s10@shop.com")
                found = ss.list_addresses("a@x.com", search="s1")
                self.assertTrue(all("s1" in it["address"] for it in found["items"]))

    def test_delete_and_select_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "config_dir", return_value=Path(tmp)):
                ss.record_from_report("a@x.com", _report(
                    [_sender("x@a.com", 9), _sender("y@a.com", 8)]), "report")
                self.assertEqual(set(ss.all_addresses("a@x.com")),
                                 {"x@a.com", "y@a.com"})
                self.assertEqual(ss.delete_addresses("a@x.com", ["x@a.com"]), 1)
                self.assertEqual(ss.all_addresses("a@x.com"), ["y@a.com"])


if __name__ == "__main__":
    unittest.main()

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

    def test_no_duplicate_by_address_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "config_dir", return_value=Path(tmp)):
                # same sender, different letter case + a second run -> still 1 row
                ss.record_from_report("a@x.com",
                                      _report([_sender("Spam@Shop.com", 9.0)]),
                                      "report")
                ss.record_from_report("a@x.com",
                                      _report([_sender("spam@shop.com", 8.0)]),
                                      "run")
                ss.record_from_report("a@x.com",
                                      _report([_sender("SPAM@SHOP.COM", 7.0)]),
                                      "report")
                lst = ss.list_addresses("a@x.com")
                self.assertEqual(lst["total"], 1)              # not re-inserted
                self.assertEqual(lst["items"][0]["address"], "spam@shop.com")
                self.assertEqual(lst["items"][0]["score"], 7.0)  # updated in place
                # a different address IS a separate row
                ss.record_from_report("a@x.com",
                                      _report([_sender("other@shop.com", 6.0)]),
                                      "report")
                self.assertEqual(ss.list_addresses("a@x.com")["total"], 2)

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

    def test_sorting_over_whole_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "config_dir", return_value=Path(tmp)):
                ss.record_from_report("a@x.com", _report([
                    _sender("low@x.com", 6.0), _sender("mid@x.com", 7.0),
                    _sender("high@x.com", 9.0)]), "report")
                # default: score desc
                top = ss.list_addresses("a@x.com")["items"][0]["address"]
                self.assertEqual(top, "high@x.com")
                # score asc
                asc = ss.list_addresses("a@x.com", sort_by="score",
                                        sort_dir="asc")
                self.assertEqual(asc["items"][0]["address"], "low@x.com")
                self.assertEqual(asc["sort_dir"], "asc")
                # sort spans the whole list, not just a page
                p = ss.list_addresses("a@x.com", sort_by="score", sort_dir="asc",
                                      limit=1, offset=0)
                self.assertEqual(p["items"][0]["address"], "low@x.com")
                p2 = ss.list_addresses("a@x.com", sort_by="score", sort_dir="asc",
                                       limit=1, offset=2)
                self.assertEqual(p2["items"][0]["address"], "high@x.com")
                # unknown sort key falls back to score
                self.assertEqual(
                    ss.list_addresses("a@x.com", sort_by="bogus")["sort_by"],
                    "score")

    def test_delete_and_select_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "config_dir", return_value=Path(tmp)):
                ss.record_from_report("a@x.com", _report(
                    [_sender("x@a.com", 9), _sender("y@a.com", 8)]), "report")
                self.assertEqual(set(ss.all_addresses("a@x.com")),
                                 {"x@a.com", "y@a.com"})
                self.assertEqual(ss.delete_addresses("a@x.com", ["x@a.com"]), 1)
                self.assertEqual(ss.all_addresses("a@x.com"), ["y@a.com"])

    def test_add_address_manual_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "config_dir", return_value=Path(tmp)):
                self.assertEqual(ss.count("a@x.com"), 0)
                self.assertTrue(ss.add_address("a@x.com", "New@Shop.com", 10))
                self.assertEqual(ss.count("a@x.com"), 1)
                item = ss.list_addresses("a@x.com")["items"][0]
                self.assertEqual(item["address"], "new@shop.com")  # lowercased
                self.assertEqual(item["score"], 10)
                # invalid address rejected
                self.assertFalse(ss.add_address("a@x.com", "not-an-email"))
                # re-adding updates the score, no duplicate
                ss.add_address("a@x.com", "new@shop.com", 4)
                self.assertEqual(ss.count("a@x.com"), 1)
                self.assertEqual(
                    ss.list_addresses("a@x.com")["items"][0]["score"], 4)

    def test_unsubscribe_fields_stored_and_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "config_dir", return_value=Path(tmp)):
                s1 = _sender("auto@x.com", 9)
                s1["unsub_mailto"] = "mailto:u@x.com?subject=stop"
                s1["unsub_http"] = None; s1["unsub_oneclick"] = False
                s2 = _sender("oneclick@x.com", 8)
                s2["unsub_mailto"] = None
                s2["unsub_http"] = "https://x.com/u?id=1"; s2["unsub_oneclick"] = True
                s3 = _sender("manual@x.com", 7)
                s3["unsub_mailto"] = None
                s3["unsub_http"] = "https://x.com/page"; s3["unsub_oneclick"] = False
                s4 = _sender("none@x.com", 6)  # no unsubscribe at all
                ss.record_from_report("a@x.com",
                                      _report([s1, s2, s3, s4]), "report")
                rows = {it["address"]: it for it in
                        ss.list_addresses("a@x.com", limit=50)["items"]}
                self.assertTrue(rows["auto@x.com"]["unsub_auto"])        # mailto
                self.assertTrue(rows["oneclick@x.com"]["unsub_auto"])    # one-click
                self.assertFalse(rows["manual@x.com"]["unsub_auto"])     # plain link
                self.assertEqual(rows["manual@x.com"]["unsub_url"],
                                 "https://x.com/page")
                self.assertFalse(rows["none@x.com"]["unsub_can"])
                # unsub_kind drives the UI badge (email / oneclick / link / "")
                self.assertEqual(rows["auto@x.com"]["unsub_kind"], "email")
                self.assertEqual(rows["oneclick@x.com"]["unsub_kind"], "oneclick")
                self.assertEqual(rows["manual@x.com"]["unsub_kind"], "link")
                self.assertEqual(rows["none@x.com"]["unsub_kind"], "")
                # only the mailto sender needs SMTP
                self.assertEqual(ss.count_unsub_email("a@x.com"), 1)
                # targets returns only rows with a method
                t = {x["address"]: x for x in ss.unsub_targets(
                    "a@x.com", ["auto@x.com", "manual@x.com", "none@x.com"])}
                self.assertIn("auto@x.com", t)
                self.assertIn("manual@x.com", t)
                self.assertNotIn("none@x.com", t)
                self.assertEqual(t["auto@x.com"]["mailto"],
                                 "mailto:u@x.com?subject=stop")

    def test_unsub_capability_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "config_dir", return_value=Path(tmp)):
                s1 = _sender("auto@x.com", 9)
                s1["unsub_mailto"] = "mailto:u@x.com"
                s1["unsub_http"] = None; s1["unsub_oneclick"] = False
                s2 = _sender("oneclick@x.com", 8)
                s2["unsub_mailto"] = None
                s2["unsub_http"] = "https://x.com/u"; s2["unsub_oneclick"] = True
                s3 = _sender("manual@x.com", 7)
                s3["unsub_mailto"] = None
                s3["unsub_http"] = "https://x.com/page"; s3["unsub_oneclick"] = False
                s4 = _sender("none@x.com", 6)  # no List-Unsubscribe at all
                ss.record_from_report("a@x.com",
                                      _report([s1, s2, s3, s4]), "report")

                def addrs(unsub):
                    return {it["address"] for it in ss.list_addresses(
                        "a@x.com", limit=50, unsub=unsub)["items"]}

                self.assertEqual(addrs("all"),
                                 {"auto@x.com", "oneclick@x.com",
                                  "manual@x.com", "none@x.com"})
                self.assertEqual(addrs("auto"),
                                 {"auto@x.com", "oneclick@x.com"})
                self.assertEqual(addrs("manual"), {"manual@x.com"})
                self.assertEqual(addrs("none"), {"none@x.com"})
                # totals reflect the filter (not just the page)
                self.assertEqual(
                    ss.list_addresses("a@x.com", unsub="manual")["total"], 1)
                # unknown filter behaves like "all"
                self.assertEqual(len(addrs("bogus")), 4)

    def test_mark_unsubscribed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "config_dir", return_value=Path(tmp)):
                s1 = _sender("oc@x.com", 8)
                s1["unsub_mailto"] = None
                s1["unsub_http"] = "https://x.com/u"; s1["unsub_oneclick"] = True
                ss.record_from_report("a@x.com", _report([s1]), "report")
                item = ss.list_addresses("a@x.com")["items"][0]
                self.assertIsNone(item["unsub_done_at"])     # not done yet
                # record an unsubscribe outcome
                self.assertTrue(ss.mark_unsubscribed(
                    "a@x.com", "OC@x.com", "oneclick",
                    "one-click confirmed", "2026-06-17T10:00:00"))
                item = ss.list_addresses("a@x.com")["items"][0]
                self.assertEqual(item["unsub_done_at"], "2026-06-17T10:00:00")
                self.assertEqual(item["unsub_done_method"], "oneclick")
                self.assertEqual(item["unsub_done_result"], "one-click confirmed")
                # unknown address updates nothing
                self.assertFalse(ss.mark_unsubscribed(
                    "a@x.com", "nope@x.com", "email", "x"))

    def test_addresses_by_score_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "config_dir", return_value=Path(tmp)):
                ss.record_from_report("a@x.com", _report([
                    _sender("hi@a.com", 9.0), _sender("mid@a.com", 6.0),
                    _sender("lo@a.com", 3.0)]), "report")
                self.assertEqual(
                    set(ss.addresses_by_score("a@x.com", "ge", 6)),
                    {"hi@a.com", "mid@a.com"})
                self.assertEqual(ss.addresses_by_score("a@x.com", "gt", 6),
                                 ["hi@a.com"])
                self.assertEqual(ss.addresses_by_score("a@x.com", "is", 6.0),
                                 ["mid@a.com"])
                self.assertEqual(set(ss.addresses_by_score("a@x.com", "le", 6)),
                                 {"mid@a.com", "lo@a.com"})
                self.assertEqual(ss.addresses_by_score("a@x.com", "lt", 6),
                                 ["lo@a.com"])
                self.assertEqual(ss.addresses_by_score("a@x.com", "bogus", 6), [])


if __name__ == "__main__":
    unittest.main()

"""Tests for the local heuristic AI-cleanup report (no LLM, no network)."""

import unittest

from imap_cleanup_tool import ai, core


class FakeAIConn:
    """Fake IMAP returning FETCH tuples with headers + FLAGS for build_ai_report."""

    def __init__(self, messages):
        self.messages = messages   # list of dicts: from,date,subject,seen,unsub,bulk

    def select(self, name, readonly=False):
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            ids = " ".join(str(i + 1) for i in range(len(self.messages)))
            return ("OK", [ids.encode() if ids else None])
        if cmd == "FETCH":
            out = []
            for i, m in enumerate(self.messages, start=1):
                flags = b"(\\Seen)" if m.get("seen") else b"()"
                meta = ("%d (UID %d FLAGS " % (i, i)).encode() + flags + b" BODY[] {1})"
                hdr = "From: %s\r\nDate: %s\r\nSubject: %s\r\n" % (
                    m.get("from", ""), m.get("date", ""), m.get("subject", ""))
                if m.get("unsub"):
                    hdr += "List-Unsubscribe: <mailto:u@x.com>\r\n"
                if m.get("bulk"):
                    hdr += "Precedence: bulk\r\n"
                out.append((meta, hdr.encode()))
            return ("OK", out)
        return ("OK", [b""])


class AiReportTests(unittest.TestCase):
    def _report(self, msgs, **kw):
        return core.build_ai_report(FakeAIConn(msgs), ["INBOX"], **kw)

    def test_newsletter_flagged_friend_not(self):
        msgs = [
            {"from": "newsletter@shop.com", "date": "Mon, 1 Jan 2024 10:00:00 +0000",
             "subject": "Sale", "seen": False, "unsub": True, "bulk": True},
            {"from": "newsletter@shop.com", "date": "Mon, 8 Jan 2024 10:00:00 +0000",
             "subject": "Sale 2", "seen": False, "unsub": True, "bulk": True},
            {"from": "alice@friends.com", "date": "Mon, 1 Jan 2024 10:00:00 +0000",
             "subject": "hi", "seen": True},
        ]
        rep = self._report(msgs, threshold=6, sample_size=5)
        by = {s["sender"]: s for s in rep["senders"]}
        self.assertGreaterEqual(by["newsletter@shop.com"]["score"], 6)
        self.assertTrue(by["newsletter@shop.com"]["flagged"])
        self.assertLess(by["alice@friends.com"]["score"], 6)
        self.assertFalse(by["alice@friends.com"]["flagged"])
        self.assertEqual(rep["flagged_count"], 1)
        # samples only attached to flagged senders, capped at sample_size
        self.assertTrue(by["newsletter@shop.com"]["samples"])
        self.assertEqual(by["alice@friends.com"]["samples"], [])

    def test_exclusions_skip_sender(self):
        rep = self._report(
            [{"from": "a@b.com", "date": "", "subject": "x", "seen": False}],
            exclude={"a@b.com"})
        self.assertEqual(rep["total_senders"], 0)

    def test_weights_can_be_overridden(self):
        msgs = [{"from": "x@y.com", "date": "", "subject": "s", "seen": False,
                 "unsub": True}]
        # zero out every weight except list_unsubscribe -> score should be 10
        rep = self._report(msgs, threshold=6, weights={
            "list_unsubscribe": 1, "unread_ratio": 0, "bulk": 0,
            "sender_pattern": 0, "frequency": 0})
        self.assertEqual(rep["senders"][0]["score"], 10.0)


class LLMHelpersTests(unittest.TestCase):
    def test_parse_verdicts_plain(self):
        out = ai.parse_verdicts(
            '{"verdicts":[{"sender":"A@B.com","delete":true,"reason":"junk",'
            '"confidence":0.9},{"sender":"c@d.com","delete":false}]}')
        self.assertTrue(out["a@b.com"]["delete"])      # lowercased key
        self.assertFalse(out["c@d.com"]["delete"])

    def test_parse_verdicts_tolerates_fences_and_prose(self):
        out = ai.parse_verdicts(
            'Sure!\n```json\n{"verdicts":[{"sender":"x@y.com","delete":true}]}\n```')
        self.assertTrue(out["x@y.com"]["delete"])

    def test_parse_verdicts_bad_input(self):
        self.assertEqual(ai.parse_verdicts(""), {})
        self.assertEqual(ai.parse_verdicts("not json"), {})

    def test_build_messages_only_flagged(self):
        report = {"senders": [
            {"sender": "spam@x.com", "flagged": True, "count": 5,
             "unread_ratio": 1.0, "per_week": 3, "list_unsubscribe": True,
             "score": 9, "samples": [{"subject": "Sale"}]},
            {"sender": "friend@x.com", "flagged": False, "count": 1,
             "unread_ratio": 0, "per_week": 0.1, "list_unsubscribe": False,
             "score": 1, "samples": []},
        ]}
        system, user = ai.build_messages(report)
        self.assertIn("STRICT JSON", system)
        self.assertIn("spam@x.com", user)
        self.assertNotIn("friend@x.com", user)


if __name__ == "__main__":
    unittest.main()

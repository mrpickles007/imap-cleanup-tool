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


class ScopingConn(FakeAIConn):
    """Like FakeAIConn but honours SEARCH scope (FROM term / rule) and records it."""

    def __init__(self, messages):
        super().__init__(messages)
        self.searches = []                 # every SEARCH argument tuple seen

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.searches.append(args)
            # args: (None, "ALL") | (None, "FROM", '"term"') | (None, *rule_parts)
            if len(args) >= 2 and args[1] == "ALL":
                wanted = list(range(len(self.messages)))
            elif len(args) >= 3 and args[1] == "FROM":
                term = args[2].strip('"').lower()
                wanted = [i for i, m in enumerate(self.messages)
                          if term in m.get("from", "").lower()]
            else:                          # a compiled rule -> match SUBJECT term
                term = (args[-1] or "").strip('"').lower()
                wanted = [i for i, m in enumerate(self.messages)
                          if term in m.get("subject", "").lower()]
            ids = " ".join(str(i + 1) for i in wanted)
            return ("OK", [ids.encode() if ids else None])
        if cmd == "FETCH":
            wanted = {int(x) for x in args[0].split(b",") if x}
            status, out = super().uid(cmd, *args)
            return (status, [t for i, t in enumerate(out, start=1) if i in wanted])
        return super().uid(cmd, *args)


class AiReportTests(unittest.TestCase):
    def _report(self, msgs, **kw):
        return core.build_ai_report(FakeAIConn(msgs), ["INBOX"], **kw)

    def test_scope_whole_folder_when_no_filter(self):
        msgs = [{"from": "a@x.com", "date": "", "subject": "s", "seen": False},
                {"from": "b@y.com", "date": "", "subject": "s", "seen": False}]
        conn = ScopingConn(msgs)
        rep = core.build_ai_report(conn, ["INBOX"], threshold=0)
        self.assertEqual(rep["total_senders"], 2)
        self.assertEqual(conn.searches[0], (None, "ALL"))   # whole folder

    def test_scope_by_target_addresses(self):
        msgs = [{"from": "a@x.com", "date": "", "subject": "s", "seen": False},
                {"from": "b@y.com", "date": "", "subject": "s", "seen": False}]
        conn = ScopingConn(msgs)
        rep = core.build_ai_report(conn, ["INBOX"], threshold=0,
                                   addresses={"a@x.com"})
        self.assertEqual([s["sender"] for s in rep["senders"]], ["a@x.com"])
        self.assertNotIn((None, "ALL"), conn.searches)      # never scanned all

    def test_scope_by_rule(self):
        msgs = [{"from": "a@x.com", "date": "", "subject": "Invoice", "seen": False},
                {"from": "b@y.com", "date": "", "subject": "Sale", "seen": False}]
        conn = ScopingConn(msgs)
        rep = core.build_ai_report(conn, ["INBOX"], threshold=0,
                                   search_argument='SUBJECT "Invoice"')
        self.assertEqual([s["sender"] for s in rep["senders"]], ["a@x.com"])

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

    def test_report_counts_flagged_messages(self):
        msgs = [
            {"from": "n@shop.com", "date": "Mon, 1 Jan 2024 10:00:00 +0000",
             "subject": "Sale", "seen": False, "unsub": True, "bulk": True},
            {"from": "n@shop.com", "date": "Mon, 8 Jan 2024 10:00:00 +0000",
             "subject": "Sale2", "seen": False, "unsub": True, "bulk": True},
            {"from": "alice@friends.com", "date": "", "subject": "hi",
             "seen": True},
        ]
        rep = self._report(msgs, threshold=6)
        # both flagged-sender messages count; the friend's does not
        self.assertEqual(rep["flagged_messages"], 2)

    def test_report_csv_has_header_and_rows(self):
        msgs = [{"from": "n@shop.com", "date": "", "subject": "Sale, big!",
                 "seen": False, "unsub": True, "bulk": True}]
        rep = self._report(msgs, threshold=0)
        csv_text = core.ai_report_csv(rep)
        lines = csv_text.splitlines()
        self.assertIn("sender,score,flagged,messages", lines[0])
        self.assertIn("n@shop.com", csv_text)
        # the comma inside the subject must be quoted, not split into a column
        import csv as _csv
        rows = list(_csv.reader(lines))
        self.assertEqual(len(rows[0]), len(rows[1]))

    def test_mixed_tz_dates_do_not_crash(self):
        # One Date header tz-aware, one naive (no zone) - must not raise
        # "can't compare offset-naive and offset-aware datetimes".
        from imap_cleanup_tool.core import _per_week
        pw = _per_week(["Mon, 1 Jan 2024 10:00:00 +0000",
                        "Mon, 8 Jan 2024 10:00:00"], 2)
        self.assertGreater(pw, 0)

    def test_report_with_mixed_tz_dates(self):
        msgs = [
            {"from": "n@shop.com", "date": "Mon, 1 Jan 2024 10:00:00 +0000",
             "subject": "a", "seen": False, "unsub": True},
            {"from": "n@shop.com", "date": "Mon, 8 Jan 2024 10:00:00",
             "subject": "b", "seen": False, "unsub": True},
        ]
        rep = self._report(msgs, threshold=0)   # must complete without error
        self.assertEqual(rep["total_senders"], 1)

    def test_aggregates_across_multiple_folders(self):
        # whole-folder report over 2 folders aggregates per sender (the fake
        # returns the same message in each folder -> count doubles).
        msgs = [{"from": "a@x.com", "date": "", "subject": "s", "seen": False}]
        rep = core.build_ai_report(FakeAIConn(msgs), ["INBOX", "Archive"],
                                   threshold=0)
        by = {s["sender"]: s for s in rep["senders"]}
        self.assertEqual(by["a@x.com"]["count"], 2)

    def test_multi_folder_with_filter(self):
        msgs = [{"from": "a@x.com", "date": "", "subject": "s", "seen": False},
                {"from": "b@y.com", "date": "", "subject": "s", "seen": False}]
        conn = ScopingConn(msgs)
        rep = core.build_ai_report(conn, ["INBOX", "Archive"], threshold=0,
                                   addresses={"a@x.com"})
        by = {s["sender"]: s for s in rep["senders"]}
        self.assertEqual(list(by), ["a@x.com"])       # filter honored per folder
        self.assertEqual(by["a@x.com"]["count"], 2)   # matched in both folders

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

    def test_validate_verdicts_rejects_bad_json(self):
        with self.assertRaises(ValueError):
            ai.validate_verdicts("not json")

    def test_validate_verdicts_rejects_bad_schema(self):
        # "delete" must be a bool; a string that isn't bool-ish must fail.
        with self.assertRaises(ValueError):
            ai.validate_verdicts('{"verdicts":[{"sender":"a@b.com",'
                                 '"delete":"maybe"}]}')

    def test_validate_verdicts_ok(self):
        out = ai.validate_verdicts('{"verdicts":[{"sender":"A@B.com",'
                                   '"delete":true}]}')
        self.assertTrue(out["a@b.com"]["delete"])

    def test_evaluate_retries_then_succeeds(self):
        """evaluate() retries the model until it returns valid JSON."""
        replies = ["garbage", "still bad",
                   '{"verdicts":[{"sender":"x@y.com","delete":true}]}']

        class _Msg:
            def __init__(self, c): self.content = c

        class _Choice:
            def __init__(self, c): self.message = _Msg(c)

        class _Usage:
            prompt_tokens = 10
            completion_tokens = 5

        class _Resp:
            def __init__(self, c):
                self.choices = [_Choice(c)]
                self.usage = _Usage()

        calls = {"n": 0}

        def fake_completion(**kw):
            r = _Resp(replies[calls["n"]])
            calls["n"] += 1
            return r

        import sys
        import types
        fake = types.ModuleType("litellm")
        fake.completion = fake_completion
        sys.modules["litellm"] = fake
        try:
            report = {"senders": [{"sender": "x@y.com", "flagged": True,
                      "count": 1, "unread_ratio": 1.0, "per_week": 1,
                      "list_unsubscribe": True, "score": 9, "samples": []}]}
            ev = ai.evaluate(report, {"model": "test/m"}, max_retries=3)
            self.assertEqual(calls["n"], 3)
            self.assertTrue(ev["verdicts"]["x@y.com"]["delete"])
            self.assertEqual(ev["prompt_tokens"], 30)   # summed across attempts
        finally:
            del sys.modules["litellm"]

    def test_evaluate_batches_flagged_senders(self):
        """532-style large reports must be sent in batches, not one giant call."""
        import sys
        import types

        calls = {"n": 0, "batch_sizes": []}

        def fake_completion(**kw):
            # echo a verdict for each sender in this batch's user message
            calls["n"] += 1
            user = kw["messages"][1]["content"]
            import json as _j
            items = _j.loads(user.split("\n", 1)[1])
            calls["batch_sizes"].append(len(items))
            verdicts = [{"sender": it["sender"], "delete": True} for it in items]

            class _Resp:
                choices = [type("C", (), {"message": type(
                    "M", (), {"content": _j.dumps({"verdicts": verdicts})})()})()]
                usage = type("U", (), {"prompt_tokens": 1,
                                       "completion_tokens": 1})()
            return _Resp()

        fake = types.ModuleType("litellm")
        fake.completion = fake_completion
        sys.modules["litellm"] = fake
        try:
            senders = [{"sender": f"s{i}@x.com", "flagged": True, "count": 1,
                        "unread_ratio": 1.0, "per_week": 1,
                        "list_unsubscribe": True, "score": 9, "samples": []}
                       for i in range(5)]
            report = {"senders": senders}
            ev = ai.evaluate(report, {"model": "m"}, batch_size=2)
            self.assertEqual(calls["n"], 3)             # ceil(5/2) batches
            self.assertEqual(calls["batch_sizes"], [2, 2, 1])
            self.assertEqual(len(ev["verdicts"]), 5)    # all merged
        finally:
            del sys.modules["litellm"]

    def test_evaluate_records_cost_per_batch(self):
        import sys
        import types

        def fake_completion(**kw):
            import json as _j
            user = kw["messages"][1]["content"]
            items = _j.loads(user.split("\n", 1)[1])
            verdicts = [{"sender": it["sender"], "delete": False} for it in items]

            class _Resp:
                choices = [type("C", (), {"message": type(
                    "M", (), {"content": _j.dumps({"verdicts": verdicts})})()})()]
                usage = type("U", (), {"prompt_tokens": 1000,
                                       "completion_tokens": 500})()
            return _Resp()

        fake = types.ModuleType("litellm")
        fake.completion = fake_completion
        sys.modules["litellm"] = fake
        recorded = []
        try:
            senders = [{"sender": f"s{i}@x.com", "flagged": True, "count": 1,
                        "unread_ratio": 1.0, "per_week": 1,
                        "list_unsubscribe": True, "score": 9, "samples": []}
                       for i in range(3)]
            cfg = {"model": "m", "track_costs": True,
                   "cost_input": 0.15, "cost_output": 0.60}
            ev = ai.evaluate({"senders": senders}, cfg, batch_size=2,
                             record_cost=lambda p, c, co: recorded.append((p, c, co)))
            self.assertEqual(len(recorded), 2)            # ceil(3/2) batches
            # per-batch cost logged; total matches the sum
            self.assertAlmostEqual(sum(r[2] for r in recorded), ev["cost"], places=6)
            self.assertEqual(ev["prompt_tokens"], 2000)   # 2 batches * 1000
        finally:
            del sys.modules["litellm"]

    def test_evaluate_stops_between_batches(self):
        import sys
        import types
        from imap_cleanup_tool.core import StopRequested

        def fake_completion(**kw):
            import json as _j

            class _Resp:
                choices = [type("C", (), {"message": type(
                    "M", (), {"content": _j.dumps({"verdicts": []})})()})()]
                usage = None
            return _Resp()

        fake = types.ModuleType("litellm")
        fake.completion = fake_completion
        sys.modules["litellm"] = fake
        try:
            senders = [{"sender": f"s{i}@x.com", "flagged": True, "count": 1,
                        "unread_ratio": 1.0, "per_week": 1,
                        "list_unsubscribe": True, "score": 9, "samples": []}
                       for i in range(4)]
            with self.assertRaises(StopRequested):
                ai.evaluate({"senders": senders}, {"model": "m"}, batch_size=2,
                            should_stop=lambda: True)
        finally:
            del sys.modules["litellm"]

    def test_evaluate_raises_after_max_retries(self):
        class _Msg:
            content = "never valid"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]
            usage = None

        import sys
        import types
        fake = types.ModuleType("litellm")
        fake.completion = lambda **kw: _Resp()
        sys.modules["litellm"] = fake
        try:
            report = {"senders": [{"sender": "x@y.com", "flagged": True,
                      "count": 1, "unread_ratio": 1.0, "per_week": 1,
                      "list_unsubscribe": True, "score": 9, "samples": []}]}
            with self.assertRaises(RuntimeError):
                ai.evaluate(report, {"model": "test/m"}, max_retries=3)
        finally:
            del sys.modules["litellm"]

    def test_system_prompt_has_safeguards(self):
        system, _ = ai.build_messages({"senders": []})
        low = system.lower()
        for kw in ("order", "appointment", "medical", "security", "personal"):
            self.assertIn(kw, low)
        self.assertIn("STRICT JSON", system)

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


class CliWeightTests(unittest.TestCase):
    def test_parse_weights_ok(self):
        from imap_cleanup_tool.cli import _parse_ai_weights
        out = _parse_ai_weights(["unread_ratio=4", "bulk=2.5"])
        self.assertEqual(out, {"unread_ratio": 4.0, "bulk": 2.5})

    def test_parse_weights_empty(self):
        from imap_cleanup_tool.cli import _parse_ai_weights
        self.assertEqual(_parse_ai_weights([]), {})

    def test_parse_weights_bad_key(self):
        from imap_cleanup_tool.cli import _parse_ai_weights
        with self.assertRaises(SystemExit):
            _parse_ai_weights(["nope=1"])

    def test_parse_weights_bad_value(self):
        from imap_cleanup_tool.cli import _parse_ai_weights
        with self.assertRaises(SystemExit):
            _parse_ai_weights(["bulk=high"])

    def test_parse_weights_no_equals(self):
        from imap_cleanup_tool.cli import _parse_ai_weights
        with self.assertRaises(SystemExit):
            _parse_ai_weights(["bulk"])


if __name__ == "__main__":
    unittest.main()

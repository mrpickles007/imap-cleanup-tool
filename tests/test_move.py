"""Tests for the move-to-folder feature: core ops (with a fake IMAP) + CLI."""

import unittest

from imap_cleanup_tool import cli, core


class FakeConn:
    """A minimal stand-in for imaplib.IMAP4 recording the commands it gets."""

    def __init__(self, capabilities=()):
        self.capabilities = capabilities
        self.calls = []
        self.create_status = "OK"
        self.create_data = [b"done"]

    def uid(self, *args):
        self.calls.append(args)
        if args and args[0] == "SEARCH":
            return ("OK", [b"1 2 3"])      # pretend the folder has 3 messages
        return ("OK", [b"1"])

    def select(self, name, readonly=False):
        self.calls.append(("SELECT", name, readonly))
        return ("OK", [b"3"])

    def expunge(self):
        self.calls.append(("EXPUNGE",))
        return ("OK", [b""])

    def create(self, name):
        self.calls.append(("CREATE", name))
        return (self.create_status, self.create_data)

    def subscribe(self, name):
        self.calls.append(("SUBSCRIBE", name))
        return ("OK", [b""])

    def unsubscribe(self, name):
        self.calls.append(("UNSUBSCRIBE", name))
        return ("OK", [b""])

    def delete(self, name):
        self.calls.append(("DELETE", name))
        return ("OK", [b"deleted"])

    def list(self):
        return ("OK", [
            rb'(\HasNoChildren) "/" "INBOX"',
            rb'(\HasNoChildren \Trash) "/" "[Gmail]/Trash"',
            rb'(\HasNoChildren) "/" "Archive2025"',
        ])


class MoveUidsTests(unittest.TestCase):
    def test_uses_move_when_capability_present(self):
        conn = FakeConn(capabilities=("IMAP4REV1", "MOVE"))
        n = core.move_uids(conn, [b"1", b"2"], "Archive", batch_size=500)
        self.assertEqual(n, 2)
        cmds = [c[0] for c in conn.calls]
        self.assertIn("MOVE", cmds)
        self.assertNotIn("COPY", cmds)
        self.assertNotIn("EXPUNGE", cmds)   # MOVE needs no expunge

    def test_falls_back_to_copy_delete_expunge(self):
        conn = FakeConn(capabilities=("IMAP4REV1",))   # no MOVE
        n = core.move_uids(conn, [b"1", b"2"], "Archive", batch_size=500)
        self.assertEqual(n, 2)
        cmds = [c[0] for c in conn.calls]
        self.assertIn("COPY", cmds)
        self.assertIn("STORE", cmds)
        self.assertIn("EXPUNGE", cmds)


class FlagSpamTests(unittest.TestCase):
    def test_moves_one_per_sender(self):
        conn = FakeConn(capabilities=("IMAP4REV1", "MOVE"))
        moved, hit = core.flag_senders_as_spam(
            conn, "INBOX", {"a@x.com", "b@y.com"}, "[Gmail]/Spam", per_sender=1)
        self.assertEqual(hit, 2)        # both senders had mail
        self.assertEqual(moved, 2)      # one message each

    def test_per_sender_none_moves_all(self):
        conn = FakeConn(capabilities=("IMAP4REV1", "MOVE"))
        moved, hit = core.flag_senders_as_spam(
            conn, "INBOX", {"a@x.com"}, "Spam", per_sender=None)
        self.assertEqual(hit, 1)
        self.assertEqual(moved, 3)      # all three messages (SEARCH -> 1 2 3)

    def test_dry_run_moves_nothing(self):
        conn = FakeConn(capabilities=("IMAP4REV1", "MOVE"))
        moved, hit = core.flag_senders_as_spam(
            conn, "INBOX", {"a@x.com"}, "Spam", per_sender=1, dry_run=True)
        self.assertEqual((moved, hit), (1, 1))
        self.assertNotIn("MOVE", [c[0] for c in conn.calls])

    def test_special_folder_by_flag(self):
        conn = FakeConn()
        self.assertEqual(core.special_folder(conn, "\\Trash"), "[Gmail]/Trash")
        self.assertIsNone(core.special_folder(conn, "\\Junk"))


class StatefulSpamConn:
    """A stateful fake IMAP: tracks inbox/junk so the move-then-delete sequence
    can be verified (used to prove 'delete mode' keeps one msg per sender in Spam)."""

    def __init__(self, inbox):
        # inbox: {uid_bytes: sender_str}
        self.inbox = dict(inbox)
        self.junk = {}                 # uid -> sender, after a MOVE
        self.deleted = set()           # uids flagged \Deleted, removed on expunge
        self.capabilities = ("IMAP4REV1", "MOVE")

    def select(self, name, readonly=False):
        return ("OK", [str(len(self.inbox)).encode()])

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            if len(args) >= 3 and args[1] == "FROM":
                term = args[2].strip('"').lower()
                hits = [u for u, s in self.inbox.items() if term in s.lower()]
            else:                      # SEARCH ALL
                hits = list(self.inbox)
            return ("OK", [b" ".join(sorted(hits, key=int)) or None])
        if cmd == "MOVE":              # (ids, dest) -> inbox -> junk
            for u in args[0].split(b","):
                if u in self.inbox:
                    self.junk[u] = self.inbox.pop(u)
            return ("OK", [b""])
        if cmd == "STORE":             # flag \Deleted (or Gmail label)
            for u in args[0].split(b","):
                if u in self.inbox:
                    self.deleted.add(u)
            return ("OK", [b""])
        return ("OK", [b""])

    def expunge(self):
        for u in list(self.deleted):
            self.inbox.pop(u, None)
        self.deleted.clear()
        return ("OK", [b""])


class SpamFlagDeleteSequenceTests(unittest.TestCase):
    def test_delete_mode_keeps_one_per_sender_in_spam(self):
        conn = StatefulSpamConn({
            b"1": "a@x.com", b"2": "a@x.com", b"3": "a@x.com",  # 3 from a
            b"4": "b@y.com",                                    # 1 from b
        })
        addrs = {"a@x.com", "b@y.com"}
        # 1) flag-as-spam moves ONE newest message per sender to Junk
        moved, hit = core.flag_senders_as_spam(
            conn, "INBOX", addrs, "[Gmail]/Spam", per_sender=1)
        self.assertEqual((moved, hit), (2, 2))         # one per sender
        # 2) delete the rest (non-Gmail -> flag + expunge)
        deleted = core.process_folder(
            conn, "INBOX", addresses=addrs, dry_run=False, expunge=True,
            scan_mode="search")
        # every sender that had mail now has >= 1 message in Spam
        self.assertEqual(set(conn.junk.values()), {"a@x.com", "b@y.com"})
        self.assertEqual(len(conn.junk), 2)            # exactly one each
        self.assertEqual(deleted, 2)                   # the other two a@x msgs
        self.assertEqual(conn.inbox, {})               # inbox cleared


class CreateFolderTests(unittest.TestCase):
    def test_create_ok(self):
        conn = FakeConn()
        msg = core.create_folder(conn, "Receipts")
        self.assertIn("Created", msg)
        self.assertIn(("CREATE", '"Receipts"'), conn.calls)

    def test_already_exists_is_tolerated(self):
        conn = FakeConn()
        conn.create_status = "NO"
        conn.create_data = [b"[ALREADYEXISTS] Duplicate folder name Receipts"]
        msg = core.create_folder(conn, "Receipts")
        self.assertIn("already exists", msg)

    def test_empty_name_rejected(self):
        with self.assertRaises(core.imaplib.IMAP4.error):
            core.create_folder(FakeConn(), "   ")


class MoveAllTests(unittest.TestCase):
    """Move with no target list / rule moves every message (search ALL)."""

    def test_move_all_moves_every_message(self):
        conn = FakeConn(capabilities=("MOVE",))
        n = core.process_folder(conn, "INBOX", search_argument="ALL",
                                move=True, dest_folder="Archive", dry_run=False)
        self.assertEqual(n, 3)
        self.assertTrue(any(c[0] == "MOVE" for c in conn.calls))

    def test_move_all_dry_run_counts_without_moving(self):
        conn = FakeConn(capabilities=("MOVE",))
        n = core.process_folder(conn, "INBOX", search_argument="ALL",
                                move=True, dest_folder="Archive", dry_run=True)
        self.assertEqual(n, 3)
        self.assertFalse(any(c[0] == "MOVE" for c in conn.calls))

    def test_count_only_does_not_act(self):
        conn = FakeConn(capabilities=("MOVE",))
        n = core.process_folder(conn, "INBOX", search_argument="ALL",
                                count_only=True, dry_run=True)
        self.assertEqual(n, 3)
        self.assertFalse(any(c[0] in ("MOVE", "COPY", "STORE")
                             for c in conn.calls))

    def test_move_into_self_is_skipped(self):
        conn = FakeConn(capabilities=("MOVE",))
        n = core.process_folder(conn, "Archive", search_argument="ALL",
                                move=True, dest_folder="Archive", dry_run=False)
        self.assertEqual(n, 0)
        self.assertFalse(any(c[0] == "MOVE" for c in conn.calls))

    def test_same_mailbox_inbox_case_insensitive(self):
        self.assertTrue(core._same_mailbox("inbox", "INBOX"))
        self.assertTrue(core._same_mailbox("Archive", "Archive"))
        self.assertFalse(core._same_mailbox("Archive", "Other"))


class DeleteFolderTests(unittest.TestCase):
    def test_protected_set_detection(self):
        prot = core.protected_folder_names(FakeConn())
        self.assertIn("INBOX", prot)
        self.assertIn("[Gmail]/Trash", prot)        # has \Trash special-use flag
        self.assertNotIn("Archive2025", prot)

    def test_delete_normal_ok(self):
        conn = FakeConn()
        msg = core.delete_folder(conn, "Archive2025")
        self.assertIn("Deleted", msg)
        self.assertIn(("DELETE", '"Archive2025"'), conn.calls)
        # must deselect (park on INBOX, read-only) before deleting
        self.assertIn(("SELECT", "INBOX", True), conn.calls)

    def test_delete_protected_refused(self):
        with self.assertRaises(ValueError):
            core.delete_folder(FakeConn(), "[Gmail]/Trash")

    def test_delete_inbox_refused(self):
        with self.assertRaises(ValueError):
            core.delete_folder(FakeConn(), "INBOX")


class CliArgsTests(unittest.TestCase):
    def test_move_flags_parse(self):
        args = cli.parse_args(["--move", "--dest-folder", "Archive",
                               "--targets", "t.txt"])
        self.assertTrue(args.move)
        self.assertEqual(args.dest_folder, "Archive")

    def test_create_folder_flag_parses(self):
        args = cli.parse_args(["--create-folder", "Receipts"])
        self.assertEqual(args.create_folder, "Receipts")

    def test_delete_folder_flag_parses(self):
        args = cli.parse_args(["--delete-folder", "Receipts"])
        self.assertEqual(args.delete_folder, "Receipts")

    def test_ai_cleanup_flags_parse(self):
        args = cli.parse_args(["--ai-cleanup", "--ai-model", "gpt",
                               "--ai-threshold", "7", "--ai-sample", "8"])
        self.assertTrue(args.ai_cleanup)
        self.assertEqual(args.ai_model, "gpt")
        self.assertEqual(args.ai_threshold, 7.0)
        self.assertEqual(args.ai_sample, 8)


if __name__ == "__main__":
    unittest.main()

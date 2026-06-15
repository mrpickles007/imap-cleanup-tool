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


if __name__ == "__main__":
    unittest.main()

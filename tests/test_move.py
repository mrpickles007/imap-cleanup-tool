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
        return ("OK", [b"1"])

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

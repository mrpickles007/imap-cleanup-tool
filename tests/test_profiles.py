"""Unit tests for connection profiles (local SQLite, optional encryption)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import profiles

try:
    import cryptography  # noqa: F401
    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover - depends on the [web] extra
    _HAVE_CRYPTO = False


class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # profiles imports config_dir into its own namespace, so patch it there.
        self._patch = mock.patch.object(
            profiles, "config_dir", return_value=Path(self._tmp.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_plain_roundtrip(self):
        profiles.save_profile("p1", "imap.example.com", 993, "u@x.com", "pw1")
        listed = profiles.list_profiles()
        self.assertEqual([p["name"] for p in listed], ["p1"])
        self.assertFalse(listed[0]["encrypted"])
        loaded = profiles.load_profile("p1")
        self.assertEqual(loaded["password"], "pw1")
        self.assertEqual(loaded["host"], "imap.example.com")
        self.assertEqual(loaded["port"], 993)

    def test_upsert_replaces(self):
        profiles.save_profile("p", "h1", 993, "u", "a")
        profiles.save_profile("p", "h2", 143, "u2", "b")
        self.assertEqual(len(profiles.list_profiles()), 1)
        self.assertEqual(profiles.load_profile("p")["host"], "h2")

    def test_name_required(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.save_profile("  ", "h", 993, "u", "p")

    def test_delete_and_unknown(self):
        profiles.save_profile("p", "h", 993, "u", "p")
        profiles.delete_profile("p")
        self.assertEqual(profiles.list_profiles(), [])
        with self.assertRaises(profiles.ProfileError):
            profiles.load_profile("nope")

    @unittest.skipUnless(_HAVE_CRYPTO, "cryptography not installed")
    def test_encrypted_roundtrip(self):
        profiles.save_profile("enc", "h", 993, "u", "mypw",
                              encrypt=True, secret="master")
        self.assertTrue(profiles.list_profiles()[0]["encrypted"])
        self.assertEqual(
            profiles.load_profile("enc", "master")["password"], "mypw")
        with self.assertRaises(profiles.ProfileError):
            profiles.load_profile("enc", "wrong")
        with self.assertRaises(profiles.ProfileError):
            profiles.load_profile("enc")          # password required

    @unittest.skipUnless(_HAVE_CRYPTO, "cryptography not installed")
    def test_encrypt_requires_secret(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.save_profile("e", "h", 993, "u", "pw",
                                  encrypt=True, secret="")


if __name__ == "__main__":
    unittest.main()

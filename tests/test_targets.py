"""Unit tests for the target-file parsing and sender matching."""

import os
import tempfile
import unittest

from imap_cleanup_tool.targets import (
    load_targets, parse_targets_text, sender_matches,
)


class ParseTargetsTextTests(unittest.TestCase):
    def test_classifies_entries(self):
        addresses, domains, exact = parse_targets_text(
            "spam@example.com\n*@newsletter.com\nannoying.com\n"
            "mail.annoying.com\n# comment\n\n  UPPER@Example.COM  ")
        self.assertEqual(addresses, {"spam@example.com", "upper@example.com"})
        self.assertEqual(domains, {"annoying.com", "mail.annoying.com"})
        self.assertEqual(exact, {"newsletter.com"})

    def test_empty_text_raises(self):
        with self.assertRaises(ValueError):
            parse_targets_text("# only a comment\n\n")


class LoadTargetsTests(unittest.TestCase):
    def _write(self, text: str) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_classifies_addresses_and_domains(self):
        path = self._write(
            "spam@example.com\n"
            "*@newsletter.com\n"
            "annoying.com\n"
            "mail.annoying.com\n"
            "# a comment\n"
            "\n"
            "  UPPER@Example.COM  \n")
        addresses, domains, exact = load_targets(path)
        self.assertEqual(
            addresses, {"spam@example.com", "upper@example.com"})
        self.assertEqual(domains, {"annoying.com", "mail.annoying.com"})
        self.assertEqual(exact, {"newsletter.com"})

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_targets(os.path.join(tempfile.gettempdir(), "no_such_file.txt"))

    def test_empty_file_raises_valueerror(self):
        path = self._write("# only comments\n\n")
        with self.assertRaises(ValueError):
            load_targets(path)


class SenderMatchesTests(unittest.TestCase):
    def test_exact_address(self):
        self.assertTrue(
            sender_matches("a@b.com", {"a@b.com"}, set()))
        self.assertFalse(
            sender_matches("x@b.com", {"a@b.com"}, set()))

    def test_exact_domain(self):
        self.assertTrue(sender_matches("a@b.com", set(), {"b.com"}))
        self.assertFalse(sender_matches("a@sub.b.com", set(), {"b.com"}))

    def test_subdomain_only_when_enabled(self):
        self.assertFalse(
            sender_matches("a@sub.b.com", set(), {"b.com"},
                           include_subdomains=False))
        self.assertTrue(
            sender_matches("a@sub.b.com", set(), {"b.com"},
                           include_subdomains=True))

    def test_empty_sender(self):
        self.assertFalse(sender_matches("", {"a@b.com"}, {"b.com"}))

    def test_wildcard_exact_domain_never_matches_subdomain(self):
        # *@b.com -> exact_domains: matches the domain exactly but never a
        # subdomain, even when include_subdomains is on.
        self.assertTrue(sender_matches("a@b.com", set(), set(), {"b.com"}))
        self.assertFalse(
            sender_matches("a@sub.b.com", set(), set(), {"b.com"},
                           include_subdomains=True))


if __name__ == "__main__":
    unittest.main()

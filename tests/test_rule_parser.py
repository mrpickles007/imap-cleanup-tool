"""Unit tests for the text-expression -> rule tree parser."""

import unittest

from imap_cleanup_tool.rules import RuleError, compile_search
from imap_cleanup_tool.rule_parser import parse_rule_expression


def _search(expr: str) -> str:
    return compile_search(parse_rule_expression(expr))


class ParseTests(unittest.TestCase):
    def test_single_condition(self):
        self.assertEqual(
            _search("sender contains amazon.com"), 'FROM "amazon.com"')

    def test_or_expression(self):
        self.assertEqual(
            _search("sender contains a OR subject contains b"),
            'OR FROM "a" SUBJECT "b"')

    def test_and_expression(self):
        self.assertEqual(
            _search("sender contains a AND subject contains b"),
            'FROM "a" SUBJECT "b"')

    def test_parentheses_nesting(self):
        self.assertEqual(
            _search("sender contains amazon.com OR "
                    "(subject is Invoice AND date starts 2025-01-01)"),
            'OR FROM "amazon.com" SUBJECT "Invoice" SINCE 01-Jan-2025')

    def test_quoted_value_with_spaces(self):
        self.assertEqual(
            _search('subject is "Black Friday"'), 'SUBJECT "Black Friday"')

    def test_case_insensitive_operators(self):
        self.assertEqual(
            _search("sender contains a or subject contains b"),
            'OR FROM "a" SUBJECT "b"')


class ParseErrorTests(unittest.TestCase):
    def test_empty_expression(self):
        with self.assertRaises(RuleError):
            parse_rule_expression("   ")

    def test_unclosed_parenthesis(self):
        with self.assertRaises(RuleError):
            parse_rule_expression("(sender contains a")

    def test_unknown_field(self):
        with self.assertRaises(RuleError):
            parse_rule_expression("recipient contains a")

    def test_unknown_operator(self):
        with self.assertRaises(RuleError):
            parse_rule_expression("sender matches a")

    def test_missing_value(self):
        with self.assertRaises(RuleError):
            parse_rule_expression("sender contains")

    def test_trailing_garbage(self):
        with self.assertRaises(RuleError):
            parse_rule_expression("sender contains a b c AND")


if __name__ == "__main__":
    unittest.main()

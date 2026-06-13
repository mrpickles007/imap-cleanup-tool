"""Unit tests for the rule tree -> IMAP SEARCH compiler."""

import unittest

from imap_cleanup_tool.rules import (
    Condition, Group, RuleError, compile_search, node_from_dict,
)
from imap_cleanup_tool.rule_parser import parse_rule_expression


class ConditionTests(unittest.TestCase):
    def test_sender_contains(self):
        cond = Condition("sender", "contains", "amazon.com")
        self.assertEqual(cond.to_imap(), ["FROM", '"amazon.com"'])

    def test_subject_is(self):
        cond = Condition("subject", "is", "Invoice")
        self.assertEqual(cond.to_imap(), ["SUBJECT", '"Invoice"'])

    def test_date_operators_and_format(self):
        self.assertEqual(
            Condition("date", "starts", "2025-01-01").to_imap(),
            ["SINCE", "01-Jan-2025"])
        self.assertEqual(
            Condition("date", "ends", "2025-12-31").to_imap(),
            ["BEFORE", "31-Dec-2025"])
        self.assertEqual(
            Condition("date", "is", "2025-06-13").to_imap(),
            ["ON", "13-Jun-2025"])

    def test_value_is_quoted_and_escaped(self):
        cond = Condition("subject", "contains", 'say "hi"')
        self.assertEqual(cond.to_imap(), ["SUBJECT", '"say \\"hi\\""'])

    def test_invalid_field(self):
        with self.assertRaises(RuleError):
            Condition("nope", "is", "x").validate()

    def test_invalid_operator_for_field(self):
        with self.assertRaises(RuleError):
            Condition("sender", "starts", "x").validate()

    def test_empty_value(self):
        with self.assertRaises(RuleError):
            Condition("sender", "is", "   ").validate()

    def test_invalid_date(self):
        with self.assertRaises(RuleError):
            Condition("date", "is", "not-a-date").to_imap()


class GroupTests(unittest.TestCase):
    def test_and_concatenates(self):
        group = Group("AND", [
            Condition("sender", "contains", "a"),
            Condition("subject", "contains", "b"),
        ])
        self.assertEqual(
            compile_search(group), 'FROM "a" SUBJECT "b"')

    def test_or_folds_prefix(self):
        group = Group("OR", [
            Condition("sender", "contains", "a"),
            Condition("sender", "contains", "b"),
        ])
        self.assertEqual(
            compile_search(group), 'OR FROM "a" FROM "b"')

    def test_nested_and_within_or(self):
        group = Group("OR", [
            Condition("sender", "contains", "amazon.com"),
            Group("AND", [
                Condition("subject", "is", "Invoice"),
                Condition("date", "starts", "2025-01-01"),
            ]),
        ])
        self.assertEqual(
            compile_search(group),
            'OR FROM "amazon.com" SUBJECT "Invoice" SINCE 01-Jan-2025')

    def test_empty_group_invalid(self):
        with self.assertRaises(RuleError):
            Group("AND", []).validate()

    def test_invalid_group_operator(self):
        with self.assertRaises(RuleError):
            Group("XOR", [Condition("sender", "is", "a")]).validate()


class SerializationTests(unittest.TestCase):
    def test_roundtrip_to_dict_and_back(self):
        original = Group("OR", [
            Condition("sender", "contains", "a"),
            Condition("subject", "is", "b"),
        ])
        rebuilt = node_from_dict(original.to_dict())
        self.assertEqual(compile_search(rebuilt), compile_search(original))

    def test_unknown_node_type(self):
        with self.assertRaises(RuleError):
            node_from_dict({"type": "bogus"})


class ExpressionTests(unittest.TestCase):
    """The text rendering used to feed scheduled jobs' --rule must re-parse."""

    def _assert_roundtrip(self, node):
        expr = node.to_expression()
        reparsed = parse_rule_expression(expr)
        self.assertEqual(compile_search(reparsed), compile_search(node))

    def test_simple_condition(self):
        self._assert_roundtrip(Condition("sender", "contains", "amazon.com"))

    def test_value_with_spaces_is_quoted(self):
        node = Condition("subject", "is", "Black Friday")
        self.assertEqual(node.to_expression(), 'subject is "Black Friday"')
        self._assert_roundtrip(node)

    def test_nested_group_roundtrip(self):
        node = Group("OR", [
            Condition("sender", "contains", "amazon.com"),
            Group("AND", [
                Condition("subject", "is", "Black Friday"),
                Condition("date", "starts", "2025-01-01"),
            ]),
        ])
        self._assert_roundtrip(node)


if __name__ == "__main__":
    unittest.main()

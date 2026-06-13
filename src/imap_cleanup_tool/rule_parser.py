"""Parse a human rule expression into a rules.py tree.

Grammar (case-insensitive AND/OR, parentheses for nesting)::

    expr     := term (("AND" | "OR") term)*
    term     := "(" expr ")" | condition
    condition:= FIELD OPERATOR VALUE

Where FIELD in {sender, subject, date}, OPERATOR in
{is, contains, starts, ends}, and VALUE is the rest of the token run until the
next AND/OR/parenthesis (quotes optional). Examples::

    sender contains amazon.com
    sender contains amazon.com OR subject contains fattura
    (sender is noreply@x.com OR sender is info@y.com) AND date starts 2025-01-01

Mixing AND and OR at the same level is left-associative; use parentheses to
control grouping explicitly.
"""

from __future__ import annotations

import re

from .rules import Condition, Group, RuleError

_FIELDS = {"sender", "subject", "date"}
_OPS = {"is", "contains", "starts", "ends"}
# Match: a parenthesis, OR a quoted string, OR a run of chars that are not
# whitespace or parentheses. This keeps "(" and ")" as standalone tokens even
# when written next to a value, e.g. "info@y.com)".
_TOKEN = re.compile(r'[()]|"[^"]*"|[^\s()]+')


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text)


class _Parser:
    """Recursive-descent parser over a flat token list."""
    # pylint: disable=too-few-public-methods

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> str:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self):
        """Parse the full token list into a tree, ensuring all tokens used."""
        node = self._parse_expr()
        if self.pos != len(self.tokens):
            raise RuleError(f"Unexpected token: {self._peek()!r}")
        return node

    def _parse_expr(self):
        nodes = [self._parse_term()]
        ops: list[str] = []
        while True:
            nxt = self._peek()
            if nxt and nxt.upper() in ("AND", "OR"):
                ops.append(self._next().upper())
                nodes.append(self._parse_term())
            else:
                break
        if not ops:
            return nodes[0]
        # Left-associative: if all ops equal, one flat group; else nest.
        if all(o == ops[0] for o in ops):
            return Group(ops[0], nodes)
        node = nodes[0]
        for operator, right in zip(ops, nodes[1:]):
            node = Group(operator, [node, right])
        return node

    def _parse_term(self):
        tok = self._peek()
        if tok == "(":
            self._next()
            node = self._parse_expr()
            if self._peek() != ")":
                raise RuleError("Unclosed parenthesis.")
            self._next()
            return node
        return self._parse_condition()

    def _parse_condition(self) -> Condition:
        field = self._require("field")
        if field.lower() not in _FIELDS:
            raise RuleError(f"Unknown field: {field!r}")
        operator = self._require("operator")
        if operator.lower() not in _OPS:
            raise RuleError(f"Unknown operator: {operator!r}")
        value = self._require("value")
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        return Condition(field.lower(), operator.lower(), value)

    def _require(self, what: str) -> str:
        tok = self._peek()
        if tok is None or tok in ("(", ")") or tok.upper() in ("AND", "OR"):
            raise RuleError(f"Expected {what}, found {tok!r}")
        return self._next()


def parse_rule_expression(text: str):
    """Parse a rule expression string into a Condition/Group tree."""
    tokens = _tokenize(text.strip())
    if not tokens:
        raise RuleError("Empty expression.")
    return _Parser(tokens).parse()

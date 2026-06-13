"""Conditional rule engine: nested AND/OR groups compiled to IMAP SEARCH.

A rule tree is built from two node kinds:

* Condition - a single test, e.g. ``sender contains "amazon"``.
* Group     - an AND/OR combination of child nodes (conditions or groups),
              which makes arbitrary nesting possible (a query builder).

The tree is serialisable to / from plain dicts (JSON-friendly) so it can be
saved in scheduled jobs, and it compiles to an IMAP SEARCH command string
understood by ``imaplib``.

Supported fields and operators
------------------------------
sender   : is | contains          -> FROM
subject  : is | contains          -> SUBJECT
date     : is | starts | ends     -> ON | SINCE | BEFORE

``is`` and ``contains`` map to the same IMAP token (servers do substring
matching on FROM/SUBJECT); ``is`` additionally re-checks an exact match
locally when the caller asks for strict matching. Dates use ``DD-Mon-YYYY``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Allowed field/operator pairs. Value is the IMAP search key (or a marker).
_FIELD_OPS: dict[str, dict[str, str]] = {
    "sender": {"is": "FROM", "contains": "FROM"},
    "subject": {"is": "SUBJECT", "contains": "SUBJECT"},
    "date": {"is": "ON", "starts": "SINCE", "ends": "BEFORE"},
}

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class RuleError(ValueError):
    """Raised when a rule tree is malformed."""


def _imap_date(value: str) -> str:
    """Normalise a date string to IMAP's DD-Mon-YYYY format.

    Accepts ISO ``YYYY-MM-DD`` or already-formatted ``DD-Mon-YYYY``.
    """
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(value, fmt)
            return f"{parsed.day:02d}-{_MONTHS[parsed.month - 1]}-{parsed.year}"
        except ValueError:
            continue
    raise RuleError(f"Invalid date: {value!r} (use YYYY-MM-DD)")


def _quote(text: str) -> str:
    """Quote a string for an IMAP search argument."""
    return '"' + text.replace('"', '\\"') + '"'


@dataclass
class Condition:
    """A single search test: field + operator + value."""

    field: str
    operator: str
    value: str

    def validate(self) -> None:
        """Raise RuleError if this condition is malformed."""
        if self.field not in _FIELD_OPS:
            raise RuleError(f"Unknown field: {self.field!r}")
        if self.operator not in _FIELD_OPS[self.field]:
            raise RuleError(
                f"Operator {self.operator!r} is not valid for {self.field!r}")
        if not self.value.strip():
            raise RuleError("The condition value is empty.")

    def to_imap(self) -> list[str]:
        """Return the IMAP SEARCH tokens for this condition."""
        self.validate()
        key = _FIELD_OPS[self.field][self.operator]
        if self.field == "date":
            return [key, _imap_date(self.value)]
        return [key, _quote(self.value)]

    def to_dict(self) -> dict:
        """Serialise this condition to a plain dict."""
        return {"type": "condition", "field": self.field,
                "operator": self.operator, "value": self.value}

    def to_expression(self) -> str:
        """Render this condition back to the text rule grammar.

        Values containing whitespace or parentheses are quoted so they survive
        re-parsing by ``rule_parser.parse_rule_expression``.
        """
        self.validate()
        value = self.value.strip()
        if (not value) or any(ch in value for ch in ' \t()"'):
            value = '"' + value.replace('"', '\\"') + '"'
        return f"{self.field} {self.operator} {value}"


@dataclass
class Group:
    """An AND/OR combination of child nodes (conditions or groups)."""

    op: str = "AND"  # "AND" or "OR"
    children: list = field(default_factory=list)

    def validate(self) -> None:
        """Raise RuleError if this group or any child is malformed."""
        if self.op not in ("AND", "OR"):
            raise RuleError(f"Invalid group operator: {self.op!r}")
        if not self.children:
            raise RuleError("A group must have at least one condition.")
        for child in self.children:
            child.validate()

    def to_imap(self) -> list[str]:
        """Compile the group to IMAP SEARCH tokens.

        IMAP search is AND by default (space-separated criteria). OR is a
        prefix operator combining exactly two criteria, so a multi-child OR is
        folded right-to-left: OR a (OR b c).
        """
        self.validate()
        child_tokens = [c.to_imap() for c in self.children]
        if self.op == "AND":
            tokens: list[str] = []
            for ct in child_tokens:
                tokens.extend(ct)
            return tokens
        # OR: fold pairs from the right.
        folded = child_tokens[-1]
        for ct in reversed(child_tokens[:-1]):
            folded = ["OR"] + ct + folded
        return folded

    def to_dict(self) -> dict:
        """Serialise this group (and children) to a plain dict."""
        return {"type": "group", "op": self.op,
                "children": [c.to_dict() for c in self.children]}

    def to_expression(self) -> str:
        """Render this group back to the parenthesised text rule grammar."""
        self.validate()
        joiner = f" {self.op} "
        return "(" + joiner.join(c.to_expression() for c in self.children) + ")"


def node_from_dict(data: dict):
    """Rebuild a Condition or Group tree from a plain dict."""
    kind = data.get("type")
    if kind == "condition":
        return Condition(data["field"], data["operator"], data["value"])
    if kind == "group":
        children = [node_from_dict(c) for c in data.get("children", [])]
        return Group(data.get("op", "AND"), children)
    raise RuleError(f"Unknown node: {kind!r}")


def compile_search(node) -> str:
    """Compile a rule tree to a single IMAP SEARCH argument string."""
    node.validate()
    return " ".join(node.to_imap())

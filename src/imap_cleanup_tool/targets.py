"""Parsing of the classic target file: one sender or domain per line."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("imap_cleanup_tool")


def parse_targets_text(text: str) -> tuple[set[str], set[str]]:
    """Parse target list *text* and return (addresses, domains), lowercased.

    Format, one entry per line::

        spam@example.com     # exact sender address
        *@newsletter.com     # whole domain (wildcard form)
        annoying.com         # whole domain (bare form)
        mail.annoying.com    # a specific subdomain only
        # comment lines and blank lines are ignored

    Raises ``ValueError`` if no valid entries are found.
    """
    addresses: set[str] = set()
    domains: set[str] = set()

    for lineno, raw in enumerate(text.splitlines(), start=1):
        entry = raw.strip().lower()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("*@"):
            domain = entry[2:].strip()
            if domain:
                domains.add(domain)
        elif "@" in entry:
            addresses.add(entry)
        else:
            domains.add(entry)
        logger.debug("Target line %d parsed: %r", lineno, entry)

    if not addresses and not domains:
        raise ValueError("No valid targets found.")

    logger.info("Loaded %d address(es) and %d domain(s).",
                len(addresses), len(domains))
    return addresses, domains


def load_targets(path: str) -> tuple[set[str], set[str]]:
    """Read the targets file and return (addresses, domains), lowercased.

    Thin wrapper over :func:`parse_targets_text`; see it for the line format.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Targets file not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        try:
            return parse_targets_text(handle.read())
        except ValueError as exc:
            raise ValueError(f"No valid targets found in {path}") from exc


def sender_matches(sender: str, addresses: set[str], domains: set[str],
                   include_subdomains: bool = False) -> bool:
    """True if sender matches an exact address or a target domain.

    Domain matching is exact by default; with ``include_subdomains`` a target
    ``addlance.com`` also matches ``mail.addlance.com``.
    """
    if not sender:
        return False
    if sender in addresses:
        return True
    domain = sender.rsplit("@", 1)[-1]
    for target in domains:
        if domain == target:
            return True
        if include_subdomains and domain.endswith("." + target):
            return True
    return False

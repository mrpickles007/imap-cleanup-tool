"""Parsing of the classic target file: one sender or domain per line."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("imap_cleanup_tool")


def parse_targets_text(text: str) -> tuple[set[str], set[str], set[str]]:
    """Parse target list *text* and return (addresses, domains, exact_domains).

    All values are lowercased. Format, one entry per line::

        spam@example.com     # exact sender address
        *@newsletter.com     # exact domain ONLY — never subdomains
        annoying.com         # domain; also its subdomains if include_subdomains
        mail.annoying.com    # that (sub)domain, treated like a bare domain
        # comment lines and blank lines are ignored

    The ``*@`` form goes into ``exact_domains`` (never expanded to subdomains);
    the bare form goes into ``domains`` (expandable with ``include_subdomains``).
    Note: the exact/no-subdomain distinction only applies to local ``full``
    scanning — server-side ``search`` is a substring match either way.

    Raises ``ValueError`` if no valid entries are found.
    """
    addresses: set[str] = set()
    domains: set[str] = set()
    exact_domains: set[str] = set()

    for lineno, raw in enumerate(text.splitlines(), start=1):
        entry = raw.strip().lower()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("*@"):
            domain = entry[2:].strip()
            if domain:
                exact_domains.add(domain)
        elif "@" in entry:
            addresses.add(entry)
        else:
            domains.add(entry)
        logger.debug("Target line %d parsed: %r", lineno, entry)

    if not addresses and not domains and not exact_domains:
        raise ValueError("No valid targets found.")

    logger.info("Loaded %d address(es), %d domain(s), %d exact-domain(s).",
                len(addresses), len(domains), len(exact_domains))
    return addresses, domains, exact_domains


def load_targets(path: str) -> tuple[set[str], set[str], set[str]]:
    """Read the targets file and return (addresses, domains, exact_domains).

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
                   exact_domains: set[str] = frozenset(),
                   include_subdomains: bool = False) -> bool:
    """True if sender matches an exact address or a target domain.

    * ``addresses`` — exact email-address match.
    * ``exact_domains`` (``*@domain``) — match the domain exactly, never a
      subdomain (the ``include_subdomains`` flag is ignored for these).
    * ``domains`` (bare form) — match the domain exactly, and also its
      subdomains when ``include_subdomains`` is true.
    """
    if not sender:
        return False
    if sender in addresses:
        return True
    domain = sender.rsplit("@", 1)[-1]
    if domain in exact_domains or domain in domains:
        return True
    if include_subdomains:
        for target in domains:
            if domain.endswith("." + target):
                return True
    return False

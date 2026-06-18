"""imap-cleanup-tool: delete or move IMAP emails by sender, domain, or rules.

Public API re-exported for convenience::

    from imap_cleanup_tool import connect, process_folder, list_senders
    from imap_cleanup_tool.rules import Condition, Group, compile_search
"""

from __future__ import annotations

from .core import (
    StopRequested,
    build_ai_report,
    connect,
    create_folder,
    delete_folder,
    delete_uids,
    empty_folder,
    fetch_from_headers,
    list_folders,
    list_senders,
    move_uids,
    process_folder,
    safe_logout,
    save_senders_csv,
    search_rule,
    search_targets,
)
from .targets import load_targets, parse_targets_text, sender_matches
from .rules import Condition, Group, compile_search, node_from_dict
from .rule_parser import parse_rule_expression

__version__ = "0.29.2"

__all__ = [
    "__version__",
    "StopRequested",
    "connect",
    "safe_logout",
    "list_folders",
    "list_senders",
    "save_senders_csv",
    "fetch_from_headers",
    "search_targets",
    "search_rule",
    "delete_uids",
    "move_uids",
    "create_folder",
    "delete_folder",
    "build_ai_report",
    "empty_folder",
    "process_folder",
    "load_targets",
    "parse_targets_text",
    "sender_matches",
    "Condition",
    "Group",
    "compile_search",
    "node_from_dict",
    "parse_rule_expression",
]

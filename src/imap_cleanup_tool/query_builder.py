"""Visual, nested query builder for the GUI.

Instead of typing a rule expression by hand (and risking syntax errors), the
user builds rules with dropdowns: each condition is Field ▸ Operator ▸ Value,
and conditions can be combined with AND/OR and grouped arbitrarily (groups
inside groups). The widget tree maps directly onto ``rules.Condition`` /
``rules.Group`` nodes, so it compiles straight to an IMAP SEARCH string.

Only the standard library (Tkinter) is used. Visual styling reuses the ttk
button styles defined by the GUI (``Accent.TButton`` / ``Danger.TButton``) and a
``theme`` object passed in (the GUI's ``Theme`` class) for colours and fonts.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .rules import Condition, Group


class ConditionRow(tk.Frame):
    """A single test row: Field ▸ Operator ▸ Value, plus a remove button."""

    FIELDS = ["sender", "subject", "date"]

    # Per-field operators as (display label, rules.py operator key). The label
    # is human-friendly; the key is what the rule engine understands.
    OPERATORS = {
        "sender": [("contains", "contains"), ("is exactly", "is")],
        "subject": [("contains", "contains"), ("is exactly", "is")],
        "date": [("on", "is"), ("on/after", "starts"), ("before", "ends")],
    }

    def __init__(self, parent: tk.Misc, theme, on_remove) -> None:
        super().__init__(parent, bg=theme.PANEL)
        self.theme = theme
        self.on_remove = on_remove
        self.field_var = tk.StringVar(value="sender")
        self.op_var = tk.StringVar()
        self.value_var = tk.StringVar()
        self._op_keys: dict[str, str] = {}

        self.field_cb = ttk.Combobox(self, textvariable=self.field_var,
                                     values=self.FIELDS, state="readonly",
                                     width=9)
        self.field_cb.pack(side="left", padx=(0, 4), pady=2)
        self.field_cb.bind("<<ComboboxSelected>>", self._on_field_change)

        self.op_cb = ttk.Combobox(self, textvariable=self.op_var,
                                  state="readonly", width=11)
        self.op_cb.pack(side="left", padx=4, pady=2)

        self.value_entry = ttk.Entry(self, textvariable=self.value_var)
        self.value_entry.pack(side="left", fill="x", expand=True, padx=4, pady=2)

        self.hint = tk.Label(self, text="", bg=theme.PANEL, fg=theme.MUTED,
                             font=theme.FONT, width=10, anchor="w")
        self.hint.pack(side="left", padx=4)

        ttk.Button(self, text="×", width=2, style="Danger.TButton",
                   command=lambda: self.on_remove(self)).pack(
            side="left", padx=(4, 0))

        self._refresh_operators()

    def _on_field_change(self, _event: object = None) -> None:
        self._refresh_operators()

    def _refresh_operators(self) -> None:
        pairs = self.OPERATORS[self.field_var.get()]
        self._op_keys = {label: key for label, key in pairs}
        self.op_cb.configure(values=[label for label, _ in pairs])
        self.op_var.set(pairs[0][0])
        self.hint.configure(
            text="YYYY-MM-DD" if self.field_var.get() == "date" else "")

    def operator_key(self) -> str:
        """Return the rules.py operator key for the current selection."""
        return self._op_keys.get(self.op_var.get(), "contains")

    def to_node(self) -> Condition:
        """Build the Condition node this row represents."""
        return Condition(self.field_var.get(), self.operator_key(),
                         self.value_var.get().strip())


class GroupFrame(tk.Frame):
    """A group node: an AND/OR combiner over child rows and nested groups."""

    GROUP_OPS = [("ALL — all must match (AND)", "AND"),
                 ("ANY — any may match (OR)", "OR")]

    def __init__(self, parent: tk.Misc, theme, on_remove=None,
                 is_root: bool = False, depth: int = 0) -> None:
        bg = theme.PANEL_HI if depth % 2 else theme.PANEL
        super().__init__(parent, bg=bg, highlightbackground=theme.ACCENT,
                         highlightthickness=1)
        self.theme = theme
        self.on_remove = on_remove
        self.is_root = is_root
        self.depth = depth
        self.child_nodes: list = []
        self.op_var = tk.StringVar(value=self.GROUP_OPS[0][0])
        self._op_keys = {label: key for label, key in self.GROUP_OPS}

        header = tk.Frame(self, bg=bg)
        header.pack(fill="x", padx=6, pady=(4, 2))
        tk.Label(header, text="Match", bg=bg, fg=theme.TEXT,
                 font=theme.FONT_BOLD).pack(side="left")
        ttk.Combobox(header, textvariable=self.op_var, state="readonly",
                     width=26, values=[l for l, _ in self.GROUP_OPS]).pack(
            side="left", padx=6)
        ttk.Button(header, text="+ Condition", command=self.add_condition).pack(
            side="left", padx=3)
        ttk.Button(header, text="+ Group", command=self.add_group).pack(
            side="left", padx=3)
        if not is_root:
            ttk.Button(header, text="× Remove group", style="Danger.TButton",
                       command=lambda: self.on_remove(self)).pack(side="right")

        self.body = tk.Frame(self, bg=bg)
        self.body.pack(fill="x", padx=(16, 6), pady=(0, 6))

    def add_condition(self) -> None:
        """Append a new condition row to this group."""
        row = ConditionRow(self.body, self.theme, on_remove=self._remove_child)
        self.child_nodes.append(row)
        row.pack(fill="x", pady=2)

    def add_group(self) -> None:
        """Append a new nested group (seeded with one condition)."""
        group = GroupFrame(self.body, self.theme, on_remove=self._remove_child,
                           is_root=False, depth=self.depth + 1)
        self.child_nodes.append(group)
        group.pack(fill="x", pady=4)
        group.add_condition()

    def _remove_child(self, widget: tk.Misc) -> None:
        if widget in self.child_nodes:
            self.child_nodes.remove(widget)
        widget.destroy()

    def operator_key(self) -> str:
        """Return 'AND' or 'OR' for the current selection."""
        return self._op_keys.get(self.op_var.get(), "AND")

    def to_node(self) -> Group:
        """Build the Group node (with all children) this frame represents."""
        return Group(self.operator_key(),
                     [w.to_node() for w in self.child_nodes])


class QueryBuilder(ttk.Frame):
    """Scrollable container hosting the root group of the builder."""

    def __init__(self, parent: tk.Misc, theme, height: int = 200) -> None:
        super().__init__(parent)
        self.theme = theme
        canvas = tk.Canvas(self, bg=theme.BG, highlightthickness=0,
                           height=height)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._canvas = canvas

        inner = ttk.Frame(canvas)
        self._window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(self._window, width=e.width))
        canvas.bind("<Enter>", self._bind_wheel)
        canvas.bind("<Leave>", self._unbind_wheel)

        self.root_group = GroupFrame(inner, theme, is_root=True, depth=0)
        self.root_group.pack(fill="x", expand=True, padx=2, pady=2)
        self.root_group.add_condition()

    def _bind_wheel(self, _event: object = None) -> None:
        self._canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event: object = None) -> None:
        self._canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event: tk.Event) -> None:
        self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def to_node(self) -> Group:
        """Return the root Group node for compilation/serialisation."""
        return self.root_group.to_node()

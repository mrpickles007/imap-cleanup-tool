"""Tkinter GUI for imap-cleanup-tool.

Run with ``imap-cleanup-tool-gui`` (installed entry point) or
``python -m imap_cleanup_tool.gui``. Uses only the standard library.

Highlights versus a plain wrapper:

* Persistent IMAP connection opened with a Connect button.
* Folder picker that *adds* selected folders to the active set and lets you
  remove them individually (no more overwrite-on-click).
* Two matching modes: a target file, or a rule expression (query-builder
  style) typed in a field and validated live.
* A Stop button for cooperative cancellation.
* A scheduler panel to save jobs, run them internally, or export an OS command.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import core, scheduler
from .query_builder import QueryBuilder
from .rules import RuleError, compile_search
from .targets import load_targets


class Theme:
    """Colour and font constants."""
    # pylint: disable=too-few-public-methods
    BG = "#0f1117"
    PANEL = "#1a1d28"
    PANEL_HI = "#222633"
    ACCENT = "#6d8cff"
    ACCENT_HI = "#8aa3ff"
    DANGER = "#ff5d73"
    DANGER_HI = "#ff7d90"
    OK = "#3ecf8e"
    MUTED = "#8b8fa3"
    TEXT = "#e8eaf0"
    LOG_BG = "#0b0d13"
    LOG_FG = "#c4c8d4"
    FONT = ("Segoe UI", 10)
    FONT_BOLD = ("Segoe UI", 10, "bold")
    FONT_MONO = ("Consolas", 9)


class QueueHandler(logging.Handler):
    """Forward log records to a queue for the GUI to display."""

    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.log_queue.put(self.format(record))


class ImapCleanupToolGUI:
    """Main application window."""
    # pylint: disable=too-many-instance-attributes,too-few-public-methods
    # pylint: disable=too-many-statements

    PRESETS = {
        "Custom": "",
        "Gmail": "imap.gmail.com",
        "iCloud": "imap.mail.me.com",
        "Outlook / Office365": "outlook.office365.com",
        "Aruba": "imaps.aruba.it",
        "Libero": "imapmail.libero.it",
    }

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.conn = None
        self.selected_folders: list[str] = ["INBOX"]

        root.title("IMAP Cleaner")
        root.geometry("900x900")
        root.minsize(820, 760)
        root.configure(bg=Theme.BG)
        self._init_combobox_popup_colors()
        self._build_styles()
        self._build_widgets()
        self._attach_logging()
        self._set_connected(False)
        self._refresh_selected_folders()
        self._poll_log_queue()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----------------------------------------------------------------- styling
    def _init_combobox_popup_colors(self) -> None:
        self.root.option_add("*TCombobox*Listbox.background", Theme.PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", Theme.TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", Theme.ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", Theme.BG)

    def _build_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=Theme.BG, foreground=Theme.TEXT,
                        fieldbackground=Theme.PANEL, font=Theme.FONT,
                        bordercolor=Theme.PANEL_HI)
        for name in ("TFrame",):
            style.configure(name, background=Theme.BG)
        style.configure("TLabel", background=Theme.BG, foreground=Theme.TEXT)
        style.configure("Header.TLabel", background=Theme.BG,
                        foreground=Theme.ACCENT, font=("Segoe UI", 11, "bold"))
        style.configure("TLabelframe", background=Theme.BG,
                        bordercolor=Theme.PANEL_HI, relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=Theme.BG,
                        foreground=Theme.ACCENT, font=Theme.FONT_BOLD)
        style.configure("TCheckbutton", background=Theme.BG, foreground=Theme.TEXT)
        style.map("TCheckbutton", background=[("active", Theme.BG)])
        style.configure("TEntry", fieldbackground=Theme.PANEL,
                        foreground=Theme.TEXT, insertcolor=Theme.TEXT, padding=4)
        style.configure("TCombobox", fieldbackground=Theme.PANEL,
                        foreground=Theme.TEXT, arrowcolor=Theme.TEXT, padding=4)
        style.map("TCombobox",
                  fieldbackground=[("readonly", Theme.PANEL)],
                  foreground=[("readonly", Theme.TEXT)],
                  selectbackground=[("readonly", Theme.PANEL)],
                  selectforeground=[("readonly", Theme.TEXT)])
        style.configure("TButton", background=Theme.PANEL_HI,
                        foreground=Theme.TEXT, borderwidth=0, padding=7)
        style.map("TButton", background=[("active", Theme.ACCENT),
                                         ("disabled", "#15171f")],
                  foreground=[("active", Theme.BG), ("disabled", Theme.MUTED)])
        style.configure("Accent.TButton", background=Theme.ACCENT,
                        foreground=Theme.BG, font=Theme.FONT_BOLD, padding=9)
        style.map("Accent.TButton", background=[("active", Theme.ACCENT_HI),
                                                ("disabled", "#2a2c38")],
                  foreground=[("disabled", Theme.MUTED)])
        style.configure("Danger.TButton", background=Theme.DANGER,
                        foreground=Theme.BG, font=Theme.FONT_BOLD, padding=9)
        style.map("Danger.TButton", background=[("active", Theme.DANGER_HI),
                                               ("disabled", "#2a2c38")],
                  foreground=[("disabled", Theme.MUTED)])

    # ----------------------------------------------------------------- widgets
    def _build_widgets(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        main = ttk.Frame(nb)
        sched = ttk.Frame(nb)
        nb.add(main, text="Cleanup")
        nb.add(sched, text="Scheduling")
        self._build_main_tab(main)
        self._build_scheduler_tab(sched)

    def _build_main_tab(self, parent: ttk.Frame) -> None:
        pad = {"padx": 6, "pady": 3}

        header = ttk.Frame(parent)
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(header, text="IMAP Cleaner", style="Header.TLabel").pack(
            side="left")
        self.status_var = tk.StringVar(value="● disconnected")
        self.status_lbl = tk.Label(header, textvariable=self.status_var,
                                   bg=Theme.BG, fg=Theme.MUTED,
                                   font=Theme.FONT_BOLD)
        self.status_lbl.pack(side="right")

        # Connection
        conn_box = ttk.Labelframe(parent, text=" Connection ")
        conn_box.pack(fill="x", pady=4)
        ttk.Label(conn_box, text="Provider").grid(row=0, column=0, sticky="w", **pad)
        self.preset_var = tk.StringVar(value="Custom")
        cb = ttk.Combobox(conn_box, textvariable=self.preset_var,
                          values=list(self.PRESETS), state="readonly", width=20)
        cb.grid(row=0, column=1, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", self._on_preset)
        ttk.Label(conn_box, text="Host").grid(row=1, column=0, sticky="w", **pad)
        self.host_var = tk.StringVar(value=os.getenv("IMAP_HOST", ""))
        ttk.Entry(conn_box, textvariable=self.host_var, width=28).grid(
            row=1, column=1, sticky="we", **pad)
        ttk.Label(conn_box, text="Port").grid(row=1, column=2, sticky="e", **pad)
        self.port_var = tk.StringVar(value=os.getenv("IMAP_PORT", "993"))
        ttk.Entry(conn_box, textvariable=self.port_var, width=7).grid(
            row=1, column=3, sticky="w", **pad)
        ttk.Label(conn_box, text="User").grid(row=2, column=0, sticky="w", **pad)
        self.user_var = tk.StringVar(value=os.getenv("IMAP_USER", ""))
        ttk.Entry(conn_box, textvariable=self.user_var, width=28).grid(
            row=2, column=1, sticky="we", **pad)
        ttk.Label(conn_box, text="Timeout").grid(row=2, column=2, sticky="e", **pad)
        self.timeout_var = tk.StringVar(value="120")
        ttk.Entry(conn_box, textvariable=self.timeout_var, width=7).grid(
            row=2, column=3, sticky="w", **pad)
        ttk.Label(conn_box, text="Password").grid(row=3, column=0, sticky="w", **pad)
        self.pass_var = tk.StringVar(value=os.getenv("IMAP_PASSWORD", ""))
        self.pass_entry = ttk.Entry(conn_box, textvariable=self.pass_var,
                                    show="•", width=28)
        self.pass_entry.grid(row=3, column=1, sticky="we", **pad)
        self.show_pass_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(conn_box, text="Show", variable=self.show_pass_var,
                        command=self._toggle_password).grid(
            row=3, column=2, sticky="w", **pad)
        bbar = ttk.Frame(conn_box)
        bbar.grid(row=4, column=0, columnspan=4, sticky="we", padx=6, pady=6)
        self.connect_btn = ttk.Button(bbar, text="Connect",
                                      style="Accent.TButton",
                                      command=self._on_connect)
        self.connect_btn.pack(side="left")
        self.disconnect_btn = ttk.Button(bbar, text="Disconnect",
                                         command=self._on_disconnect)
        self.disconnect_btn.pack(side="left", padx=6)
        self.folders_btn = ttk.Button(bbar, text="Load folders",
                                      command=self._on_list_folders)
        self.folders_btn.pack(side="left", padx=6)
        conn_box.columnconfigure(1, weight=1)

        self._build_folder_box(parent)
        self._build_match_box(parent)
        self._build_options_box(parent)
        self._build_action_bar(parent)
        self._build_log_pane(parent)

    def _build_folder_box(self, parent: ttk.Frame) -> None:
        box = ttk.Labelframe(parent, text=" Folders ")
        box.pack(fill="x", pady=4)
        cols = ttk.Frame(box)
        cols.pack(fill="x", padx=6, pady=6)

        left = ttk.Frame(cols)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Available (double-click to add)").pack(
            anchor="w")
        self.available_list = tk.Listbox(
            left, height=5, bg=Theme.PANEL, fg=Theme.TEXT,
            selectbackground=Theme.ACCENT, selectforeground=Theme.BG,
            borderwidth=0, highlightthickness=0, activestyle="none")
        self.available_list.pack(fill="both", expand=True)
        self.available_list.bind("<Double-Button-1>", self._add_folder)

        mid = ttk.Frame(cols)
        mid.pack(side="left", padx=8)
        ttk.Button(mid, text="→", width=3, command=self._add_folder).pack(pady=2)
        ttk.Button(mid, text="←", width=3, command=self._remove_folder).pack(pady=2)

        right = ttk.Frame(cols)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="Selected (double-click to remove)").pack(
            anchor="w")
        self.selected_list = tk.Listbox(
            right, height=5, bg=Theme.PANEL, fg=Theme.TEXT,
            selectbackground=Theme.DANGER, selectforeground=Theme.BG,
            borderwidth=0, highlightthickness=0, activestyle="none")
        self.selected_list.pack(fill="both", expand=True)
        self.selected_list.bind("<Double-Button-1>", self._remove_folder)

        entry_row = ttk.Frame(box)
        entry_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(entry_row, text="Add manually:").pack(side="left")
        self.manual_folder_var = tk.StringVar()
        ttk.Entry(entry_row, textvariable=self.manual_folder_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(entry_row, text="Add",
                   command=self._add_manual_folder).pack(side="left")

    def _build_match_box(self, parent: ttk.Frame) -> None:
        box = ttk.Labelframe(parent, text=" Selection criteria ")
        box.pack(fill="x", pady=4)
        self.match_mode = tk.StringVar(value="targets")
        row = ttk.Frame(box)
        row.pack(fill="x", padx=6, pady=4)
        ttk.Radiobutton(row, text="Target list (file)",
                        variable=self.match_mode, value="targets",
                        command=self._update_match_mode).pack(side="left")
        ttk.Radiobutton(row, text="Rule (query builder)",
                        variable=self.match_mode, value="rule",
                        command=self._update_match_mode).pack(side="left", padx=12)

        self.targets_frame = ttk.Frame(box)
        self.targets_frame.pack(fill="x", padx=6, pady=4)
        self.targets_var = tk.StringVar(value="targets.txt")
        ttk.Entry(self.targets_frame, textvariable=self.targets_var).pack(
            side="left", fill="x", expand=True)
        ttk.Button(self.targets_frame, text="Browse…",
                   command=self._on_browse_targets).pack(side="left", padx=6)

        self.rule_frame = ttk.Frame(box)
        ttk.Label(self.rule_frame, foreground=Theme.MUTED, background=Theme.BG,
                  text=("Build your rule with the dropdowns — no typing of "
                        "expressions. Use “+ Condition” to add a test and "
                        "“+ Group” for nested AND/OR groups.")
                  ).pack(anchor="w", pady=(0, 2))
        self.query_builder = QueryBuilder(self.rule_frame, Theme)
        self.query_builder.pack(fill="x", expand=True)
        ttk.Button(self.rule_frame, text="Validate rule",
                   command=self._validate_rule).pack(anchor="w", pady=(4, 0))
        self._update_match_mode()

    def _build_options_box(self, parent: ttk.Frame) -> None:
        pad = {"padx": 6, "pady": 3}
        box = ttk.Labelframe(parent, text=" Options ")
        box.pack(fill="x", pady=4)
        ttk.Label(box, text="Scan mode").grid(row=0, column=0, sticky="w", **pad)
        self.scan_var = tk.StringVar(value="search")
        ttk.Combobox(box, textvariable=self.scan_var, values=["search", "full"],
                     state="readonly", width=10).grid(row=0, column=1,
                                                       sticky="w", **pad)
        ttk.Label(box, text="Batch").grid(row=0, column=2, sticky="e", **pad)
        self.batch_var = tk.StringVar(value="500")
        ttk.Entry(box, textvariable=self.batch_var, width=7).grid(
            row=0, column=3, sticky="w", **pad)
        self.dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Dry-run", variable=self.dry_var).grid(
            row=1, column=0, columnspan=2, sticky="w", **pad)
        self.subdomains_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Include subdomains (full)",
                        variable=self.subdomains_var).grid(
            row=1, column=2, columnspan=2, sticky="w", **pad)
        self.expunge_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Expunge (permanent)",
                        variable=self.expunge_var).grid(
            row=2, column=0, columnspan=2, sticky="w", **pad)
        self.gmail_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Gmail: move to Trash",
                        variable=self.gmail_var).grid(
            row=2, column=2, columnspan=2, sticky="w", **pad)
        self.empty_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Empty folder (delete EVERYTHING)",
                        variable=self.empty_var).grid(
            row=3, column=0, columnspan=3, sticky="w", **pad)
        self.save_senders_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Save senders to CSV",
                        variable=self.save_senders_var).grid(
            row=4, column=0, columnspan=2, sticky="w", **pad)
        self.save_path_var = tk.StringVar(value="senders.csv")
        ttk.Entry(box, textvariable=self.save_path_var, width=24).grid(
            row=4, column=2, columnspan=2, sticky="we", **pad)

    def _build_action_bar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=6)
        self.run_btn = ttk.Button(bar, text="▶  Run", style="Accent.TButton",
                                  command=self._on_run)
        self.run_btn.pack(side="left")
        self.senders_btn = ttk.Button(bar, text="List senders",
                                      command=self._on_list_senders)
        self.senders_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(bar, text="■ Stop", style="Danger.TButton",
                                   command=self._on_stop)
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="Clear log", command=self._clear_log).pack(
            side="right")

    def _build_log_pane(self, parent: ttk.Frame) -> None:
        box = ttk.Labelframe(parent, text=" Log ")
        box.pack(fill="both", expand=True, pady=(4, 0))
        self.log = scrolledtext.ScrolledText(
            box, wrap="word", bg=Theme.LOG_BG, fg=Theme.LOG_FG,
            insertbackground=Theme.TEXT, borderwidth=0, font=Theme.FONT_MONO)
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        self.log.configure(state="disabled")

    def _build_scheduler_tab(self, parent: ttk.Frame) -> None:
        pad = {"padx": 6, "pady": 4}
        box = ttk.Labelframe(parent, text=" New scheduled job ")
        box.pack(fill="x", padx=10, pady=8)
        ttk.Label(box, text="Name").grid(row=0, column=0, sticky="w", **pad)
        self.job_name_var = tk.StringVar(value="nightly_cleanup")
        ttk.Entry(box, textvariable=self.job_name_var, width=26).grid(
            row=0, column=1, sticky="w", **pad)
        ttk.Label(box, text="Frequency").grid(row=1, column=0, sticky="w", **pad)
        self.job_kind_var = tk.StringVar(value="daily")
        ttk.Combobox(box, textvariable=self.job_kind_var,
                     values=["daily", "interval"], state="readonly",
                     width=12).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(box, text="Time (HH:MM) or minutes").grid(
            row=1, column=2, sticky="e", **pad)
        self.job_when_var = tk.StringVar(value="03:00")
        ttk.Entry(box, textvariable=self.job_when_var, width=10).grid(
            row=1, column=3, sticky="w", **pad)
        ttk.Label(box, text=("The job reuses the fields from the «Cleanup» tab "
                             "(host, user, folders, criteria, options).")
                  ).grid(row=2, column=0, columnspan=4, sticky="w", **pad)
        brow = ttk.Frame(box)
        brow.grid(row=3, column=0, columnspan=4, sticky="we", **pad)
        ttk.Button(brow, text="Save job", style="Accent.TButton",
                   command=self._save_job).pack(side="left")
        ttk.Button(brow, text="Export system command",
                   command=self._export_job).pack(side="left", padx=6)

        list_box = ttk.Labelframe(parent, text=" Saved jobs ")
        list_box.pack(fill="both", expand=True, padx=10, pady=8)
        self.jobs_list = tk.Listbox(
            list_box, height=8, bg=Theme.PANEL, fg=Theme.TEXT,
            selectbackground=Theme.ACCENT, selectforeground=Theme.BG,
            borderwidth=0, highlightthickness=0, activestyle="none")
        self.jobs_list.pack(fill="both", expand=True, padx=6, pady=6)
        jrow = ttk.Frame(list_box)
        jrow.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(jrow, text="Refresh list",
                   command=self._refresh_jobs).pack(side="left")
        ttk.Button(jrow, text="Delete selected",
                   command=self._delete_job).pack(side="left", padx=6)
        self.sched_running = tk.BooleanVar(value=False)
        ttk.Checkbutton(jrow, text="Internal scheduler active",
                        variable=self.sched_running,
                        command=self._toggle_internal_scheduler).pack(
            side="right")
        self._internal = scheduler.InternalScheduler(self._run_job_blocking)
        self._refresh_jobs()

    # --------------------------------------------------------------- logging
    def _attach_logging(self) -> None:
        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
        core.logger.handlers.clear()
        core.logger.addHandler(handler)
        core.logger.setLevel(logging.DEBUG)

    def _poll_log_queue(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(line)
        self.root.after(100, self._poll_log_queue)

    def _append_log(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _log_direct(self, msg: str) -> None:
        self.log_queue.put(msg)

    # --------------------------------------------------------------- helpers
    def _on_preset(self, _event: object = None) -> None:
        host = self.PRESETS.get(self.preset_var.get(), "")
        if host:
            self.host_var.set(host)
        if host == "imap.gmail.com":
            self.gmail_var.set(True)

    def _toggle_password(self) -> None:
        self.pass_entry.configure(show="" if self.show_pass_var.get() else "•")

    def _update_match_mode(self) -> None:
        if self.match_mode.get() == "targets":
            self.rule_frame.pack_forget()
            self.targets_frame.pack(fill="x", padx=6, pady=4)
        else:
            self.targets_frame.pack_forget()
            self.rule_frame.pack(fill="x", padx=6, pady=4)

    def _validate_rule(self) -> None:
        try:
            argument = compile_search(self.query_builder.to_node())
        except RuleError as exc:
            messagebox.showerror("Invalid rule", str(exc))
            return
        self._log_direct(f"Valid rule -> SEARCH {argument}")

    def _on_browse_targets(self) -> None:
        path = filedialog.askopenfilename(
            title="Select target file",
            filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if path:
            self.targets_var.set(path)

    # ------------------------------------------------------- folder selection
    def _add_folder(self, _event: object = None) -> None:
        sel = self.available_list.curselection()
        for index in sel:
            name = self.available_list.get(index)
            if name not in self.selected_folders:
                self.selected_folders.append(name)
        self._refresh_selected_folders()

    def _add_manual_folder(self) -> None:
        name = self.manual_folder_var.get().strip()
        if name and name not in self.selected_folders:
            self.selected_folders.append(name)
            self.manual_folder_var.set("")
            self._refresh_selected_folders()

    def _remove_folder(self, _event: object = None) -> None:
        sel = self.selected_list.curselection()
        for index in reversed(sel):
            del self.selected_folders[index]
        self._refresh_selected_folders()

    def _refresh_selected_folders(self) -> None:
        self.selected_list.delete(0, "end")
        for name in self.selected_folders:
            self.selected_list.insert("end", name)

    def _populate_available(self, names: list[str]) -> None:
        self.available_list.delete(0, "end")
        for name in names:
            self.available_list.insert("end", name)

    # ------------------------------------------------------------ connection
    def _read_params(self) -> dict | None:
        host = self.host_var.get().strip()
        user = self.user_var.get().strip()
        if not host or not user:
            messagebox.showerror("Missing data", "Host and User are required.")
            return None
        try:
            port = int(self.port_var.get())
            timeout = int(self.timeout_var.get())
            batch = int(self.batch_var.get())
        except ValueError:
            messagebox.showerror("Invalid value",
                                 "Port, timeout and batch must be numbers.")
            return None
        folders = self.selected_folders or ["INBOX"]
        return {"host": host, "user": user, "password": self.pass_var.get(),
                "port": port, "timeout": timeout, "batch": batch,
                "folders": list(folders)}

    def _set_connected(self, connected: bool) -> None:
        self.connect_btn.configure(state="disabled" if connected else "normal")
        state = "normal" if connected else "disabled"
        for btn in (self.disconnect_btn, self.folders_btn, self.run_btn,
                    self.senders_btn):
            btn.configure(state=state)
        self.stop_btn.configure(state="disabled")
        self.status_var.set("● connected" if connected else "● disconnected")
        self.status_lbl.configure(fg=Theme.OK if connected else Theme.MUTED)

    def _busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for btn in (self.run_btn, self.senders_btn, self.folders_btn):
            btn.configure(state=state)
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def _run_in_thread(self, fn) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("In progress",
                                "Wait for the current operation to finish.")
            return
        self.stop_event.clear()
        self._busy(True)

        def wrapper() -> None:
            try:
                fn()
            except core.StopRequested:
                self._log_direct("⏹  Operation cancelled by the user.")
            except (OSError, core.imaplib.IMAP4.error) as net_exc:
                self._log_direct(f"[NETWORK ERROR] {net_exc}")
                self.conn = None
                self.root.after(0, lambda: self._set_connected(False))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._log_direct(f"[ERROR] {exc}")
            finally:
                self.root.after(0, lambda: self._busy(False))

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def _on_stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.stop_btn.configure(state="disabled")
            self._log_direct("Stop request sent…")

    def _require_conn(self) -> bool:
        if self.conn is None:
            messagebox.showwarning("Not connected", "Press «Connect» first.")
            return False
        return True

    def _on_connect(self) -> None:
        params = self._read_params()
        if not params:
            return

        def task() -> None:
            self.conn = core.connect(params["host"], params["port"],
                                     params["user"], params["password"],
                                     params["timeout"])
            self.root.after(0, lambda: self._set_connected(True))

        self._run_in_thread(task)

    def _on_disconnect(self) -> None:
        core.safe_logout(self.conn)
        self.conn = None
        self._set_connected(False)
        self._log_direct("Disconnected.")

    def _on_list_folders(self) -> None:
        if not self._require_conn():
            return

        def task() -> None:
            names = core.list_folders(self.conn)
            self.root.after(0, lambda: self._populate_available(names))
            self._log_direct(f"Found {len(names)} folder(s).")

        self._run_in_thread(task)

    def _on_list_senders(self) -> None:
        if not self._require_conn():
            return
        params = self._read_params()
        if not params:
            return
        save = (self.save_path_var.get().strip()
                if self.save_senders_var.get() else None)

        def task() -> None:
            for folder in params["folders"]:
                core.list_senders(self.conn, folder, params["batch"],
                                  should_stop=self.stop_event.is_set,
                                  account=params["user"], save_path=save or None)

        self._run_in_thread(task)

    def _build_match_kwargs(self) -> dict:
        """Resolve target/rule selection into process_folder kwargs."""
        if self.match_mode.get() == "rule":
            argument = compile_search(self.query_builder.to_node())
            return {"search_argument": argument}
        path = self.targets_var.get().strip()
        if not path or not os.path.isfile(path):
            raise FileNotFoundError("Target file missing or invalid.")
        addresses, domains = load_targets(path)
        return {"addresses": addresses, "domains": domains}

    def _on_run(self) -> None:
        if not self._require_conn():
            return
        params = self._read_params()
        if not params:
            return
        dry = self.dry_var.get()
        if not dry and not messagebox.askyesno(
                "Confirm",
                f"Operation on folders: {', '.join(params['folders'])}.\n"
                "Continue?"):
            return

        def task() -> None:
            if self.empty_var.get():
                total = sum(core.empty_folder(self.conn, f, dry_run=dry,
                                              should_stop=self.stop_event.is_set)
                            for f in params["folders"])
                self._log_direct(f"Done. {total} message(s) processed.")
                return
            try:
                match_kwargs = self._build_match_kwargs()
            except (FileNotFoundError, ValueError, RuleError) as exc:
                self._log_direct(f"[ERROR] {exc}")
                return
            total = 0
            for folder in params["folders"]:
                total += core.process_folder(
                    self.conn, folder, dry_run=dry,
                    expunge=self.expunge_var.get(),
                    include_subdomains=self.subdomains_var.get(),
                    batch_size=params["batch"], scan_mode=self.scan_var.get(),
                    gmail_trash=self.gmail_var.get(),
                    should_stop=self.stop_event.is_set, **match_kwargs)
            self._log_direct(f"Done. {total} message(s) processed.")

        self._run_in_thread(task)

    # ------------------------------------------------------------- scheduler
    def _job_args(self) -> list[str]:
        """Build CLI args representing the current form, for a scheduled job."""
        params = self._read_params()
        args: list[str] = []
        if params:
            args += ["--host", params["host"], "--user", params["user"],
                     "--port", str(params["port"])]
            for folder in params["folders"]:
                args += ["--folder", folder]
        if self.match_mode.get() == "rule":
            args += ["--rule", self.query_builder.to_node().to_expression()]
        else:
            args += ["--targets", self.targets_var.get().strip()]
        if self.gmail_var.get():
            args.append("--gmail-trash")
        if self.expunge_var.get():
            args.append("--expunge")
        if self.empty_var.get():
            args.append("--empty-folder")
        args += ["--scan-mode", self.scan_var.get(), "--yes"]
        return args

    def _current_schedule(self) -> dict:
        kind = self.job_kind_var.get()
        when = self.job_when_var.get().strip()
        if kind == "daily":
            return {"kind": "daily", "time": when or "03:00"}
        return {"kind": "interval", "minutes": int(when or "60")}

    def _build_job(self) -> "scheduler.Job | None":
        """Assemble a Job from the current form, or None if the rule is invalid."""
        try:
            args = self._job_args()
        except RuleError as exc:
            messagebox.showerror("Invalid rule", str(exc))
            return None
        return scheduler.Job(name=self.job_name_var.get().strip() or "job",
                             args=args, schedule=self._current_schedule())

    def _save_job(self) -> None:
        job = self._build_job()
        if job is None:
            return
        scheduler.upsert_job(job)
        self._log_direct(f"Job '{job.name}' saved.")
        self._refresh_jobs()

    def _export_job(self) -> None:
        job = self._build_job()
        if job is None:
            return
        command = scheduler.export_system(job)
        self._log_direct("System command (copy and run):")
        self._log_direct(command)

    def _refresh_jobs(self) -> None:
        self.jobs_list.delete(0, "end")
        for job in scheduler.load_jobs():
            self.jobs_list.insert(
                "end", f"{job.name}  [{job.schedule.get('kind')}]")

    def _delete_job(self) -> None:
        sel = self.jobs_list.curselection()
        if not sel:
            return
        name = self.jobs_list.get(sel[0]).split("  [")[0]
        scheduler.delete_job(name)
        self._refresh_jobs()
        self._log_direct(f"Job '{name}' deleted.")

    def _run_job_blocking(self, job: scheduler.Job) -> None:
        """Execute a job by invoking the CLI in-process."""
        from .cli import main as cli_main  # local import avoids cycle
        cli_main(job.args)

    def _toggle_internal_scheduler(self) -> None:
        if self.sched_running.get():
            self._internal.start()
            self._log_direct("Internal scheduler started.")
        else:
            self._internal.stop()
            self._log_direct("Internal scheduler stopped.")

    def _on_close(self) -> None:
        self._internal.stop()
        core.safe_logout(self.conn)
        self.root.destroy()


def main() -> None:
    """Launch the GUI."""
    root = tk.Tk()
    ImapCleanupToolGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

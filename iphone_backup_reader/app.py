from __future__ import annotations

import json
import posixpath
import queue
import threading
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .backup import DEFAULT_BACKUP_ROOT, Backup, BackupError, BackupFile, discover_backups
from .device import (
    ConnectedDevice,
    DeviceLogStream,
    DeviceSupportError,
    create_backup,
    export_crash_report,
    export_media_path,
    get_debug_snapshot,
    list_connected_devices,
    list_crash_reports,
    list_media_files,
)


class BackupReaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("iPhone Backup Reader")
        self.geometry("1280x780")
        self.minsize(900, 600)

        self.backup_root = tk.StringVar(value=str(DEFAULT_BACKUP_ROOT))
        self.category = tk.StringVar(value="All")
        self.search_text = tk.StringVar()
        self.status_text = tk.StringVar(value="Ready")
        self.backups: list[Backup] = []
        self.current_backup: Backup | None = None
        self.file_entries: dict[str, BackupFile] = {}
        self.devices: dict[str, ConnectedDevice] = {}
        self._search_generation = 0
        self._preview_generation = 0
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self.debug_device = tk.StringVar()
        self.debug_media_path = tk.StringVar(value="/")
        self.debug_log_filter = tk.StringVar()
        self.debug_snapshot_text = ""
        self.log_stream: DeviceLogStream | None = None

        self._configure_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(50, self._drain_ui_queue)
        self.after(100, self.refresh_backups)

    def _post_ui(self, callback: Callable[[], None]) -> None:
        self._ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        try:
            for _ in range(100):
                try:
                    self._ui_queue.get_nowait()()
                except queue.Empty:
                    break
        finally:
            self.after(50, self._drain_ui_queue)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Heading.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Muted.TLabel", foreground="#555555")

    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))

        backups_tab = ttk.Frame(notebook, padding=8)
        device_tab = ttk.Frame(notebook, padding=12)
        debug_tab = ttk.Frame(notebook, padding=12)
        notebook.add(backups_tab, text="Backups")
        notebook.add(device_tab, text="Connected iPhone")
        notebook.add(debug_tab, text="Admin / Debug")
        self._build_backups_tab(backups_tab)
        self._build_device_tab(device_tab)
        self._build_debug_tab(debug_tab)

        ttk.Separator(self).pack(fill=tk.X, padx=8, pady=(6, 0))
        ttk.Label(self, textvariable=self.status_text, anchor=tk.W).pack(fill=tk.X, padx=12, pady=5)

    def _build_backups_tab(self, parent: ttk.Frame) -> None:
        folder_bar = ttk.Frame(parent)
        folder_bar.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(folder_bar, text="Backup folder:").pack(side=tk.LEFT)
        ttk.Entry(folder_bar, textvariable=self.backup_root).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(folder_bar, text="Browse…", command=self.choose_backup_root).pack(side=tk.LEFT)
        ttk.Button(folder_bar, text="Refresh", command=self.refresh_backups).pack(side=tk.LEFT, padx=(6, 0))

        horizontal = ttk.Panedwindow(parent, orient=tk.HORIZONTAL)
        horizontal.pack(fill=tk.BOTH, expand=True)

        backup_panel = ttk.Frame(horizontal, padding=(0, 0, 8, 0))
        content_panel = ttk.Frame(horizontal)
        horizontal.add(backup_panel, weight=1)
        horizontal.add(content_panel, weight=4)

        ttk.Label(backup_panel, text="Available backups", style="Heading.TLabel").pack(anchor=tk.W, pady=(0, 6))
        self.backup_tree = ttk.Treeview(backup_panel, columns=("date",), show="tree headings", selectmode="browse")
        self.backup_tree.heading("#0", text="Device")
        self.backup_tree.heading("date", text="Backup date")
        self.backup_tree.column("#0", width=180)
        self.backup_tree.column("date", width=125)
        self.backup_tree.pack(fill=tk.BOTH, expand=True)
        self.backup_tree.bind("<<TreeviewSelect>>", self._backup_selected)

        self.details = ttk.Label(content_panel, text="Select a backup", style="Muted.TLabel", justify=tk.LEFT)
        self.details.pack(fill=tk.X, pady=(0, 8))

        filter_bar = ttk.Frame(content_panel)
        filter_bar.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(filter_bar, text="Category:").pack(side=tk.LEFT)
        category_box = ttk.Combobox(
            filter_bar,
            textvariable=self.category,
            values=("All", "Photos", "Messages", "Contacts", "Notes"),
            state="readonly",
            width=12,
        )
        category_box.pack(side=tk.LEFT, padx=(5, 12))
        category_box.bind("<<ComboboxSelected>>", lambda _event: self.search_files())
        ttk.Label(filter_bar, text="Search:").pack(side=tk.LEFT)
        search_entry = ttk.Entry(filter_bar, textvariable=self.search_text)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        search_entry.bind("<Return>", lambda _event: self.search_files())
        ttk.Button(filter_bar, text="Search", command=self.search_files).pack(side=tk.LEFT)
        ttk.Button(filter_bar, text="Export selected…", command=self.export_selected).pack(side=tk.LEFT, padx=(6, 0))

        vertical = ttk.Panedwindow(content_panel, orient=tk.VERTICAL)
        vertical.pack(fill=tk.BOTH, expand=True)

        files_frame = ttk.Frame(vertical)
        preview_frame = ttk.Frame(vertical)
        vertical.add(files_frame, weight=3)
        vertical.add(preview_frame, weight=2)

        self.files_tree = ttk.Treeview(
            files_frame,
            columns=("domain", "path", "size"),
            show="headings",
            selectmode="extended",
        )
        self.files_tree.heading("domain", text="Domain")
        self.files_tree.heading("path", text="Original path")
        self.files_tree.heading("size", text="Size")
        self.files_tree.column("domain", width=170, stretch=False)
        self.files_tree.column("path", width=550)
        self.files_tree.column("size", width=90, anchor=tk.E, stretch=False)
        files_scroll = ttk.Scrollbar(files_frame, orient=tk.VERTICAL, command=self.files_tree.yview)
        self.files_tree.configure(yscrollcommand=files_scroll.set)
        self.files_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        files_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.files_tree.bind("<<TreeviewSelect>>", self._file_selected)

        ttk.Label(preview_frame, text="Preview", style="Heading.TLabel").pack(anchor=tk.W, pady=(8, 3))
        preview_container = ttk.Frame(preview_frame)
        preview_container.pack(fill=tk.BOTH, expand=True)
        self.preview_text = tk.Text(preview_container, wrap=tk.NONE, font=("Consolas", 10), state=tk.DISABLED)
        preview_y = ttk.Scrollbar(preview_container, orient=tk.VERTICAL, command=self.preview_text.yview)
        preview_x = ttk.Scrollbar(preview_container, orient=tk.HORIZONTAL, command=self.preview_text.xview)
        self.preview_text.configure(yscrollcommand=preview_y.set, xscrollcommand=preview_x.set)
        self.preview_text.grid(row=0, column=0, sticky="nsew")
        preview_y.grid(row=0, column=1, sticky="ns")
        preview_x.grid(row=1, column=0, sticky="ew")
        preview_container.rowconfigure(0, weight=1)
        preview_container.columnconfigure(0, weight=1)

    def _build_device_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Connected iPhone", style="Heading.TLabel").pack(anchor=tk.W)
        ttk.Label(
            parent,
            text=(
                "Unlock the iPhone, connect it by USB, and tap Trust. iOS data is read by first creating "
                "a standard backup; the phone is not exposed as a normal disk."
            ),
            wraplength=850,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 12))

        controls = ttk.Frame(parent)
        controls.pack(fill=tk.X)
        self.refresh_devices_button = ttk.Button(controls, text="Refresh devices", command=self.refresh_devices)
        self.refresh_devices_button.pack(side=tk.LEFT)
        ttk.Button(controls, text="Install help", command=self.show_device_help).pack(side=tk.LEFT, padx=6)

        self.device_tree = ttk.Treeview(
            parent,
            columns=("name", "model", "ios", "serial", "udid"),
            show="headings",
            selectmode="browse",
            height=7,
        )
        for column, title, width in (
            ("name", "Name", 150),
            ("model", "Model", 120),
            ("ios", "iOS", 80),
            ("serial", "Serial", 140),
            ("udid", "UDID", 300),
        ):
            self.device_tree.heading(column, text=title)
            self.device_tree.column(column, width=width)
        self.device_tree.pack(fill=tk.X, pady=10)

        backup_options = ttk.LabelFrame(parent, text="Create a readable backup", padding=10)
        backup_options.pack(fill=tk.X, pady=(4, 8))
        ttk.Label(backup_options, text="Destination:").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(backup_options, textvariable=self.backup_root).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(backup_options, text="Browse…", command=self.choose_backup_root).grid(row=0, column=2)
        backup_options.columnconfigure(1, weight=1)
        ttk.Button(
            backup_options,
            text="Incremental backup",
            command=lambda: self.start_device_backup(full=False),
        ).grid(row=1, column=1, sticky=tk.E, pady=(10, 0), padx=(0, 140))
        ttk.Button(
            backup_options,
            text="Full backup",
            command=lambda: self.start_device_backup(full=True),
        ).grid(row=1, column=1, sticky=tk.E, pady=(10, 0))

        ttk.Label(parent, text="Activity", style="Heading.TLabel").pack(anchor=tk.W, pady=(8, 3))
        self.device_log = tk.Text(parent, height=14, wrap=tk.WORD, font=("Consolas", 10), state=tk.DISABLED)
        self.device_log.pack(fill=tk.BOTH, expand=True)

    def _build_debug_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Device Admin / Debug Console", style="Heading.TLabel").pack(anchor=tk.W)
        ttk.Label(
            parent,
            text=(
                "Trusted USB access can read Apple diagnostics, console logs, crash reports, and /var/mobile/Media. "
                "Stock iOS blocks root filesystem, memory, keychain, and private app-container access; Developer Mode "
                "does not remove those protections."
            ),
            wraplength=1050,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        device_bar = ttk.Frame(parent)
        device_bar.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(device_bar, text="Device:").pack(side=tk.LEFT)
        self.debug_device_box = ttk.Combobox(
            device_bar, textvariable=self.debug_device, state="readonly", width=55
        )
        self.debug_device_box.pack(side=tk.LEFT, padx=6)
        ttk.Button(device_bar, text="Refresh devices", command=self.refresh_devices).pack(side=tk.LEFT)

        debug_notebook = ttk.Notebook(parent)
        debug_notebook.pack(fill=tk.BOTH, expand=True)
        info_tab = ttk.Frame(debug_notebook, padding=8)
        console_tab = ttk.Frame(debug_notebook, padding=8)
        media_tab = ttk.Frame(debug_notebook, padding=8)
        crashes_tab = ttk.Frame(debug_notebook, padding=8)
        debug_notebook.add(info_tab, text="Raw Device Info")
        debug_notebook.add(console_tab, text="Live Console")
        debug_notebook.add(media_tab, text="Media Files")
        debug_notebook.add(crashes_tab, text="Crash Reports")

        info_controls = ttk.Frame(info_tab)
        info_controls.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(info_controls, text="Read diagnostics", command=self.load_debug_snapshot).pack(side=tk.LEFT)
        ttk.Button(info_controls, text="Save JSON…", command=self.save_debug_snapshot).pack(side=tk.LEFT, padx=6)
        self.debug_info_text = self._scrolling_text(info_tab)

        console_controls = ttk.Frame(console_tab)
        console_controls.pack(fill=tk.X, pady=(0, 6))
        self.start_log_button = ttk.Button(console_controls, text="Start live console", command=self.start_live_console)
        self.start_log_button.pack(side=tk.LEFT)
        self.stop_log_button = ttk.Button(
            console_controls, text="Stop", command=self.stop_live_console, state=tk.DISABLED
        )
        self.stop_log_button.pack(side=tk.LEFT, padx=6)
        ttk.Button(console_controls, text="Clear", command=lambda: self._replace_text(self.debug_console_text, "")).pack(
            side=tk.LEFT
        )
        ttk.Label(console_controls, text="Show lines containing:").pack(side=tk.LEFT, padx=(18, 4))
        ttk.Entry(console_controls, textvariable=self.debug_log_filter, width=30).pack(side=tk.LEFT)
        self.debug_console_text = self._scrolling_text(console_tab)

        media_controls = ttk.Frame(media_tab)
        media_controls.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(media_controls, text="Parent", command=self.open_media_parent).pack(side=tk.LEFT)
        ttk.Entry(media_controls, textvariable=self.debug_media_path).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(media_controls, text="List path", command=self.load_media_files).pack(side=tk.LEFT)
        ttk.Button(media_controls, text="Open selected", command=self.open_selected_media).pack(side=tk.LEFT, padx=6)
        ttk.Button(media_controls, text="Export selected…", command=self.export_selected_media).pack(side=tk.LEFT)
        self.media_list = tk.Listbox(media_tab, font=("Consolas", 10), selectmode=tk.BROWSE)
        self.media_list.pack(fill=tk.BOTH, expand=True)
        self.media_list.bind("<Double-Button-1>", lambda _event: self.open_selected_media())

        crash_controls = ttk.Frame(crashes_tab)
        crash_controls.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(crash_controls, text="Refresh reports", command=self.load_crash_reports).pack(side=tk.LEFT)
        ttk.Button(crash_controls, text="Export selected…", command=self.export_selected_crash).pack(side=tk.LEFT, padx=6)
        self.crash_list = tk.Listbox(crashes_tab, font=("Consolas", 10), selectmode=tk.BROWSE)
        self.crash_list.pack(fill=tk.BOTH, expand=True)

    def _scrolling_text(self, parent: ttk.Frame) -> tk.Text:
        container = ttk.Frame(parent)
        container.pack(fill=tk.BOTH, expand=True)
        text = tk.Text(container, wrap=tk.NONE, font=("Consolas", 10), state=tk.DISABLED)
        vertical = ttk.Scrollbar(container, orient=tk.VERTICAL, command=text.yview)
        horizontal = ttk.Scrollbar(container, orient=tk.HORIZONTAL, command=text.xview)
        text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        return text

    def choose_backup_root(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.backup_root.get(), title="Choose backup folder")
        if selected:
            self.backup_root.set(selected)
            self.refresh_backups()

    def refresh_backups(self) -> None:
        root = Path(self.backup_root.get()).expanduser()
        self.status_text.set(f"Scanning {root}…")

        def worker() -> None:
            try:
                backups = discover_backups(root)
                self._post_ui(lambda: self._show_backups(backups))
            except Exception as error:
                message = str(error)
                self._post_ui(lambda: self._show_error("Could not scan backups", message))

        threading.Thread(target=worker, daemon=True).start()

    def _show_backups(self, backups: list[Backup]) -> None:
        self.backups = backups
        self.backup_tree.delete(*self.backup_tree.get_children())
        for index, backup in enumerate(backups):
            date = backup.backup_date.strftime("%Y-%m-%d %H:%M") if backup.backup_date else "Unknown"
            suffix = " (encrypted)" if backup.is_encrypted else ""
            self.backup_tree.insert("", tk.END, iid=str(index), text=backup.device_name + suffix, values=(date,))
        self.status_text.set(f"Found {len(backups)} backup{'s' if len(backups) != 1 else ''}.")
        if backups:
            self.backup_tree.selection_set("0")
            self.backup_tree.focus("0")
            self._backup_selected()

    def _backup_selected(self, _event: object | None = None) -> None:
        selection = self.backup_tree.selection()
        if not selection:
            return
        self.current_backup = self.backups[int(selection[0])]
        backup = self.current_backup
        date = backup.backup_date.strftime("%Y-%m-%d %H:%M:%S") if backup.backup_date else "Unknown"
        encryption = "Encrypted (content browsing unavailable)" if backup.is_encrypted else "Not encrypted"
        self.details.configure(
            text=(
                f"{backup.device_name}  •  {backup.product_type}  •  iOS {backup.product_version}\n"
                f"Backup: {date}  •  {encryption}\n"
                f"Serial: {backup.serial_number}  •  UDID: {backup.udid}"
            )
        )
        self.search_files()

    def search_files(self) -> None:
        backup = self.current_backup
        if backup is None:
            return
        self._search_generation += 1
        generation = self._search_generation
        query = self.search_text.get()
        category = self.category.get()
        self.status_text.set("Searching backup manifest…")

        def worker() -> None:
            try:
                entries = backup.search_files(query, category)
                self._post_ui(lambda: self._show_files(generation, entries))
            except BackupError as error:
                message = str(error)
                self._post_ui(lambda: self._show_search_error(generation, message))

        threading.Thread(target=worker, daemon=True).start()

    def _show_files(self, generation: int, entries: list[BackupFile]) -> None:
        if generation != self._search_generation:
            return
        self.files_tree.delete(*self.files_tree.get_children())
        self.file_entries.clear()
        for index, entry in enumerate(entries):
            iid = str(index)
            size = f"{entry.size:,}" if entry.size is not None else ""
            self.files_tree.insert("", tk.END, iid=iid, values=(entry.domain, entry.relative_path, size))
            self.file_entries[iid] = entry
        suffix = " (first 5,000 shown)" if len(entries) == 5000 else ""
        self.status_text.set(f"Found {len(entries):,} files{suffix}.")

    def _show_search_error(self, generation: int, message: str) -> None:
        if generation != self._search_generation:
            return
        self.files_tree.delete(*self.files_tree.get_children())
        self.file_entries.clear()
        self._set_preview(message)
        self.status_text.set(message)

    def _file_selected(self, _event: object | None = None) -> None:
        selection = self.files_tree.selection()
        backup = self.current_backup
        if not selection or backup is None:
            return
        entry = self.file_entries.get(selection[0])
        if entry is None:
            return
        self._preview_generation += 1
        generation = self._preview_generation
        self._set_preview("Loading preview…")

        def worker() -> None:
            try:
                content = backup.preview(entry)
            except (BackupError, OSError) as error:
                content = f"Preview unavailable: {error}"
            self._post_ui(lambda: self._show_preview(generation, content))

        threading.Thread(target=worker, daemon=True).start()

    def _show_preview(self, generation: int, content: str) -> None:
        if generation == self._preview_generation:
            self._set_preview(content)

    def _set_preview(self, content: str) -> None:
        self.preview_text.configure(state=tk.NORMAL)
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert("1.0", content)
        self.preview_text.configure(state=tk.DISABLED)

    def export_selected(self) -> None:
        backup = self.current_backup
        selections = self.files_tree.selection()
        if backup is None or not selections:
            messagebox.showinfo("Export", "Select one or more files first.", parent=self)
            return
        destination = filedialog.askdirectory(title="Export selected files")
        if not destination:
            return
        entries = [self.file_entries[iid] for iid in selections if iid in self.file_entries]
        exported = 0
        errors: list[str] = []
        for entry in entries:
            try:
                backup.export(entry, Path(destination))
                exported += 1
            except (BackupError, OSError) as error:
                errors.append(f"{entry.display_path}: {error}")
        detail = f"Exported {exported} file{'s' if exported != 1 else ''} to {destination}."
        if errors:
            detail += f"\n\n{len(errors)} failed:\n" + "\n".join(errors[:10])
        messagebox.showinfo("Export complete", detail, parent=self)

    def refresh_devices(self) -> None:
        self.refresh_devices_button.configure(state=tk.DISABLED)
        self._append_device_log("Looking for USB iPhones…")

        def worker() -> None:
            try:
                devices = list_connected_devices()
                self._post_ui(lambda: self._show_devices(devices))
            except DeviceSupportError as error:
                message = str(error)
                self._post_ui(lambda: self._device_error(message))

        threading.Thread(target=worker, daemon=True).start()

    def _show_devices(self, devices: list[ConnectedDevice]) -> None:
        self.refresh_devices_button.configure(state=tk.NORMAL)
        self.device_tree.delete(*self.device_tree.get_children())
        self.devices.clear()
        for index, device in enumerate(devices):
            iid = str(index)
            self.devices[iid] = device
            self.device_tree.insert(
                "", tk.END, iid=iid, values=(device.name, device.product_type, device.ios_version, device.serial_number, device.udid)
            )
        debug_values = [f"{device.name} | iOS {device.ios_version} | {device.udid}" for device in devices]
        self.debug_device_box.configure(values=debug_values)
        if devices:
            self.device_tree.selection_set("0")
            self.debug_device_box.current(0)
            self._append_device_log(f"Found {len(devices)} USB iPhone{'s' if len(devices) != 1 else ''}.")
        else:
            self.debug_device.set("")
            self._append_device_log("No USB iPhone found. Unlock it, reconnect USB, and tap Trust.")

    def _selected_debug_device(self) -> ConnectedDevice | None:
        index = self.debug_device_box.current()
        if index < 0:
            messagebox.showinfo("Admin / Debug", "Refresh devices and select an iPhone first.", parent=self)
            return None
        return self.devices.get(str(index))

    def _run_device_action(
        self,
        action: Callable[[], object],
        success: Callable[[object], None],
        activity: str,
    ) -> None:
        self.status_text.set(activity)

        def worker() -> None:
            try:
                result = action()
                self._post_ui(lambda: success(result))
            except DeviceSupportError as error:
                message = str(error)
                self._post_ui(lambda: self._device_error(message))

        threading.Thread(target=worker, daemon=True).start()

    def load_debug_snapshot(self) -> None:
        device = self._selected_debug_device()
        if device is None:
            return
        self._run_device_action(
            lambda: get_debug_snapshot(device),
            self._show_debug_snapshot,
            "Reading raw device properties and diagnostics…",
        )

    def _show_debug_snapshot(self, value: object) -> None:
        self.debug_snapshot_text = json.dumps(value, indent=2, ensure_ascii=False, default=_json_default)
        self._replace_text(self.debug_info_text, self.debug_snapshot_text)
        self.status_text.set("Device diagnostics loaded.")

    def save_debug_snapshot(self) -> None:
        if not self.debug_snapshot_text:
            messagebox.showinfo("Save diagnostics", "Read diagnostics first.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            title="Save device diagnostics",
            defaultextension=".json",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if path:
            try:
                Path(path).write_text(self.debug_snapshot_text, encoding="utf-8")
                self.status_text.set(f"Saved diagnostics to {path}.")
            except OSError as error:
                self._show_error("Could not save diagnostics", str(error))

    def start_live_console(self) -> None:
        device = self._selected_debug_device()
        if device is None or self.log_stream is not None:
            return

        def output(line: str) -> None:
            self._post_ui(lambda text=line: self._append_debug_log(text))

        def stopped(error: str | None) -> None:
            self._post_ui(lambda message=error: self._console_stopped(message))

        try:
            self.log_stream = DeviceLogStream(device, output, stopped)
            self.log_stream.start()
        except DeviceSupportError as error:
            self.log_stream = None
            self._device_error(str(error))
            return
        self.start_log_button.configure(state=tk.DISABLED)
        self.stop_log_button.configure(state=tk.NORMAL)
        self.status_text.set("Live iPhone console is running…")

    def stop_live_console(self) -> None:
        if self.log_stream is not None:
            self.status_text.set("Stopping live console…")
            self.log_stream.stop()

    def _console_stopped(self, error: str | None) -> None:
        self.log_stream = None
        self.start_log_button.configure(state=tk.NORMAL)
        self.stop_log_button.configure(state=tk.DISABLED)
        if error:
            self._append_debug_log(f"[Console stopped: {error}]")
            self.status_text.set(f"Console stopped: {error}")
        else:
            self._append_debug_log("[Console stopped]")
            self.status_text.set("Live console stopped.")

    def _append_debug_log(self, line: str) -> None:
        filter_text = self.debug_log_filter.get().strip().lower()
        if filter_text and filter_text not in line.lower():
            return
        text = self.debug_console_text
        text.configure(state=tk.NORMAL)
        text.insert(tk.END, line + "\n")
        if int(text.index("end-1c").split(".")[0]) > 20_000:
            text.delete("1.0", "2001.0")
        text.see(tk.END)
        text.configure(state=tk.DISABLED)

    def load_media_files(self) -> None:
        device = self._selected_debug_device()
        if device is None:
            return
        path = self.debug_media_path.get().strip() or "/"
        if not path.startswith("/"):
            path = "/" + path
        self.debug_media_path.set(posixpath.normpath(path))
        self._run_device_action(
            lambda: list_media_files(device, self.debug_media_path.get()),
            self._show_media_files,
            f"Listing {self.debug_media_path.get()}…",
        )

    def _show_media_files(self, value: object) -> None:
        names = value if isinstance(value, list) else []
        self.media_list.delete(0, tk.END)
        for name in names:
            self.media_list.insert(tk.END, str(name))
        self.status_text.set(f"Found {len(names):,} entries in {self.debug_media_path.get()}.")

    def open_media_parent(self) -> None:
        current = self.debug_media_path.get().strip() or "/"
        self.debug_media_path.set(posixpath.dirname(current.rstrip("/")) or "/")
        self.load_media_files()

    def open_selected_media(self) -> None:
        selection = self.media_list.curselection()
        if not selection:
            return
        name = str(self.media_list.get(selection[0]))
        self.debug_media_path.set(posixpath.join(self.debug_media_path.get(), name))
        self.load_media_files()

    def export_selected_media(self) -> None:
        device = self._selected_debug_device()
        selection = self.media_list.curselection()
        if device is None or not selection:
            return
        destination = filedialog.askdirectory(title="Export raw media data")
        if not destination:
            return
        remote_path = posixpath.join(self.debug_media_path.get(), str(self.media_list.get(selection[0])))
        self._run_device_action(
            lambda: export_media_path(device, remote_path, Path(destination)),
            lambda _result: self.status_text.set(f"Exported {remote_path} to {destination}."),
            f"Exporting {remote_path}…",
        )

    def load_crash_reports(self) -> None:
        device = self._selected_debug_device()
        if device is None:
            return
        self._run_device_action(
            lambda: list_crash_reports(device),
            self._show_crash_reports,
            "Reading crash reports…",
        )

    def _show_crash_reports(self, value: object) -> None:
        reports = value if isinstance(value, list) else []
        self.crash_list.delete(0, tk.END)
        for report in reports:
            self.crash_list.insert(tk.END, str(report))
        self.status_text.set(f"Found {len(reports):,} crash-report entries.")

    def export_selected_crash(self) -> None:
        device = self._selected_debug_device()
        selection = self.crash_list.curselection()
        if device is None or not selection:
            return
        destination = filedialog.askdirectory(title="Export crash report")
        if not destination:
            return
        report = str(self.crash_list.get(selection[0]))
        self._run_device_action(
            lambda: export_crash_report(device, report, Path(destination)),
            lambda _result: self.status_text.set(f"Exported {report} to {destination}."),
            f"Exporting {report}…",
        )

    def _replace_text(self, widget: tk.Text, content: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert("1.0", content)
        widget.configure(state=tk.DISABLED)

    def _device_error(self, message: str) -> None:
        self.refresh_devices_button.configure(state=tk.NORMAL)
        self._append_device_log(message)
        self.status_text.set(message)

    def start_device_backup(self, full: bool) -> None:
        selection = self.device_tree.selection()
        if not selection:
            messagebox.showinfo("Create backup", "Refresh devices and select an iPhone first.", parent=self)
            return
        device = self.devices[selection[0]]
        destination = Path(self.backup_root.get()).expanduser()
        mode = "full" if full else "incremental"
        if not messagebox.askokcancel(
            "Create backup",
            f"Create a {mode} backup of {device.name} in {destination}?\n\nKeep the phone unlocked and connected.",
            parent=self,
        ):
            return
        self._append_device_log(f"Starting {mode} backup of {device.name}…")
        self.status_text.set("Backup in progress. Keep the iPhone connected…")

        def output(line: str) -> None:
            if line:
                self._post_ui(lambda: self._append_device_log(line))

        def worker() -> None:
            try:
                create_backup(device, destination, full, output)
                self._post_ui(lambda: self._backup_finished(device))
            except DeviceSupportError as error:
                message = str(error)
                self._post_ui(lambda: self._device_error(message))

        threading.Thread(target=worker, daemon=True).start()

    def _backup_finished(self, device: ConnectedDevice) -> None:
        self._append_device_log(f"Backup of {device.name} completed.")
        self.status_text.set("Backup completed.")
        self.refresh_backups()

    def _append_device_log(self, line: str) -> None:
        self.device_log.configure(state=tk.NORMAL)
        self.device_log.insert(tk.END, line + "\n")
        self.device_log.see(tk.END)
        self.device_log.configure(state=tk.DISABLED)

    def show_device_help(self) -> None:
        messagebox.showinfo(
            "Connected iPhone setup",
            "1. Install Apple Devices or iTunes.\n"
            "2. Unlock the iPhone, connect USB, and tap Trust.\n"
            '3. In PowerShell, run:\n\npy -m pip install -e ".[device]"\n\n'
            "Then restart this application.",
            parent=self,
        )

    def _show_error(self, title: str, message: str) -> None:
        self.status_text.set(message)
        messagebox.showerror(title, message, parent=self)

    def _close(self) -> None:
        if self.log_stream is not None:
            self.log_stream.stop()
        self.destroy()


def _json_default(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"encoding": "hex", "data": value.hex()}
    return repr(value)


def main() -> None:
    app = BackupReaderApp()
    app.mainloop()


if __name__ == "__main__":
    main()

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, quote_plus

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .config import AppConfig, load_config, save_config
from .dialogs import AdvancedSettingsDialog, DiffDialog
from .gui_style import configure_style
from .preview import PreviewPanel
from .profile_panel import ProfilePanel
from .adapters.base import ScanCancelled
from .engine import SyncEngine, SyncEngineError
from .models import (
    ConflictPolicy,
    Endpoint,
    OperationAction,
    SyncMode,
    SyncOperation,
    SyncOptions,
    SyncPlan,
    TargetMode,
)


MODE_LABELS = {
    "单向同步（一个来源 → 其他所选端）": SyncMode.ONE_WAY,
    "双向同步（所选两端或三端互相同步）": SyncMode.BIDIRECTIONAL,
}
TARGET_MODE_LABELS = {
    "保持来源目录结构": TargetMode.PRESERVE,
    "放入各目标端指定目录": TargetMode.SELECTED,
    "放入各目标端根目录": TargetMode.ROOT,
}
CONFLICT_POLICY_LABELS = {
    "手动比较（最安全）": ConflictPolicy.MANUAL,
    "自动采用最后修改时间最新的版本（仅 Joplin ↔ Obsidian）": ConflictPolicy.LATEST,
}


def redact_sensitive_text(text: str, *secrets: str) -> str:
    redacted = str(text)
    for secret in secrets:
        if not secret or len(secret) < 4:
            continue
        for candidate in {secret, quote(secret, safe=""), quote_plus(secret, safe="")}:
            redacted = redacted.replace(candidate, "[已脱敏]")
    return redacted


def _app_icon_path() -> str:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return str(base_dir / "assets" / "app-icon.png")


class SyncApp(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            self._app_icon = tk.PhotoImage(file=_app_icon_path())
            self.iconphoto(True, self._app_icon)
        except (OSError, tk.TclError):
            self._app_icon = None
        self.title("Note Sync Hub — Joplin / Obsidian / 思源安全同步")
        self.geometry("1440x900")
        self.minsize(1100, 700)
        try:
            self.state("zoomed")
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.events: queue.Queue = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self._closing = False
        self._busy = False
        self._active_secrets = ()
        self.plan: Optional[SyncPlan] = None
        self.plan_engine: Optional[SyncEngine] = None
        self.plan_config: Optional[Dict[str, object]] = None
        self.plan_options: Optional[SyncOptions] = None
        self.operation_by_iid: Dict[str, SyncOperation] = {}
        self.folder_values: Dict[Endpoint, List[str]] = {endpoint: [] for endpoint in Endpoint}
        self.folder_lists: Dict[Endpoint, tk.Listbox] = {}
        self.target_folder_combos: Dict[Endpoint, ttk.Combobox] = {}
        self.endpoint_checks: Dict[Endpoint, ttk.Checkbutton] = {}
        self.log_messages: List[str] = []
        configure_style(self)
        self._load_variables()
        self._build()
        self._toggle_options()
        self.profile_panel.restore_last()
        self.after(100, self._drain_events)

    def _load_variables(self) -> None:
        try:
            config = load_config()
        except Exception:
            config = AppConfig()
        self.joplin_api_var = tk.StringVar(value=config.joplin_api_base)
        self.joplin_token_var = tk.StringVar(value=config.joplin_token)
        self.vault_var = tk.StringVar(value=config.obsidian_vault_path)
        self.siyuan_api_var = tk.StringVar(value=config.siyuan_api_base)
        self.siyuan_token_var = tk.StringVar(value=config.siyuan_token)
        self.request_timeout = config.request_timeout
        self.obsidian_attachments_folder = config.obsidian_attachments_folder
        self.joplin_default_notebook = config.joplin_default_notebook
        self.siyuan_default_notebook = config.siyuan_default_notebook

        self.endpoint_vars = {
            Endpoint.JOPLIN: tk.BooleanVar(value=True),
            Endpoint.OBSIDIAN: tk.BooleanVar(value=True),
            Endpoint.SIYUAN: tk.BooleanVar(value=True),
        }
        self.target_endpoint_vars = {
            Endpoint.JOPLIN: tk.BooleanVar(value=False),
            Endpoint.OBSIDIAN: tk.BooleanVar(value=True),
            Endpoint.SIYUAN: tk.BooleanVar(value=True),
        }
        self.mode_var = tk.StringVar(value=next(iter(MODE_LABELS)))
        self.source_var = tk.StringVar(value=Endpoint.JOPLIN.label)
        self.primary_var = tk.StringVar(value=Endpoint.JOPLIN.label)
        self.conflict_policy_var = tk.StringVar(value=next(iter(CONFLICT_POLICY_LABELS)))
        self._last_source_endpoint: Optional[Endpoint] = Endpoint.JOPLIN
        self.scope_var = tk.StringVar(value="all")
        self.target_mode_var = tk.StringVar(value=next(iter(TARGET_MODE_LABELS)))
        self.target_folder_vars = {endpoint: tk.StringVar() for endpoint in Endpoint}
        self.include_subfolders_var = tk.BooleanVar(value=True)
        self.propagate_deletions_var = tk.BooleanVar(value=False)
        self.delete_text_var = tk.StringVar()
        self.delete_hint_var = tk.StringVar()
        self.status_var = tk.StringVar(value="请先确认三端连接设置，然后测试连接并刷新目录。")
        self.progress_text_var = tk.StringVar(value="就绪")

    # ------------------------------------------------------------------ build

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=8, style="App.TFrame")
        outer.pack(fill="both", expand=True)

        # Header
        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(header, text="Note Sync Hub", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Joplin、Obsidian、思源笔记：先生成只读预览，再执行；冲突默认不会自动覆盖。",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(1, 0))

        self.profile_panel = ProfilePanel(outer, self._collect_config, self._collect_options,
                                          self._apply_options, self.status_var.set)
        self.profile_panel.pack(fill="x", pady=(0, 8))

        # Main horizontal split: left settings | right preview+log
        self._main_pane = ttk.Panedwindow(outer, orient="horizontal")
        self._main_pane.pack(fill="both", expand=True, pady=(0, 4))

        left = ttk.Frame(self._main_pane)
        right = ttk.Frame(self._main_pane)
        self._main_pane.add(left, weight=2)
        self._main_pane.add(right, weight=5)

        self._build_left(left)
        self._build_right(right)

        # Status bar + progress (full width, below pane)
        bottom_bar = ttk.Frame(outer, style="App.TFrame")
        bottom_bar.pack(fill="x", pady=(4, 0))
        self.progress = ttk.Progressbar(bottom_bar, mode="determinate")
        self.progress.pack(side="right", fill="x", expand=False, padx=(4, 0), ipadx=60)
        ttk.Label(bottom_bar, textvariable=self.progress_text_var, style="App.Subtitle.TLabel").pack(side="right", padx=(0, 6))
        ttk.Label(bottom_bar, textvariable=self.status_var, style="App.Subtitle.TLabel").pack(
            side="left", fill="x", expand=True
        )

    def _build_left(self, parent: ttk.Frame) -> None:
        self.connection_toggle = ttk.Button(parent, text="收起连接设置", command=self._toggle_connections)
        self.connection_toggle.pack(anchor="w", pady=(0, 4))
        self._connections_visible = True
        self._build_connection(parent)
        self._build_options(parent)
        folder_frame = ttk.Frame(parent)
        folder_frame.pack(fill="both", expand=True)
        self._build_folders(folder_frame)

    def _build_connection(self, parent: ttk.Frame) -> None:
        connection = ttk.LabelFrame(parent, text="1. 连接设置与启用的笔记端", padding=8)
        self.connection_box = connection
        connection.pack(fill="x", pady=(0, 6))
        connection.columnconfigure(2, weight=1)
        connection.columnconfigure(4, weight=1)

        for column, label in enumerate(("启用", "笔记端", "地址或目录", "", "Token")):
            ttk.Label(connection, text=label, style="Subtitle.TLabel").grid(
                row=0, column=column, sticky="w", padx=(0, 7), pady=(0, 3)
            )

        rows = (
            (Endpoint.JOPLIN, "Joplin", self.joplin_api_var, self.joplin_token_var),
            (Endpoint.OBSIDIAN, "Obsidian", self.vault_var, None),
            (Endpoint.SIYUAN, "思源笔记", self.siyuan_api_var, self.siyuan_token_var),
        )
        for row, (endpoint, label, path_var, token_var) in enumerate(rows, 1):
            check = ttk.Checkbutton(
                connection, variable=self.endpoint_vars[endpoint], command=self._toggle_options
            )
            check.grid(row=row, column=0, sticky="w", padx=(0, 7), pady=2)
            self.endpoint_checks[endpoint] = check
            ttk.Label(connection, text=label, width=10).grid(row=row, column=1, sticky="w", padx=(0, 7), pady=2)
            ttk.Entry(connection, textvariable=path_var).grid(row=row, column=2, sticky="ew", pady=2)
            if endpoint == Endpoint.OBSIDIAN:
                ttk.Button(connection, text="选择…", command=self._choose_vault).grid(
                    row=row, column=3, sticky="w", padx=6, pady=2
                )
                ttk.Label(connection, text="本地 Vault，无需 Token", style="Subtitle.TLabel").grid(
                    row=row, column=4, sticky="w", pady=2
                )
            else:
                ttk.Label(connection, text="").grid(row=row, column=3, padx=6)
                ttk.Entry(connection, textvariable=token_var, show="•").grid(row=row, column=4, sticky="ew", pady=2)

        actions = ttk.Frame(connection)
        actions.grid(row=1, column=5, rowspan=3, sticky="nsew", padx=(10, 0))
        self.test_button = ttk.Button(actions, text="测试所选连接", command=self._test_connections)
        self.test_button.pack(fill="x")
        ttk.Button(actions, text="高级设置…", command=self._advanced_settings).pack(fill="x", pady=(5, 0))
        ttk.Button(actions, text="保存设置", command=self._save_settings).pack(fill="x", pady=(5, 0))

    def _build_options(self, parent: ttk.Frame) -> None:
        options = ttk.LabelFrame(parent, text="2. 同步方式、范围与安全选项", padding=8)
        self.options_box = options
        options.pack(fill="x", pady=(0, 6))

        line1 = ttk.Frame(options)
        line1.pack(fill="x")
        ttk.Label(line1, text="方式：").pack(side="left")
        for label in MODE_LABELS:
            ttk.Radiobutton(
                line1, text=label, value=label, variable=self.mode_var, command=self._toggle_options
            ).pack(side="left", padx=(0, 12))
        ttk.Separator(line1, orient="vertical").pack(side="left", fill="y", padx=(0, 12))
        ttk.Label(line1, text="双向冲突：").pack(side="left")
        self.conflict_policy_combo = ttk.Combobox(
            line1, textvariable=self.conflict_policy_var, values=list(CONFLICT_POLICY_LABELS),
            state="disabled", width=46,
        )
        self.conflict_policy_combo.pack(side="left")

        direction_line = ttk.Frame(options)
        direction_line.pack(fill="x", pady=(7, 0))
        ttk.Label(direction_line, text="单向来源：").pack(side="left")
        self.source_combo = ttk.Combobox(direction_line, textvariable=self.source_var, state="readonly", width=12)
        self.source_combo.pack(side="left")
        self.source_combo.bind("<<ComboboxSelected>>", lambda _e: self._toggle_options())
        ttk.Label(direction_line, text="单向目标：").pack(side="left", padx=(18, 4))
        self.target_endpoint_checks: Dict[Endpoint, ttk.Checkbutton] = {}
        for endpoint in Endpoint:
            check = ttk.Checkbutton(
                direction_line, text=endpoint.label,
                variable=self.target_endpoint_vars[endpoint], command=self._toggle_options,
            )
            check.pack(side="left", padx=(0, 10))
            self.target_endpoint_checks[endpoint] = check
        ttk.Label(direction_line, text="目标可多选", style="Subtitle.TLabel").pack(side="left", padx=(6, 0))
        ttk.Separator(direction_line, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Label(direction_line, text="双向主端：").pack(side="left")
        self.primary_combo = ttk.Combobox(
            direction_line, textvariable=self.primary_var, state="disabled", width=12
        )
        self.primary_combo.pack(side="left")

        line2 = ttk.Frame(options)
        line2.pack(fill="x", pady=(7, 0))
        ttk.Radiobutton(line2, text="全部笔记", value="all", variable=self.scope_var, command=self._toggle_options).pack(side="left")
        ttk.Radiobutton(line2, text="仅所选目录", value="selected", variable=self.scope_var, command=self._toggle_options).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(line2, text="包含子目录", variable=self.include_subfolders_var).pack(side="left", padx=(12, 0))
        ttk.Label(line2, text="单向目标位置：").pack(side="left", padx=(22, 4))
        self.target_mode_combo = ttk.Combobox(
            line2, textvariable=self.target_mode_var, values=list(TARGET_MODE_LABELS),
            state="readonly", width=24,
        )
        self.target_mode_combo.pack(side="left")
        self.target_mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._toggle_options())

        line3 = ttk.Frame(options)
        line3.pack(fill="x", pady=(7, 0))
        self.delete_check = ttk.Checkbutton(
            line3, textvariable=self.delete_text_var, variable=self.propagate_deletions_var
        )
        self.delete_check.pack(side="left")
        ttk.Label(line3, textvariable=self.delete_hint_var, foreground="#9a5b00").pack(side="left", padx=(12, 0))

    def _toggle_connections(self, visible=None) -> None:
        self._connections_visible = not self._connections_visible if visible is None else visible
        if self._connections_visible:
            self.connection_box.pack(fill="x", pady=(0, 6), before=self.options_box)
        else:
            self.connection_box.pack_forget()
        self.connection_toggle.configure(text="收起连接设置" if self._connections_visible else "展开连接设置")

    def _build_folders(self, parent: ttk.Frame) -> None:
        folders = ttk.LabelFrame(parent, text="3. 目录范围与单向目标目录", padding=8)
        folders.pack(fill="both", expand=True)

        self.folder_panes = ttk.Panedwindow(folders, orient="horizontal")
        self.folder_panes.pack(fill="both", expand=True)
        for endpoint in Endpoint:
            frame = ttk.LabelFrame(self.folder_panes, text=endpoint.label, padding=6)
            self.folder_panes.add(frame, weight=1)
            list_frame = ttk.Frame(frame)
            list_frame.pack(fill="both", expand=True)
            listbox = tk.Listbox(list_frame, height=4, selectmode="extended", exportselection=False)
            scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
            listbox.configure(yscrollcommand=scrollbar.set)
            listbox.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            self.folder_lists[endpoint] = listbox
            target_line = ttk.Frame(frame)
            target_line.pack(fill="x", pady=(5, 0))
            ttk.Label(target_line, text="单向写入到：").pack(side="left")
            combo = ttk.Combobox(target_line, textvariable=self.target_folder_vars[endpoint], state="disabled")
            combo.pack(side="left", fill="x", expand=True)
            self.target_folder_combos[endpoint] = combo

        folder_actions = ttk.Frame(folders)
        folder_actions.pack(fill="x", pady=(6, 0))
        self.refresh_button = ttk.Button(folder_actions, text="刷新所选端目录", command=self._refresh_folders)
        self.refresh_button.pack(side="left")
        ttk.Label(
            folder_actions,
            text="思源的文档也可作为目录；选中文档时会包含该文档本身，勾选子目录后还会包含其子文档。",
            style="Subtitle.TLabel",
        ).pack(side="left", padx=(10, 0))
        self.preview_button = ttk.Button(
            folder_actions, text="生成只读同步预览", style="Accent.TButton", command=self._preview
        )
        self.preview_button.pack(side="right")

    def _build_right(self, parent: ttk.Frame) -> None:
        # Vertical pane: preview (top, large) | log (bottom, collapsible)
        self._right_pane = ttk.Panedwindow(parent, orient="vertical")
        self._right_pane.pack(fill="both", expand=True)

        preview_frame = ttk.Frame(self._right_pane)
        log_frame = ttk.Frame(self._right_pane)
        self._right_pane.add(preview_frame, weight=4)
        self._right_pane.add(log_frame, weight=1)

        self._build_preview(preview_frame)
        self._build_log(log_frame)

    def _build_preview(self, parent: ttk.Frame) -> None:
        self.preview_panel = PreviewPanel(parent, self._resolve_selected_conflict)
        self.preview_panel.pack(fill="both", expand=True)
        self.preview_tree = self.preview_panel.tree
        self.operation_by_iid = self.preview_panel.operations

        action_bar = ttk.Frame(parent)
        action_bar.pack(fill="x", pady=(4, 0))
        self.resolve_button = ttk.Button(action_bar, text="比较并处理所选冲突…", command=self._resolve_selected_conflict)
        self.resolve_button.pack(side="left")
        self.execute_button = ttk.Button(
            action_bar, text="执行勾选的安全操作", style="Accent.TButton", command=self._execute
        )
        self.execute_button.pack(side="right")
        self.cancel_button = ttk.Button(action_bar, text="取消当前任务", command=self._cancel_worker, state="disabled")
        self.cancel_button.pack(side="right", padx=(0, 8))

    def _build_log(self, parent: ttk.Frame) -> None:
        log = ttk.LabelFrame(parent, text="运行日志", padding=6)
        log.pack(fill="both", expand=True)
        log.rowconfigure(0, weight=1)
        log.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            log, wrap="none", font=("Consolas", 9), undo=False, state="disabled", height=6,
            bg="#ffffff", fg="#1f2937", relief="flat",
            highlightthickness=1, highlightbackground="#dbe2ec",
        )
        vsb = ttk.Scrollbar(log, orient="vertical", command=self.log_text.yview)
        hsb = ttk.Scrollbar(log, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

    # --------------------------------------------------------- option logic

    def _enabled_endpoints(self) -> Tuple[Endpoint, ...]:
        return tuple(endpoint for endpoint in Endpoint if self.endpoint_vars[endpoint].get())

    def _sync_endpoints(self) -> Tuple[Endpoint, ...]:
        enabled = self._enabled_endpoints()
        if MODE_LABELS[self.mode_var.get()] == SyncMode.BIDIRECTIONAL:
            return enabled
        source = next((ep for ep in enabled if ep.label == self.source_var.get()), None)
        targets = tuple(
            ep for ep in enabled
            if ep != source and self.target_endpoint_vars[ep].get()
        )
        return tuple(ep for ep in Endpoint if ep == source or ep in targets)

    def _toggle_options(self) -> None:
        enabled_endpoints = self._enabled_endpoints()
        values = [ep.label for ep in enabled_endpoints]
        self.source_combo.configure(values=values)
        if self.source_var.get() not in values and values:
            self.source_var.set(values[0])
        one_way = MODE_LABELS[self.mode_var.get()] == SyncMode.ONE_WAY
        self.source_combo.configure(state="readonly" if one_way and values else "disabled")
        latest_supported = (
            not one_way and set(enabled_endpoints) == {Endpoint.JOPLIN, Endpoint.OBSIDIAN}
        )
        if not latest_supported:
            manual_label = next(
                label for label, policy in CONFLICT_POLICY_LABELS.items()
                if policy == ConflictPolicy.MANUAL
            )
            self.conflict_policy_var.set(manual_label)
        self.conflict_policy_combo.configure(state="readonly" if latest_supported else "disabled")
        selected_scope = self.scope_var.get() == "selected"
        mapping = one_way and selected_scope
        if not mapping:
            preserve_label = next(
                label for label, tm in TARGET_MODE_LABELS.items()
                if tm == TargetMode.PRESERVE
            )
            self.target_mode_var.set(preserve_label)
        self.target_mode_combo.configure(state="readonly" if mapping else "disabled")
        selected_mode = TARGET_MODE_LABELS[self.target_mode_var.get()] == TargetMode.SELECTED
        source = next((ep for ep in enabled_endpoints if ep.label == self.source_var.get()), None)
        self.primary_combo.configure(values=values)
        if self.primary_var.get() not in values and values:
            self.primary_var.set(values[0])
        self.primary_combo.configure(state="disabled" if one_way or not values else "readonly")
        if one_way and source != self._last_source_endpoint:
            if self._last_source_endpoint in enabled_endpoints:
                self.target_endpoint_vars[self._last_source_endpoint].set(True)
            self._last_source_endpoint = source
        for endpoint, check in self.target_endpoint_checks.items():
            if endpoint == source or endpoint not in enabled_endpoints:
                self.target_endpoint_vars[endpoint].set(False)
                check.configure(state="disabled")
            else:
                check.configure(state="normal" if one_way else "disabled")
        if one_way:
            self.delete_text_var.set("将来源端的删除同步到目标端（危险，默认关闭）")
            self.delete_hint_var.set("Joplin 用废纸篓，Obsidian 用系统回收站，思源移入统一回收站。")
        else:
            self.delete_text_var.set("将双向主端的删除同步到其他端（危险，默认关闭）")
            self.delete_hint_var.set("非主端删除会恢复；思源副本移入统一的 Note Sync Hub 回收站。")
        endpoints = self._sync_endpoints()
        for endpoint, listbox in self.folder_lists.items():
            enabled = endpoint in endpoints and selected_scope and (not one_way or endpoint == source)
            listbox.configure(state="normal" if enabled else "disabled")
            target_enabled = mapping and selected_mode and endpoint in endpoints and endpoint != source
            self.target_folder_combos[endpoint].configure(state="readonly" if target_enabled else "disabled")

    # --------------------------------------------------------- config / options

    def _collect_config(self) -> AppConfig:
        return AppConfig(
            joplin_api_base=self.joplin_api_var.get().strip().rstrip("/"),
            joplin_token=self.joplin_token_var.get().strip(),
            obsidian_vault_path=self.vault_var.get().strip(),
            siyuan_api_base=self.siyuan_api_var.get().strip().rstrip("/"),
            siyuan_token=self.siyuan_token_var.get().strip(),
            request_timeout=self.request_timeout,
            obsidian_attachments_folder=self.obsidian_attachments_folder,
            joplin_default_notebook=self.joplin_default_notebook,
            siyuan_default_notebook=self.siyuan_default_notebook,
        )

    def _selected_folders(self, endpoint: Endpoint) -> Tuple[str, ...]:
        listbox = self.folder_lists[endpoint]
        return tuple(self.folder_values[endpoint][i] for i in listbox.curselection())

    def _apply_options(self, options: SyncOptions) -> None:
        self.plan = None
        self.plan_engine = None
        self.plan_config = None
        self.plan_options = None
        self._render_plan()
        for endpoint in Endpoint:
            self.endpoint_vars[endpoint].set(endpoint in options.endpoints)
            self.target_endpoint_vars[endpoint].set(endpoint in options.targets)
            self.target_folder_vars[endpoint].set(options.target_folders.get(endpoint, ""))
            selected = options.selected_folders.get(endpoint, ())
            values = sorted(set(self.folder_values[endpoint]) | set(selected), key=str.casefold)
            self._populate_folders(endpoint, values, selected)
        self.mode_var.set(next(label for label, mode in MODE_LABELS.items() if mode == options.mode))
        self.source_var.set((options.source or options.endpoints[0]).label)
        self.primary_var.set((options.primary or options.endpoints[0]).label)
        self._last_source_endpoint = options.source
        self.scope_var.set("all" if options.scope_all else "selected")
        self.include_subfolders_var.set(options.include_subfolders)
        self.propagate_deletions_var.set(options.propagate_deletions)
        self.target_mode_var.set(next(label for label, mode in TARGET_MODE_LABELS.items() if mode == options.target_mode))
        self.conflict_policy_var.set(next(label for label, policy in CONFLICT_POLICY_LABELS.items() if policy == options.conflict_policy))
        self._toggle_options()

        self._toggle_connections(False)

    def _populate_folders(self, endpoint, values, selected) -> None:
        self.folder_values[endpoint] = values
        listbox = self.folder_lists[endpoint]
        listbox.configure(state="normal")
        listbox.delete(0, "end")
        for index, value in enumerate(values):
            listbox.insert("end", value or "（根目录）")
            if value in selected:
                listbox.selection_set(index)
        self.target_folder_combos[endpoint].configure(values=[value for value in values if value])

    def _collect_options(self) -> SyncOptions:
        endpoints = self._sync_endpoints()
        mode = MODE_LABELS[self.mode_var.get()]
        scope_all = self.scope_var.get() == "all"
        source = (
            next((ep for ep in endpoints if ep.label == self.source_var.get()), None)
            if mode == SyncMode.ONE_WAY else None
        )
        primary = (
            next((ep for ep in endpoints if ep.label == self.primary_var.get()), None)
            if mode == SyncMode.BIDIRECTIONAL else None
        )
        mapping_enabled = mode == SyncMode.ONE_WAY and not scope_all
        target_mode = (
            TARGET_MODE_LABELS[self.target_mode_var.get()] if mapping_enabled else TargetMode.PRESERVE
        )
        selected_folders = {ep: self._selected_folders(ep) for ep in endpoints}
        options = SyncOptions(
            mode=mode,
            endpoints=endpoints,
            source=source,
            primary=primary,
            scope_all=scope_all,
            selected_folders=selected_folders,
            include_subfolders=self.include_subfolders_var.get(),
            target_mode=target_mode,
            target_folders={
                ep: self.target_folder_vars[ep].get().strip()
                for ep in endpoints
                if target_mode == TargetMode.SELECTED and ep != source
            },
            propagate_deletions=self.propagate_deletions_var.get(),
            conflict_policy=(
                CONFLICT_POLICY_LABELS[self.conflict_policy_var.get()]
                if mode == SyncMode.BIDIRECTIONAL else ConflictPolicy.MANUAL
            ),
        )
        options.validate()
        return options

    def _choose_vault(self) -> None:
        selected = filedialog.askdirectory(title="选择 Obsidian Vault", initialdir=self.vault_var.get() or None)
        if selected:
            self.vault_var.set(selected)

    def _advanced_settings(self) -> None:
        dialog = AdvancedSettingsDialog(self, self._collect_config())
        self.wait_window(dialog)
        if not dialog.result:
            return
        (
            self.request_timeout,
            self.obsidian_attachments_folder,
            self.joplin_default_notebook,
            self.siyuan_default_notebook,
        ) = dialog.result

    def _save_settings(self, *, notify: bool = True) -> None:
        try:
            path = save_config(self._collect_config())
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        if notify:
            messagebox.showinfo("已保存", f"设置已保存到：\n{path}", parent=self)

    def _engine(self, config: AppConfig) -> SyncEngine:
        return SyncEngine(config, logger=lambda msg: self.events.put(("log", msg)), cancel_event=self.cancel_event)

    # --------------------------------------------------------- folder actions

    def _test_connections(self) -> None:
        endpoints = self._sync_endpoints()
        if not endpoints:
            messagebox.showwarning("未选择笔记端", "请至少选择一个需要测试的笔记端。", parent=self)
            return
        config = self._collect_config()
        self._run_async(
            "正在测试连接……",
            lambda: self._engine(config).test_connections(endpoints),
            self._connections_done,
        )

    def _connections_done(self, results: Dict[Endpoint, str]) -> None:
        message = "\n".join(results[ep] for ep in results)
        self.status_var.set(message.replace("\n", "；"))
        messagebox.showinfo("连接测试完成", message, parent=self)

    def _refresh_folders(self) -> None:
        endpoints = self._sync_endpoints()
        if not endpoints:
            messagebox.showwarning("未选择笔记端", "请先选择要读取目录的笔记端。", parent=self)
            return
        config = self._collect_config()
        self._run_async(
            "正在刷新目录……",
            lambda: self._engine(config).discover_folders(endpoints),
            self._folders_done,
        )

    def _folders_done(self, folders: Dict[Endpoint, List[str]]) -> None:
        for endpoint, values in folders.items():
            selected = self._selected_folders(endpoint)
            display_values = sorted(set(values) | set(selected), key=str.casefold)
            self._populate_folders(endpoint, display_values or [""], selected)
            if set(selected) - set(values):
                self._append_log(f"{endpoint.label} 部分已选目录当前不存在，已保留选择，请核对同步范围。")
        # restore correct enabled/disabled state after populating
        self._toggle_options()
        summary = "；".join(f"{ep.label} {len(v)} 个目录" for ep, v in folders.items())
        self.status_var.set("目录刷新完成：" + summary)

    # --------------------------------------------------------- preview / execute

    def _preview(self) -> None:
        try:
            options = self._collect_options()
            config = self._collect_config()
            config.validate(options.endpoints)
        except Exception as exc:
            messagebox.showerror("无法生成预览", str(exc), parent=self)
            return
        engine = self._engine(config)
        self._run_async(
            "正在扫描并生成只读预览……",
            lambda: engine.preview(options),
            lambda plan: self._preview_done(plan, engine, config),
        )

    def _preview_done(self, plan: SyncPlan, engine: SyncEngine, config: AppConfig) -> None:
        self.plan = plan
        self.plan_engine = engine
        self.plan_config = config.to_dict()
        self.plan_options = plan.options
        self._render_plan()
        counts = plan.counts()
        conflict_count = counts.get(OperationAction.CONFLICT.value, 0)
        self.status_var.set(
            f"预览完成：共 {len(plan.operations)} 项，可执行 {len(plan.executable_operations())} 项，"
            f"待处理冲突 {conflict_count} 项。预览本身没有修改任何笔记。"
        )

    def _render_plan(self) -> None:
        self.preview_panel.set_plan(self.plan)

    def _resolve_selected_conflict(self) -> None:
        if self._busy:
            return
        selected = self.preview_tree.selection()
        if not selected:
            messagebox.showinfo("请选择预览项", "请先在同步预览中选择一条冲突。", parent=self)
            return
        operation = self.operation_by_iid[selected[0]]
        if operation.action != OperationAction.CONFLICT:
            messagebox.showinfo("无需处理", "所选项目不是待处理冲突。", parent=self)
            return
        if not self.plan or not operation.can_resolve:
            messagebox.showwarning(
                "无法在软件内自动处理",
                operation.reason + "\n\n请先在对应笔记端修复，再重新生成预览。",
                parent=self,
            )
            return
        primary = self.plan.options.primary
        versions = sorted(
            operation.versions.values(),
            key=lambda note: (0 if note.endpoint == primary else 1, note.endpoint.value),
        )
        if not versions:
            return
        if len(versions) == 1:
            source = versions[0]
            if not messagebox.askyesno(
                "恢复已删除副本",
                f"是否以 {source.endpoint.label} 当前版本为准，并在其他所选端恢复这条笔记？",
                parent=self,
            ):
                return
            merged = source
        else:
            merged = versions[0]
            for next_version in versions[1:]:
                dialog = DiffDialog(self, merged, next_version)
                self.wait_window(dialog)
                if dialog.result is None:
                    return
                merged = dialog.result

        try:
            self.plan_engine.resolve_conflict(self.plan, operation, merged)
        except ValueError as exc:
            messagebox.showwarning("无法处理此冲突", str(exc), parent=self)
            return
        self._render_plan()
        self.status_var.set("冲突已处理并加入可执行操作；仍需点击【执行勾选的安全操作】。")

    def _execute(self) -> None:
        if not self.plan or not self.plan_engine:
            messagebox.showinfo("没有同步预览", "请先生成只读同步预览。", parent=self)
            return
        if self.plan_config != self._collect_config().to_dict():
            messagebox.showwarning("设置已变化", "连接设置在预览后发生了变化，请重新生成预览。", parent=self)
            return
        try:
            current_options = self._collect_options()
        except Exception as exc:
            messagebox.showwarning("同步选项已变化", f"当前同步选项无效：{exc}\n\n请重新生成预览。", parent=self)
            return
        if current_options != self.plan.options:
            messagebox.showwarning("同步选项已变化", "同步方式、范围或目录选择在预览后发生了变化，请重新生成预览。", parent=self)
            return
        executable = self.plan.executable_operations()
        if not executable:
            messagebox.showinfo("没有可执行操作", "当前预览没有可安全执行的项目。", parent=self)
            return
        unresolved = sum(op.action == OperationAction.CONFLICT for op in self.plan.operations)
        deletes = sum(op.action == OperationAction.DELETE for op in executable)
        parts = [f"将执行 {len(executable)} 项操作。"]
        if unresolved:
            parts.append(f"另有 {unresolved} 项未处理冲突会被跳过。")
        if deletes:
            parts.append(
                f"其中 {deletes} 项会移除目标副本；Joplin 使用废纸篓，Obsidian 使用 Windows 回收站，"
                "思源移入统一的 Note Sync Hub 回收站。"
            )
        parts.append("执行前还会再次扫描；只要预览后有变化，就会自动停止。是否继续？")
        if not messagebox.askyesno("确认执行同步", "\n\n".join(parts), parent=self):
            return
        plan = self.plan
        engine = self.plan_engine
        self._run_async(
            "正在执行同步……",
            lambda: engine.execute(
                plan,
                cancel_event=self.cancel_event,
                progress=lambda current, total, message: self.events.put(
                    ("progress", current, total, message)
                ),
            ),
            self._execute_done,
        )

    def _execute_done(self, result) -> None:
        self.plan = None
        self.plan_engine = None
        self.plan_config = None
        self.plan_options = None
        self._render_plan()
        successful_targets = sum(item.success for item in result.targets)
        label = "同步已取消" if result.cancelled else "同步完成"
        summary = f"{label}：完成 {result.completed} 项，目标操作成功 {successful_targets} 次，跳过 {result.skipped} 项，错误 {len(result.errors)} 项。"
        self.status_var.set(summary)
        for item in result.targets:
            self._append_log(f"{'成功' if item.success else '失败'}：{item.title} → {item.endpoint.label} / {item.action.label}" + (f"：{item.error}" if item.error else ""))
        if not self._closing:
            if result.errors:
                messagebox.showwarning("同步结束但有错误", self._redact_runtime_secrets(summary + "\n\n" + "\n".join(result.errors)), parent=self)
            else:
                messagebox.showinfo(label, summary, parent=self)

    # --------------------------------------------------------- async runner

    def _run_async(self, label: str, task: Callable[[], object], success: Callable[[object], None]) -> None:
        if self._busy or self._closing:
            messagebox.showinfo("任务正在运行", "请等待当前任务完成，或先取消当前任务。", parent=self)
            return
        # Fix: clear cancel flag before each new task so a previous cancellation
        # doesn't immediately abort the next run.
        self.cancel_event.clear()
        self._active_secrets = (self.joplin_token_var.get(), self.siyuan_token_var.get())
        self.progress.configure(value=0, maximum=100, mode="indeterminate")
        self.progress.start(12)
        self.progress_text_var.set(label)
        self._set_busy(True)

        def runner() -> None:
            try:
                result = task()
            except Exception as exc:
                self.events.put(("error", exc, traceback.format_exc()))
            else:
                self.events.put(("success", success, result))
            finally:
                self.events.put(("finished",))

        self.worker = threading.Thread(target=runner, daemon=False)
        self.worker.start()

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._append_log(str(event[1]))
                elif kind == "progress":
                    current, total, message = int(event[1]), int(event[2]), str(event[3])
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=max(total, 1), value=current)
                    self.progress_text_var.set(message)
                elif kind == "success":
                    callback, result = event[1], event[2]
                    callback(result)
                elif kind == "error":
                    exc, detail = event[1], event[2]
                    self._append_log(detail)
                    title = "同步已停止" if isinstance(exc, SyncEngineError) else "操作失败"
                    safe_message = self._redact_runtime_secrets(str(exc))
                    if not isinstance(exc, ScanCancelled) and not self._closing:
                        messagebox.showerror(title, safe_message, parent=self)
                    self.status_var.set(safe_message)
                elif kind == "finished":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress_text_var.set("就绪")
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _append_log(self, message: str) -> None:
        message = self._redact_runtime_secrets(message)
        self.log_messages.append(message.rstrip())
        if len(self.log_messages) > 2000:
            del self.log_messages[:500]
        # Write to inline log widget
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _redact_runtime_secrets(self, text: str) -> str:
        return redact_sensitive_text(
            text,
            self.joplin_token_var.get(),
            self.siyuan_token_var.get(),
            *self._active_secrets,
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.preview_panel.busy = busy
        self.profile_panel.set_busy(busy)
        state = "disabled" if busy else "normal"
        for button in (self.test_button, self.refresh_button, self.preview_button, self.execute_button):
            button.configure(state=state)
        # resolve_button only makes sense when a plan exists
        self.resolve_button.configure(state=state if self.plan else "disabled")
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def _cancel_worker(self) -> None:
        self.cancel_event.set()
        self.progress_text_var.set("正在请求取消；当前单条笔记完成后停止……")

    def _on_close(self) -> None:
        if self._closing:
            return
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "任务仍在运行",
                "关闭会请求取消，并等待当前笔记处理完成、保存进度后退出。是否继续？",
                parent=self,
            ):
                return
            self._closing = True
            self.cancel_event.set()
            self._set_busy(True)
            self.progress_text_var.set("正在等待当前笔记完成并保存进度……")
            self.after(100, self._finish_close)
            return
        self.destroy()

    def _finish_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.after(100, self._finish_close)
        else:
            self.destroy()


def _set_windows_app_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NoteSyncHub.App")
    except (AttributeError, OSError):
        pass


def main() -> None:
    _set_windows_app_id()
    app = SyncApp()
    app.mainloop()


if __name__ == "__main__":
    main()

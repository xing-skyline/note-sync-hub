from __future__ import annotations

import ctypes
import queue
import threading
import traceback
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .config import AppConfig, load_config, save_config
from .diffmerge import DiffChoice, NoteDiff, build_note_diff
from .engine import SyncEngine, SyncEngineError
from .models import (
    Endpoint,
    Note,
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


def _asset_subset(body: str, *notes: Note) -> Dict[str, object]:
    combined = {}
    for note in notes:
        combined.update(note.assets)
    return {digest: asset for digest, asset in combined.items() if f"notesync-asset://{digest}/" in body}


class DiffDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, left: Note, right: Note):
        super().__init__(parent)
        self.left = left
        self.right = right
        self.result: Optional[Note] = None
        self.note_diff: NoteDiff = build_note_diff(
            left.body,
            right.body,
            left.endpoint.label,
            right.endpoint.label,
        )
        self._segments = {str(segment.index): segment for segment in self.note_diff.differences}
        self.meta_endpoint_var = tk.StringVar(value=left.endpoint.value)

        self.title(f"逐块比较与合并 — {left.title}")
        self.geometry("1220x790")
        self.minsize(920, 620)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._build()
        self.grab_set()
        self.focus_set()

    def _build(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="逐块比较与合并", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            root,
            text="每个差异块都要明确选择左侧、右侧或两份都保留；应用前不会修改任何笔记。",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 8))

        paths = ttk.Frame(root)
        paths.pack(fill="x", pady=(0, 8))
        ttk.Label(paths, text=f"左侧 {self.left.endpoint.label}：{self.left.folder}/{self.left.title}").pack(anchor="w")
        ttk.Label(paths, text=f"右侧 {self.right.endpoint.label}：{self.right.folder}/{self.right.title}").pack(anchor="w")

        body = ttk.Panedwindow(root, orient="horizontal")
        body.pack(fill="both", expand=True)
        segment_frame = ttk.Frame(body)
        compare_frame = ttk.Frame(body)
        body.add(segment_frame, weight=2)
        body.add(compare_frame, weight=5)

        self.segment_tree = ttk.Treeview(
            segment_frame,
            columns=("number", "kind", "choice"),
            show="headings",
            selectmode="extended",
        )
        for column, title, width in (
            ("number", "#", 42),
            ("kind", "差异类型", 92),
            ("choice", "处理方式", 220),
        ):
            self.segment_tree.heading(column, text=title)
            self.segment_tree.column(column, width=width, stretch=column == "choice")
        segment_scroll = ttk.Scrollbar(segment_frame, orient="vertical", command=self.segment_tree.yview)
        self.segment_tree.configure(yscrollcommand=segment_scroll.set)
        self.segment_tree.pack(side="left", fill="both", expand=True)
        segment_scroll.pack(side="right", fill="y")
        self.segment_tree.bind("<<TreeviewSelect>>", self._show_selected)
        for segment in self.note_diff.differences:
            iid = str(segment.index)
            self.segment_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    segment.index,
                    segment.kind_label,
                    segment.choice.label(self.left.endpoint.label, self.right.endpoint.label),
                ),
            )

        compare = ttk.Panedwindow(compare_frame, orient="horizontal")
        compare.pack(fill="both", expand=True)
        left_frame = ttk.LabelFrame(compare, text=self.left.endpoint.label, padding=6)
        right_frame = ttk.LabelFrame(compare, text=self.right.endpoint.label, padding=6)
        compare.add(left_frame, weight=1)
        compare.add(right_frame, weight=1)
        self.left_text = self._read_only_text(left_frame)
        self.right_text = self._read_only_text(right_frame)

        choices = ttk.LabelFrame(root, text="所选差异块", padding=8)
        choices.pack(fill="x", pady=(10, 6))
        for choice in (DiffChoice.USE_LEFT, DiffChoice.USE_RIGHT, DiffChoice.KEEP_BOTH):
            ttk.Button(
                choices,
                text=choice.label(self.left.endpoint.label, self.right.endpoint.label),
                command=lambda item=choice: self._set_choice(item),
            ).pack(side="left", padx=(0, 6))
        ttk.Separator(choices, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(
            choices,
            text=f"全部采用 {self.left.endpoint.label}",
            command=lambda: self._choose_all(DiffChoice.USE_LEFT),
        ).pack(side="left", padx=3)
        ttk.Button(
            choices,
            text=f"全部采用 {self.right.endpoint.label}",
            command=lambda: self._choose_all(DiffChoice.USE_RIGHT),
        ).pack(side="left", padx=3)
        self.unresolved_var = tk.StringVar()
        ttk.Label(choices, textvariable=self.unresolved_var).pack(side="right")

        metadata = ttk.LabelFrame(root, text="标题、标签和目录以哪一端为准", padding=7)
        metadata.pack(fill="x", pady=(0, 6))
        for note in (self.left, self.right):
            ttk.Radiobutton(
                metadata,
                text=f"{note.endpoint.label}（{note.title}）",
                value=note.endpoint.value,
                variable=self.meta_endpoint_var,
            ).pack(side="left", padx=(0, 12))

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(4, 0))
        ttk.Button(actions, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(actions, text="应用合并方案", style="Accent.TButton", command=self._apply).pack(
            side="right", padx=(0, 8)
        )

        self._refresh_unresolved()
        if self.note_diff.differences:
            first = str(self.note_diff.differences[0].index)
            self.segment_tree.selection_set(first)
            self.segment_tree.focus(first)
            self._show_selected()

    @staticmethod
    def _read_only_text(parent: tk.Misc) -> tk.Text:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="word", font=("Consolas", 10), padx=8, pady=8, undo=False)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set, state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return text

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _show_selected(self, _event=None) -> None:
        selected = self.segment_tree.selection()
        if not selected:
            return
        segment = self._segments[selected[0]]
        self._set_text(self.left_text, segment.left_preview or "（这一端没有对应内容）")
        self._set_text(self.right_text, segment.right_preview or "（这一端没有对应内容）")

    def _set_choice(self, choice: DiffChoice) -> None:
        selected = self.segment_tree.selection()
        if not selected:
            messagebox.showinfo("请选择差异块", "请先在左侧选择一个或多个差异块。", parent=self)
            return
        for iid in selected:
            segment = self._segments[iid]
            segment.choice = choice
            self.segment_tree.set(
                iid,
                "choice",
                choice.label(self.left.endpoint.label, self.right.endpoint.label),
            )
        self._refresh_unresolved()

    def _choose_all(self, choice: DiffChoice) -> None:
        self.note_diff.choose_all(choice)
        for iid, segment in self._segments.items():
            self.segment_tree.set(
                iid,
                "choice",
                segment.choice.label(self.left.endpoint.label, self.right.endpoint.label),
            )
        self._refresh_unresolved()

    def _refresh_unresolved(self) -> None:
        self.unresolved_var.set(f"尚未选择：{self.note_diff.unresolved_count} 个")

    def _apply(self) -> None:
        if self.note_diff.unresolved_count:
            messagebox.showwarning(
                "仍有未处理差异",
                f"还有 {self.note_diff.unresolved_count} 个差异块没有选择处理方式。",
                parent=self,
            )
            return
        try:
            merged_body, _same_body = self.note_diff.render()
        except ValueError as exc:
            messagebox.showerror("合并方案无效", str(exc), parent=self)
            return
        metadata_note = self.left if self.meta_endpoint_var.get() == self.left.endpoint.value else self.right
        self.result = replace(
            metadata_note,
            body=merged_body,
            assets=_asset_subset(merged_body, self.left, self.right),
        )
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class AdvancedSettingsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, config: AppConfig):
        super().__init__(parent)
        self.result: Optional[Tuple[int, str, str, str]] = None
        self.title("高级设置")
        self.geometry("620x275")
        self.resizable(False, False)
        self.transient(parent)
        self.timeout_var = tk.StringVar(value=str(config.request_timeout))
        self.attachment_var = tk.StringVar(value=config.obsidian_attachments_folder)
        self.joplin_default_var = tk.StringVar(value=config.joplin_default_notebook)
        self.siyuan_default_var = tk.StringVar(value=config.siyuan_default_notebook)

        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        rows = (
            ("网络超时（秒）", self.timeout_var),
            ("Obsidian 默认附件目录", self.attachment_var),
            ("Joplin 根目录写入时使用的笔记本", self.joplin_default_var),
            ("思源根目录写入时使用的笔记本", self.siyuan_default_var),
        )
        for row, (label, variable) in enumerate(rows):
            ttk.Label(root, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=6)
            ttk.Entry(root, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=6)
        ttk.Label(
            root,
            text="这些默认目录只在目标路径没有明确笔记本/附件目录时使用。",
            style="Subtitle.TLabel",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 10))
        actions = ttk.Frame(root)
        actions.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(actions, text="确定", command=self._apply).pack(side="right", padx=(0, 8))
        self.grab_set()

    def _apply(self) -> None:
        try:
            timeout = int(self.timeout_var.get())
            if timeout < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("设置无效", "网络超时必须是大于 0 的整数。", parent=self)
            return
        self.result = (
            timeout,
            self.attachment_var.get().strip() or "attachments",
            self.joplin_default_var.get().strip() or "Note Sync Hub",
            self.siyuan_default_var.get().strip() or "Note Sync Hub",
        )
        self.destroy()


class SyncApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Note Sync Hub — Joplin / Obsidian / 思源安全同步")
        self.geometry("1380x940")
        self.minsize(1100, 760)
        try:
            self.state("zoomed")
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.events: queue.Queue = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
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
        self._configure_style()
        self._load_variables()
        self._build()
        self._toggle_options()
        self.after(100, self._drain_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Subtitle.TLabel", font=("Microsoft YaHei UI", 9), foreground="#555555")
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview", rowheight=25)

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

    def _build(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="Note Sync Hub", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Joplin、Obsidian、思源笔记：先生成只读预览，再执行；多端冲突不会自动覆盖。",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(1, 0))

        connection = ttk.LabelFrame(root, text="1. 连接设置与启用的笔记端", padding=8)
        connection.pack(fill="x", pady=(0, 8))
        connection.columnconfigure(2, weight=1)
        connection.columnconfigure(4, weight=1)

        headers = ("启用", "笔记端", "地址或目录", "", "Token")
        for column, label in enumerate(headers):
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
                connection,
                variable=self.endpoint_vars[endpoint],
                command=self._toggle_options,
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

        connection_actions = ttk.Frame(connection)
        connection_actions.grid(row=1, column=5, rowspan=3, sticky="nsew", padx=(10, 0))
        self.test_button = ttk.Button(connection_actions, text="测试所选连接", command=self._test_connections)
        self.test_button.pack(fill="x")
        ttk.Button(connection_actions, text="高级设置…", command=self._advanced_settings).pack(fill="x", pady=(5, 0))
        ttk.Button(connection_actions, text="保存设置", command=self._save_settings).pack(fill="x", pady=(5, 0))

        options = ttk.LabelFrame(root, text="2. 同步方式、范围与安全选项", padding=8)
        options.pack(fill="x", pady=(0, 8))
        line1 = ttk.Frame(options)
        line1.pack(fill="x")
        ttk.Label(line1, text="方式：").pack(side="left")
        for label in MODE_LABELS:
            ttk.Radiobutton(
                line1,
                text=label,
                value=label,
                variable=self.mode_var,
                command=self._toggle_options,
            ).pack(side="left", padx=(0, 12))
        direction_line = ttk.Frame(options)
        direction_line.pack(fill="x", pady=(7, 0))
        ttk.Label(direction_line, text="单向来源：").pack(side="left")
        self.source_combo = ttk.Combobox(direction_line, textvariable=self.source_var, state="readonly", width=12)
        self.source_combo.pack(side="left")
        self.source_combo.bind("<<ComboboxSelected>>", lambda _event: self._toggle_options())
        ttk.Label(direction_line, text="单向目标：").pack(side="left", padx=(18, 4))
        self.target_endpoint_checks: Dict[Endpoint, ttk.Checkbutton] = {}
        for endpoint in Endpoint:
            check = ttk.Checkbutton(
                direction_line,
                text=endpoint.label,
                variable=self.target_endpoint_vars[endpoint],
                command=self._toggle_options,
            )
            check.pack(side="left", padx=(0, 10))
            self.target_endpoint_checks[endpoint] = check
        ttk.Label(
            direction_line,
            text="目标可多选",
            style="Subtitle.TLabel",
        ).pack(side="left", padx=(6, 0))
        ttk.Separator(direction_line, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Label(direction_line, text="双向主端：").pack(side="left")
        self.primary_combo = ttk.Combobox(
            direction_line,
            textvariable=self.primary_var,
            state="disabled",
            width=12,
        )
        self.primary_combo.pack(side="left")

        line2 = ttk.Frame(options)
        line2.pack(fill="x", pady=(7, 0))
        ttk.Radiobutton(
            line2,
            text="全部笔记",
            value="all",
            variable=self.scope_var,
            command=self._toggle_options,
        ).pack(side="left")
        ttk.Radiobutton(
            line2,
            text="仅所选目录",
            value="selected",
            variable=self.scope_var,
            command=self._toggle_options,
        ).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(line2, text="包含子目录", variable=self.include_subfolders_var).pack(side="left", padx=(12, 0))
        ttk.Label(line2, text="单向目标位置：").pack(side="left", padx=(22, 4))
        self.target_mode_combo = ttk.Combobox(
            line2,
            textvariable=self.target_mode_var,
            values=list(TARGET_MODE_LABELS),
            state="readonly",
            width=24,
        )
        self.target_mode_combo.pack(side="left")
        self.target_mode_combo.bind("<<ComboboxSelected>>", lambda _event: self._toggle_options())

        line3 = ttk.Frame(options)
        line3.pack(fill="x", pady=(7, 0))
        self.delete_check = ttk.Checkbutton(
            line3,
            textvariable=self.delete_text_var,
            variable=self.propagate_deletions_var,
        )
        self.delete_check.pack(side="left")
        ttk.Label(
            line3,
            textvariable=self.delete_hint_var,
            foreground="#9a5b00",
        ).pack(side="left", padx=(12, 0))

        folders = ttk.LabelFrame(root, text="3. 目录范围与单向目标目录", padding=8)
        folders.pack(fill="x", pady=(0, 8))
        panes = ttk.Panedwindow(folders, orient="horizontal")
        panes.pack(fill="x")
        for endpoint in Endpoint:
            frame = ttk.LabelFrame(panes, text=endpoint.label, padding=6)
            panes.add(frame, weight=1)
            list_frame = ttk.Frame(frame)
            list_frame.pack(fill="both", expand=True)
            listbox = tk.Listbox(list_frame, height=3, selectmode="extended", exportselection=False)
            scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
            listbox.configure(yscrollcommand=scrollbar.set)
            listbox.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            self.folder_lists[endpoint] = listbox
            target_line = ttk.Frame(frame)
            target_line.pack(fill="x", pady=(5, 0))
            ttk.Label(target_line, text="单向写入到：").pack(side="left")
            combo = ttk.Combobox(
                target_line,
                textvariable=self.target_folder_vars[endpoint],
                state="disabled",
            )
            combo.pack(side="left", fill="x", expand=True)
            self.target_folder_combos[endpoint] = combo

        folder_actions = ttk.Frame(folders)
        folder_actions.pack(fill="x", pady=(7, 0))
        self.refresh_button = ttk.Button(folder_actions, text="刷新所选端目录", command=self._refresh_folders)
        self.refresh_button.pack(side="left")
        ttk.Label(
            folder_actions,
            text="思源的文档也可作为目录；选中文档时会包含该文档本身，勾选子目录后还会包含其子文档。",
            style="Subtitle.TLabel",
        ).pack(side="left", padx=(10, 0))
        self.preview_button = ttk.Button(
            folder_actions,
            text="生成只读同步预览",
            style="Accent.TButton",
            command=self._preview,
        )
        self.preview_button.pack(side="right")

        preview = ttk.LabelFrame(root, text="4. 同步预览（双击冲突也可处理）", padding=8)
        preview.pack(fill="both", expand=True, pady=(0, 8))
        columns = ("action", "title", "direction", "reason")
        self.preview_tree = ttk.Treeview(
            preview,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=5,
        )
        for column, title, width, stretch in (
            ("action", "操作", 86, False),
            ("title", "笔记", 220, True),
            ("direction", "方向", 260, True),
            ("reason", "原因 / 安全说明", 650, True),
        ):
            self.preview_tree.heading(column, text=title)
            self.preview_tree.column(column, width=width, stretch=stretch)
        preview_scroll_y = ttk.Scrollbar(preview, orient="vertical", command=self.preview_tree.yview)
        preview_scroll_x = ttk.Scrollbar(preview, orient="horizontal", command=self.preview_tree.xview)
        self.preview_tree.configure(yscrollcommand=preview_scroll_y.set, xscrollcommand=preview_scroll_x.set)
        self.preview_tree.grid(row=0, column=0, sticky="nsew")
        preview_scroll_y.grid(row=0, column=1, sticky="ns")
        preview_scroll_x.grid(row=1, column=0, sticky="ew")
        preview.rowconfigure(0, weight=1)
        preview.columnconfigure(0, weight=1)
        self.preview_tree.bind("<Double-1>", lambda _event: self._resolve_selected_conflict())
        self.preview_tree.tag_configure("conflict", foreground="#a33a2b")
        self.preview_tree.tag_configure("delete", foreground="#9a5b00")
        self.preview_tree.tag_configure("skip", foreground="#777777")

        bottom = ttk.Frame(root)
        bottom.pack(fill="x")
        self.resolve_button = ttk.Button(bottom, text="比较并处理所选冲突…", command=self._resolve_selected_conflict)
        self.resolve_button.pack(side="left")
        ttk.Button(bottom, text="查看运行日志…", command=self._show_log).pack(side="left", padx=(8, 0))
        self.execute_button = ttk.Button(
            bottom,
            text="执行预览中的安全操作",
            style="Accent.TButton",
            command=self._execute,
        )
        self.execute_button.pack(side="right")
        self.cancel_button = ttk.Button(bottom, text="取消当前任务", command=self._cancel_worker, state="disabled")
        self.cancel_button.pack(side="right", padx=(0, 8))
        self.progress = ttk.Progressbar(bottom, mode="determinate", length=250)
        self.progress.pack(side="right", padx=(0, 10))
        ttk.Label(bottom, textvariable=self.progress_text_var).pack(side="right", padx=(0, 8))

        ttk.Label(root, textvariable=self.status_var, style="Subtitle.TLabel", wraplength=1280).pack(
            fill="x", pady=(5, 3)
        )

    def _enabled_endpoints(self) -> Tuple[Endpoint, ...]:
        return tuple(endpoint for endpoint in Endpoint if self.endpoint_vars[endpoint].get())

    def _sync_endpoints(self) -> Tuple[Endpoint, ...]:
        enabled = self._enabled_endpoints()
        if MODE_LABELS[self.mode_var.get()] == SyncMode.BIDIRECTIONAL:
            return enabled
        source = next((endpoint for endpoint in enabled if endpoint.label == self.source_var.get()), None)
        targets = tuple(
            endpoint
            for endpoint in enabled
            if endpoint != source and self.target_endpoint_vars[endpoint].get()
        )
        return tuple(endpoint for endpoint in Endpoint if endpoint == source or endpoint in targets)

    def _toggle_options(self) -> None:
        enabled_endpoints = self._enabled_endpoints()
        values = [endpoint.label for endpoint in enabled_endpoints]
        self.source_combo.configure(values=values)
        if self.source_var.get() not in values and values:
            self.source_var.set(values[0])
        one_way = MODE_LABELS[self.mode_var.get()] == SyncMode.ONE_WAY
        self.source_combo.configure(state="readonly" if one_way and values else "disabled")
        selected_scope = self.scope_var.get() == "selected"
        mapping = one_way and selected_scope
        if not mapping:
            preserve_label = next(
                label for label, target_mode in TARGET_MODE_LABELS.items()
                if target_mode == TargetMode.PRESERVE
            )
            self.target_mode_var.set(preserve_label)
        self.target_mode_combo.configure(state="readonly" if mapping else "disabled")
        selected_mode = TARGET_MODE_LABELS[self.target_mode_var.get()] == TargetMode.SELECTED
        source = next((endpoint for endpoint in enabled_endpoints if endpoint.label == self.source_var.get()), None)
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
            self.delete_hint_var.set("开启后使用废纸篓/回收站；思源不会自动删除。")
        else:
            self.delete_text_var.set("将双向主端的删除同步到其他端（危险，默认关闭）")
            self.delete_hint_var.set("非主端删除会从主端恢复；思源不会自动删除。")
        endpoints = self._sync_endpoints()

        for endpoint, listbox in self.folder_lists.items():
            enabled = endpoint in endpoints and selected_scope and (not one_way or endpoint == source)
            listbox.configure(state="normal" if enabled else "disabled")
            target_enabled = mapping and selected_mode and endpoint in endpoints and endpoint != source
            self.target_folder_combos[endpoint].configure(state="readonly" if target_enabled else "disabled")

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
        return tuple(self.folder_values[endpoint][index] for index in listbox.curselection())

    def _collect_options(self) -> SyncOptions:
        endpoints = self._sync_endpoints()
        mode = MODE_LABELS[self.mode_var.get()]
        scope_all = self.scope_var.get() == "all"
        source = (
            next((endpoint for endpoint in endpoints if endpoint.label == self.source_var.get()), None)
            if mode == SyncMode.ONE_WAY
            else None
        )
        primary = (
            next((endpoint for endpoint in endpoints if endpoint.label == self.primary_var.get()), None)
            if mode == SyncMode.BIDIRECTIONAL
            else None
        )
        mapping_enabled = mode == SyncMode.ONE_WAY and not scope_all
        target_mode = (
            TARGET_MODE_LABELS[self.target_mode_var.get()]
            if mapping_enabled
            else TargetMode.PRESERVE
        )
        selected_folders = {endpoint: self._selected_folders(endpoint) for endpoint in endpoints}
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
                endpoint: self.target_folder_vars[endpoint].get().strip()
                for endpoint in endpoints
                if target_mode == TargetMode.SELECTED and endpoint != source
            },
            propagate_deletions=self.propagate_deletions_var.get(),
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
        return SyncEngine(config, logger=lambda message: self.events.put(("log", message)))

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
        message = "\n".join(results[endpoint] for endpoint in results)
        self.status_var.set(message.replace("\n", "；"))
        messagebox.showinfo("连接测试完成", message, parent=self)

    def _refresh_folders(self) -> None:
        endpoints = self._sync_endpoints()
        if len(endpoints) < 1:
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
            display_values = values or [""]
            self.folder_values[endpoint] = display_values
            listbox = self.folder_lists[endpoint]
            listbox.configure(state="normal")
            listbox.delete(0, "end")
            for value in display_values:
                listbox.insert("end", value or "（根目录）")
            combo_values = [value for value in display_values if value]
            self.target_folder_combos[endpoint].configure(values=combo_values)
            if self.target_folder_vars[endpoint].get() not in combo_values:
                self.target_folder_vars[endpoint].set(combo_values[0] if combo_values else "")
        self._toggle_options()
        summary = "；".join(f"{endpoint.label} {len(values)} 个目录" for endpoint, values in folders.items())
        self.status_var.set("目录刷新完成：" + summary)

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
        for iid in self.preview_tree.get_children():
            self.preview_tree.delete(iid)
        self.operation_by_iid.clear()
        if not self.plan:
            return
        for index, operation in enumerate(self.plan.operations):
            iid = f"op-{index}"
            tag = ""
            if operation.action == OperationAction.CONFLICT:
                tag = "conflict"
            elif operation.action == OperationAction.DELETE:
                tag = "delete"
            elif operation.action == OperationAction.SKIP:
                tag = "skip"
            self.preview_tree.insert(
                "",
                "end",
                iid=iid,
                values=(operation.action.label, operation.title, operation.direction_label, operation.reason),
                tags=(tag,) if tag else (),
            )
            self.operation_by_iid[iid] = operation

    def _resolve_selected_conflict(self) -> None:
        selected = self.preview_tree.selection()
        if not selected:
            messagebox.showinfo("请选择预览项", "请先在同步预览中选择一条冲突。", parent=self)
            return
        operation = self.operation_by_iid[selected[0]]
        if operation.action != OperationAction.CONFLICT:
            messagebox.showinfo("无需处理", "所选项目不是待处理冲突。", parent=self)
            return
        if not self.plan or not operation.global_id:
            messagebox.showwarning(
                "无法在软件内自动处理",
                "这是重复同步 ID 或同路径多条笔记造成的歧义，请先到对应笔记软件中人工整理重复项。",
                parent=self,
            )
            return
        if "附件" in operation.reason and "无法" in operation.reason:
            messagebox.showwarning(
                "请先修复附件",
                "该冲突来自缺失或不明确的附件。请先在来源笔记中修复链接，再重新生成预览。",
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

        operation.resolved_note = merged
        operation.source = merged.endpoint
        operation.targets = tuple(self.plan.options.endpoints)
        operation.target_folders = {
            endpoint: operation.versions[endpoint].folder if endpoint in operation.versions else merged.folder
            for endpoint in operation.targets
        }
        operation.action = OperationAction.UPDATE
        operation.reason = "已人工逐块比较；执行时会把确认后的版本写入所有所选端。"
        self._render_plan()
        self.status_var.set("冲突已处理并加入可执行操作；仍需点击“执行预览中的安全操作”。")

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
        unresolved = sum(operation.action == OperationAction.CONFLICT for operation in self.plan.operations)
        deletes = sum(operation.action == OperationAction.DELETE for operation in executable)
        parts = [f"将执行 {len(executable)} 项操作。"]
        if unresolved:
            parts.append(f"另有 {unresolved} 项未处理冲突会被跳过。")
        if deletes:
            parts.append(f"其中 {deletes} 项会删除目标副本；Joplin 使用废纸篓，Obsidian 使用 Windows 回收站。")
        parts.append("执行前还会再次扫描；只要预览后有变化，就会自动停止。是否继续？")
        if not messagebox.askyesno("确认执行同步", "\n\n".join(parts), parent=self):
            return
        self.cancel_event.clear()
        engine = self.plan_engine
        plan = self.plan
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
        for iid in self.preview_tree.get_children():
            self.preview_tree.delete(iid)
        self.operation_by_iid.clear()
        summary = f"完成 {result.completed} 项，跳过 {result.skipped} 项，错误 {len(result.errors)} 项。"
        self.status_var.set(summary)
        if result.errors:
            messagebox.showwarning("同步完成但有错误", summary + "\n\n" + "\n".join(result.errors), parent=self)
        else:
            messagebox.showinfo("同步完成", summary, parent=self)

    def _run_async(self, label: str, task: Callable[[], object], success: Callable[[object], None]) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("任务正在运行", "请等待当前任务完成，或先取消当前任务。", parent=self)
            return
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

        self.worker = threading.Thread(target=runner, daemon=True)
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
                    messagebox.showerror(title, str(exc), parent=self)
                    self.status_var.set(str(exc))
                elif kind == "finished":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress_text_var.set("就绪")
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _append_log(self, message: str) -> None:
        self.log_messages.append(message.rstrip())
        if len(self.log_messages) > 2000:
            del self.log_messages[:500]

    def _show_log(self) -> None:
        window = tk.Toplevel(self)
        window.title("Note Sync Hub 运行日志")
        window.geometry("900x560")
        window.transient(self)
        frame = ttk.Frame(window, padding=10)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="word", font=("Consolas", 9), undo=False)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", "\n".join(self.log_messages) or "（当前还没有运行日志）")
        text.configure(state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for button in (self.test_button, self.refresh_button, self.preview_button, self.execute_button, self.resolve_button):
            button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def _cancel_worker(self) -> None:
        self.cancel_event.set()
        self.progress_text_var.set("正在请求取消；当前单条笔记完成后停止……")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "任务仍在运行",
                "当前任务仍在运行。关闭窗口会请求取消，当前正在写入的单条笔记可能仍会完成。是否关闭？",
                parent=self,
            ):
                return
            self.cancel_event.set()
        self.destroy()


def _set_windows_app_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("xing-skyline.NoteSyncHub")
    except (AttributeError, OSError):
        pass


def main() -> None:
    _set_windows_app_id()
    app = SyncApp()
    app.mainloop()

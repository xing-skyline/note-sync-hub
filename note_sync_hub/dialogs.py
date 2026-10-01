from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from dataclasses import replace
from typing import Dict, Optional, Tuple

from .config import AppConfig
from .diffmerge import DiffChoice, NoteDiff, build_note_diff
from .models import Note


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
        self.left_text = self._scrolled_text(left_frame)
        self.right_text = self._scrolled_text(right_frame)

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
    def _scrolled_text(parent: tk.Misc) -> tk.Text:
        """Read-only text widget with both scrollbars."""
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text = tk.Text(frame, wrap="none", font=("Consolas", 10), padx=8, pady=8, undo=False,
                       bg="#ffffff", fg="#1f2937", relief="flat",
                       highlightthickness=1, highlightbackground="#dbe2ec")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set, state="disabled")
        text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
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
        self.geometry("640x310")
        self.resizable(True, False)
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

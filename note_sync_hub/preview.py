"""Preview table: filter, inspect paths, and select approved operations."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .models import OperationAction


class PreviewPanel(ttk.Frame):
    def __init__(self, parent, resolve):
        super().__init__(parent)
        self.plan = None
        self.busy = False
        self.operations = {}
        self.filter_var = tk.StringVar(value="全部")
        self.search_var = tk.StringVar()
        self.summary_var = tk.StringVar(value="生成预览后，可勾选本次执行的项目。")
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 5))
        ttk.Label(toolbar, text="显示：").pack(side="left")
        ttk.Combobox(toolbar, textvariable=self.filter_var, state="readonly", width=12,
                     values=["全部", "可执行", *[action.label for action in OperationAction]]).pack(side="left")
        ttk.Label(toolbar, text="搜索：").pack(side="left", padx=(10, 0))
        ttk.Entry(toolbar, textvariable=self.search_var, width=20).pack(side="left", fill="x", expand=True)
        ttk.Button(toolbar, text="勾选可见项", command=lambda: self.select_visible(True)).pack(side="left", padx=(5, 0))
        ttk.Button(toolbar, text="取消勾选", command=lambda: self.select_visible(False)).pack(side="left")
        table = ttk.Frame(self)
        table.pack(fill="both", expand=True)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        columns = ("selected", "action", "title", "direction", "source_path", "target_path", "reason")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        for column, label, width in zip(columns,
                ("执行", "操作", "笔记", "方向", "来源路径", "目标路径", "原因 / 安全说明"),
                (48, 90, 160, 190, 230, 260, 380)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, stretch=column not in {"selected", "action"})
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("conflict", foreground="#a33a2b")
        self.tree.tag_configure("delete", foreground="#9a5b00")
        self.tree.tag_configure("skip", foreground="#777777")
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", self._space)
        self.tree.bind("<Double-1>", lambda event: resolve() if not self.busy and self.tree.identify_column(event.x) != "#1" else None)
        self.filter_var.trace_add("write", lambda *_: self.render())
        self.search_var.trace_add("write", lambda *_: self.render())
        ttk.Label(self, textvariable=self.summary_var).pack(anchor="w", pady=(4, 0))

    def set_plan(self, plan):
        self.plan = plan
        self.render()

    @staticmethod
    def paths(operation):
        source = operation.source_note
        source_path = f"{source.endpoint.label}: {source.locator or '/'.join(filter(None, (source.folder, source.title)))}" if source else ""
        targets = operation.targets or tuple(operation.versions)
        paths = []
        for endpoint in targets:
            if endpoint in operation.target_paths:
                path = operation.target_paths[endpoint]
            elif endpoint in operation.versions:
                note = operation.versions[endpoint]
                path = note.locator or "/".join(filter(None, (note.folder, note.title)))
            else:
                path = "/".join(filter(None, (operation.target_folders.get(endpoint, ""), operation.title)))
            paths.append(f"{endpoint.label}: {path}")
        return source_path, "；".join(paths)

    def render(self):
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.operations.clear()
        if not self.plan:
            self.summary_var.set("生成预览后，可勾选本次执行的项目。")
            return
        query = self.search_var.get().strip().casefold()
        wanted = self.filter_var.get()
        for index, operation in enumerate(self.plan.operations):
            source_path, target_path = self.paths(operation)
            if wanted == "可执行" and not operation.executable:
                continue
            if wanted not in {"全部", "可执行", operation.action.label}:
                continue
            if query and query not in f"{operation.title} {source_path} {target_path} {operation.reason}".casefold():
                continue
            iid = f"op-{index}"
            mark = ("☑" if operation.selected else "☐") if operation.executable else "—"
            self.tree.insert("", "end", iid=iid, values=(mark, operation.action.label, operation.title,
                             operation.direction_label, source_path, target_path, operation.reason), tags=(operation.action.value,))
            self.operations[iid] = operation
        for iid in selected:
            if iid in self.operations:
                self.tree.selection_set(iid)
        count = len(self.plan.executable_operations())
        self.summary_var.set(f"已勾选 {count} 项；当前显示 {len(self.operations)} / {len(self.plan.operations)} 项。筛选不会改变其他项目的勾选状态。")

    def select_visible(self, selected):
        if self.busy:
            return
        for operation in self.operations.values():
            if operation.executable:
                operation.selected = selected
        self.render()

    def _toggle(self, iid):
        operation = self.operations.get(iid)
        if not self.busy and operation and operation.executable:
            operation.selected = not operation.selected
            self.render()

    def _click(self, event):
        if self.tree.identify_column(event.x) == "#1":
            self._toggle(self.tree.identify_row(event.y))

    def _space(self, _event):
        for iid in self.tree.selection():
            self._toggle(iid)
        return "break"

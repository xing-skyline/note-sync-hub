import tkinter as tk
from tkinter import messagebox, ttk

from .config import save_config
from .profiles import ProfileStore


class ProfilePanel(ttk.Frame):
    def __init__(self, parent, collect_config, collect_options, apply_options, status):
        super().__init__(parent)
        self.collect_config, self.collect_options = collect_config, collect_options
        self.apply_options, self.status = apply_options, status
        self.store = ProfileStore()
        self.name_var = tk.StringVar()
        self.busy = False
        ttk.Label(self, text="同步方案：").pack(side="left")
        self.combo = ttk.Combobox(self, textvariable=self.name_var, width=25, postcommand=self.refresh)
        self.combo.pack(side="left", padx=(0, 6))
        self.load_button = ttk.Button(self, text="载入方案", command=self.load)
        self.load_button.pack(side="left")
        self.save_button = ttk.Button(self, text="保存当前方案", command=self.save)
        self.save_button.pack(side="left", padx=6)
        ttk.Label(self, text="输入名称保存；下次启动恢复最后保存的方案。", style="Subtitle.TLabel").pack(side="left")

    def identity(self):
        return self.collect_config().state_path().stem

    def refresh(self):
        try:
            self.combo.configure(values=self.store.names(self.identity()))
        except ValueError as exc:
            self.status(str(exc))

    def restore_last(self):
        self.refresh()
        try:
            name = self.store.last_name(self.identity())
            if name:
                self.name_var.set(name)
                self.apply_options(self.store.get(name, self.identity()))
                self.status(f"已恢复同步方案“{name}”。生成预览后再执行。")
        except ValueError as exc:
            self.status(str(exc))

    def load(self):
        if self.busy:
            return
        try:
            options = self.store.get(self.name_var.get().strip(), self.identity())
            self.apply_options(options)
            self.status(f"已载入方案“{self.name_var.get()}”；请重新生成预览。")
        except ValueError as exc:
            messagebox.showwarning("无法载入方案", str(exc), parent=self)

    def save(self):
        if self.busy:
            return
        try:
            config = self.collect_config()
            options = self.collect_options()
            self.store.save(self.name_var.get(), options, self.identity())
            save_config(config)
            self.refresh()
            self.status(f"同步方案“{self.name_var.get().strip()}”及连接设置已保存。")
        except (ValueError, OSError) as exc:
            messagebox.showwarning("无法保存方案", str(exc), parent=self)

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.combo, self.load_button, self.save_button):
            widget.configure(state="disabled" if busy else "normal")

import tkinter as tk
from tkinter import ttk


def configure_style(self) -> None:
    style = ttk.Style(self)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    BG      = "#eef1f6"
    CARD    = "#ffffff"
    BORDER  = "#dbe2ec"
    INK     = "#1f2937"
    MUTED   = "#64748b"
    ACCENT  = "#2563eb"
    ACCENT_D= "#1e40af"
    GHOST   = "#e8edf5"
    GHOST_A = "#dbe3ee"
    WARN    = "#b45309"

    # White card surface everywhere; the gray only peeks at the window edge.
    # Keeping one uniform background avoids gray/white mismatches on the
    # dozens of nested frames and labels.
    self.configure(background=BG)
    style.configure(".", font=("Microsoft YaHei UI", 10), background=CARD, foreground=INK)

    style.configure("TFrame", background=CARD)
    style.configure("App.TFrame", background=BG)

    style.configure("TLabel", background=CARD, foreground=INK)
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"), foreground=INK, background=BG)
    style.configure("Subtitle.TLabel", font=("Microsoft YaHei UI", 9), foreground=MUTED, background=CARD)
    style.configure("App.Subtitle.TLabel", font=("Microsoft YaHei UI", 9), foreground=MUTED, background=BG)
    style.configure("Warn.TLabel", background=CARD, foreground=WARN)

    style.configure("TLabelframe", background=CARD, bordercolor=BORDER, relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=CARD, foreground=INK, font=("Microsoft YaHei UI", 10, "bold"))

    style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 6), borderwidth=1)
    style.map("TButton", relief=[("pressed", "sunken")])

    style.configure("Accent.TButton",
        font=("Microsoft YaHei UI", 10, "bold"),
        background=ACCENT, foreground="#ffffff",
        bordercolor=ACCENT, padding=(14, 7))
    style.map("Accent.TButton",
        background=[("disabled", "#bfdbfe"), ("pressed", ACCENT_D), ("active", ACCENT_D)],
        bordercolor=[("disabled", "#bfdbfe"), ("active", ACCENT_D)],
        foreground=[("disabled", "#ffffff")])

    style.configure("Ghost.TButton",
        background=GHOST, foreground=INK, bordercolor=GHOST, padding=(12, 6))
    style.map("Ghost.TButton",
        background=[("disabled", GHOST), ("pressed", GHOST_A), ("active", GHOST_A)],
        foreground=[("disabled", "#9aa7b8")])

    style.configure("TRadiobutton", background=CARD, foreground=INK)
    style.configure("TCheckbutton", background=CARD, foreground=INK)
    style.map("TRadiobutton", background=[("active", CARD)])
    style.map("TCheckbutton", background=[("active", CARD)])

    style.configure("TEntry", padding=5, fieldbackground=CARD, bordercolor=BORDER)
    style.configure("TCombobox", padding=4, fieldbackground=CARD, bordercolor=BORDER)
    style.configure("TSeparator", background=BORDER)

    style.configure("Treeview",
        background=CARD, fieldbackground=CARD, foreground=INK,
        rowheight=28, borderwidth=0, font=("Microsoft YaHei UI", 9))
    style.configure("Treeview.Heading",
        background="#f1f5f9", foreground="#334155",
        font=("Microsoft YaHei UI", 9, "bold"), relief="flat", padding=(6, 6))
    style.map("Treeview",
        background=[("selected", "#dbeafe")],
        foreground=[("selected", INK)])

    style.configure("Vertical.TScrollbar",
        background="#cbd5e1", troughcolor=BG, borderwidth=0, arrowcolor=INK)
    style.configure("Horizontal.TScrollbar",
        background="#cbd5e1", troughcolor=BG, borderwidth=0, arrowcolor=INK)
    style.configure("TProgressbar",
        background=ACCENT, troughcolor="#e2e8f0", borderwidth=0, thickness=8)

    # Store for use in tk.Text widgets
    self._card_bg = CARD
    self._ink = INK
    self._border = BORDER

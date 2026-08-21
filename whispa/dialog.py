"""The manual 'that's not what I said' dialog.

Automatic learning needs UI Automation, which not every application exposes.
This is the fallback that always works: it shows what was typed, you fix it,
and the difference is learnt exactly as if it had been read back automatically.

Must be called on the tkinter thread - use `Overlay.call_soon`.
"""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)


def show_correction_dialog(
    original: str,
    on_corrected: Callable[[str], None],
    parent=None,
) -> None:
    import tkinter as tk

    if not original:
        return

    win = tk.Toplevel(parent) if parent is not None else tk.Tk()
    win.title("Fix last dictation")
    win.attributes("-topmost", True)
    win.resizable(False, False)
    win.configure(bg="#1b1b22")

    tk.Label(
        win,
        text="whispa typed this. Edit it to what you actually said:",
        bg="#1b1b22",
        fg="#c9c9d4",
        font=("Segoe UI", 9),
        wraplength=460,
        justify="left",
    ).pack(padx=16, pady=(16, 8), anchor="w")

    text = tk.Text(
        win, width=58, height=4, wrap="word",
        bg="#24242e", fg="#f0f0f6", insertbackground="#f0f0f6",
        relief="flat", font=("Segoe UI", 10), padx=8, pady=8,
    )
    text.insert("1.0", original)
    text.pack(padx=16)
    text.focus_set()

    hint = tk.Label(
        win,
        text="Only fix the misheard words - rewriting the sentence teaches nothing.",
        bg="#1b1b22", fg="#7f7f8c", font=("Segoe UI", 8),
    )
    hint.pack(padx=16, pady=(6, 0), anchor="w")

    buttons = tk.Frame(win, bg="#1b1b22")
    buttons.pack(padx=16, pady=14, fill="x")

    def submit(_event=None):
        corrected = text.get("1.0", "end").strip()
        win.destroy()
        if corrected and corrected != original.strip():
            try:
                on_corrected(corrected)
            except Exception:
                log.exception("could not record the correction")

    def cancel(_event=None):
        win.destroy()

    tk.Button(
        buttons, text="Cancel", command=cancel, relief="flat",
        bg="#2c2c38", fg="#c9c9d4", padx=14, pady=4, font=("Segoe UI", 9),
    ).pack(side="right")
    tk.Button(
        buttons, text="Learn this", command=submit, relief="flat",
        bg="#3d6bff", fg="#ffffff", padx=14, pady=4, font=("Segoe UI", 9, "bold"),
    ).pack(side="right", padx=(0, 8))

    win.bind("<Control-Return>", submit)
    win.bind("<Escape>", cancel)

    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    x = (win.winfo_screenwidth() - w) // 2
    y = (win.winfo_screenheight() - h) // 3
    win.geometry(f"+{x}+{y}")
    win.lift()

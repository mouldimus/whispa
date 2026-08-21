"""The debug console reachable from the tray.

Running under pythonw there is no stdout, so "show me what's going on" has to
be answered by the app itself. A plain console window would only show what
happens *after* it is opened, which is the wrong half - by the time you notice
something is wrong, the interesting lines have already been emitted. So log
records are kept in a ring buffer from startup, and the window opens showing
that history and then streams new lines into it.

The buffer is an ordinary logging handler and is tested; the window needs a
display and is not.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

log = logging.getLogger(__name__)

DEFAULT_CAPACITY = 2000


class RingBufferHandler(logging.Handler):
    """Keeps the most recent formatted records in memory.

    Entries are handed out by sequence number rather than by index, so a reader
    that falls behind while old lines are being evicted still resumes at the
    right place instead of silently skipping or repeating lines.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        super().__init__()
        self.capacity = capacity
        self._records: deque[tuple[int, str]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._next_seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # pragma: no cover - formatting must never crash logging
            return
        with self._lock:
            self._records.append((self._next_seq, text))
            self._next_seq += 1

    def since(self, seq: int) -> tuple[list[str], int]:
        """Lines with a sequence number >= `seq`, plus the next sequence."""
        with self._lock:
            lines = [text for s, text in self._records if s >= seq]
            return lines, self._next_seq

    def snapshot(self) -> list[str]:
        with self._lock:
            return [text for _s, text in self._records]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    @property
    def next_seq(self) -> int:
        with self._lock:
            return self._next_seq


class LogWindow:
    """A tkinter window tailing the ring buffer. Open it on the tkinter thread."""

    def __init__(self, buffer: RingBufferHandler, log_path=None) -> None:
        self.buffer = buffer
        self.log_path = log_path
        self._win = None
        self._text = None
        self._seq = 0
        self._follow = None

    @property
    def is_open(self) -> bool:
        return self._win is not None

    def open(self, parent=None) -> None:
        import tkinter as tk

        if self._win is not None:
            # Already open: raise it rather than stacking duplicate windows.
            try:
                self._win.lift()
                self._win.focus_force()
                return
            except Exception:
                self._win = None

        self._win = tk.Toplevel(parent) if parent is not None else tk.Tk()
        win = self._win
        win.title("whispa - debug console")
        win.geometry("900x460")
        win.configure(bg="#14141a")

        bar = tk.Frame(win, bg="#14141a")
        bar.pack(fill="x", padx=10, pady=(10, 4))

        self._follow = tk.BooleanVar(value=True)
        tk.Checkbutton(
            bar, text="Follow", variable=self._follow, bg="#14141a", fg="#c9c9d4",
            selectcolor="#24242e", activebackground="#14141a",
            activeforeground="#ffffff", font=("Segoe UI", 9), borderwidth=0,
        ).pack(side="left")

        for label, cmd in (
            ("Copy all", self._copy_all),
            ("Clear", self._clear),
        ):
            tk.Button(
                bar, text=label, command=cmd, relief="flat", bg="#2c2c38",
                fg="#c9c9d4", padx=10, pady=2, font=("Segoe UI", 9),
            ).pack(side="right", padx=(6, 0))

        if self.log_path is not None:
            tk.Label(
                bar, text=str(self.log_path), bg="#14141a", fg="#6f6f7c",
                font=("Consolas", 8),
            ).pack(side="left", padx=(14, 0))

        self._text = tk.Text(
            win, wrap="none", bg="#0f0f14", fg="#d7d7e0",
            insertbackground="#d7d7e0", relief="flat",
            font=("Consolas", 9), padx=10, pady=8,
        )
        scroll = tk.Scrollbar(win, command=self._text.yview)
        self._text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y", pady=(0, 10))
        self._text.pack(fill="both", expand=True, padx=(10, 0), pady=(0, 10))

        # Colour the levels so errors are findable by eye.
        self._text.tag_configure("ERROR", foreground="#ff7676")
        self._text.tag_configure("WARNING", foreground="#ffc46b")
        self._text.tag_configure("DEBUG", foreground="#7c7c8a")

        self._seq = 0
        win.protocol("WM_DELETE_WINDOW", self.close)
        self._pump()

    def _tag_for(self, line: str) -> str:
        for level in ("ERROR", "CRITICAL", "WARNING", "DEBUG"):
            if f" {level} " in line or f" {level:<7} " in line:
                return "ERROR" if level == "CRITICAL" else level
        return ""

    def _append(self, lines) -> None:
        if not lines or self._text is None:
            return
        self._text.configure(state="normal")
        for line in lines:
            tag = self._tag_for(line)
            self._text.insert("end", line + "\n", (tag,) if tag else ())
        self._text.configure(state="disabled")
        if self._follow is not None and self._follow.get():
            self._text.see("end")

    def _pump(self) -> None:
        if self._win is None:
            return
        try:
            lines, self._seq = self.buffer.since(self._seq)
            self._append(lines)
            self._win.after(300, self._pump)
        except Exception:
            log.debug("log window pump failed", exc_info=True)
            self.close()

    def _copy_all(self) -> None:
        try:
            self._win.clipboard_clear()
            self._win.clipboard_append("\n".join(self.buffer.snapshot()))
        except Exception:
            log.debug("could not copy the log", exc_info=True)

    def _clear(self) -> None:
        self.buffer.clear()
        if self._text is not None:
            self._text.configure(state="normal")
            self._text.delete("1.0", "end")
            self._text.configure(state="disabled")
        self._seq = self.buffer.next_seq

    def close(self) -> None:
        win, self._win = self._win, None
        self._text = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

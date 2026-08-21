"""On-screen status indicator.

A small always-on-top pill near the bottom of the screen that answers the three
questions you actually have while dictating:

1. is it running at all?          -> a dim dot when idle
2. is it hearing me?              -> live level bars driven by real microphone
                                     RMS, so a muted mic shows a flat meter
                                     instead of a reassuring animation
3. is it still thinking?          -> an animated amber pulse while decoding

Built on tkinter, which ships with Python - the alternative GUI toolkits would
have cost a dependency an order of magnitude larger than this whole app.

The overlay must never take focus. If it did, the injected keystrokes would go
to the overlay instead of the document the user was typing into, which is the
single worst thing this file could do. On Windows that is enforced with the
WS_EX_NOACTIVATE and WS_EX_TRANSPARENT extended styles.
"""

from __future__ import annotations

import logging
import math
import queue
import time

from .app import State

log = logging.getLogger(__name__)

# Colours per state: (dot, bar/accent, text)
_PALETTE = {
    State.IDLE: ("#5a5a62", "#3a3a42", "#9a9aa4"),
    State.RECORDING: ("#ff4d4d", "#ff4d4d", "#ffd9d9"),
    State.TRANSCRIBING: ("#ffb020", "#ffb020", "#ffe9c2"),
    State.ERROR: ("#ff5cc8", "#ff5cc8", "#ffd6f2"),
}

_BG = "#14141a"
# Colour keyed out to fake rounded corners; must not appear in the artwork.
_CHROMA = "#010203"

_BARS = 14
# Below this the meter is drawn grey with a "no signal" note, because a mic
# that is muted or unplugged should look different from a quiet room.
SIGNAL_FLOOR = 0.02


def should_be_visible(
    state: State,
    forced: bool,
    always_visible: bool,
    flash_until: float,
    now: float,
) -> bool:
    """Whether the pill belongs on screen.

    Pulled out of the widget so the rule is testable without a display: idle is
    hidden unless pinned, anything else is shown, and a just-finished dictation
    stays up briefly so the result is readable.
    """
    if forced or always_visible:
        return True
    if state is not State.IDLE:
        return True
    return now < flash_until


def bar_heights(level: float, phase: float, bars: int = _BARS) -> list[float]:
    """Half-heights for the level meter, in pixels.

    Centre bars react most and the edges least, which reads as a waveform
    rather than a progress bar. Crucially the amplitude comes from `level`,
    which is real measured input - at zero level every bar sits at the 2px
    floor, so a dead microphone is visibly dead.
    """
    level = max(0.0, min(1.0, level))
    out = []
    for i in range(bars):
        weight = 0.42 + 0.58 * math.sin(math.pi * (i + 0.5) / bars)
        jitter = 0.82 + 0.18 * math.sin(phase * 9.0 + i * 1.3)
        out.append(max(2.0, level * 15.0 * weight * jitter))
    return out


class Overlay:
    """Owns a tkinter window. `run()` must be called on the main thread."""

    WIDTH = 208
    HEIGHT = 46
    BOTTOM_MARGIN = 90

    def __init__(self, level_source=None, always_visible: bool = False) -> None:
        # A zero-argument callable returning 0.0-1.0; the recorder supplies it.
        self.level_source = level_source or (lambda: 0.0)
        self.always_visible = always_visible
        self._root = None
        self._canvas = None
        # State changes arrive from the hotkey and worker threads; tkinter is
        # not thread-safe, so they are queued and drained by the UI timer.
        self._events: queue.Queue = queue.Queue()
        # Work that must happen on the tkinter thread, e.g. opening the
        # correction dialog from a tray menu click.
        self._calls: queue.Queue = queue.Queue()
        self._state = State.IDLE
        self._detail = ""
        self._forced = False
        self._flash_until = 0.0
        self._phase = 0.0
        self._closing = False

    # --- thread-safe API ----------------------------------------------------

    def set_state(self, state: State, detail: str = "", force: bool = False) -> None:
        """`force` pins the pill on screen - used while the model loads, when
        there is no console left to print progress to."""
        self._events.put((state, detail, force))

    def call_soon(self, fn) -> None:
        """Run `fn` on the tkinter thread. Safe to call from any thread."""
        self._calls.put(fn)

    def stop(self) -> None:
        self._closing = True

    # --- window plumbing ----------------------------------------------------

    def _no_focus_steal(self) -> None:
        """Apply WS_EX_NOACTIVATE | WS_EX_TRANSPARENT on Windows."""
        try:
            import ctypes

            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            hwnd = ctypes.windll.user32.GetParent(self._root.winfo_id())
            current = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                current | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            )
        except Exception:
            # Not Windows, or the call failed. The overlay still works; it may
            # just be clickable and appear in alt-tab.
            log.debug("could not apply click-through window styles", exc_info=True)

    def _build(self) -> None:
        import tkinter as tk

        self._root = tk.Tk()
        self._root.withdraw()
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        try:
            self._root.attributes("-alpha", 0.93)
        except Exception:
            pass

        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = (screen_w - self.WIDTH) // 2
        y = screen_h - self.HEIGHT - self.BOTTOM_MARGIN
        self._root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

        self._canvas = tk.Canvas(
            self._root,
            width=self.WIDTH,
            height=self.HEIGHT,
            highlightthickness=0,
            bg=_CHROMA,
        )
        self._canvas.pack()
        try:
            self._root.attributes("-transparentcolor", _CHROMA)
        except Exception:
            self._canvas.configure(bg=_BG)

        self._root.deiconify()
        self._no_focus_steal()
        self._root.after(16, self._on_state_change_only)

    # --- drawing ------------------------------------------------------------

    def _rounded(self, x0, y0, x1, y1, r, fill):
        c = self._canvas
        c.create_oval(x0, y0, x0 + 2 * r, y0 + 2 * r, fill=fill, outline=fill)
        c.create_oval(x1 - 2 * r, y0, x1, y0 + 2 * r, fill=fill, outline=fill)
        c.create_oval(x0, y1 - 2 * r, x0 + 2 * r, y1, fill=fill, outline=fill)
        c.create_oval(x1 - 2 * r, y1 - 2 * r, x1, y1, fill=fill, outline=fill)
        c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline=fill)
        c.create_rectangle(x0, y0 + r, x1, y1 - r, fill=fill, outline=fill)

    def _draw(self) -> None:
        c = self._canvas
        c.delete("all")
        state = self._state
        dot, accent, text_col = _PALETTE.get(state, _PALETTE[State.IDLE])

        if time.monotonic() < self._flash_until:
            dot = accent = "#3ddc84"
            text_col = "#d6ffe8"

        self._rounded(1, 1, self.WIDTH - 1, self.HEIGHT - 1, 14, _BG)

        cx, cy = 24, self.HEIGHT // 2
        if state is State.RECORDING:
            # Breathing halo, so "live" reads at a glance.
            pulse = 6 + 2.2 * math.sin(self._phase * 4.0)
            c.create_oval(
                cx - pulse - 3, cy - pulse - 3, cx + pulse + 3, cy + pulse + 3,
                fill="#3a1420", outline="",
            )
            c.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, fill=dot, outline="")
        elif state is State.TRANSCRIBING:
            for i in range(3):
                a = self._phase * 3.0 + i * 2.0
                r = 2.6 + 1.4 * math.sin(a)
                c.create_oval(
                    cx - 10 + i * 9 - r, cy - r, cx - 10 + i * 9 + r, cy + r,
                    fill=dot, outline="",
                )
        else:
            c.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=dot, outline="")

        if state is State.RECORDING:
            self._draw_level(accent)
        else:
            label = {
                State.IDLE: self._detail or "ready",
                State.TRANSCRIBING: self._detail or "thinking",
                State.ERROR: self._detail or "error",
            }.get(state, "")
            if len(label) > 22:
                label = label[:21] + "…"
            c.create_text(
                46, cy, text=label, anchor="w",
                fill=text_col, font=("Segoe UI", 10),
            )

    def _draw_level(self, colour: str) -> None:
        """Live meter. Bar heights come from the real input signal."""
        c = self._canvas
        try:
            level = max(0.0, min(1.0, float(self.level_source())))
        except Exception:
            level = 0.0
        x0, cy = 46, self.HEIGHT // 2
        width, gap = 6, 4
        for i, h in enumerate(bar_heights(level, self._phase)):
            x = x0 + i * (width + gap)
            c.create_rectangle(
                x, cy - h, x + width, cy + h,
                fill=colour if level > SIGNAL_FLOOR else "#333340", outline="",
            )
        if level <= SIGNAL_FLOOR:
            c.create_text(
                x0 + 58, cy + 15, text="no signal", anchor="c",
                fill="#8a8a94", font=("Segoe UI", 7),
            )

    # --- loop ---------------------------------------------------------------

    def _pump_events(self) -> bool:
        changed = False
        while True:
            try:
                state, detail, force = self._events.get_nowait()
            except queue.Empty:
                break
            if state is State.IDLE and self._state is State.TRANSCRIBING and detail:
                self._flash_until = time.monotonic() + 1.1
            self._state, self._detail, self._forced = state, detail, force
            changed = True
        while True:
            try:
                fn = self._calls.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log.debug("scheduled overlay call failed", exc_info=True)
        return changed

    def _on_state_change_only(self) -> None:
        if self._closing:
            try:
                self._root.destroy()
            except Exception:
                pass
            return
        self._pump_events()
        self._phase = time.monotonic()
        visible = should_be_visible(
            self._state,
            self._forced,
            self.always_visible,
            self._flash_until,
            time.monotonic(),
        )
        try:
            if visible:
                self._root.deiconify()
                self._draw()
            else:
                self._root.withdraw()
        except Exception:
            log.debug("overlay draw failed", exc_info=True)
        # 20 fps while something is happening; idle costs nothing because the
        # window is hidden and we only re-check for events.
        self._root.after(50 if visible else 120, self._on_state_change_only)

    def run(self) -> None:
        self._build()
        self._root.mainloop()

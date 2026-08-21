"""Global push-to-talk hotkey.

The matching logic is deliberately split from pynput: `HotkeySpec` works on
canonical key *names* ("ctrl", "f9", "d") and holds all the state, while
`GlobalHotkey` does nothing but translate real pynput events into those names.
That keeps the interesting half testable on a machine with no keyboard.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger(__name__)

MODIFIERS = frozenset({"ctrl", "alt", "shift", "cmd"})

# pynput reports left/right variants separately; nobody wants to configure
# "ctrl_l", and a hotkey bound to one side that silently fails on the other is
# a support nightmare. Collapse them.
_ALIASES = {
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "shift_l": "shift",
    "shift_r": "shift",
    "cmd_l": "cmd",
    "cmd_r": "cmd",
    "win": "cmd",
    "super": "cmd",
    "control": "ctrl",
}


def canonical(name: str) -> str:
    name = (name or "").strip().lower().strip("<>")
    return _ALIASES.get(name, name)


def parse_hotkey(spec: str) -> frozenset[str]:
    """'<ctrl>+<alt>+d' -> {'ctrl', 'alt', 'd'};  'f9' -> {'f9'}."""
    parts = [canonical(p) for p in (spec or "").split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey spec: {spec!r}")
    return frozenset(parts)


class HotkeySpec:
    """Tracks held keys and decides when to start/stop.

    Three modes:

    * 'hold'   - push-to-talk. Start when the combination goes down, stop as
                 soon as any part of it comes up.
    * 'toggle' - press once to start, again to stop; releases are ignored.
    * 'hybrid' - both, on one key, chosen by how long you hold it. A quick tap
                 latches recording on until the next tap; holding it down for
                 longer than `tap_seconds` behaves exactly like push-to-talk.
                 This is the default, because which one you want depends on
                 whether you are dictating a phrase or a paragraph, and you
                 shouldn't have to decide that in advance.
    """

    def __init__(
        self,
        spec: str,
        mode: str = "hybrid",
        tap_seconds: float = 0.35,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.required = parse_hotkey(spec)
        self.mode = mode
        self.tap_seconds = tap_seconds
        # Injectable so the tap/hold boundary can be tested without sleeping.
        self._clock = clock or time.monotonic
        self.held: set[str] = set()
        self.active = False
        # hybrid only: recording is latched on after a tap, and survives the
        # key coming back up.
        self.latched = False
        self._pressed_at = 0.0
        # Set when a press already produced the 'stop', so the release that
        # follows it doesn't produce a second one.
        self._consumed = False

    @property
    def held_modifiers(self) -> set[str]:
        return self.held & MODIFIERS

    def _satisfied(self) -> bool:
        return self.required <= self.held

    def press(self, name: str) -> str | None:
        """Feed a key-down. Returns 'start', 'stop', or None."""
        name = canonical(name)
        # Auto-repeat sends a stream of presses while a key is held; without
        # this guard a held hotkey would re-trigger start on every repeat.
        already = name in self.held
        self.held.add(name)
        if already or not self._satisfied():
            return None
        if self.mode == "hybrid":
            if self.active and self.latched:
                # Second tap of a latched recording: end it here, and remember
                # that the matching release must not fire another stop.
                self.active = False
                self.latched = False
                self._consumed = True
                return "stop"
            if not self.active:
                self.active = True
                self.latched = False
                self._consumed = False
                self._pressed_at = self._clock()
                return "start"
            return None
        if self.mode == "toggle":
            self.active = not self.active
            return "start" if self.active else "stop"
        if not self.active:
            self.active = True
            return "start"
        return None

    def release(self, name: str) -> str | None:
        """Feed a key-up. Returns 'stop' or None."""
        name = canonical(name)
        self.held.discard(name)
        if self.mode == "hybrid":
            if name not in self.required:
                return None
            if self._consumed:
                self._consumed = False
                return None
            if not self.active:
                return None
            if (self._clock() - self._pressed_at) < self.tap_seconds:
                # Too quick to be push-to-talk: treat it as a tap and keep
                # recording until the next press.
                self.latched = True
                return None
            self.active = False
            self.latched = False
            return "stop"
        if self.mode == "toggle":
            return None
        if self.active and name in self.required:
            self.active = False
            return "stop"
        return None

    def reset(self) -> None:
        """Drop all state - used when the listener restarts or focus is lost."""
        self.held.clear()
        self.active = False
        self.latched = False
        self._consumed = False


def _key_name(key) -> str:
    """pynput Key/KeyCode -> canonical name."""
    name = getattr(key, "name", None)
    if name:
        return canonical(name)
    char = getattr(key, "char", None)
    if char:
        return canonical(char)
    vk = getattr(key, "vk", None)
    return f"vk{vk}" if vk is not None else "unknown"


class GlobalHotkey:
    """System-wide listener. Windows needs no special permissions for this;
    macOS needs Accessibility, and X11 needs a display."""

    def __init__(
        self,
        spec: str,
        mode: str,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        on_held_change: Callable[[set[str]], None] | None = None,
        tap_seconds: float = 0.35,
    ) -> None:
        self.matcher = HotkeySpec(spec, mode, tap_seconds=tap_seconds)
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_held_change = on_held_change
        self._listener = None

    def _dispatch(self, action: str | None) -> None:
        if action == "start":
            self.on_start()
        elif action == "stop":
            self.on_stop()

    def _on_press(self, key) -> None:
        try:
            action = self.matcher.press(_key_name(key))
            if self.on_held_change:
                self.on_held_change(self.matcher.held_modifiers)
            self._dispatch(action)
        except Exception:
            # An exception escaping a pynput callback kills the listener
            # thread, and the hotkey silently stops working for the rest of the
            # session. Swallow and log instead.
            log.exception("error handling key press")

    def _on_release(self, key) -> None:
        try:
            action = self.matcher.release(_key_name(key))
            if self.on_held_change:
                self.on_held_change(self.matcher.held_modifiers)
            self._dispatch(action)
        except Exception:
            log.exception("error handling key release")

    def start(self) -> None:
        from pynput import keyboard

        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.daemon = True
        self._listener.start()
        log.info(
            "hotkey listening: %s (%s)",
            "+".join(sorted(self.matcher.required)),
            self.matcher.mode,
        )

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self.matcher.reset()

    def join(self) -> None:
        if self._listener is not None:
            self._listener.join()

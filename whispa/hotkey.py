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


# The shortcuts offered in the tray menu. A single unmodified key is the
# safest choice - see the note on Config.hotkey - so the function keys come
# first, but a chord is there for keyboards where F9 is already spoken for, and
# for anyone who would rather not lose a function key.
PRESETS: tuple[tuple[str, str], ...] = (
    ("F9", "f9"),
    ("F8", "f8"),
    ("F4", "f4"),
    ("Scroll Lock", "scroll_lock"),
    ("Pause / Break", "pause"),
    ("Ctrl + Win (Start)", "<ctrl>+<cmd>"),
    ("Ctrl + Shift + Space", "<ctrl>+<shift>+space"),
    ("Ctrl + Alt + D", "<ctrl>+<alt>+d"),
)


def preset_label(spec: str) -> str:
    """The menu label for a spec, or the spec itself if it is hand-written."""
    wanted = parse_hotkey(spec) if spec else frozenset()
    for label, candidate in PRESETS:
        if parse_hotkey(candidate) == wanted:
            return label
    return spec


# The Windows key reports these virtual key codes. Windows opens the Start menu
# when either comes *up* without another key having been pressed in between,
# which for a Ctrl+Win dictation hotkey means the Start menu steals focus at
# exactly the moment the transcript is about to be typed.
WIN_VKS = frozenset({91, 92})
# An unassigned virtual key code. Tapping it while Win is held is enough for
# Windows to treat the Win press as part of a combination and leave the Start
# menu closed - the same trick AutoHotkey uses. Nothing else reacts to it.
DUMMY_VK = 0xE8


def suppress_start_menu(
    required: frozenset[str] | set[str],
    held: set[str],
    vk: int,
    is_down: bool,
    swallowed: bool,
) -> tuple[bool, bool]:
    """Decide whether to hide one Windows-key event from the rest of Windows.

    Returns (suppress, swallowed-state). The rule is deliberately narrow: the
    Win key is only hidden while the *other* keys of the hotkey are already
    held, so Win on its own, Win+E, Win+D and every other system shortcut keep
    working. A key-up is hidden only if we hid its key-down, so Windows can
    never be left believing the key is still down.
    """
    if "cmd" not in required or vk not in WIN_VKS:
        return False, swallowed
    if is_down:
        if (set(required) - {"cmd"}) <= set(held):
            return True, True
        return False, swallowed
    if swallowed:
        return True, False
    return False, swallowed


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
        suppress_start_menu: bool = True,
    ) -> None:
        self.spec = spec
        self.tap_seconds = tap_seconds
        self.matcher = HotkeySpec(spec, mode, tap_seconds=tap_seconds)
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_held_change = on_held_change
        self.suppress_start_menu = suppress_start_menu
        self._swallowed_win = False
        self._listener = None

    def _dispatch(self, action: str | None) -> None:
        if action == "start":
            self.on_start()
        elif action == "stop":
            self.on_stop()

    def _defuse_start_menu(self) -> None:
        """Tap an unassigned key so a Win press already seen by Windows counts
        as part of a combination.

        Needed for the order Win-then-Ctrl: the Win key-down went through
        before we knew a hotkey was being formed, so it cannot be hidden, and
        hiding the key-up instead would leave Windows thinking Win is stuck
        down. A dummy keystroke while it is held is the safe way out.
        """
        try:
            from pynput.keyboard import Controller, KeyCode

            controller = Controller()
            dummy = KeyCode.from_vk(DUMMY_VK)
            controller.press(dummy)
            controller.release(dummy)
        except Exception:
            log.debug("could not defuse the Start menu", exc_info=True)

    def _win32_filter(self, msg, data):
        """pynput hook: decide what Windows itself gets to see."""
        try:
            # 256/257 = WM_KEYDOWN/WM_KEYUP, 260/261 = the WM_SYSKEY* pair the
            # Windows key actually arrives as.
            is_down = msg in (256, 260)
            suppress, self._swallowed_win = suppress_start_menu(
                self.matcher.required,
                self.matcher.held,
                getattr(data, "vkCode", -1),
                is_down,
                self._swallowed_win,
            )
            if suppress and self._listener is not None:
                self._listener.suppress_event()
        except Exception:
            log.debug("key filter failed", exc_info=True)

    def _on_press(self, key) -> None:
        try:
            name = _key_name(key)
            was_swallowed = self._swallowed_win
            action = self.matcher.press(name)
            if self.on_held_change:
                self.on_held_change(self.matcher.held_modifiers)
            if (
                action == "start"
                and self.suppress_start_menu
                and "cmd" in self.matcher.required
                and not was_swallowed
                and name != "cmd"
            ):
                # Win went down before the rest of the chord, so it reached
                # Windows unfiltered; stop its release opening the Start menu.
                self._defuse_start_menu()
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
        import sys

        from pynput import keyboard

        kwargs = {"on_press": self._on_press, "on_release": self._on_release}
        # The filter is a Windows-only pynput option, and passing it anywhere
        # else is an error rather than a no-op.
        if self.suppress_start_menu and sys.platform == "win32":
            kwargs["win32_event_filter"] = self._win32_filter
        self._listener = keyboard.Listener(**kwargs)
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
        self._swallowed_win = False
        self.matcher.reset()

    def rebind(self, spec: str, mode: str | None = None) -> None:
        """Switch to a different shortcut without restarting whispa.

        parse_hotkey() runs before anything is torn down, so a typo leaves the
        old shortcut working rather than leaving the app with no way to
        dictate at all.
        """
        parse_hotkey(spec)
        running = self._listener is not None
        self.stop()
        self.spec = spec
        self.matcher = HotkeySpec(
            spec, mode or self.matcher.mode, tap_seconds=self.tap_seconds
        )
        if running:
            self.start()

    def join(self) -> None:
        if self._listener is not None:
            self._listener.join()

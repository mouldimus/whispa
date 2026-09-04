"""Global push-to-talk hotkey.

The matching logic is deliberately split from pynput: `HotkeySpec` works on
canonical key *names* ("ctrl", "f9", "d") and holds all the state, while
`GlobalHotkey` does nothing but translate real pynput events into those names.
That keeps the interesting half testable on a machine with no keyboard.
"""

from __future__ import annotations

import logging
import sys
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
        is_down: Callable[[str], bool | None] | None = None,
    ) -> None:
        self.required = parse_hotkey(spec)
        self.mode = mode
        self.tap_seconds = tap_seconds
        # Injectable so the tap/hold boundary can be tested without sleeping.
        self._clock = clock or time.monotonic
        # Asks the OS whether a key is physically down right now: True, False,
        # or None when it cannot tell. See _prune() for why this exists.
        self._is_down = is_down
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

    def _prune(self, keep: str) -> bool:
        """Drop keys from `held` that the OS says are not actually down.

        A global hook only sees the key-ups it is sent, and Windows withholds
        plenty: the release of the Win key after Win+L, everything typed on
        the secure desktop (Ctrl+Alt+Del, a UAC prompt), and any event another
        hook decided to swallow. Without this check one lost key-up leaves a
        modifier marked held forever, and from then on the *other* half of
        the chord - or, once both halves are stuck, any key at all - starts a
        recording. That is the "it fires on Ctrl alone / on Shift / for no
        reason" bug.

        `keep` is the key of the event being handled, which is never pruned:
        the OS state may not have caught up with the event that reported it.
        Returns True if a key the shortcut needs was found to be stale.
        """
        if self._is_down is None:
            return False
        lost_required = False
        for key in list(self.held):
            if key == keep:
                continue
            try:
                down = self._is_down(key)
            except Exception:
                log.debug("key state probe failed for %s", key, exc_info=True)
                continue
            if down is False:
                self.held.discard(key)
                if key in self.required:
                    lost_required = True
                    log.info("dropping %s: marked held but no longer down", key)
        return lost_required

    def press(self, name: str) -> str | None:
        """Feed a key-down. Returns 'start', 'stop', or None."""
        name = canonical(name)
        if self._prune(name) and self.active and not self.latched and self.mode != "toggle":
            # Push-to-talk whose release we never saw. Treat this event as the
            # missing key-up: a stop, unless it came so soon after the press
            # that the press was a tap, which in hybrid mode latches instead.
            self.held.add(name)
            if self.mode == "hybrid" and (self._clock() - self._pressed_at) < self.tap_seconds:
                self.latched = True
                return None
            self.active = False
            self.latched = False
            self._consumed = False
            return "stop"
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
        self._prune(name)
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


# An unassigned virtual key code. Windows opens the Start menu when the Win
# key comes *up* with no other key pressed in between - which for a Ctrl+Win
# dictation hotkey means the Start menu steals focus at exactly the moment the
# transcript is about to be typed. Tapping this code while Win is held is
# enough for Windows to treat the press as part of a combination and leave the
# menu closed - the same trick AutoHotkey uses. Nothing else reacts to it.
DUMMY_VK = 0xE8

# Virtual key codes for the modifier names, for asking Windows whether they
# are really down. Left/right variants share the generic code.
_MODIFIER_VKS = {
    "ctrl": (0x11,),
    "alt": (0x12,),
    "shift": (0x10,),
    "cmd": (0x5B, 0x5C),
}


def windows_key_state() -> Callable[[str], bool | None]:
    """A probe for HotkeySpec(is_down=...) backed by GetAsyncKeyState.

    Returns a callable answering "is this key physically down right now?"
    with True/False, or None for a name it cannot map to a virtual key code.
    Windows only.
    """
    import ctypes

    from pynput.keyboard import Key

    get_state = ctypes.windll.user32.GetAsyncKeyState  # type: ignore[attr-defined]
    get_state.argtypes = (ctypes.c_int,)
    get_state.restype = ctypes.c_short
    vk_for_char = ctypes.windll.user32.VkKeyScanW  # type: ignore[attr-defined]
    vk_for_char.argtypes = (ctypes.c_wchar,)
    vk_for_char.restype = ctypes.c_short

    def vks(name: str) -> tuple[int, ...]:
        if name in _MODIFIER_VKS:
            return _MODIFIER_VKS[name]
        if name.startswith("vk") and name[2:].isdigit():
            return (int(name[2:]),)
        key = getattr(Key, name, None)
        vk = getattr(getattr(key, "value", None), "vk", None)
        if vk is not None:
            return (vk,)
        if len(name) == 1:
            code = vk_for_char(name)
            if code != -1:
                return (code & 0xFF,)
        return ()

    def is_down(name: str) -> bool | None:
        codes = vks(name)
        if not codes:
            return None
        return any(get_state(vk) & 0x8000 for vk in codes)

    return is_down


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
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_held_change = on_held_change
        self.suppress_start_menu = suppress_start_menu
        self._listener = None
        self._is_down = None
        if sys.platform == "win32":
            try:
                self._is_down = windows_key_state()
            except Exception:
                log.warning("cannot read the real key state; a lost key-up "
                            "may leave the shortcut half-pressed", exc_info=True)
        self.matcher = self._matcher(spec, mode)

    def _matcher(self, spec: str, mode: str) -> HotkeySpec:
        return HotkeySpec(
            spec, mode, tap_seconds=self.tap_seconds, is_down=self._is_down
        )

    def _dispatch(self, action: str | None) -> None:
        if action == "start":
            self.on_start()
        elif action == "stop":
            self.on_stop()

    def _defuse_start_menu(self) -> None:
        """Tap an unassigned key so a Win press already seen by Windows counts
        as part of a combination.

        Works whichever key went down first: the Start menu opens on the Win
        key-up only if nothing else was pressed after the Win key-down, and
        the dummy tap always is. An earlier version hid the Win press from
        Windows with pynput's event filter instead, but a filtered event is
        never delivered to the listener either, so the matcher never saw the
        Win key when Ctrl was pressed first and that order did nothing.
        """
        try:
            from pynput.keyboard import Controller, KeyCode

            controller = Controller()
            dummy = KeyCode.from_vk(DUMMY_VK)
            controller.press(dummy)
            controller.release(dummy)
        except Exception:
            log.debug("could not defuse the Start menu", exc_info=True)

    def _on_press(self, key) -> None:
        try:
            name = _key_name(key)
            action = self.matcher.press(name)
            if self.on_held_change:
                self.on_held_change(self.matcher.held_modifiers)
            if (
                action == "start"
                and self.suppress_start_menu
                and "cmd" in self.matcher.required
            ):
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
        self.matcher = self._matcher(spec, mode or self.matcher.mode)
        if running:
            self.start()

    def join(self) -> None:
        if self._listener is not None:
            self._listener.join()

"""Getting the transcript into whatever window has focus.

Two strategies, because no single one works everywhere:

* "paste"  - put the text on the clipboard and send ctrl+v. Fast regardless of
             length, and correct for unicode. Clobbers the clipboard, so the
             previous contents are saved and put back afterwards.
* "type"   - synthesise the keystrokes directly. Survives apps that ignore or
             intercept programmatic paste (some terminals, some games), but is
             slow for long text and can drop characters under load.

Both must deal with the same trap: the user has just released a hotkey, and may
still be physically holding a modifier. Sending ctrl+v while alt is down gives
the target app alt+ctrl+v, which is usually a no-op or, worse, a menu. So we
wait for the modifiers to come up first.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

log = logging.getLogger(__name__)


class Injector(Protocol):
    """Injectors type exactly what they are handed.

    Text finalisation (replacements, the trailing space) belongs to the engine,
    so that the dry-run injector reports the same string the real one would
    type - otherwise --dry-run quietly lies about the output.
    """

    def inject(self, text: str) -> bool: ...


def _modifier_keys():
    from pynput.keyboard import Key

    return [Key.ctrl, Key.alt, Key.shift, Key.cmd, Key.alt_gr]


class KeyboardInjector:
    """pynput-backed injector. Windows is the target, but it also runs on
    X11/macOS, which is what makes a smoke test possible off-Windows."""

    def __init__(
        self,
        method: str = "paste",
        type_delay: float = 0.005,
        restore_clipboard: bool = True,
        modifier_release_timeout: float = 1.0,
    ) -> None:
        self.method = method
        self.type_delay = type_delay
        self.restore_clipboard = restore_clipboard
        self.modifier_release_timeout = modifier_release_timeout
        self._controller = None
        # Populated by the hotkey listener so we know what is *actually* held,
        # rather than guessing. Falls back to a fixed sleep when unset.
        self.held_modifiers: set = set()

    def _keyboard(self):
        if self._controller is None:
            from pynput.keyboard import Controller

            self._controller = Controller()
        return self._controller

    def _wait_for_modifiers_released(self) -> None:
        """Block until no modifier is held, or the timeout expires."""
        deadline = time.monotonic() + self.modifier_release_timeout
        while self.held_modifiers and time.monotonic() < deadline:
            time.sleep(0.01)
        if self.held_modifiers:
            log.warning(
                "modifiers still held after %.1fs (%s); injecting anyway",
                self.modifier_release_timeout,
                self.held_modifiers,
            )
        # Even once the OS reports the keys up, the target app may not have
        # processed the key-up event yet. A short settle beats a race.
        time.sleep(0.03)

    def inject(self, text: str) -> bool:
        if not text:
            return False
        if self.method == "clipboard":
            return self._set_clipboard(text)

        self._wait_for_modifiers_released()

        if self.method == "type":
            return self._type(text)
        return self._paste(text)

    # --- strategies ---------------------------------------------------------

    def _type(self, text: str) -> bool:
        kb = self._keyboard()
        try:
            for ch in text:
                kb.type(ch)
                if self.type_delay:
                    time.sleep(self.type_delay)
            return True
        except Exception:
            log.exception("typing injection failed")
            return False

    def _paste(self, text: str) -> bool:
        previous = self._get_clipboard() if self.restore_clipboard else None
        if not self._set_clipboard(text):
            return False
        try:
            from pynput.keyboard import Key

            kb = self._keyboard()
            with kb.pressed(Key.ctrl):
                kb.press("v")
                kb.release("v")
        except Exception:
            log.exception("paste injection failed")
            return False
        finally:
            if previous is not None:
                # The target app reads the clipboard asynchronously after the
                # keystroke; restoring immediately can win the race and paste
                # the *old* contents instead.
                time.sleep(0.25)
                self._set_clipboard(previous)
        return True

    # --- clipboard ----------------------------------------------------------

    def _get_clipboard(self) -> str | None:
        try:
            import pyperclip

            return pyperclip.paste()
        except Exception:
            log.debug("could not read clipboard", exc_info=True)
            return None

    def _set_clipboard(self, text: str) -> bool:
        try:
            import pyperclip

            pyperclip.copy(text)
            return True
        except Exception:
            log.exception("could not write to clipboard")
            return False


class NullInjector:
    """Logs instead of typing. Used by --dry-run and by the tests."""

    def __init__(self) -> None:
        self.injected: list[str] = []

    def inject(self, text: str) -> bool:
        if not text:
            return False
        self.injected.append(text)
        log.info("[dry-run] would inject: %r", text)
        return True

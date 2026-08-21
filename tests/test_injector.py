"""Tests for the injector's modifier handling.

The actual keystrokes need Windows, but the part that historically breaks -
pasting while the user is still holding a modifier - is testable anywhere,
because it is just bookkeeping plus a wait.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whispa.inject import KeyboardInjector, NullInjector


class RecordingInjector(KeyboardInjector):
    """Real modifier logic, fake keyboard and clipboard."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.typed: list[str] = []
        self.pasted: list[str] = []
        self.clipboard: str | None = "original clipboard"
        self.clipboard_writes: list[str] = []

    def _type(self, text: str) -> bool:
        self.typed.append(text)
        return True

    def _paste(self, text: str) -> bool:
        previous = self._get_clipboard() if self.restore_clipboard else None
        self._set_clipboard(text)
        self.pasted.append(text)
        if previous is not None:
            self._set_clipboard(previous)
        return True

    def _get_clipboard(self):
        return self.clipboard

    def _set_clipboard(self, text: str) -> bool:
        self.clipboard = text
        self.clipboard_writes.append(text)
        return True


class TestInjector(unittest.TestCase):
    def test_paste_is_default(self):
        inj = RecordingInjector()
        self.assertTrue(inj.inject("hello"))
        self.assertEqual(inj.pasted, ["hello"])
        self.assertEqual(inj.typed, [])

    def test_type_method(self):
        inj = RecordingInjector(method="type")
        inj.inject("hello")
        self.assertEqual(inj.typed, ["hello"])
        self.assertEqual(inj.pasted, [])

    def test_clipboard_method_does_not_press_keys(self):
        inj = RecordingInjector(method="clipboard")
        inj.inject("hello")
        self.assertEqual(inj.pasted, [])
        self.assertEqual(inj.typed, [])
        self.assertEqual(inj.clipboard, "hello")

    def test_clipboard_is_restored_after_paste(self):
        inj = RecordingInjector()
        inj.inject("dictated text")
        self.assertEqual(inj.clipboard, "original clipboard")
        # The dictated text must have been on the clipboard *before* restore.
        self.assertEqual(inj.clipboard_writes, ["dictated text", "original clipboard"])

    def test_clipboard_not_restored_when_disabled(self):
        inj = RecordingInjector(restore_clipboard=False)
        inj.inject("dictated text")
        self.assertEqual(inj.clipboard, "dictated text")

    def test_empty_text_is_a_no_op(self):
        inj = RecordingInjector()
        self.assertFalse(inj.inject(""))
        self.assertEqual(inj.pasted, [])

    def test_injects_verbatim_no_trailing_space_added(self):
        # Trailing space is the engine's job; the injector must not second-guess
        # it, or --dry-run and the real path would disagree.
        inj = RecordingInjector()
        inj.inject("exact")
        self.assertEqual(inj.pasted, ["exact"])

    def test_waits_for_held_modifiers_then_proceeds(self):
        inj = RecordingInjector(modifier_release_timeout=2.0)
        inj.held_modifiers = {"ctrl"}

        def release_soon():
            time.sleep(0.15)
            inj.held_modifiers = set()

        threading.Thread(target=release_soon, daemon=True).start()
        t0 = time.monotonic()
        inj.inject("text")
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 2.0, "should proceed once modifiers clear")
        self.assertEqual(inj.pasted, ["text"])

    def test_gives_up_waiting_and_injects_anyway(self):
        # A stuck modifier must not mean dictation silently stops working.
        inj = RecordingInjector(modifier_release_timeout=0.2)
        inj.held_modifiers = {"alt"}
        t0 = time.monotonic()
        inj.inject("text")
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertEqual(inj.pasted, ["text"])

    def test_no_wait_when_nothing_held(self):
        inj = RecordingInjector(modifier_release_timeout=5.0)
        t0 = time.monotonic()
        inj.inject("text")
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_clipboard_method_skips_the_modifier_wait(self):
        # Nothing is being keyed, so held modifiers are irrelevant.
        inj = RecordingInjector(method="clipboard", modifier_release_timeout=5.0)
        inj.held_modifiers = {"ctrl", "alt"}
        t0 = time.monotonic()
        inj.inject("text")
        self.assertLess(time.monotonic() - t0, 0.5)


class TestNullInjector(unittest.TestCase):
    def test_records_and_reports(self):
        inj = NullInjector()
        self.assertTrue(inj.inject("hello"))
        self.assertFalse(inj.inject(""))
        self.assertEqual(inj.injected, ["hello"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Noticing that you fixed something.

After text is injected we read the focused control back a few seconds later and
compare. Whatever the injected span has *become* is what you meant; the
difference is the training signal `learn.py` consumes.

Reading arbitrary application text needs Windows UI Automation, which works in
most modern apps (browsers, Office, Electron apps like Slack and VS Code) and
not in some others (many games, a few legacy Win32 controls). So this degrades
in three steps rather than pretending:

* UIA available and the control exposes text  -> fully automatic learning
* UIA unavailable                             -> automatic learning is off, and
                                                 the tray's "Fix last dictation"
                                                 dialog still teaches it
* neither                                     -> plain dictation, no learning

The overlay and the dictation loop never depend on any of this.
"""

from __future__ import annotations

import logging
import threading
from difflib import SequenceMatcher
from typing import Callable, Protocol

log = logging.getLogger(__name__)

# How much surrounding text is used to bound the alignment window.
ANCHOR = 28
# Above this, align a window around the injection rather than whole documents.
MAX_DIFF_CHARS = 20000
# Below this similarity the "after" snapshot is a different document, not an
# edited one - most likely focus moved to another window.
UNRELATED_DOCUMENT_FLOOR = 0.4


class TextObserver(Protocol):
    def snapshot(self) -> str | None: ...
    @property
    def available(self) -> bool: ...


def _map_start(offset: int, opcodes) -> int | None:
    for tag, i1, i2, j1, j2 in opcodes:
        if i1 <= offset < i2:
            return j1 + (offset - i1) if tag == "equal" else j1
    return None


def _map_end(offset: int, opcodes) -> int | None:
    for tag, i1, i2, j1, j2 in opcodes:
        if i1 < offset <= i2:
            return j1 + (offset - i1) if tag == "equal" else j2
    return None


def locate_edited_span(
    before: str, after: str, injected: str, anchor: int = ANCHOR
) -> str | None:
    """Find what `injected` turned into, by aligning the two snapshots.

    Character offsets alone are useless, because the user may have typed
    anywhere in the document between the snapshots and shifted everything
    along. So the two versions are aligned as a whole and the injected span's
    offsets are mapped through that alignment - which stays correct no matter
    what else was edited, unlike matching on the surrounding text.

    `anchor` is retained for callers and bounds the window used on large
    documents, where aligning the whole thing would be needlessly slow.
    """
    if not injected or before is None or after is None:
        return None
    pos = before.rfind(injected)
    if pos < 0:
        return None
    end = pos + len(injected)

    offset = 0
    if len(before) > MAX_DIFF_CHARS or len(after) > MAX_DIFF_CHARS:
        # Align only a window around the injection. Generous enough to absorb
        # ordinary editing, small enough to stay fast on a large document.
        window = max(anchor * 20, len(injected) * 4, 500)
        lo = max(0, pos - window)
        hi = min(len(before), end + window)
        before = before[lo:hi]
        pos, end, offset = pos - lo, end - lo, lo
        after = after[max(0, lo - window) : min(len(after), hi + window)]

    matcher = SequenceMatcher(a=before, b=after, autojunk=False)
    opcodes = matcher.get_opcodes()
    # If the two snapshots have little in common, focus moved to a different
    # control entirely and the mapping would be meaningless. `ratio` reuses the
    # matching blocks computed for the opcodes, so this costs nothing extra.
    if matcher.ratio() < UNRELATED_DOCUMENT_FLOOR:
        return None
    start_j = 0 if pos == 0 else _map_start(pos, opcodes)
    end_j = len(after) if end >= len(before) else _map_end(end, opcodes)
    if start_j is None or end_j is None or end_j < start_j:
        return None
    return after[start_j:end_j]


class NullObserver:
    """Used when read-back is unavailable or switched off."""

    available = False

    def snapshot(self) -> str | None:
        return None


class UIAObserver:
    """Reads the focused control's text via Windows UI Automation."""

    def __init__(self) -> None:
        self._auto = None
        self._checked = False
        self._ok = False

    @property
    def available(self) -> bool:
        if not self._checked:
            self._checked = True
            try:
                import uiautomation  # noqa: F401

                self._ok = True
            except Exception:
                log.info(
                    "uiautomation not installed - automatic learning is off "
                    "(use the tray's 'Fix last dictation' instead)"
                )
                self._ok = False
        return self._ok

    def snapshot(self) -> str | None:
        if not self.available:
            return None
        try:
            import uiautomation as auto

            control = auto.GetFocusedControl()
            if control is None:
                return None
            # TextPattern covers editors and browsers; ValuePattern covers
            # simple edit boxes. Try the richer one first.
            try:
                pattern = control.GetTextPattern()
                if pattern is not None:
                    return pattern.DocumentRange.GetText(-1)
            except Exception:
                pass
            try:
                pattern = control.GetValuePattern()
                if pattern is not None:
                    return pattern.Value
            except Exception:
                pass
            return None
        except Exception:
            log.debug("UIA snapshot failed", exc_info=True)
            return None


class CorrectionWatcher:
    """Schedules the read-back and hands any correction to a callback.

    Each new dictation cancels the pending check for the previous one: if you
    are dictating quickly, the older span has probably scrolled out of the
    control anyway, and a stale comparison is worse than no comparison.
    """

    def __init__(
        self,
        observer: TextObserver,
        on_correction: Callable[[str, str], None],
        delay: float = 6.0,
    ) -> None:
        self.observer = observer
        self.on_correction = on_correction
        self.delay = delay
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self.last_injected: str | None = None

    @property
    def available(self) -> bool:
        return bool(getattr(self.observer, "available", False))

    def note_injection(self, injected: str) -> None:
        """Call immediately after text lands in the target window."""
        self.last_injected = injected
        if not self.available:
            return
        before = self.observer.snapshot()
        if before is None or injected not in before:
            # The control does not expose its text, or the paste has not landed
            # yet. Either way there is nothing dependable to compare against.
            log.debug("no usable read-back anchor for this control")
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(
                self.delay, self._check, args=(before, injected)
            )
            self._timer.daemon = True
            self._timer.start()

    def _check(self, before: str, injected: str) -> None:
        try:
            after = self.observer.snapshot()
            if after is None or after == before:
                return
            edited = locate_edited_span(before, after, injected)
            if edited is None or edited.strip() == injected.strip():
                return
            log.debug("read-back: %r -> %r", injected, edited)
            self.on_correction(injected, edited)
        except Exception:
            log.debug("correction read-back failed", exc_info=True)

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


def make_observer(enabled: bool) -> TextObserver:
    if not enabled:
        return NullObserver()
    observer = UIAObserver()
    return observer if observer.available else NullObserver()

"""Noticing that you fixed something.

After text is injected we keep reading the focused control back for a while
and compare. Whatever the injected span has *become*, once you stop editing
it, is what you meant; the difference is the training signal `learn.py`
consumes.

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
import time
from dataclasses import dataclass
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


@dataclass
class _Tracked:
    """One dictation still being watched for edits."""

    injected: str
    # What the span looked like the last time it was handed to the callback;
    # the next report diffs against this, so each fix is learnt exactly once.
    reported: str
    # What the span looks like in the latest snapshot it was found in.
    current: str
    # The document that `current` was located in, or None until the pasted
    # text has been seen to land.
    snapshot: str | None
    born: float
    stable_since: float
    misses: int = 0


class CorrectionWatcher:
    """Keeps watching recent dictations and reports what each one becomes.

    People do not fix a mishearing on a fixed schedule: they read the sentence
    back, dictate the next one, then go back and change a word - or fix it
    thirty seconds later. So rather than one read-back at a fixed delay, the
    focused control is polled for as long as `window` seconds after each
    dictation, every recent dictation is tracked at once, and an edit is only
    reported once it has sat unchanged for `settle` seconds, so a half-typed
    word is never mistaken for the intended one.

    Tracking is incremental: each time the span is found in a new snapshot,
    that snapshot becomes the baseline. Each tick then only has to absorb the
    last couple of seconds of typing, wherever in the document it happened.
    """

    def __init__(
        self,
        observer: TextObserver,
        on_correction: Callable[[str, str], None],
        settle: float = 3.0,
        window: float = 120.0,
        poll: float = 2.0,
        max_tracked: int = 6,
        anchor_timeout: float = 3.0,
        miss_limit: int = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.observer = observer
        self.on_correction = on_correction
        self.settle = settle
        self.window = window
        self.poll = poll
        self.max_tracked = max_tracked
        # How long to keep looking for the pasted text before concluding the
        # control does not expose it. Pastes land asynchronously.
        self.anchor_timeout = anchor_timeout
        # Consecutive changed snapshots the span could not be found in before
        # a dictation is given up on. Generous, so alt-tabbing away to type
        # something else and coming back to fix the sentence still works.
        self.miss_limit = miss_limit
        self._clock = clock
        self._items: list[_Tracked] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = False
        self._thread: threading.Thread | None = None
        self._last_seen: str | None = None
        self._anchor_failed = False
        self.last_injected: str | None = None

    @property
    def available(self) -> bool:
        return bool(getattr(self.observer, "available", False))

    @property
    def tracking(self) -> int:
        with self._lock:
            return len(self._items)

    def note_injection(self, injected: str) -> None:
        """Call immediately after text lands in the target window."""
        self.last_injected = injected
        if not self.available or not injected or not injected.strip():
            return
        now = self._clock()
        with self._lock:
            self._items.append(_Tracked(injected, injected, injected, None, now, now))
            del self._items[: -self.max_tracked]
            self._stopped = False
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="whispa-readback", daemon=True
                )
                self._thread.start()
        self._wake.set()

    # --- polling ------------------------------------------------------------

    def _run(self) -> None:
        while True:
            # Cleared before the tick, so a dictation that arrives during the
            # tick still cuts the wait short rather than being missed.
            self._wake.clear()
            with self._lock:
                if self._stopped or not self._items:
                    self._thread = None
                    return
                items = list(self._items)
            try:
                self._tick(items)
            except Exception:
                log.debug("correction read-back failed", exc_info=True)
            # A paste that has not landed yet is checked again quickly; a
            # settled document only needs a look every couple of seconds.
            anchoring = any(item.snapshot is None for item in items)
            self._wake.wait(min(self.poll, 0.3) if anchoring else self.poll)

    def poll_once(self) -> None:
        """Run one polling step synchronously. For tests and diagnostics."""
        with self._lock:
            items = list(self._items)
        self._tick(items)

    def _tick(self, items: list[_Tracked]) -> None:
        now = self._clock()
        after = self.observer.snapshot()
        expired = [item for item in items if now - item.born > self.window]
        self._drop(expired)
        # None means the focused control cannot be read right now (focus has
        # moved to a window UIA cannot see into). Nothing new can be observed,
        # but an edit already seen still settles and gets reported.
        changed = after is not None and after != self._last_seen
        if after is not None:
            self._last_seen = after

        dropped: list[_Tracked] = []
        for item in items:
            if item in expired:
                continue
            if item.snapshot is None:
                if after is not None and not self._anchor(item, after, now):
                    dropped.append(item)
                elif after is None and now - item.born >= self.anchor_timeout:
                    dropped.append(item)
                continue
            if changed and not self._follow(item, after, now):
                dropped.append(item)
                continue
            if (
                item.current.strip() != item.reported.strip()
                and now - item.stable_since >= self.settle
            ):
                log.debug("read-back: %r -> %r", item.reported, item.current)
                self.on_correction(item.reported, item.current)
                item.reported = item.current
        self._drop(dropped)

    def _anchor(self, item: _Tracked, after: str, now: float) -> bool:
        """Wait for the pasted text to show up in the control.

        Returns False once it is clear this control will not show it.
        """
        for candidate in (item.injected, item.injected.strip()):
            if candidate and candidate in after:
                item.snapshot = after
                item.current = item.reported = candidate
                item.stable_since = now
                if self._anchor_failed:
                    self._anchor_failed = False
                    log.info("read-back working again in the focused window")
                return True
        if now - item.born < self.anchor_timeout:
            return True
        if not self._anchor_failed:
            self._anchor_failed = True
            log.info(
                "can't read the typed text back from the focused window, so "
                "edits made here won't be learnt (the tray's 'Fix last "
                "dictation...' still works)"
            )
        return False

    def _follow(self, item: _Tracked, after: str, now: float) -> bool:
        """Re-locate the span in a changed document. False = give up on it."""
        if item.current and item.current in after:
            item.snapshot = after
            item.misses = 0
            return True
        edited = locate_edited_span(item.snapshot, after, item.current)
        if edited is None:
            # Focus is probably on a different window right now.
            item.misses += 1
            return item.misses <= self.miss_limit
        item.misses = 0
        item.snapshot = after
        if edited != item.current:
            item.current = edited
            item.stable_since = now
        return True

    def _drop(self, items: list[_Tracked]) -> None:
        if not items:
            return
        with self._lock:
            self._items = [i for i in self._items if i not in items]

    def cancel(self) -> None:
        with self._lock:
            self._items = []
            self._stopped = True
        self._wake.set()


def make_observer(enabled: bool) -> TextObserver:
    if not enabled:
        return NullObserver()
    observer = UIAObserver()
    return observer if observer.available else NullObserver()

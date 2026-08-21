"""The state machine that ties hotkey -> mic -> whisper -> keyboard together."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import numpy as np

from .audio import duration_seconds, peak_level
from .format import render_marks
from .transcribe import clean_text

log = logging.getLogger(__name__)


class State(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    ERROR = "error"


@dataclass
class Stats:
    utterances: int = 0
    dropped_short: int = 0
    dropped_silent: int = 0
    dropped_empty: int = 0
    characters: int = 0
    audio_seconds: float = 0.0
    transcribe_seconds: float = 0.0
    errors: int = 0
    history: list[str] = field(default_factory=list)

    def summary(self) -> str:
        rt = (
            self.audio_seconds / self.transcribe_seconds
            if self.transcribe_seconds
            else 0.0
        )
        return (
            f"{self.utterances} utterance(s), {self.characters} chars, "
            f"{self.audio_seconds:.1f}s audio in {self.transcribe_seconds:.1f}s "
            f"({rt:.2f}x realtime), {self.errors} error(s)"
        )


class DictationEngine:
    """Owns the recording lifecycle.

    Transcription runs on a dedicated worker thread. Two reasons: the hotkey
    callback thread belongs to pynput and blocking it stops all key handling,
    and a single worker (rather than one thread per utterance) keeps the
    injected text in the order it was spoken.
    """

    # Peak amplitude below this is a muted or disconnected mic, not quiet
    # speech. Worth distinguishing: "nothing was said" and "your mic is off"
    # need different responses from the user.
    SILENCE_PEAK = 0.005

    def __init__(
        self,
        recorder,
        transcriber,
        injector,
        sample_rate: int = 16000,
        min_seconds: float = 0.35,
        replacements: dict[str, str] | None = None,
        trailing_space: bool = True,
        on_state: Callable[[State, str], None] | None = None,
        learner=None,
        watcher=None,
        formatter: Callable[[str], str] | None = None,
    ) -> None:
        self.recorder = recorder
        self.transcriber = transcriber
        self.injector = injector
        self.sample_rate = sample_rate
        self.min_seconds = min_seconds
        self.replacements = replacements or {}
        self.trailing_space = trailing_space
        self.on_state = on_state
        self.learner = learner
        self.watcher = watcher
        # Shapes the finished transcript into paragraphs and lists. Injected
        # rather than imported so the engine has no opinion about formatting
        # and the tests can watch exactly what gets typed.
        self.formatter = formatter
        self.stats = Stats()
        # The exact string last injected, so the manual "fix that" dialog knows
        # what it is correcting.
        self.last_injected: str | None = None

        self._state = State.IDLE
        self._lock = threading.Lock()
        self._jobs: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()
        self._started_at = 0.0

    # --- state --------------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    def _set_state(self, state: State, detail: str = "") -> None:
        self._state = state
        if self.on_state:
            try:
                self.on_state(state, detail)
            except Exception:
                log.debug("state callback failed", exc_info=True)

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Spin up the worker thread. Safe to call twice."""
        if self._worker is not None:
            return
        self._stopping.clear()
        self._worker = threading.Thread(
            target=self._run_worker, name="whispa-transcribe", daemon=True
        )
        self._worker.start()

    def shutdown(self, timeout: float = 10.0) -> None:
        """Finish any queued transcriptions, then stop the worker."""
        self._stopping.set()
        self._jobs.put(None)
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None

    # --- hotkey callbacks ---------------------------------------------------

    def begin_recording(self) -> None:
        with self._lock:
            if self.recorder.is_recording:
                return
            try:
                self.recorder.start()
            except Exception as exc:
                self.stats.errors += 1
                log.exception("could not open the microphone")
                self._set_state(State.ERROR, f"microphone unavailable: {exc}")
                return
            self._started_at = time.monotonic()
        self._set_state(State.RECORDING, "listening")

    def end_recording(self) -> None:
        with self._lock:
            if not self.recorder.is_recording:
                return
            try:
                audio = self.recorder.stop()
            except Exception as exc:
                self.stats.errors += 1
                log.exception("could not stop the microphone cleanly")
                self._set_state(State.ERROR, f"recording failed: {exc}")
                return

        seconds = duration_seconds(audio, self.sample_rate)
        if seconds < self.min_seconds:
            # Almost always a mis-press or a key bounce, not speech.
            self.stats.dropped_short += 1
            log.info("ignoring %.2fs recording (below %.2fs)", seconds, self.min_seconds)
            self._set_state(State.IDLE, "too short")
            return
        if peak_level(audio) < self.SILENCE_PEAK:
            self.stats.dropped_silent += 1
            log.warning("recording was silent - is the microphone muted?")
            self._set_state(State.IDLE, "silent - check your microphone")
            return

        self._set_state(State.TRANSCRIBING, f"{seconds:.1f}s")
        self._jobs.put(audio)

    # --- worker -------------------------------------------------------------

    def _run_worker(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                self._jobs.task_done()
                if self._stopping.is_set():
                    return
                continue
            try:
                self._handle(job)
            except Exception:
                self.stats.errors += 1
                log.exception("transcription job failed")
                self._set_state(State.ERROR, "transcription failed")
            finally:
                self._jobs.task_done()

    def _handle(self, audio: np.ndarray) -> None:
        seconds = duration_seconds(audio, self.sample_rate)
        t0 = time.monotonic()
        raw = self.transcriber.transcribe(audio)
        elapsed = time.monotonic() - t0
        # Confirmed corrections are applied on top of the hand-written ones,
        # and win, because they were learnt from this user's own edits.
        replacements = dict(self.replacements)
        if self.learner is not None:
            replacements.update(self.learner.replacements())
        text = clean_text(raw, replacements)
        # The transcriber marks pauses with private-use characters; whether or
        # not a formatter is configured, none of them may reach the clipboard.
        text = self.formatter(text) if self.formatter else render_marks(text, "off")

        self.stats.audio_seconds += seconds
        self.stats.transcribe_seconds += elapsed

        if not text:
            self.stats.dropped_empty += 1
            self._set_state(State.IDLE, "nothing recognised")
            return

        # Appended here rather than in the injector so every injector - real,
        # clipboard-only or dry-run - agrees on the exact string.
        if self.trailing_space and not text.endswith(" "):
            text = text + " "

        ok = self.injector.inject(text)
        self.stats.utterances += 1
        self.stats.characters += len(text)
        self.stats.history.append(text)
        if not ok:
            self.stats.errors += 1
            self._set_state(State.ERROR, "could not type the text")
            return

        self.last_injected = text
        if self.watcher is not None:
            # Watch what the user does to this text over the next few seconds;
            # any edit is a correction to learn from.
            self.watcher.note_injection(text)
        preview = text if len(text) <= 60 else text[:57] + "..."
        self._set_state(State.IDLE, preview)

    # --- helpers for tests / the tray ---------------------------------------

    def teach(self, corrected: str) -> list[tuple[str, str]]:
        """Manual correction path: 'what I actually said was ...'."""
        if self.learner is None or not self.last_injected:
            return []
        return self.learner.observe(self.last_injected, corrected)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until the queue drains. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._jobs.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

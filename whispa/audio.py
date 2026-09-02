"""Microphone capture.

Kept deliberately thin: `sounddevice` streams float32 frames into a list while
the hotkey is held, and `stop()` concatenates them into the single mono array
that faster-whisper wants. No resampling, no file I/O, no ffmpeg.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

import numpy as np

from . import devices

log = logging.getLogger(__name__)


class Recorder(Protocol):
    """The surface the engine depends on, so tests can supply a fake."""

    def start(self) -> None: ...
    def stop(self) -> np.ndarray: ...
    @property
    def is_recording(self) -> bool: ...
    @property
    def level(self) -> float: ...


class MicRecorder:
    """Records from the configured (or system default) input device at 16 kHz mono.

    `device` is a spec as described in whispa/devices.py: None for the system
    default, a name, or an index. It is resolved to a real device on every
    `refresh()`, which a background poll runs every few seconds while idle, so
    a headset plugged in mid-session is picked up without a restart and a
    device index that shifted is not a problem.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        device: int | str | None = None,
        max_seconds: float = 300.0,
        poll_seconds: float = 15.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.max_seconds = max_seconds
        self.poll_seconds = poll_seconds
        self._frames: list[np.ndarray] = []
        self._stream: Any = None
        self._lock = threading.Lock()
        # Held while PortAudio is being re-initialised or a stream opened or
        # closed: tearing PortAudio down under an open stream is a crash.
        self._device_lock = threading.Lock()
        self._recording = False
        self._overflowed = False
        self._level = 0.0
        self._choice: devices.Choice | None = None
        self._poll_stop = threading.Event()
        self._poller: threading.Thread | None = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def level(self) -> float:
        """Current input loudness, 0.0-1.0, for the on-screen meter.

        This is what makes the indicator honest: it is computed from the audio
        actually arriving from the device, so a dead or muted microphone shows
        a flat meter instead of a reassuring animation.
        """
        return self._level

    @property
    def device_name(self) -> str:
        """The microphone the next recording will use, for the tray and log."""
        return self._choice.describe() if self._choice is not None else "not checked yet"

    # --- device choice ------------------------------------------------------

    def refresh(self) -> "devices.Choice | None":
        """Re-enumerate devices and re-resolve the spec.

        Skipped while recording (PortAudio cannot be restarted under an open
        stream); the next idle poll picks it up.
        """
        with self._device_lock:
            if self._recording:
                return self._choice
            devices.refresh()
            return self._resolve_locked()

    def _resolve_locked(self) -> "devices.Choice":
        choice = devices.resolve(self.device)
        previous = self._choice
        if previous is None or choice != previous:
            if choice.fallback:
                log.warning("microphone: %s (%s)", choice.describe(), choice.fallback)
            else:
                log.info("microphone: %s", choice.describe())
        self._choice = choice
        return choice

    def set_device(self, spec: int | str | None) -> "devices.Choice | None":
        self.device = spec
        return self.refresh()

    def start_polling(self) -> None:
        """Keep the device choice current in the background.

        This is what makes "system default" actually follow the system: with
        no poll, whispa would record from whatever was the default when it
        started, for as long as it ran.
        """
        if self.poll_seconds <= 0 or self._poller is not None:
            return

        def run() -> None:
            while not self._poll_stop.wait(self.poll_seconds):
                try:
                    self.refresh()
                except Exception:
                    log.debug("device poll failed", exc_info=True)

        self._poll_stop.clear()
        self._poller = threading.Thread(target=run, name="whispa-devices", daemon=True)
        self._poller.start()

    def stop_polling(self) -> None:
        self._poll_stop.set()
        self._poller = None

    # --- capture ------------------------------------------------------------

    def _update_level(self, block: np.ndarray) -> None:
        rms = float(np.sqrt(np.mean(np.square(block)))) if len(block) else 0.0
        # Speech RMS sits around 0.02-0.2, so a linear bar barely moves. Map it
        # through a decibel-ish curve for something that looks like a meter.
        if rms <= 1e-5:
            scaled = 0.0
        else:
            db = 20.0 * np.log10(rms)
            scaled = float(np.clip((db + 60.0) / 60.0, 0.0, 1.0))
        # Fast attack so speech registers immediately, slow release so the bar
        # doesn't strobe between syllables.
        self._level = scaled if scaled > self._level else self._level * 0.75 + scaled * 0.25

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status:
            # Overruns are common on a busy machine and are not fatal - the
            # dropped frames just become a small gap in the audio.
            log.debug("audio status: %s", status)
        block = indata.copy().reshape(-1)
        self._update_level(block)
        with self._lock:
            if not self._recording:
                return
            if self._collected_seconds() >= self.max_seconds:
                self._overflowed = True
                return
            self._frames.append(block)

    def _collected_seconds(self) -> float:
        return sum(len(f) for f in self._frames) / float(self.sample_rate)

    def _open_stream(self, index: int | None) -> Any:
        import sounddevice as sd  # imported lazily: no audio device in CI/tests

        stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=index,
            callback=self._callback,
            blocksize=0,
        )
        stream.start()
        return stream

    def start(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._frames = []
            self._overflowed = False
            self._level = 0.0
            self._recording = True
        try:
            with self._device_lock:
                if self._choice is None:
                    self._resolve_locked()
                try:
                    self._stream = self._open_stream(self._choice.index)
                except Exception as exc:
                    # The device list is only as fresh as the last poll. If the
                    # chosen microphone has just gone away, look again once
                    # before giving up - the hotkey press is the moment the
                    # user least wants a "microphone unavailable".
                    log.info("could not open %s (%s); re-checking devices", self.device_name, exc)
                    devices.refresh()
                    choice = self._resolve_locked()
                    self._stream = self._open_stream(choice.index)
        except Exception:
            with self._lock:
                self._recording = False
            raise
        log.debug("recording started (%s)", self.device_name)

    def stop(self) -> np.ndarray:
        with self._lock:
            if not self._recording:
                return np.zeros(0, dtype=np.float32)
            self._recording = False
            self._level = 0.0
        with self._device_lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                finally:
                    self._stream = None
        with self._lock:
            frames, self._frames = self._frames, []
        if self._overflowed:
            log.warning("recording hit the %.0fs cap and was truncated", self.max_seconds)
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames).astype(np.float32, copy=False)


def speech_spans(
    audio: np.ndarray,
    sample_rate: int,
    min_silence: float = 1.0,
    frame_seconds: float = 0.03,
    floor: float = 0.01,
    relative: float = 0.06,
) -> list[tuple[int, int]]:
    """Find the runs of speech, split wherever the speaker paused.

    Whisper cannot help here: its own timestamps are stretched to cover
    silence, so a deliberate two-second pause between paragraphs comes back as
    one unbroken segment with the words either side simply held longer. The
    pause is only visible in the waveform, which is where this looks for it.

    Returns [(start_sample, end_sample), ...], one per run of speech, with
    silences shorter than `min_silence` left inside a run. The bounds are the
    speech itself, unpadded, so the distance between two spans is the length of
    the real pause; whoever slices the audio adds their own padding. A
    recording with no long pause gives exactly one span, which is the common
    case and costs one model call as before.
    """
    if audio is None or len(audio) == 0:
        return []
    frame = max(1, int(frame_seconds * sample_rate))
    usable = (len(audio) // frame) * frame
    if usable < frame:
        return [(0, len(audio))]
    frames = np.abs(audio[:usable]).reshape(-1, frame).max(axis=1)
    # Relative to this recording's own peak, so a quiet mic and a loud one both
    # work, with an absolute floor so that pure noise is never "speech".
    threshold = max(floor, relative * float(frames.max()))
    loud = frames > threshold
    if not loud.any():
        return []

    gap_frames = max(1, int(round(min_silence / frame_seconds)))
    spans: list[tuple[int, int]] = []
    start: int | None = None
    quiet = 0
    for index, is_loud in enumerate(loud):
        if is_loud:
            if start is None:
                start = index
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= gap_frames:
                spans.append((start, index - quiet + 1))
                start = None
                quiet = 0
    if start is not None:
        spans.append((start, len(loud)))

    return [
        (first * frame, min(len(audio), last * frame))
        for first, last in spans
        if last > first
    ]


def duration_seconds(audio: np.ndarray, sample_rate: int) -> float:
    return len(audio) / float(sample_rate) if sample_rate else 0.0


def peak_level(audio: np.ndarray) -> float:
    """Peak absolute amplitude, used to tell 'silence' from 'mic is muted'."""
    return float(np.max(np.abs(audio))) if len(audio) else 0.0


def list_input_devices(all_apis: bool = False) -> list[dict[str, Any]]:
    """Enumerate usable input devices; see whispa/devices.py."""
    return devices.list_input_devices(all_apis=all_apis)

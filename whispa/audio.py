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
    """Records from the default (or configured) input device at 16 kHz mono."""

    def __init__(
        self,
        sample_rate: int = 16000,
        device: int | None = None,
        max_seconds: float = 300.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.max_seconds = max_seconds
        self._frames: list[np.ndarray] = []
        self._stream: Any = None
        self._lock = threading.Lock()
        self._recording = False
        self._overflowed = False
        self._level = 0.0

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

    def start(self) -> None:
        import sounddevice as sd  # imported lazily: no audio device in CI/tests

        with self._lock:
            if self._recording:
                return
            self._frames = []
            self._overflowed = False
            self._level = 0.0
            self._recording = True
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=self.device,
            callback=self._callback,
            blocksize=0,
        )
        self._stream.start()
        log.debug("recording started (device=%s)", self.device)

    def stop(self) -> np.ndarray:
        with self._lock:
            if not self._recording:
                return np.zeros(0, dtype=np.float32)
            self._recording = False
            self._level = 0.0
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


def duration_seconds(audio: np.ndarray, sample_rate: int) -> float:
    return len(audio) / float(sample_rate) if sample_rate else 0.0


def peak_level(audio: np.ndarray) -> float:
    """Peak absolute amplitude, used to tell 'silence' from 'mic is muted'."""
    return float(np.max(np.abs(audio))) if len(audio) else 0.0


def list_input_devices() -> list[dict[str, Any]]:
    """Enumerate usable input devices, for the tray menu and troubleshooting."""
    import sounddevice as sd

    devices = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            devices.append(
                {
                    "index": idx,
                    "name": dev.get("name", "?"),
                    "channels": dev.get("max_input_channels", 0),
                    "default_samplerate": dev.get("default_samplerate"),
                }
            )
    return devices

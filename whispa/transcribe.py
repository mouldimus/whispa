"""faster-whisper wrapper.

Takes the float32 array straight from the recorder - no temp WAV files, so no
ffmpeg dependency and no disk round-trip per utterance.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


class Transcriber(Protocol):
    def transcribe(self, audio: np.ndarray) -> str: ...
    def load(self) -> None: ...


# Whisper emits a small set of stock phrases when handed silence or noise -
# training-data artefacts from unlabelled segments. Injecting "Thank you." into
# the user's document because they fumbled the hotkey is worse than injecting
# nothing, so these are dropped when they are the *entire* result.
_HALLUCINATION_ON_SILENCE = {
    "you",
    "thank you.",
    "thanks for watching!",
    "thank you for watching!",
    "thank you very much.",
    "bye.",
    "please subscribe!",
    ".",
    "...",
}


class WhisperTranscriber:
    def __init__(
        self,
        model: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 0,
        language: str | None = "en",
        beam_size: int = 1,
        vad_filter: bool = True,
        initial_prompt: str = "",
        sample_rate: int = 16000,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.initial_prompt = initial_prompt or None
        self.sample_rate = sample_rate
        self._model = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Download (first run only) and load the model into memory."""
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        t0 = time.monotonic()
        self._model = WhisperModel(
            self.model_name,
            device=self.device,
            compute_type=self.compute_type,
            cpu_threads=self.cpu_threads,
        )
        log.info("model %r loaded in %.1fs", self.model_name, time.monotonic() - t0)

    def warmup(self) -> None:
        """Decode 0.5s of silence so the first real utterance isn't slow."""
        self.load()
        silence = np.zeros(self.sample_rate // 2, dtype=np.float32)
        try:
            self.transcribe(silence)
        except Exception:  # pragma: no cover - warmup must never be fatal
            log.debug("warmup transcription failed", exc_info=True)

    def transcribe(self, audio: np.ndarray) -> str:
        if audio is None or len(audio) == 0:
            return ""
        self.load()
        assert self._model is not None

        t0 = time.monotonic()
        segments, _info = self._model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
            initial_prompt=self.initial_prompt,
            condition_on_previous_text=False,
        )
        # `segments` is a generator; consuming it is where decoding happens.
        text = " ".join(seg.text.strip() for seg in segments).strip()
        elapsed = time.monotonic() - t0
        audio_seconds = len(audio) / float(self.sample_rate)
        log.info(
            "transcribed %.1fs of audio in %.1fs (%.2fx realtime), %d chars",
            audio_seconds,
            elapsed,
            (audio_seconds / elapsed) if elapsed else 0.0,
            len(text),
        )
        return text


def clean_text(text: str, replacements: dict[str, str] | None = None) -> str:
    """Normalise whitespace, drop silence-hallucinations, apply replacements."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return ""
    if text.lower() in _HALLUCINATION_ON_SILENCE:
        log.info("dropping likely silence-hallucination: %r", text)
        return ""
    for src, dst in (replacements or {}).items():
        text = re.sub(rf"\b{re.escape(src)}\b", dst, text, flags=re.IGNORECASE)
    return text

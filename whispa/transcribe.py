"""faster-whisper wrapper.

Takes the float32 array straight from the recorder - no temp WAV files, so no
ffmpeg dependency and no disk round-trip per utterance.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from typing import Protocol

import numpy as np

from .audio import speech_spans
from .format import Segment, join_segments

log = logging.getLogger(__name__)

# Each slice of a paused recording is decoded with this much extra audio either
# side, so that a word starting right on the boundary is not cut in half.
SLICE_PAD_SECONDS = 0.15


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
        paragraph_pause_seconds: float = 2.0,
        comma_pause_seconds: float = 0.8,
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
        # A silence this long between two segments is a paragraph break; 0
        # turns pause-based paragraphing off.
        self.paragraph_pause_seconds = paragraph_pause_seconds
        # A shorter pause than that, where whisper still tends to drop the
        # comma it would otherwise have written. Only fires when the next
        # word starts a new clause - see join_segments and the comment on
        # Config.comma_pause_seconds. 0 turns it off.
        self.comma_pause_seconds = comma_pause_seconds
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

    def _decode(self, audio: np.ndarray, words: bool = False):
        assert self._model is not None
        segments, _info = self._model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
            initial_prompt=self.initial_prompt,
            condition_on_previous_text=False,
            word_timestamps=words,
        )
        # `segments` is a generator; consuming it is where decoding happens.
        return list(segments)

    @staticmethod
    def _plain(raw) -> list[Segment]:
        return [
            Segment(text=(seg.text or "").strip(), start=seg.start, end=seg.end)
            for seg in raw
            if (seg.text or "").strip()
        ]

    def transcribe_segments(self, audio: np.ndarray) -> list[Segment]:
        """Decode, and mark where the speaker paused.

        The pauses are found in the waveform, not in whisper's timestamps.
        Whisper stretches those over silence - a two-second gap comes back as
        the word before it simply lasting two seconds longer - and its VAD
        deletes the silence outright, so the transcript on its own carries no
        trace of where the paragraphs were.

        Decoding the pieces separately would show the pauses, and also makes
        the transcription measurably worse: a two-second fragment with nothing
        around it comes back as "ASK NOT!" where the whole recording gives
        "ask not what your country". So the audio is decoded once, exactly as
        it always was, and only the *text* is cut - at the words whose timings
        straddle each silence.

        (A two-pass version was tried here - decode the words with no
        punctuation, then decode again primed with that line so a second pass
        could add the punctuation. Measured against real audio it made things
        worse: whisper's prompt biases *style* as well as vocabulary, so an
        unpunctuated, lowercase prompt produced unpunctuated, lowercase
        output. Reverted; see join_segments for what replaced it.)
        """
        if audio is None or len(audio) == 0:
            return []
        self.load()
        pauses = self._pauses(audio)
        if not pauses:
            # No pause to mark, so no need to pay for word timings either.
            return self._plain(self._decode(audio))

        raw = self._decode(audio, words=True)
        words = [w for seg in raw for w in (getattr(seg, "words", None) or [])]
        if not words:
            # Some builds return no word timings; a run-on transcript beats one
            # chopped in the wrong places.
            log.debug("no word timings available; paragraphs not marked")
            return self._plain(raw)
        return self._split_at_pauses(words, pauses)

    def _pause_threshold(self) -> float:
        """The shortest gap worth finding in the waveform at all - whichever
        of the two thresholds below is smaller and actually turned on."""
        candidates = [t for t in (self.paragraph_pause_seconds, self.comma_pause_seconds) if t > 0]
        return min(candidates) if candidates else 0.0

    def _pauses(self, audio: np.ndarray) -> list[tuple[float, float]]:
        """Every silence long enough to matter, in seconds.

        Whether a given gap becomes a paragraph break or just a comma is
        decided later, in join_segments, purely from its length - this only
        finds where the speaker actually paused.
        """
        threshold = self._pause_threshold()
        if threshold <= 0:
            return []
        spans = speech_spans(audio, self.sample_rate, min_silence=threshold)
        pauses = [
            (previous[1] / self.sample_rate, current[0] / self.sample_rate)
            for previous, current in zip(spans, spans[1:])
        ]
        if pauses:
            log.info(
                "%d pause(s) in the recording: %s",
                len(pauses),
                ", ".join(f"{e - s:.1f}s" for s, e in pauses),
            )
        return pauses

    @staticmethod
    def _split_at_pauses(words, pauses: list[tuple[float, float]]) -> list[Segment]:
        """Group the words into one Segment per paragraph.

        A word begins a new paragraph if it had not finished when the silence
        began - which covers both the word whose timing was stretched across
        the gap and the word that genuinely starts after it.
        """
        groups: list[list] = [[]]
        boundaries: list[tuple[float, float]] = []
        remaining = list(pauses)
        for word in words:
            while remaining and word.end > remaining[0][0] and groups[-1]:
                boundaries.append(remaining.pop(0))
                groups.append([])
            groups[-1].append(word)

        out: list[Segment] = []
        for index, group in enumerate(groups):
            text = "".join(w.word for w in group).strip()
            if not text:
                continue
            # Report the silence either side as the bounds, so the gap between
            # two paragraphs is the length of the real pause rather than
            # whisper's stretched idea of it.
            start = boundaries[index - 1][1] if index else group[0].start
            end = boundaries[index][0] if index < len(boundaries) else group[-1].end
            out.append(Segment(text=text, start=start, end=end))
        return out

    def transcribe(self, audio: np.ndarray) -> str:
        if audio is None or len(audio) == 0:
            return ""
        t0 = time.monotonic()
        text = join_segments(
            self.transcribe_segments(audio),
            self.paragraph_pause_seconds,
            self.comma_pause_seconds,
        ).strip()
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

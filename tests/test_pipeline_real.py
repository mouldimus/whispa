"""End-to-end check of the real pipeline against real speech.

This exercises WhisperTranscriber -> DictationEngine -> injector with an actual
model and an actual recording. The mic and the keyboard are the only parts
faked, because this box has neither.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from faster_whisper.audio import decode_audio

from whispa.app import DictationEngine, State
from whispa.inject import NullInjector
from whispa.transcribe import WhisperTranscriber

SAMPLE = Path(__file__).parent / "data" / "jfk.flac"
EXPECTED_WORDS = ["ask", "not", "country", "americans"]


class ReplayRecorder:
    """Stands in for the microphone by replaying a file."""

    def __init__(self, audio: np.ndarray) -> None:
        self.audio = audio
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        self._recording = True

    def stop(self) -> np.ndarray:
        self._recording = False
        return self.audio


def main() -> int:
    audio = decode_audio(str(SAMPLE), sampling_rate=16000)
    audio = np.asarray(audio, dtype=np.float32)
    print(f"loaded {len(audio)/16000:.2f}s of audio, peak={np.max(np.abs(audio)):.3f}")

    states: list[tuple[State, str]] = []
    transcriber = WhisperTranscriber(model="base.en", compute_type="int8")
    injector = NullInjector()
    engine = DictationEngine(
        recorder=ReplayRecorder(audio),
        transcriber=transcriber,
        injector=injector,
        on_state=lambda s, d: states.append((s, d)),
    )
    engine.start()

    engine.begin_recording()
    assert engine.state is State.RECORDING, engine.state
    engine.end_recording()

    assert engine.wait_idle(timeout=180), "transcription did not finish in time"
    engine.shutdown()

    print("states:", [(s.value, d) for s, d in states])
    print("stats :", engine.stats.summary())

    assert injector.injected, "nothing was injected"
    text = injector.injected[0]
    print(f"TRANSCRIPT: {text!r}")

    lowered = text.lower()
    missing = [w for w in EXPECTED_WORDS if w not in lowered]
    assert not missing, f"transcript missing expected words {missing}: {text!r}"
    assert text.endswith(" "), "trailing space should be appended for the next dictation"
    assert engine.stats.utterances == 1, engine.stats
    assert engine.stats.errors == 0, engine.stats
    assert engine.state is State.IDLE, engine.state

    print("\nPASS: real audio -> real model -> injected text")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

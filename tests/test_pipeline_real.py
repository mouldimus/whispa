"""End-to-end check of the real pipeline against real speech.

This exercises WhisperTranscriber -> DictationEngine -> injector with an actual
model and an actual recording. The mic and the keyboard are the only parts
faked, because this box has neither.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from faster_whisper.audio import decode_audio

from whispa.app import DictationEngine, State
from whispa.inject import NullInjector
from whispa.learn import CorrectionLearner
from whispa.observe import CorrectionWatcher
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

    if not learning_loop(audio, transcriber, text):
        return 1
    return 0


class ReplayObserver:
    """Stands in for UI Automation by replaying document snapshots."""

    available = True

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)

    def snapshot(self):
        return self.snapshots.pop(0) if self.snapshots else None


def learning_loop(audio, transcriber, first_transcript: str) -> bool:
    """Verify the full correction loop against real model output.

    The real transcript is edited the way a user would edit it, the read-back
    watcher observes that edit, and a second real transcription is checked for
    the learnt correction. Both ends of the loop are genuine model output.
    """
    print("\n--- learning loop ---")
    with tempfile.TemporaryDirectory() as tmp:
        learner = CorrectionLearner(Path(tmp) / "learned.json", min_count=2)

        # The user twice capitalises "country" - a proper noun in their world.
        for i in range(2):
            before = f"Notes {i}: {first_transcript}"
            after = before.replace("country", "Country")
            watcher = CorrectionWatcher(
                observer=ReplayObserver([before, after]),
                on_correction=learner.observe,
                delay=0.01,
            )
            watcher.note_injection(first_transcript)
            time.sleep(0.2)

        learned = learner.replacements()
        print("learned:", learned)
        assert "country" in learned, f"correction was not learnt: {learned}"
        assert learned["country"] == "Country", learned

        # Now transcribe the same real audio again, with the learner attached.
        injector = NullInjector()
        engine = DictationEngine(
            recorder=ReplayRecorder(audio),
            transcriber=transcriber,
            injector=injector,
            learner=learner,
        )
        engine.start()
        engine.begin_recording()
        engine.end_recording()
        assert engine.wait_idle(timeout=180), "second transcription timed out"
        engine.shutdown()

        second = injector.injected[0]
        print(f"SECOND PASS: {second!r}")
        assert "Country" in second, f"learnt correction not applied: {second!r}"
        assert "country" not in second, f"uncorrected word survived: {second!r}"
        print("\nPASS: real transcript -> user edit -> learnt -> applied next time")
    return True


if __name__ == "__main__":
    raise SystemExit(main())

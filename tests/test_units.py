"""Unit tests for the parts that need no microphone, keyboard or model."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whispa.app import DictationEngine, State
from whispa.audio import duration_seconds, peak_level
from whispa.config import Config
from whispa.hotkey import HotkeySpec, canonical, parse_hotkey
from whispa.inject import NullInjector
from whispa.transcribe import clean_text


class FakeRecorder:
    def __init__(self, audio: np.ndarray, fail: bool = False) -> None:
        self.audio = audio
        self.fail = fail
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        if self.fail:
            raise OSError("no such device")
        self._recording = True

    def stop(self) -> np.ndarray:
        self._recording = False
        return self.audio


class FakeTranscriber:
    def __init__(self, text: str = "hello world") -> None:
        self.text = text
        self.calls = 0

    def load(self) -> None:
        pass

    def transcribe(self, audio: np.ndarray) -> str:
        self.calls += 1
        return self.text


def speech(seconds: float, rate: int = 16000) -> np.ndarray:
    """Audible noise - loud enough to clear the silence gate."""
    return (np.random.RandomState(0).randn(int(seconds * rate)) * 0.2).astype(np.float32)


class TestHotkeyParsing(unittest.TestCase):
    def test_canonicalises_sides(self):
        self.assertEqual(canonical("ctrl_l"), "ctrl")
        self.assertEqual(canonical("<Ctrl_R>"), "ctrl")
        self.assertEqual(canonical("alt_gr"), "alt")
        self.assertEqual(canonical("F9"), "f9")

    def test_parses_chord_and_single(self):
        self.assertEqual(parse_hotkey("<ctrl>+<alt>+d"), frozenset({"ctrl", "alt", "d"}))
        self.assertEqual(parse_hotkey("f9"), frozenset({"f9"}))

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            parse_hotkey("")


class TestHoldMode(unittest.TestCase):
    def setUp(self):
        self.spec = HotkeySpec("f9", "hold")

    def test_press_release_cycle(self):
        self.assertEqual(self.spec.press("f9"), "start")
        self.assertEqual(self.spec.release("f9"), "stop")

    def test_auto_repeat_does_not_restart(self):
        self.assertEqual(self.spec.press("f9"), "start")
        # A held key repeats; only the first press may start a recording.
        self.assertIsNone(self.spec.press("f9"))
        self.assertIsNone(self.spec.press("f9"))
        self.assertEqual(self.spec.release("f9"), "stop")

    def test_unrelated_keys_ignored(self):
        self.assertIsNone(self.spec.press("a"))
        self.assertIsNone(self.spec.release("a"))

    def test_release_without_press_is_safe(self):
        self.assertIsNone(self.spec.release("f9"))


class TestChordMode(unittest.TestCase):
    def test_full_chord_required(self):
        spec = HotkeySpec("<ctrl>+<alt>+d", "hold")
        self.assertIsNone(spec.press("ctrl"))
        self.assertIsNone(spec.press("alt"))
        self.assertEqual(spec.press("d"), "start")
        self.assertEqual(spec.held_modifiers, {"ctrl", "alt"})
        # Letting go of any member ends the recording.
        self.assertEqual(spec.release("d"), "stop")
        self.assertIsNone(spec.release("ctrl"))
        self.assertEqual(spec.held_modifiers, {"alt"})

    def test_side_variants_satisfy_chord(self):
        spec = HotkeySpec("<ctrl>+<shift>+space", "hold")
        spec.press("ctrl_r")
        spec.press("shift_l")
        self.assertEqual(spec.press("space"), "start")


class TestToggleMode(unittest.TestCase):
    def test_alternates_and_ignores_release(self):
        spec = HotkeySpec("f9", "toggle")
        self.assertEqual(spec.press("f9"), "start")
        self.assertIsNone(spec.release("f9"))
        self.assertEqual(spec.press("f9"), "stop")
        self.assertIsNone(spec.release("f9"))
        self.assertEqual(spec.press("f9"), "start")


class TestCleanText(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(clean_text("  hello   there \n world "), "hello there world")

    def test_drops_silence_hallucinations(self):
        for junk in ("Thank you.", "thanks for watching!", "you", "..."):
            self.assertEqual(clean_text(junk), "", junk)

    def test_keeps_hallucination_phrase_inside_real_speech(self):
        text = clean_text("I said thank you to the driver")
        self.assertEqual(text, "I said thank you to the driver")

    def test_applies_replacements(self):
        self.assertEqual(
            clean_text("gonna test it", {"gonna": "going to"}), "going to test it"
        )

    def test_replacement_is_word_bounded(self):
        # Must not turn "begonnat" or similar into mush.
        self.assertEqual(clean_text("begonnat", {"gonna": "going to"}), "begonnat")

    def test_empty_input(self):
        self.assertEqual(clean_text(""), "")
        self.assertEqual(clean_text(None), "")


class TestConfig(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = Config(hotkey="scroll_lock", model="small.en")
            cfg.save(path)
            self.assertEqual(Config.load(path).hotkey, "scroll_lock")
            self.assertEqual(Config.load(path).model, "small.en")

    def test_missing_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.load(Path(tmp) / "nope.json")
            self.assertEqual(cfg.hotkey, "f9")

    def test_unknown_keys_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"hotkey": "f8", "from_the_future": 1}))
            self.assertEqual(Config.load(path).hotkey, "f8")

    def test_validate_catches_bad_values(self):
        self.assertTrue(Config(hotkey_mode="wobble").validate())
        self.assertTrue(Config(inject_method="telepathy").validate())
        self.assertTrue(Config(beam_size=0).validate())
        self.assertFalse(Config().validate())


class TestAudioHelpers(unittest.TestCase):
    def test_duration(self):
        self.assertAlmostEqual(duration_seconds(np.zeros(16000), 16000), 1.0)
        self.assertEqual(duration_seconds(np.zeros(0), 16000), 0.0)

    def test_peak(self):
        self.assertAlmostEqual(peak_level(np.array([0.0, -0.7, 0.2], dtype=np.float32)), 0.7)
        self.assertEqual(peak_level(np.zeros(0)), 0.0)


class TestEngine(unittest.TestCase):
    def _engine(self, audio, text="hello world", **kw):
        self.injector = NullInjector()
        self.transcriber = FakeTranscriber(text)
        engine = DictationEngine(
            recorder=FakeRecorder(audio),
            transcriber=self.transcriber,
            injector=self.injector,
            on_state=lambda s, d: self.states.append((s, d)),
            **kw,
        )
        self.states = []
        engine.on_state = lambda s, d: self.states.append((s, d))
        engine.start()
        return engine

    def test_happy_path_injects_with_trailing_space(self):
        engine = self._engine(speech(2.0))
        engine.begin_recording()
        engine.end_recording()
        self.assertTrue(engine.wait_idle(5))
        engine.shutdown()
        self.assertEqual(self.injector.injected, ["hello world "])
        self.assertEqual(engine.stats.utterances, 1)
        self.assertEqual(engine.stats.errors, 0)

    def test_short_press_is_dropped(self):
        engine = self._engine(speech(0.1))
        engine.begin_recording()
        engine.end_recording()
        engine.shutdown()
        self.assertEqual(self.injector.injected, [])
        self.assertEqual(engine.stats.dropped_short, 1)
        self.assertEqual(self.transcriber.calls, 0)

    def test_silent_recording_is_dropped_before_the_model_runs(self):
        engine = self._engine(np.zeros(32000, dtype=np.float32))
        engine.begin_recording()
        engine.end_recording()
        engine.shutdown()
        self.assertEqual(self.injector.injected, [])
        self.assertEqual(engine.stats.dropped_silent, 1)
        self.assertEqual(self.transcriber.calls, 0, "must not burn CPU on silence")

    def test_hallucinated_silence_is_not_injected(self):
        engine = self._engine(speech(2.0), text="Thank you.")
        engine.begin_recording()
        engine.end_recording()
        self.assertTrue(engine.wait_idle(5))
        engine.shutdown()
        self.assertEqual(self.injector.injected, [])
        self.assertEqual(engine.stats.dropped_empty, 1)

    def test_double_start_is_idempotent(self):
        engine = self._engine(speech(2.0))
        engine.begin_recording()
        engine.begin_recording()
        self.assertIs(engine.state, State.RECORDING)
        engine.end_recording()
        # A second stop with nothing recording must not enqueue a phantom job.
        engine.end_recording()
        self.assertTrue(engine.wait_idle(5))
        engine.shutdown()
        self.assertEqual(len(self.injector.injected), 1)

    def test_microphone_failure_reports_error(self):
        injector = NullInjector()
        engine = DictationEngine(
            recorder=FakeRecorder(speech(1.0), fail=True),
            transcriber=FakeTranscriber(),
            injector=injector,
            on_state=lambda s, d: None,
        )
        engine.start()
        engine.begin_recording()
        engine.shutdown()
        self.assertIs(engine.state, State.ERROR)
        self.assertEqual(engine.stats.errors, 1)
        self.assertEqual(injector.injected, [])

    def test_order_is_preserved_across_queued_utterances(self):
        injector = NullInjector()

        class Counting:
            def __init__(self):
                self.n = 0

            def load(self):
                pass

            def transcribe(self, audio):
                self.n += 1
                return f"utterance {self.n}"

        engine = DictationEngine(
            recorder=FakeRecorder(speech(1.0)),
            transcriber=Counting(),
            injector=injector,
        )
        engine.start()
        for _ in range(3):
            engine.begin_recording()
            engine.end_recording()
        self.assertTrue(engine.wait_idle(10))
        engine.shutdown()
        self.assertEqual(
            injector.injected, ["utterance 1 ", "utterance 2 ", "utterance 3 "]
        )

    def test_trailing_space_can_be_disabled(self):
        engine = self._engine(speech(2.0), trailing_space=False)
        engine.begin_recording()
        engine.end_recording()
        self.assertTrue(engine.wait_idle(5))
        engine.shutdown()
        self.assertEqual(self.injector.injected, ["hello world"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Text shaping, spoken commands, and changing the shortcut at runtime."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whispa.audio import speech_spans
from whispa.app import DictationEngine
from whispa.config import Config
from whispa.format import (
    PARA_MARK,
    Segment,
    apply_voice_commands,
    command_table,
    format_text,
    join_segments,
    make_formatter,
)
from whispa.hotkey import (
    GlobalHotkey,
    PRESETS,
    parse_hotkey,
    preset_label,
    suppress_start_menu,
)
from whispa.inject import NullInjector
from whispa.transcribe import WhisperTranscriber, clean_text


class FakeRecorder:
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


class FakeTranscriber:
    def __init__(self, text: str) -> None:
        self.text = text

    def load(self) -> None:
        pass

    def transcribe(self, audio) -> str:
        return self.text


class TestPauseParagraphs(unittest.TestCase):
    def test_long_gap_becomes_a_paragraph(self):
        text = join_segments(
            [Segment("First thought.", 0.0, 2.0), Segment("Second thought.", 3.5, 5.0)],
            pause_seconds=1.0,
        )
        self.assertEqual(text, "First thought." + PARA_MARK + "Second thought.")

    def test_short_gap_stays_in_one_paragraph(self):
        text = join_segments(
            [Segment("First.", 0.0, 2.0), Segment("Second.", 2.3, 4.0)],
            pause_seconds=1.0,
        )
        self.assertEqual(text, "First. Second.")

    def test_overlapping_timestamps_are_not_a_pause(self):
        # faster-whisper's VAD can hand back a segment starting fractionally
        # before the previous one ended.
        text = join_segments(
            [Segment("First.", 0.0, 2.0), Segment("Second.", 1.9, 4.0)], 1.0
        )
        self.assertEqual(text, "First. Second.")

    def test_pause_paragraphs_can_be_disabled(self):
        text = join_segments(
            [Segment("First.", 0.0, 2.0), Segment("Second.", 9.0, 10.0)], 0.0
        )
        self.assertEqual(text, "First. Second.")

    def test_empty_segments_are_skipped(self):
        text = join_segments([Segment("", 0.0, 1.0), Segment("Words.", 1.0, 2.0)], 1.0)
        self.assertEqual(text, "Words.")

    def test_marks_survive_clean_text(self):
        # clean_text collapses whitespace, which is exactly why the marks are
        # not whitespace.
        raw = "First." + PARA_MARK + "  Second."
        self.assertIn(PARA_MARK, clean_text(raw))


class FakeWord:
    """Stands in for a faster-whisper word timing."""

    def __init__(self, word: str, start: float, end: float) -> None:
        self.word = word
        self.start = start
        self.end = end


class TestPauseDetection(unittest.TestCase):
    SR = 16000

    def _speech(self, seconds: float) -> np.ndarray:
        rng = np.random.default_rng(0)
        return rng.uniform(-0.5, 0.5, int(seconds * self.SR)).astype(np.float32)

    def _silence(self, seconds: float) -> np.ndarray:
        return np.zeros(int(seconds * self.SR), dtype=np.float32)

    def test_a_long_silence_splits_the_recording(self):
        audio = np.concatenate(
            [self._speech(1.0), self._silence(2.5), self._speech(1.0)]
        )
        spans = speech_spans(audio, self.SR, min_silence=2.0)
        self.assertEqual(len(spans), 2)
        gap = (spans[1][0] - spans[0][1]) / self.SR
        self.assertGreater(gap, 2.0)

    def test_a_short_pause_does_not(self):
        audio = np.concatenate(
            [self._speech(1.0), self._silence(1.2), self._speech(1.0)]
        )
        self.assertEqual(len(speech_spans(audio, self.SR, min_silence=2.0)), 1)

    def test_silence_alone_has_no_speech(self):
        self.assertEqual(speech_spans(self._silence(3.0), self.SR), [])

    def test_quiet_speech_is_still_speech(self):
        # The threshold is relative to the recording's own peak, so a quiet
        # microphone is not mistaken for a room with nobody in it.
        audio = np.concatenate(
            [self._speech(1.0), self._silence(2.5), self._speech(1.0)]
        ) * 0.05
        self.assertEqual(len(speech_spans(audio, self.SR, min_silence=2.0)), 2)


class TestSplitAtPauses(unittest.TestCase):
    """Whisper stretches a word over the silence; the split has to cope."""

    def test_break_lands_before_the_stretched_word(self):
        words = [
            FakeWord(" what", 4.14, 5.48),
            # "your" is held across the 5.5-8.0s silence by the aligner.
            FakeWord(" your", 5.48, 8.20),
            FakeWord(" country", 8.20, 8.64),
        ]
        segments = WhisperTranscriber._split_at_pauses(words, [(5.5, 8.0)])
        self.assertEqual([s.text for s in segments], ["what", "your country"])

    def test_reported_bounds_are_the_real_pause(self):
        words = [FakeWord(" one", 0.0, 1.0), FakeWord(" two", 1.0, 4.5)]
        segments = WhisperTranscriber._split_at_pauses(words, [(1.2, 4.0)])
        self.assertAlmostEqual(segments[0].end, 1.2)
        self.assertAlmostEqual(segments[1].start, 4.0)
        # ... which is what makes join_segments mark the paragraph.
        self.assertIn(PARA_MARK, join_segments(segments, pause_seconds=2.0))

    def test_two_pauses_give_three_paragraphs(self):
        words = [
            FakeWord(" one", 0.0, 1.0),
            FakeWord(" two", 3.5, 4.0),
            FakeWord(" three", 7.0, 7.5),
        ]
        segments = WhisperTranscriber._split_at_pauses(
            words, [(1.0, 3.5), (4.0, 7.0)]
        )
        self.assertEqual([s.text for s in segments], ["one", "two", "three"])

    def test_a_pause_before_the_first_word_is_not_an_empty_paragraph(self):
        words = [FakeWord(" hello", 3.0, 3.5)]
        segments = WhisperTranscriber._split_at_pauses(words, [(0.0, 2.8)])
        self.assertEqual([s.text for s in segments], ["hello"])


class TestVoiceCommands(unittest.TestCase):
    def test_new_paragraph(self):
        self.assertEqual(
            format_text("Ship on Friday. New paragraph. Tell the team."),
            "Ship on Friday.\n\nTell the team.",
        )

    def test_bullets_and_numbers(self):
        out = format_text(
            "Shopping. Bullet point milk. Bullet point bread. "
            "New paragraph. Numbered point wake up. Numbered point leave."
        )
        self.assertEqual(
            out,
            "Shopping.\n- Milk.\n- Bread.\n\n1. Wake up.\n2. Leave.",
        )

    def test_numbering_restarts_after_prose(self):
        out = format_text(
            "Numbered point one. Numbered point two. New paragraph. "
            "Then later. Numbered point one again."
        )
        self.assertEqual(
            out, "1. One.\n2. Two.\n\nThen later.\n1. One again."
        )

    def test_command_words_in_prose_are_left_alone(self):
        # The whole point of the boundary rule: these are sentences, not
        # instructions.
        for sentence in (
            "We opened a new line of business this year.",
            "He made the next point rather well.",
        ):
            self.assertEqual(format_text(sentence), sentence)

    def test_commands_can_be_turned_off(self):
        self.assertEqual(
            format_text("Done. New paragraph. Next.", use_voice_commands=False),
            "Done. New paragraph. Next.",
        )

    def test_paragraph_style_off_keeps_one_line(self):
        out = format_text(
            "One. New paragraph. Two.", paragraph_style="off"
        )
        self.assertEqual(out, "One. Two.")

    def test_paragraph_style_single(self):
        out = format_text("One. New paragraph. Two.", paragraph_style="single")
        self.assertEqual(out, "One.\nTwo.")

    def test_extra_command_from_config(self):
        table = command_table({"full stop": "."})
        out = apply_voice_commands("three, full stop, four", table)
        self.assertEqual(format_text(out, use_voice_commands=False), "Three. Four")

    def test_a_default_command_can_be_removed(self):
        table = command_table({"next point": ""})
        self.assertNotIn("next point", table)
        self.assertIn("new paragraph", table)

    def test_custom_bullet_prefix(self):
        out = format_text("Bullet point milk.", bullet="* ")
        self.assertEqual(out, "* Milk.")

    def test_capitalisation_can_be_turned_off(self):
        self.assertEqual(
            format_text("bullet point milk.", capitalise=False), "- milk."
        )

    def test_formatter_from_config(self):
        cfg = Config(paragraph_style="single", voice_command_extras={"full stop": "."})
        out = make_formatter(cfg)("one. new paragraph. two, full stop, three")
        self.assertEqual(out, "One.\nTwo. Three")


class TestEngineFormatting(unittest.TestCase):
    def _engine(self, text: str, **cfg_kwargs) -> tuple[DictationEngine, NullInjector]:
        injector = NullInjector()
        cfg = Config(**cfg_kwargs)
        engine = DictationEngine(
            recorder=FakeRecorder(np.ones(16000, dtype=np.float32) * 0.2),
            transcriber=FakeTranscriber(text),
            injector=injector,
            formatter=make_formatter(cfg),
        )
        return engine, injector

    def _run(self, engine) -> None:
        engine.start()
        engine.begin_recording()
        engine.end_recording()
        engine.wait_idle(timeout=5)
        engine.shutdown()

    def test_paragraphs_reach_the_injector(self):
        engine, injector = self._engine("One thought." + PARA_MARK + "Another.")
        self._run(engine)
        self.assertEqual(injector.injected, ["One thought.\n\nAnother. "])

    def test_marks_never_reach_the_injector_without_a_formatter(self):
        injector = NullInjector()
        engine = DictationEngine(
            recorder=FakeRecorder(np.ones(16000, dtype=np.float32) * 0.2),
            transcriber=FakeTranscriber("One." + PARA_MARK + "Two."),
            injector=injector,
        )
        self._run(engine)
        self.assertEqual(injector.injected, ["One. Two. "])

    def test_spoken_command_reaches_the_injector(self):
        engine, injector = self._engine("Milk. Bullet point bread.")
        self._run(engine)
        self.assertEqual(injector.injected, ["Milk.\n- Bread. "])


class TestStartMenuSuppression(unittest.TestCase):
    CTRL_WIN = parse_hotkey("<ctrl>+<cmd>")

    def test_win_is_hidden_once_the_rest_of_the_chord_is_held(self):
        suppress, swallowed = suppress_start_menu(
            self.CTRL_WIN, {"ctrl"}, 91, True, False
        )
        self.assertTrue(suppress)
        self.assertTrue(swallowed)

    def test_win_alone_still_reaches_windows(self):
        suppress, swallowed = suppress_start_menu(self.CTRL_WIN, set(), 91, True, False)
        self.assertFalse(suppress)
        self.assertFalse(swallowed)

    def test_the_release_of_a_hidden_press_is_hidden_too(self):
        suppress, swallowed = suppress_start_menu(
            self.CTRL_WIN, {"ctrl", "cmd"}, 92, False, True
        )
        self.assertTrue(suppress)
        # ... and only once, so Windows is never left thinking Win is down.
        self.assertFalse(swallowed)

    def test_a_release_we_did_not_hide_is_left_alone(self):
        suppress, _ = suppress_start_menu(self.CTRL_WIN, {"ctrl"}, 91, False, False)
        self.assertFalse(suppress)

    def test_other_keys_are_never_touched(self):
        suppress, _ = suppress_start_menu(self.CTRL_WIN, {"ctrl"}, 65, True, False)
        self.assertFalse(suppress)

    def test_hotkeys_without_win_are_never_touched(self):
        suppress, _ = suppress_start_menu(parse_hotkey("f9"), {"ctrl"}, 91, True, False)
        self.assertFalse(suppress)


class TestPresets(unittest.TestCase):
    def test_every_preset_parses(self):
        for _label, spec in PRESETS:
            self.assertTrue(parse_hotkey(spec))

    def test_ctrl_win_is_offered(self):
        self.assertIn(parse_hotkey("<ctrl>+<cmd>"), {parse_hotkey(s) for _l, s in PRESETS})

    def test_label_is_order_independent(self):
        self.assertEqual(preset_label("<cmd>+<ctrl>"), "Ctrl + Win (Start)")

    def test_unknown_spec_labels_as_itself(self):
        self.assertEqual(preset_label("f7"), "f7")


class TestRebind(unittest.TestCase):
    def _hotkey(self, spec: str) -> GlobalHotkey:
        return GlobalHotkey(
            spec=spec, mode="hybrid", on_start=lambda: None, on_stop=lambda: None
        )

    def test_rebinding_changes_what_matches(self):
        hk = self._hotkey("f9")
        self.assertEqual(hk.matcher.press("f9"), "start")
        hk.rebind("<ctrl>+<cmd>")
        self.assertEqual(hk.spec, "<ctrl>+<cmd>")
        self.assertIsNone(hk.matcher.press("f9"))
        self.assertIsNone(hk.matcher.press("ctrl"))
        self.assertEqual(hk.matcher.press("cmd"), "start")

    def test_rebinding_keeps_the_mode_and_tap_threshold(self):
        hk = GlobalHotkey(
            spec="f9",
            mode="toggle",
            on_start=lambda: None,
            on_stop=lambda: None,
            tap_seconds=0.5,
        )
        hk.rebind("f8")
        self.assertEqual(hk.matcher.mode, "toggle")
        self.assertEqual(hk.matcher.tap_seconds, 0.5)

    def test_a_bad_spec_leaves_the_old_shortcut_working(self):
        hk = self._hotkey("f9")
        with self.assertRaises(ValueError):
            hk.rebind("")
        self.assertEqual(hk.spec, "f9")
        self.assertEqual(hk.matcher.press("f9"), "start")


if __name__ == "__main__":
    unittest.main()

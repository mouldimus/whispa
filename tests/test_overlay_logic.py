"""Tests for the indicator's decision logic.

The window itself needs a display and cannot be exercised here, but what it
decides to show can be, and that is where the behaviour the user asked for
lives: on / hearing-me / thinking.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whispa.app import State
from whispa.overlay import SIGNAL_FLOOR, bar_heights, should_be_visible


class TestVisibility(unittest.TestCase):
    NOW = 1000.0

    def visible(self, state, forced=False, always=False, flash=0.0):
        return should_be_visible(state, forced, always, flash, self.NOW)

    def test_hidden_when_idle(self):
        self.assertFalse(self.visible(State.IDLE))

    def test_shown_while_recording(self):
        self.assertTrue(self.visible(State.RECORDING))

    def test_shown_while_transcribing(self):
        self.assertTrue(self.visible(State.TRANSCRIBING))

    def test_shown_on_error(self):
        self.assertTrue(self.visible(State.ERROR))

    def test_pinned_when_always_visible(self):
        self.assertTrue(self.visible(State.IDLE, always=True))

    def test_forced_during_model_load(self):
        # Startup pins the pill so there is feedback with no console window.
        self.assertTrue(self.visible(State.IDLE, forced=True))

    def test_lingers_briefly_after_finishing(self):
        self.assertTrue(self.visible(State.IDLE, flash=self.NOW + 0.5))
        self.assertFalse(self.visible(State.IDLE, flash=self.NOW - 0.5))


class TestLevelMeterGeometry(unittest.TestCase):
    def test_silence_leaves_every_bar_at_the_floor(self):
        # This is the point of the meter: no signal must look like no signal.
        self.assertTrue(all(h == 2.0 for h in bar_heights(0.0, 0.0)))

    def test_speech_lifts_the_bars(self):
        heights = bar_heights(0.6, 0.0)
        self.assertTrue(any(h > 4.0 for h in heights))

    def test_louder_input_gives_taller_bars(self):
        quiet = sum(bar_heights(0.2, 0.0))
        loud = sum(bar_heights(0.9, 0.0))
        self.assertGreater(loud, quiet)

    def test_centre_bars_move_more_than_edges(self):
        heights = bar_heights(1.0, 0.0)
        self.assertGreater(heights[len(heights) // 2], heights[0])

    def test_bar_count_is_stable(self):
        self.assertEqual(len(bar_heights(0.5, 0.0)), len(bar_heights(0.1, 3.3)))

    def test_out_of_range_levels_are_clamped(self):
        self.assertTrue(all(h == 2.0 for h in bar_heights(-5.0, 0.0)))
        top = bar_heights(50.0, 0.0)
        self.assertTrue(all(h <= 16.0 for h in top))

    def test_signal_floor_is_low_enough_for_quiet_speech(self):
        # A quiet but real voice must not be reported as "no signal".
        self.assertLess(SIGNAL_FLOOR, 0.1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

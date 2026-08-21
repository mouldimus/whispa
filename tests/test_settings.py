"""Tests for the two tray settings: the debug console and Windows autostart."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whispa.autostart import (
    VALUE_NAME,
    AutostartManager,
    MemoryRegistry,
    build_launch_command,
)
from whispa.console import RingBufferHandler


class TestLaunchCommand(unittest.TestCase):
    def test_prefers_the_vbs_launcher(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "whispa-silent.vbs").write_text("' launcher")
            cmd = build_launch_command(root, Path("C:/py/pythonw.exe"))
            self.assertIn("wscript.exe", cmd)
            self.assertIn("whispa-silent.vbs", cmd)
            # Quoted, because Windows paths contain spaces.
            self.assertIn('"', cmd)

    def test_falls_back_to_an_explicit_path_injection(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = build_launch_command(root, Path("C:/py/pythonw.exe"))
            # "-m whispa" alone would fail: a Run entry sets no working
            # directory, so the package folder must be put on the path.
            self.assertIn("sys.path.insert", cmd)
            self.assertIn(str(root), cmd)

    def test_raises_when_nothing_can_launch_it(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                build_launch_command(Path(tmp), None)


class TestAutostartManager(unittest.TestCase):
    def _manager(self, initial=None):
        return AutostartManager(
            backend=MemoryRegistry(initial), command="wscript.exe whispa.vbs"
        )

    def test_off_by_default(self):
        self.assertFalse(self._manager().is_enabled())

    def test_enable_then_disable(self):
        m = self._manager()
        self.assertTrue(m.enable())
        self.assertTrue(m.is_enabled())
        self.assertEqual(m.backend.values[VALUE_NAME], "wscript.exe whispa.vbs")
        self.assertTrue(m.disable())
        self.assertFalse(m.is_enabled())

    def test_toggle_returns_the_resulting_state(self):
        m = self._manager()
        self.assertTrue(m.toggle())
        self.assertTrue(m.is_enabled())
        self.assertFalse(m.toggle())
        self.assertFalse(m.is_enabled())

    def test_disable_is_idempotent(self):
        m = self._manager()
        self.assertTrue(m.disable())
        self.assertFalse(m.is_enabled())

    def test_enable_twice_leaves_one_entry(self):
        m = self._manager()
        m.enable()
        m.enable()
        self.assertEqual(len(m.backend.values), 1)

    def test_detects_a_stale_entry_after_the_folder_moves(self):
        m = self._manager({VALUE_NAME: "wscript.exe C:/old/location/whispa.vbs"})
        self.assertTrue(m.is_enabled())
        self.assertTrue(m.is_stale())

    def test_repair_rewrites_a_stale_entry(self):
        m = self._manager({VALUE_NAME: "wscript.exe C:/old/whispa.vbs"})
        self.assertTrue(m.repair_if_stale())
        self.assertEqual(m.backend.values[VALUE_NAME], "wscript.exe whispa.vbs")
        self.assertFalse(m.is_stale())

    def test_repair_does_nothing_when_correct(self):
        m = self._manager()
        m.enable()
        self.assertFalse(m.repair_if_stale())

    def test_repair_does_nothing_when_disabled(self):
        self.assertFalse(self._manager().repair_if_stale())

    def test_toggle_repairs_rather_than_disabling_a_stale_entry(self):
        # Turning the switch "off" when it is pointing at an old folder would
        # look like the setting was broken. Re-point it instead.
        m = self._manager({VALUE_NAME: "wscript.exe C:/old/whispa.vbs"})
        self.assertTrue(m.toggle())
        self.assertEqual(m.backend.values[VALUE_NAME], "wscript.exe whispa.vbs")

    def test_write_failure_is_reported_not_raised(self):
        class Failing(MemoryRegistry):
            def write(self, name, value):
                raise OSError("access denied")

        m = AutostartManager(backend=Failing(), command="x")
        self.assertFalse(m.enable())


class TestRingBufferHandler(unittest.TestCase):
    def _logger(self, capacity=100):
        buf = RingBufferHandler(capacity=capacity)
        buf.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        logger = logging.getLogger(f"test.ring.{id(buf)}")
        logger.handlers = [buf]
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        return logger, buf

    def test_captures_history_before_the_window_opens(self):
        # The whole point: the console must show what already happened.
        logger, buf = self._logger()
        logger.info("something happened earlier")
        self.assertIn("something happened earlier", "\n".join(buf.snapshot()))

    def test_since_returns_only_new_lines(self):
        logger, buf = self._logger()
        logger.info("first")
        lines, seq = buf.since(0)
        self.assertEqual(len(lines), 1)
        logger.info("second")
        lines2, seq2 = buf.since(seq)
        self.assertEqual(len(lines2), 1)
        self.assertIn("second", lines2[0])
        self.assertGreater(seq2, seq)

    def test_no_new_lines_returns_empty(self):
        logger, buf = self._logger()
        logger.info("only")
        _lines, seq = buf.since(0)
        self.assertEqual(buf.since(seq)[0], [])

    def test_respects_capacity(self):
        logger, buf = self._logger(capacity=5)
        for i in range(20):
            logger.info("line %d", i)
        self.assertEqual(len(buf.snapshot()), 5)
        self.assertIn("line 19", buf.snapshot()[-1])

    def test_reader_that_fell_behind_does_not_repeat_lines(self):
        # Sequence numbers, not indices: eviction must not resend old lines.
        logger, buf = self._logger(capacity=5)
        logger.info("line 0")
        _lines, seq = buf.since(0)
        for i in range(1, 20):
            logger.info("line %d", i)
        newer, _ = buf.since(seq)
        self.assertNotIn("line 0", "\n".join(newer))
        self.assertIn("line 19", "\n".join(newer))

    def test_clear_empties_the_buffer(self):
        logger, buf = self._logger()
        logger.info("gone")
        buf.clear()
        self.assertEqual(buf.snapshot(), [])

    def test_levels_are_preserved_for_colouring(self):
        logger, buf = self._logger()
        logger.error("broken")
        self.assertIn("ERROR", buf.snapshot()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

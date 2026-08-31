"""Tests for the git-based auto-updater (whispa/update.py).

No real git process and no network is involved: CommandRunner is a plain
callable, so a fake one stands in exactly like MemoryRegistry does for the
autostart tests.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whispa.update import AutoUpdater, CommandResult, is_clean, parse_behind_count


class FakeRunner:
    """Canned answers keyed by the git subcommand (args[1]), plus a call log.

    `on_pull` lets a test mutate the working tree (e.g. rewrite
    requirements.txt) as a side effect of a successful `git pull`, the way a
    real pull would.
    """

    def __init__(self, responses: dict[str, CommandResult], on_pull=None) -> None:
        self.responses = responses
        self.on_pull = on_pull
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd, timeout):
        self.calls.append(args)
        if args[:2] == ["git", "pull"] and self.on_pull is not None:
            self.on_pull()
        key = args[1] if args and args[0] == "git" else args[0]
        return self.responses.get(key, CommandResult(0, "", ""))


OK = lambda out="": CommandResult(0, out, "")
FAIL = lambda err="failed": CommandResult(1, "", err)


class TestPureHelpers(unittest.TestCase):
    def test_parse_behind_count(self):
        self.assertEqual(parse_behind_count("3\n"), 3)
        self.assertEqual(parse_behind_count("0"), 0)
        self.assertIsNone(parse_behind_count(""))
        self.assertIsNone(parse_behind_count("fatal: not a repo"))

    def test_is_clean(self):
        self.assertTrue(is_clean(""))
        self.assertTrue(is_clean("   \n"))
        self.assertFalse(is_clean(" M whispa/config.py\n"))


class TestAutoUpdater(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make(self, responses, on_pull=None):
        runner = FakeRunner(responses, on_pull=on_pull)
        return AutoUpdater(root=self.root, run=runner), runner

    def test_not_a_git_checkout_short_circuits(self):
        updater, runner = self._make({})
        self.assertFalse(updater.is_git_checkout())
        self.assertFalse(updater.check_and_apply())
        self.assertEqual(runner.calls, [])  # never even asked git anything

    def _make_git_dir(self):
        (self.root / ".git").mkdir()

    def test_dirty_working_tree_skips_update(self):
        self._make_git_dir()
        updater, runner = self._make(
            {
                "rev-parse": OK("master\n"),
                "status": OK(" M whispa/app.py\n"),
            }
        )
        self.assertFalse(updater.check_and_apply())
        # Never reaches fetch once the tree is known to be dirty.
        self.assertTrue(all(c[1] != "fetch" for c in runner.calls))

    def test_offline_fetch_failure_is_silent(self):
        self._make_git_dir()
        updater, runner = self._make(
            {
                "rev-parse": OK("master\n"),
                "status": OK(""),
                "fetch": FAIL("could not resolve host"),
            }
        )
        self.assertFalse(updater.check_and_apply())

    def test_up_to_date_does_nothing(self):
        self._make_git_dir()
        updater, runner = self._make(
            {
                "rev-parse": OK("master\n"),
                "status": OK(""),
                "fetch": OK(),
                "rev-list": OK("0\n"),
            }
        )
        self.assertFalse(updater.check_and_apply())
        self.assertTrue(all(c[:2] != ["git", "pull"] for c in runner.calls))

    def test_pull_failure_reports_no_update(self):
        self._make_git_dir()
        updater, runner = self._make(
            {
                "rev-parse": OK("master\n"),
                "status": OK(""),
                "fetch": OK(),
                "rev-list": OK("2\n"),
                "pull": FAIL("not fast-forward"),
            }
        )
        self.assertFalse(updater.check_and_apply())

    def test_successful_pull_with_unchanged_requirements_skips_pip(self):
        self._make_git_dir()
        (self.root / "requirements.txt").write_text("faster-whisper==1.0\n")
        updater, runner = self._make(
            {
                "rev-parse": OK("master\n"),
                "status": OK(""),
                "fetch": OK(),
                "rev-list": OK("1\n"),
                "pull": OK(),
            }
        )
        self.assertTrue(updater.check_and_apply())
        self.assertTrue(all("pip" not in c[0] for c in runner.calls if c))

    def test_changed_requirements_triggers_dependency_sync(self):
        self._make_git_dir()
        req = self.root / "requirements.txt"
        req.write_text("faster-whisper==1.0\n")

        def bump_requirements():
            req.write_text("faster-whisper==1.1\n")

        updater, runner = self._make(
            {
                "rev-parse": OK("master\n"),
                "status": OK(""),
                "fetch": OK(),
                "rev-list": OK("1\n"),
                "pull": OK(),
            },
            on_pull=bump_requirements,
        )
        self.assertTrue(updater.check_and_apply())
        pip_calls = [c for c in runner.calls if "pip" in c]
        self.assertEqual(len(pip_calls), 1)
        self.assertIn("-r", pip_calls[0])


if __name__ == "__main__":
    unittest.main()

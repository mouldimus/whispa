"""Tests for learning from edits, and for locating the edited text."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whispa.learn import CorrectionLearner, extract_corrections, tokenize
from whispa.observe import CorrectionWatcher, NullObserver, locate_edited_span


class TestExtractCorrections(unittest.TestCase):
    def test_single_word_fix(self):
        pairs = extract_corrections("send it to Jon", "send it to John")
        self.assertEqual(pairs, [("Jon", "John")])

    def test_case_fix_is_learnable(self):
        pairs = extract_corrections("deploy to kubernetes", "deploy to Kubernetes")
        self.assertEqual(pairs, [("kubernetes", "Kubernetes")])

    def test_multiple_fixes_in_one_edit(self):
        pairs = extract_corrections(
            "the pod hit an oom in reddis", "the pod hit an OOM in Redis"
        )
        self.assertIn(("oom", "OOM"), pairs)
        self.assertIn(("reddis", "Redis"), pairs)

    def test_identical_text_teaches_nothing(self):
        self.assertEqual(extract_corrections("same text", "same text"), [])

    def test_rewrite_is_rejected(self):
        # Completely different sentence: the user changed their mind, which
        # says nothing about what was misheard.
        pairs = extract_corrections(
            "ask not what your country can do for you",
            "please book a table for four at seven",
        )
        self.assertEqual(pairs, [])

    def test_pure_insertion_is_not_a_correction(self):
        pairs = extract_corrections("deploy the service", "deploy the service now")
        self.assertEqual(pairs, [])

    def test_pure_deletion_is_not_a_correction(self):
        pairs = extract_corrections("deploy the service now", "deploy the service")
        self.assertEqual(pairs, [])

    def test_long_phrase_swap_is_rejected(self):
        pairs = extract_corrections(
            "one two three four five six", "one alpha beta gamma delta six"
        )
        self.assertEqual(pairs, [])

    def test_empty_inputs(self):
        self.assertEqual(extract_corrections("", "something"), [])
        self.assertEqual(extract_corrections("something", ""), [])
        self.assertEqual(extract_corrections(None, None), [])

    def test_tokenizer_keeps_contractions(self):
        self.assertIn("don't", tokenize("don't stop"))


class TestCorrectionLearner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "learned.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_correction_is_not_yet_applied(self):
        learner = CorrectionLearner(self.path, min_count=2)
        learner.observe("call Jon", "call John")
        # A single edit could be a change of mind, not a mishearing.
        self.assertEqual(learner.replacements(), {})

    def test_repeated_correction_becomes_active(self):
        learner = CorrectionLearner(self.path, min_count=2)
        learner.observe("call Jon", "call John")
        learner.observe("email Jon", "email John")
        self.assertEqual(learner.replacements(), {"jon": "John"})

    def test_persists_across_restart(self):
        learner = CorrectionLearner(self.path, min_count=2)
        learner.observe("call Jon", "call John")
        learner.observe("email Jon", "email John")
        reloaded = CorrectionLearner(self.path, min_count=2)
        self.assertEqual(reloaded.replacements(), {"jon": "John"})

    def test_manual_correction_is_trusted_immediately(self):
        learner = CorrectionLearner(self.path, min_count=3)
        learner.record_pair("reddis", "Redis")
        self.assertEqual(learner.replacements(), {"reddis": "Redis"})

    def test_most_confirmed_fix_wins_a_conflict(self):
        learner = CorrectionLearner(self.path, min_count=2)
        for _ in range(2):
            learner.observe("meet Jon", "meet John")
        for _ in range(4):
            learner.observe("meet Jon", "meet Jonathan")
        self.assertEqual(learner.replacements()["jon"], "Jonathan")

    def test_vocabulary_and_prompt_bias(self):
        learner = CorrectionLearner(self.path, min_count=2)
        for _ in range(2):
            learner.observe("deploy to reddis", "deploy to Redis")
        self.assertIn("Redis", learner.vocabulary())
        self.assertIn("Redis", learner.prompt_bias())
        self.assertTrue(learner.prompt_bias("Existing prompt.").startswith("Existing"))

    def test_prompt_bias_without_data_returns_base(self):
        learner = CorrectionLearner(self.path, min_count=2)
        self.assertEqual(learner.prompt_bias("base text"), "base text")
        self.assertEqual(learner.prompt_bias(), "")

    def test_stats(self):
        learner = CorrectionLearner(self.path, min_count=2)
        learner.observe("call Jon", "call John")
        self.assertEqual(learner.stats, {"tracked": 1, "active": 0})
        learner.observe("call Jon", "call John")
        self.assertEqual(learner.stats, {"tracked": 1, "active": 1})

    def test_corrupt_file_does_not_crash(self):
        self.path.write_text("{ this is not json")
        learner = CorrectionLearner(self.path, min_count=2)
        self.assertEqual(learner.replacements(), {})

    def test_saved_file_is_valid_json(self):
        learner = CorrectionLearner(self.path, min_count=1)
        learner.observe("call Jon", "call John")
        data = json.loads(self.path.read_text())
        self.assertEqual(data["version"], 1)
        self.assertTrue(data["corrections"])


class TestLocateEditedSpan(unittest.TestCase):
    def test_finds_corrected_text_between_anchors(self):
        before = "Meeting notes: call Jon tomorrow about the thing."
        after = "Meeting notes: call John tomorrow about the thing."
        self.assertEqual(
            locate_edited_span(before, after, "call Jon ").strip(), "call John"
        )

    def test_unchanged_text_returns_the_same_span(self):
        before = "prefix here dictated words suffix here"
        after = before
        self.assertEqual(
            locate_edited_span(before, after, "dictated words"), "dictated words"
        )

    def test_injection_at_start_of_document(self):
        before = "hello wurld"
        after = "hello world"
        self.assertIsNotNone(locate_edited_span(before, after, "hello wurld"))

    def test_missing_injection_gives_up(self):
        self.assertIsNone(
            locate_edited_span("nothing like it", "nothing like it", "absent text")
        )

    def test_unfindable_prefix_gives_up(self):
        before = "unique prefix here TARGET tail"
        after = "totally different document"
        self.assertIsNone(locate_edited_span(before, after, "TARGET"))

    def test_survives_edits_elsewhere_in_the_document(self):
        before = "Intro paragraph. call Jon tomorrow. Closing line."
        # The user also edited an unrelated part of the document.
        after = "A much longer intro paragraph. call John tomorrow. Closing line."
        span = locate_edited_span(before, after, "call Jon tomorrow.")
        self.assertIsNotNone(span)
        self.assertIn("John", span)


class TestCorrectionWatcher(unittest.TestCase):
    class FakeObserver:
        available = True

        def __init__(self, snapshots):
            self.snapshots = list(snapshots)

        def snapshot(self):
            return self.snapshots.pop(0) if self.snapshots else None

    def test_learns_from_a_read_back_edit(self):
        learned = []
        before = "notes: call Jon tomorrow."
        after = "notes: call John tomorrow."
        watcher = CorrectionWatcher(
            observer=self.FakeObserver([before, after]),
            on_correction=lambda a, b: learned.append((a, b)),
            delay=0.01,
        )
        watcher.note_injection("call Jon ")
        import time

        time.sleep(0.15)
        self.assertEqual(len(learned), 1)
        self.assertIn("John", learned[0][1])

    def test_no_edit_means_nothing_learnt(self):
        learned = []
        text = "notes: call Jon tomorrow."
        watcher = CorrectionWatcher(
            observer=self.FakeObserver([text, text]),
            on_correction=lambda a, b: learned.append((a, b)),
            delay=0.01,
        )
        watcher.note_injection("call Jon ")
        import time

        time.sleep(0.15)
        self.assertEqual(learned, [])

    def test_unavailable_observer_is_a_no_op(self):
        learned = []
        watcher = CorrectionWatcher(
            observer=NullObserver(),
            on_correction=lambda a, b: learned.append((a, b)),
            delay=0.01,
        )
        watcher.note_injection("anything")
        self.assertEqual(learned, [])
        self.assertFalse(watcher.available)
        # The last utterance is still tracked for the manual dialog.
        self.assertEqual(watcher.last_injected, "anything")


if __name__ == "__main__":
    unittest.main(verbosity=2)

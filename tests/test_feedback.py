"""Checks that marks on a digest go with one round of feedback and then clear."""

import tempfile
import unittest
from pathlib import Path

from research_agent.store import Store


class FeedbackRoundsTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "state.db")
        self.store.save_feedback("d", "p1", 1, "good")
        self.store.save_feedback("d", "p2", -1, "")
        self.store.save_feedback("d", "p3", 0, "")  # marked, then cleared

    def test_marks_are_pending_until_sent(self):
        pending = self.store.feedback_for_run("d")
        self.assertEqual({k: v["verdict"] for k, v in pending.items()}, {"p1": 1, "p2": -1, "p3": 0})

    def test_sending_a_round_clears_the_digest(self):
        rid = self.store.add_refinement("d", "", 1, 1)
        self.store.claim_feedback("d", rid)
        self.assertEqual(self.store.feedback_for_run("d"), {})
        sent = self.store.feedback_for_run("d", rid)
        self.assertEqual({k: v["verdict"] for k, v in sent.items()}, {"p1": 1, "p2": -1})
        self.assertEqual(sent["p1"]["note"], "good")

    def test_new_marks_after_a_round_are_pending_again(self):
        rid = self.store.add_refinement("d", "", 1, 1)
        self.store.claim_feedback("d", rid)
        self.store.save_feedback("d", "p1", -1, "changed my mind")
        self.store.save_feedback("d", "p4", 1, "")
        self.assertEqual(set(self.store.feedback_for_run("d")), {"p1", "p4"})
        self.assertEqual(set(self.store.feedback_for_run("d", rid)), {"p2"})

    def test_failed_round_gives_the_marks_back(self):
        rid = self.store.add_refinement("d", "", 1, 1)
        self.store.claim_feedback("d", rid)
        self.store.release_feedback(rid)
        self.assertEqual(set(self.store.feedback_for_run("d")), {"p1", "p2"})
        self.assertEqual(self.store.feedback_for_run("d", rid), {})


if __name__ == "__main__":
    unittest.main()

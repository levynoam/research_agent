"""Checks what deleting a digest removes and what it keeps."""

import tempfile
import unittest
from pathlib import Path

from research_agent.store import Store


def paper(pid):
    return {"id": pid, "version": "v1", "title": pid, "abstract": "", "authors": [],
            "categories": ["cs.LG"], "announced": "2026-11-05", "url": ""}


class DeleteRunTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "state.db")
        for run_id, pids in [("a", ["p1", "p2"]), ("b", ["p2", "p3"])]:
            self.store.save_run(run_id, status="ok", kind="daily", label=run_id,
                                last_announced="2026-11-0" + ("5" if run_id == "a" else "6"))
            for pid in pids:
                self.store.add_paper(paper(pid), run_id)
                self.store.save_json("research", pid, {"notes": pid})
        self.store.save_feedback("b", "p3", 1, "")
        self.store.add_cost("b", "triage", "m", 1, 1, 0.5)

    def test_removes_the_run_and_its_own_papers_only(self):
        self.assertEqual(self.store.delete_run("b"), ["p3"])
        self.assertIsNone(self.store.run("b"))
        self.assertEqual(self.store.known_ids(), {"p1", "p2"})
        self.assertEqual(self.store.load_json("research", "p2"), {"notes": "p2"})
        self.assertIsNone(self.store.load_json("research", "p3"))
        self.assertEqual(self.store.feedback_for_run("b"), {})

    def test_feed_position_moves_back_and_spend_is_kept(self):
        self.store.delete_run("b")
        self.assertEqual(self.store.last_announced(), "2026-11-05")
        self.assertEqual(self.store.spend_for_run("b"), 0.5)


if __name__ == "__main__":
    unittest.main()

"""Checks which cut-short runs the web app resumes, without starting any run."""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from research_agent import web
from research_agent.store import Store


class ResumeTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "status").mkdir()
        self.store = Store(tmp / "state.db")
        self.spawned = []
        for target, value in [("ROOT", tmp), ("STATUS_DIR", tmp / "status")]:
            patcher = mock.patch.object(web, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(web, "_spawn", lambda *a: self.spawned.append(a))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.online = mock.patch.object(web, "_online", return_value=True)
        self.online.start()
        self.addCleanup(self.online.stop)

    def add(self, run_id, days_ago=0, **fields):
        started = (date.today() - timedelta(days=days_ago)).isoformat() + "T07:00:00"
        self.store.save_run(run_id, started=started, kind="daily", label=run_id, **fields)

    def test_resumes_an_incomplete_run(self):
        self.add("a", status="ok", incomplete=0)
        self.add("b", status="ok", incomplete=1, resumes=0)
        self.assertEqual(web.resume_incomplete(), "b")
        self.assertEqual(self.spawned[0][3], ["--resume", "b"])
        self.assertEqual(self.store.run("b")["resumes"], 1)

    def test_resumes_a_run_whose_process_died(self):
        self.add("c", status="running")
        self.assertEqual(web.resume_incomplete(), "c")

    def test_leaves_old_and_retried_runs_alone(self):
        self.add("old", days_ago=5, status="ok", incomplete=1)
        self.add("tried", status="ok", incomplete=1, resumes=web.MAX_AUTO_RESUMES)
        self.assertIsNone(web.resume_incomplete())
        self.assertEqual(self.spawned, [])

    def test_waits_while_offline(self):
        self.add("b", status="ok", incomplete=1)
        with mock.patch.object(web, "_online", return_value=False):
            self.assertIsNone(web.resume_incomplete())
        self.assertEqual(self.store.run("b")["resumes"] or 0, 0)


if __name__ == "__main__":
    unittest.main()

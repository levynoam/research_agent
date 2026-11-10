"""Checks that a capacity failure stops a stage instead of leaving papers out, and that
a stopped run can be continued from the web app."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from research_agent import deepread as deepread_module
from research_agent import triage as triage_module
from research_agent import web
from research_agent.llm import ModelsUnavailable
from research_agent.progress import NullProgress
from research_agent.store import Store

CFG = {"models": {"triage": "m", "deepread": "m"},
       "triage": {"batch_size": 2, "workers": 2},
       "deepread": {"workers": 2, "min_score": 6, "max_chars": 1000}}


def paper(pid):
    return {"id": pid, "version": "v1", "title": pid, "abstract": "", "authors": [],
            "categories": ["cs.LG"], "announced": "2026-11-05", "url": ""}


class BusyLLM:
    def chat_json(self, *args, **kwargs):
        raise ModelsUnavailable("no model could take the call: opus is at capacity")


class StagesStopTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "state.db")
        self.papers = [paper(f"p{i}") for i in range(4)]
        for p in self.papers:
            self.store.add_paper(p, "r")

    def test_triage_stops(self):
        criteria = mock.Mock(text="", topics=[])
        with self.assertRaises(ModelsUnavailable):
            triage_module.triage(self.papers, criteria, CFG, BusyLLM(), self.store, "r",
                                 NullProgress())

    def test_full_read_stops(self):
        with mock.patch.object(deepread_module, "summarize",
                               side_effect=ModelsUnavailable("all busy")):
            with self.assertRaises(ModelsUnavailable):
                deepread_module.deepread(self.papers, None, None, CFG, None, self.store, "r",
                                         NullProgress())

    def test_other_trouble_with_one_paper_still_only_skips_it(self):
        with mock.patch.object(deepread_module, "summarize", side_effect=ValueError("bad JSON")):
            deepread_module.deepread(self.papers, None, None, CFG, None, self.store, "r",
                                     NullProgress())


class RetryTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "status").mkdir()
        self.store = Store(tmp / "state.db")
        self.spawned = []
        for target, value in [("ROOT", tmp), ("STATUS_DIR", tmp / "status"),
                              ("_spawn", lambda *a: self.spawned.append(a))]:
            patcher = mock.patch.object(web, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_continues_a_failed_run(self):
        self.store.save_run("r", started="2026-11-10T22:00:00", status="failed", kind="daily",
                            label="Tuesday", n_fetched=1)
        self.store.add_paper(paper("p1"), "r")
        self.assertEqual(web.resume_run("r"), {"run_id": "r"})
        self.assertEqual(self.spawned, [("r", "daily", "Tuesday", ["--resume", "r"])])
        row = next(x for x in web.runs() if x["id"] == "r")
        self.assertTrue(row["can_retry"])

    def test_a_run_without_papers_cannot_be_continued(self):
        self.store.save_run("r", started="2026-11-10T22:00:00", status="failed", kind="daily",
                            label="Tuesday")
        with self.assertRaises(HTTPException) as caught:
            web.resume_run("r")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.spawned, [])
        self.assertFalse(next(x for x in web.runs() if x["id"] == "r")["can_retry"])

    def test_a_finished_run_shows_no_retry(self):
        self.store.save_run("r", started="2026-11-10T22:00:00", status="ok", kind="daily",
                            label="Tuesday", n_fetched=5, incomplete=0)
        self.assertFalse(next(x for x in web.runs() if x["id"] == "r")["can_retry"])


if __name__ == "__main__":
    unittest.main()

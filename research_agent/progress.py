"""Live progress of a run, written to logs/status/<run id>.json for the web app to show."""

import json
import os
import threading
import time
from pathlib import Path

from research_agent.config import ROOT

STATUS_DIR = ROOT / "logs" / "status"

STAGES = {  # stage key -> label shown in the web app, in pipeline order
    "queued": "Waiting for another run to finish",
    "fetch": "Fetching new papers from arXiv",
    "triage": "Screening abstracts",
    "deepread": "Reading full papers",
    "enrich": "Looking up authors and similar papers",
    "research": "Researching top papers on the web",
    "figures": "Choosing and generating images",
    "digest": "Writing the digest",
    "done": "Done",
}


def status_path(run_id: str) -> Path:
    return STATUS_DIR / f"{run_id}.json"


def read_status(run_id: str) -> dict | None:
    try:
        return json.loads(status_path(run_id).read_text())
    except (OSError, ValueError):
        return None


def write_status(run_id: str, data: dict):
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = status_path(run_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(status_path(run_id))  # atomic, so the web app never reads half a file


class Progress:
    def __init__(self, run_id: str, kind: str, label: str, spend_fn=None):
        self.run_id = run_id
        self.spend_fn = spend_fn or (lambda: 0.0)
        self.lock = threading.Lock()
        existing = read_status(run_id) or {}
        self.data = {
            "run_id": run_id, "kind": kind, "label": label, "state": "running",
            "stage": "fetch", "stage_label": STAGES["fetch"], "done": 0, "total": 0,
            "detail": "", "usd": 0.0, "pid": os.getpid(),
            "started": existing.get("requested") or time.time(), "updated": time.time(),
            "requested": existing.get("requested"), "digest": None, "error": None,
        }

    def _save(self):
        self.data["updated"] = time.time()
        try:
            self.data["usd"] = round(self.spend_fn(), 4)
        except Exception:
            pass
        write_status(self.run_id, self.data)

    def stage(self, key: str, total: int = 0, detail: str = ""):
        with self.lock:
            self.data.update(stage=key, stage_label=STAGES.get(key, key), done=0,
                             total=total, detail=detail)
            self._save()

    def advance(self, n: int = 1, detail: str | None = None):
        with self.lock:
            self.data["done"] += n
            if detail is not None:
                self.data["detail"] = detail
            self._save()

    def finish(self, state: str, digest: str | None = None, error: str | None = None,
               detail: str = ""):
        with self.lock:
            self.data.update(state=state, stage="done", stage_label=STAGES["done"],
                             digest=digest, error=error, detail=detail)
            self._save()


class NullProgress:
    """Stand-in when nothing needs to watch (e.g. tests)."""

    def stage(self, *a, **k):
        pass

    def advance(self, *a, **k):
        pass

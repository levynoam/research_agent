"""SQLite database that remembers papers, results and spend between runs.

A "run" produces one digest. Daily runs are named by date (2026-10-28); ad-hoc runs
get ids like adhoc-20261028-153000. Relevance judgments and summaries are stored per
run, since an ad-hoc focus changes them. Author data, research notes and images
describe the paper itself, so they are shared by all runs.
"""

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY,          -- arXiv id without version, e.g. 2510.12345
    version TEXT,
    title TEXT,
    abstract TEXT,
    authors TEXT,                 -- JSON list of names
    categories TEXT,              -- JSON list
    announced TEXT,               -- date the paper appeared in the feed
    run_date TEXT,                -- the run that first picked it up
    url TEXT
);
CREATE TABLE IF NOT EXISTS run_papers (
    run_id TEXT, id TEXT, PRIMARY KEY (run_id, id)
);
CREATE TABLE IF NOT EXISTS triage (
    run_id TEXT, id TEXT, score INTEGER, topics TEXT, reason TEXT,
    PRIMARY KEY (run_id, id)
);
CREATE TABLE IF NOT EXISTS deepread (
    run_id TEXT, id TEXT, relevant INTEGER, score INTEGER, data TEXT, source TEXT,
    PRIMARY KEY (run_id, id)
);
CREATE TABLE IF NOT EXISTS enrich (id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS research (id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS figures (id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS costs (
    ts TEXT, run_date TEXT, stage TEXT, model TEXT,   -- run_date holds the run id
    input_tokens INTEGER, output_tokens INTEGER, usd REAL
);
CREATE TABLE IF NOT EXISTS runs (
    run_date TEXT PRIMARY KEY,                       -- the run id
    started TEXT, finished TEXT, status TEXT,
    n_fetched INTEGER, n_matched INTEGER, usd REAL, digest TEXT, last_announced TEXT,
    kind TEXT, label TEXT, params TEXT
);
"""


def _columns(db, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _migrate(db):
    """Upgrades databases created by earlier versions (one digest per day, no run ids)."""
    if "run_id" not in _columns(db, "triage"):
        db.executescript("""
            ALTER TABLE triage RENAME TO triage_old;
            CREATE TABLE triage (run_id TEXT, id TEXT, score INTEGER, topics TEXT,
                                 reason TEXT, PRIMARY KEY (run_id, id));
            INSERT INTO triage SELECT p.run_date, t.id, t.score, t.topics, t.reason
                FROM triage_old t JOIN papers p ON p.id = t.id;
            DROP TABLE triage_old;
            ALTER TABLE deepread RENAME TO deepread_old;
            CREATE TABLE deepread (run_id TEXT, id TEXT, relevant INTEGER, score INTEGER,
                                   data TEXT, source TEXT, PRIMARY KEY (run_id, id));
            INSERT INTO deepread SELECT p.run_date, d.id, d.relevant, d.score, d.data, d.source
                FROM deepread_old d JOIN papers p ON p.id = d.id;
            DROP TABLE deepread_old;
            INSERT OR IGNORE INTO run_papers SELECT run_date, id FROM papers;
        """)
    for col in ("kind", "label", "params"):
        if col not in _columns(db, "runs"):
            db.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
    db.commit()


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        _migrate(self.db)
        self.lock = threading.Lock()  # stages call models in parallel threads

    def execute(self, sql: str, params=()):
        with self.lock:
            cur = self.db.execute(sql, params)
            self.db.commit()
            return cur

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    # --- papers -----------------------------------------------------------
    def known_ids(self) -> set[str]:
        return {r["id"] for r in self.query("SELECT id FROM papers")}

    def add_paper(self, p: dict, run_id: str):
        self.execute(
            "INSERT OR IGNORE INTO papers VALUES (?,?,?,?,?,?,?,?,?)",
            (p["id"], p["version"], p["title"], p["abstract"], json.dumps(p["authors"]),
             json.dumps(p["categories"]), p["announced"], run_id, p["url"]),
        )
        self.execute("INSERT OR IGNORE INTO run_papers VALUES (?,?)", (run_id, p["id"]))

    def paper(self, pid: str) -> dict:
        r = self.query("SELECT * FROM papers WHERE id=?", (pid,))[0]
        d = dict(r)
        d["authors"] = json.loads(d["authors"])
        d["categories"] = json.loads(d["categories"])
        return d

    def papers_for_run(self, run_id: str) -> list[dict]:
        rows = self.query("SELECT id FROM run_papers WHERE run_id=?", (run_id,))
        return [self.paper(r["id"]) for r in rows]

    def count_for_run(self, run_id: str) -> int:
        return self.query("SELECT COUNT(*) n FROM run_papers WHERE run_id=?", (run_id,))[0]["n"]

    # --- triage -----------------------------------------------------------
    def untriaged(self, run_id: str) -> list[dict]:
        rows = self.query(
            "SELECT r.id FROM run_papers r LEFT JOIN triage t ON t.run_id=r.run_id AND t.id=r.id "
            "WHERE r.run_id=? AND t.id IS NULL", (run_id,))
        return [self.paper(r["id"]) for r in rows]

    def save_triage(self, run_id, pid, score, topics, reason):
        self.execute("INSERT OR REPLACE INTO triage VALUES (?,?,?,?,?)",
                     (run_id, pid, score, json.dumps(topics), reason))

    def triaged(self, run_id: str, min_score: int) -> list[dict]:
        rows = self.query("SELECT * FROM triage WHERE run_id=? AND score>=? ORDER BY score DESC",
                          (run_id, min_score))
        return [dict(r) | {"topics": json.loads(r["topics"])} for r in rows]

    # --- full reads -------------------------------------------------------
    def save_deepread(self, run_id, pid, data: dict, relevant: bool, score: int, source: str):
        self.execute("INSERT OR REPLACE INTO deepread VALUES (?,?,?,?,?,?)",
                     (run_id, pid, int(relevant), score, json.dumps(data), source))

    def load_deepread(self, run_id: str, pid: str) -> dict | None:
        rows = self.query("SELECT data FROM deepread WHERE run_id=? AND id=?", (run_id, pid))
        return json.loads(rows[0]["data"]) if rows else None

    def deepread_rows(self, run_id: str, relevant: bool) -> list[sqlite3.Row]:
        return self.query("SELECT id, score, data FROM deepread WHERE run_id=? AND relevant=? "
                          "ORDER BY score DESC", (run_id, int(relevant)))

    # --- per-paper data shared by all runs (enrich, research, figures) ---------
    def save_json(self, table: str, pid: str, data: dict):
        self.execute(f"INSERT OR REPLACE INTO {table} (id, data) VALUES (?,?)",
                     (pid, json.dumps(data)))

    def load_json(self, table: str, pid: str) -> dict | None:
        rows = self.query(f"SELECT data FROM {table} WHERE id=?", (pid,))
        return json.loads(rows[0]["data"]) if rows else None

    # --- costs ------------------------------------------------------------
    def add_cost(self, run_id, stage, model, tin, tout, usd):
        self.execute("INSERT INTO costs VALUES (?,?,?,?,?,?,?)",
                     (datetime.now().isoformat(timespec="seconds"), run_id, stage, model,
                      tin, tout, usd))

    def spend_since(self, since: date) -> float:
        r = self.query("SELECT COALESCE(SUM(usd),0) s FROM costs WHERE ts>=?",
                       (since.isoformat(),))
        return r[0]["s"]

    def spend_for_run(self, run_id: str) -> float:
        r = self.query("SELECT COALESCE(SUM(usd),0) s FROM costs WHERE run_date=?", (run_id,))
        return r[0]["s"]

    # --- runs -------------------------------------------------------------
    def last_announced(self) -> str | None:
        r = self.query("SELECT MAX(last_announced) d FROM runs "
                       "WHERE status='ok' AND COALESCE(kind,'daily')='daily'")
        return r[0]["d"]

    def save_run(self, run_id, **fields):
        self.execute("INSERT OR IGNORE INTO runs (run_date) VALUES (?)", (run_id,))
        for k, v in fields.items():
            self.execute(f"UPDATE runs SET {k}=? WHERE run_date=?", (v, run_id))

    def runs(self) -> list[dict]:
        return [dict(r) for r in self.query("SELECT * FROM runs ORDER BY started DESC")]

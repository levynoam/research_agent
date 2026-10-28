"""SQLite database that remembers papers, results and spend between runs."""

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
    run_date TEXT,                -- date of the run that picked it up
    url TEXT
);
CREATE TABLE IF NOT EXISTS triage (
    id TEXT PRIMARY KEY, score INTEGER, topics TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS deepread (
    id TEXT PRIMARY KEY, relevant INTEGER, score INTEGER, data TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS enrich (id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS research (id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS figures (id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS costs (
    ts TEXT, run_date TEXT, stage TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, usd REAL
);
CREATE TABLE IF NOT EXISTS runs (
    run_date TEXT PRIMARY KEY, started TEXT, finished TEXT, status TEXT,
    n_fetched INTEGER, n_matched INTEGER, usd REAL, digest TEXT, last_announced TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
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

    def add_paper(self, p: dict, run_date: str):
        self.execute(
            "INSERT OR IGNORE INTO papers VALUES (?,?,?,?,?,?,?,?,?)",
            (p["id"], p["version"], p["title"], p["abstract"], json.dumps(p["authors"]),
             json.dumps(p["categories"]), p["announced"], run_date, p["url"]),
        )

    def paper(self, pid: str) -> dict:
        r = self.query("SELECT * FROM papers WHERE id=?", (pid,))[0]
        d = dict(r)
        d["authors"] = json.loads(d["authors"])
        d["categories"] = json.loads(d["categories"])
        return d

    def papers_for_run(self, run_date: str) -> list[dict]:
        rows = self.query("SELECT id FROM papers WHERE run_date=?", (run_date,))
        return [self.paper(r["id"]) for r in rows]

    # --- per-stage results ------------------------------------------------
    def untriaged(self, run_date: str) -> list[dict]:
        rows = self.query(
            "SELECT p.id FROM papers p LEFT JOIN triage t ON p.id=t.id "
            "WHERE p.run_date=? AND t.id IS NULL", (run_date,))
        return [self.paper(r["id"]) for r in rows]

    def save_triage(self, pid, score, topics, reason):
        self.execute("INSERT OR REPLACE INTO triage VALUES (?,?,?,?)",
                     (pid, score, json.dumps(topics), reason))

    def triaged(self, run_date: str, min_score: int) -> list[dict]:
        rows = self.query(
            "SELECT t.* FROM triage t JOIN papers p ON p.id=t.id "
            "WHERE p.run_date=? AND t.score>=? ORDER BY t.score DESC", (run_date, min_score))
        return [dict(r) | {"topics": json.loads(r["topics"])} for r in rows]

    def save_json(self, table: str, pid: str, data: dict, **extra):
        cols = ["id", "data", *extra.keys()]
        vals = [pid, json.dumps(data), *extra.values()]
        self.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
                     f"VALUES ({','.join('?' * len(cols))})", vals)

    def load_json(self, table: str, pid: str) -> dict | None:
        rows = self.query(f"SELECT data FROM {table} WHERE id=?", (pid,))
        return json.loads(rows[0]["data"]) if rows else None

    # --- costs ------------------------------------------------------------
    def add_cost(self, run_date, stage, model, tin, tout, usd):
        self.execute("INSERT INTO costs VALUES (?,?,?,?,?,?,?)",
                     (datetime.now().isoformat(timespec="seconds"), run_date, stage, model,
                      tin, tout, usd))

    def spend_since(self, since: date) -> float:
        r = self.query("SELECT COALESCE(SUM(usd),0) s FROM costs WHERE ts>=?",
                       (since.isoformat(),))
        return r[0]["s"]

    def spend_for_run(self, run_date: str) -> float:
        r = self.query("SELECT COALESCE(SUM(usd),0) s FROM costs WHERE run_date=?", (run_date,))
        return r[0]["s"]

    # --- runs -------------------------------------------------------------
    def last_announced(self) -> str | None:
        r = self.query("SELECT MAX(last_announced) d FROM runs WHERE status='ok'")
        return r[0]["d"]

    def save_run(self, run_date, **fields):
        existing = self.query("SELECT run_date FROM runs WHERE run_date=?", (run_date,))
        if not existing:
            self.execute("INSERT INTO runs (run_date) VALUES (?)", (run_date,))
        for k, v in fields.items():
            self.execute(f"UPDATE runs SET {k}=? WHERE run_date=?", (v, run_date))

"""Local web app: browse digests, request ad-hoc digests, watch the agent work.

Serves http://localhost:8765 and only accepts requests from this Mac's own browser.
Start it with scripts/web.sh (or install it to run at login: scripts/install_web.sh).
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from research_agent.cli import MAX_ADHOC_DAYS
from research_agent.config import (ROOT, ensure_from_template, load_config, load_criteria,
                                   load_format)
from research_agent.progress import STATUS_DIR, read_status, write_status
from research_agent.store import Store

HOST, PORT = "127.0.0.1", int(os.environ.get("RESEARCH_AGENT_WEB_PORT", 8765))
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
DIGESTS = ROOT / "digests"
STATIC = ROOT / "research_agent" / "static"
CATEGORY_RE = re.compile(r"^[a-z\-]+(\.[A-Za-z\-]+)?$")
PROMPT_BACKUPS = ROOT / "cache" / "prompt-backups"
MAX_PROMPT_BYTES = 200_000

# The two files that steer the agent, editable from the web page. Each is checked with
# the same loader the agent uses before it is saved, so a broken file never lands.
PROMPTS = {
    "criteria": {"file": "criteria.md", "title": "Research criteria", "check": load_criteria,
                 "help": "What you care about: arXiv categories (in the --- block at the top) "
                         "and topics (each '## ' heading under '# Topics'). Read at the start "
                         "of every run."},
    "format": {"file": "summary_format.md", "title": "Summary format", "check": load_format,
               "help": "How summaries and the digest look: each '## ' heading under "
                       "'# Sections' is one section of every summary. Read at the start of "
                       "every run."},
}

app = FastAPI(title="research-agent", docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Only this Mac's browser may use the app. Checking Host blocks DNS-rebinding tricks;
    # checking Origin on POSTs stops other websites from starting (paid) runs.
    if request.headers.get("host") not in ALLOWED_HOSTS:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if request.method != "GET":
        origin = request.headers.get("origin")
        if origin not in {f"http://{h}" for h in ALLOWED_HOSTS}:
            return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _statuses() -> dict[str, dict]:
    out = {}
    for f in STATUS_DIR.glob("*.json") if STATUS_DIR.exists() else []:
        s = read_status(f.stem)
        if not s:
            continue
        if s.get("state") in ("queued", "running") and not _alive(s.get("pid")):
            s["state"] = "failed"
            s["error"] = s.get("error") or "the agent process stopped unexpectedly"
        out[f.stem] = s
    return out


def _log_tail(run_id: str, lines: int = 12) -> list[str]:
    path = ROOT / "logs" / f"run-{run_id}.log"
    try:
        text = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    keep = [ln for ln in text if not ln.startswith("For more information")]
    return keep[-lines:]


def _spawn(run_id: str, kind: str, label: str, args: list[str]):
    """Starts the agent as its own process, so it keeps going if the web app restarts."""
    (ROOT / "logs").mkdir(exist_ok=True)
    out = open(ROOT / "logs" / f"spawn-{run_id}.out", "w")
    proc = subprocess.Popen([str(ROOT / "scripts" / "run.sh"), *args], cwd=ROOT,
                            stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    now = datetime.now().timestamp()
    write_status(run_id, {"run_id": run_id, "kind": kind, "label": label, "state": "queued",
                          "stage": "queued", "stage_label": "Starting", "done": 0, "total": 0,
                          "detail": "", "usd": 0.0, "pid": proc.pid, "requested": now,
                          "started": now, "updated": now, "digest": None, "error": None})


# --- pages and files ---------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
DIGESTS.mkdir(exist_ok=True)
app.mount("/digests", StaticFiles(directory=DIGESTS), name="digests")


# --- API ---------------------------------------------------------------------------
@app.get("/api/overview")
def overview():
    cfg, criteria = load_config(), load_criteria()
    store = Store(ROOT / "state.db")
    today = date.today()
    return {
        "today": today.isoformat(), "categories": criteria.categories,
        "topics": criteria.topics, "max_adhoc_days": MAX_ADHOC_DAYS,
        "spend_today": round(store.spend_since(today), 2),
        "spend_month": round(store.spend_since(today.replace(day=1)), 2),
        "budget_daily": cfg["budget"]["daily_usd"], "budget_monthly": cfg["budget"]["monthly_usd"],
    }


@app.get("/api/runs")
def runs():
    statuses = _statuses()
    rows = {r["run_date"]: r for r in Store(ROOT / "state.db").runs()}
    out = []
    for run_id in set(rows) | set(statuses):
        r, s = rows.get(run_id, {}), statuses.get(run_id, {})
        html = DIGESTS / f"{run_id}.html"
        state = s.get("state") or {"ok": "done"}.get(r.get("status"), r.get("status"))
        out.append({
            "id": run_id, "kind": r.get("kind") or s.get("kind") or "daily",
            "label": r.get("label") or s.get("label") or run_id, "state": state,
            "started": r.get("started") or (datetime.fromtimestamp(s["started"]).isoformat(
                timespec="seconds") if s.get("started") else None),
            "matched": r.get("n_matched"), "scanned": r.get("n_fetched"),
            "usd": round(r.get("usd") or s.get("usd") or 0, 2),
            "digest": f"/digests/{html.name}" if html.exists() else None,
            "params": json.loads(r["params"]) if r.get("params") else None,
            "error": s.get("error"), "note": r.get("note"),
        })
    out.sort(key=lambda x: x["started"] or "", reverse=True)
    return out


@app.get("/api/status")
def status():
    statuses = _statuses()
    active = [s for s in statuses.values() if s.get("state") in ("queued", "running")]
    active.sort(key=lambda s: s.get("requested") or s.get("started") or 0)
    for s in active:
        s["log"] = _log_tail(s["run_id"])
    recent = sorted((s for s in statuses.values() if s.get("state") in ("done", "failed")),
                    key=lambda s: s.get("updated") or 0, reverse=True)[:1]
    return {"active": active, "last_finished": recent[0] if recent else None}


class AdhocRequest(BaseModel):
    date_from: str
    date_to: str
    focus: str = ""
    categories: list[str] = []
    research: bool = True
    images: bool = True


@app.post("/api/adhoc")
def adhoc(req: AdhocRequest):
    try:
        d_from, d_to = date.fromisoformat(req.date_from), date.fromisoformat(req.date_to)
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-10-21.")
    if d_from > d_to:
        raise HTTPException(400, "The start date is after the end date.")
    if d_to > date.today():
        raise HTTPException(400, "The end date is in the future.")
    if (d_to - d_from).days >= MAX_ADHOC_DAYS:
        raise HTTPException(400, f"Pick at most {MAX_ADHOC_DAYS} days.")
    cats = [c.strip() for c in req.categories if c.strip()]
    if not cats or not all(CATEGORY_RE.match(c) for c in cats):
        raise HTTPException(400, "Categories look like cs.LG, stat.ML or quant-ph.")
    focus = " ".join(req.focus.split())[:500]

    run_id = f"adhoc-{datetime.now():%Y%m%d-%H%M%S}"
    label = (focus or "My topics") + f" · {d_from:%d %b} – {d_to:%d %b %Y}"
    args = ["--from", d_from.isoformat(), "--to", d_to.isoformat(), "--run-id", run_id,
            "--categories", ",".join(cats)]
    if focus:
        args += ["--focus", focus]
    if not req.research:
        args.append("--no-research")
    if not req.images:
        args.append("--no-images")
    _spawn(run_id, "adhoc", label, args)
    return {"run_id": run_id}


@app.post("/api/daily")
def daily():
    run_id = date.today().isoformat()
    s = read_status(run_id)
    if s and s.get("state") in ("queued", "running") and _alive(s.get("pid")):
        raise HTTPException(409, "Today's digest is already being made.")
    _spawn(run_id, "daily", f"{date.today():%A %d %B %Y}", [])
    return {"run_id": run_id}


def _version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _summary(name: str, path: Path) -> str:
    if name == "criteria":
        c = load_criteria(path)
        return f"{len(c.categories)} categories, {len(c.topics)} topics: " + "; ".join(c.topics)
    f = load_format(path)
    return f"{len(f.sections)} summary sections: " + ", ".join(f.section_names())


@app.get("/api/prompts")
def prompts():
    out = []
    for name, p in PROMPTS.items():
        path = ensure_from_template(ROOT / p["file"])
        text = path.read_text()
        try:
            summary, problem = _summary(name, path), None
        except Exception as e:  # show a file that's broken on disk, so it can be fixed here
            summary, problem = "", str(e)
        out.append({"name": name, "file": p["file"], "title": p["title"], "help": p["help"],
                    "text": text, "version": _version(text), "summary": summary,
                    "problem": problem})
    return out


class PromptSave(BaseModel):
    text: str
    version: str  # the version the edit started from, to avoid overwriting newer changes


@app.post("/api/prompts/{name}")
def save_prompt(name: str, req: PromptSave):
    if name not in PROMPTS:
        raise HTTPException(404, "Unknown file.")
    path = ensure_from_template(ROOT / PROMPTS[name]["file"])
    text = req.text.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    if len(text.encode()) > MAX_PROMPT_BYTES:
        raise HTTPException(400, "The file is too long.")
    current = path.read_text()
    if _version(current) != req.version:
        raise HTTPException(409, f"{path.name} was changed somewhere else since you opened "
                                 "it. Reload to see that version (copy your edits first).")
    if text == current:
        return {"version": req.version, "summary": _summary(name, path), "saved": False}

    # Write to a temporary file next to the real one, check it, then swap it in.
    fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        try:
            summary = _summary(name, Path(tmp))
        except Exception as e:
            raise HTTPException(400, f"Not saved: {str(e).replace(tmp, path.name)}")
        PROMPT_BACKUPS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (PROMPT_BACKUPS / f"{path.stem}-{stamp}.md").write_text(current)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"version": _version(text), "summary": summary, "saved": True}


def main():
    import uvicorn

    os.chdir(ROOT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()

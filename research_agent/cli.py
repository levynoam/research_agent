"""Command line entry point: `python -m research_agent run` / `status`."""

import argparse
import fcntl
import logging
import os
import subprocess
import sys
from datetime import date, datetime

from research_agent import arxiv_http
from research_agent.config import ROOT, load_config, load_criteria, load_format
from research_agent.deepread import deepread
from research_agent.digest import write_digest
from research_agent.fetch import fetch_new
from research_agent.figures import make_figures
from research_agent.llm import LLM, BudgetExceeded
from research_agent.research import research_paper
from research_agent.scholar import Scholar, enrich_paper
from research_agent.store import Store
from research_agent.triage import triage

log = logging.getLogger("research_agent")


def notify(title: str, message: str):
    script = f'display notification "{message}" with title "{title}"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def setup_logging(run_date: str):
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(ROOT / "logs" / f"run-{run_date}.log")]
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    for noisy in ("httpx", "httpx2", "openai", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_run(args):
    os.chdir(ROOT)
    run_date = date.today().isoformat()
    setup_logging(run_date)
    # Only one run at a time (e.g. a manual run while the scheduled one is going), so
    # arXiv never sees two of our processes at once.
    lock_file = open(ROOT / "logs" / ".run.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("another run is already in progress; exiting")
        return
    cfg, criteria, fmt = load_config(), load_criteria(), load_format()
    store = Store(ROOT / "state.db")
    llm = LLM(cfg, store, run_date)
    scholar = Scholar(cfg)
    store.save_run(run_date, started=datetime.now().isoformat(timespec="seconds"),
                   status="running")
    note = None
    log.info("run %s: categories %s, topics %s", run_date, criteria.categories, criteria.topics)

    try:
        # 1. Fetch
        papers, announced = fetch_new(criteria.categories, store.last_announced(),
                                      cfg["catchup"]["max_days"], store.known_ids())
        if args.limit:
            papers = papers[:args.limit]
        for p in papers:
            store.add_paper(p, run_date)
        fetched = len(store.papers_for_run(run_date))
        store.save_run(run_date, n_fetched=fetched, last_announced=announced)
        if fetched == 0:
            log.info("nothing new from arXiv today (weekend or holiday)")
            store.save_run(run_date, status="ok", finished=datetime.now().isoformat())
            return

        # 2. Triage every abstract
        tcfg = cfg["triage"]
        triage(store.untriaged(run_date), criteria, cfg, llm, store)
        candidates = store.triaged(run_date, tcfg["threshold"])[:tcfg["max_deepread_per_day"]]
        log.info("triage: %d candidates for full read", len(candidates))

        # 3. Full read of candidates
        if args.resummarize:
            # Redo today's full reads, e.g. after editing summary_format.md. Paper text is
            # cached, so papers already downloaded are not fetched from arXiv again.
            store.execute("DELETE FROM deepread WHERE id IN "
                          "(SELECT id FROM papers WHERE run_date=?)", (run_date,))
            log.info("re-summarizing today's papers with the current summary_format.md")
        deepread(candidates, criteria, fmt, cfg, llm, store)
        relevant = store.query(
            "SELECT d.id, d.score FROM deepread d JOIN papers p ON p.id=d.id "
            "WHERE p.run_date=? AND d.relevant=1 ORDER BY d.score DESC", (run_date,))
        log.info("deepread: %d of %d confirmed relevant", len(relevant), len(candidates))

        # 3a. Author and related-work lookups (free APIs, no model calls)
        if cfg["enrich"]["enabled"] and not args.no_enrich:
            for r in relevant:
                if not store.load_json("enrich", r["id"]):
                    store.save_json("enrich", r["id"],
                                    enrich_paper(store.paper(r["id"]), cfg, scholar, llm))

        # 3b. Research agent on the very top papers
        rcfg = cfg["research"]
        if rcfg["enabled"] and not args.no_research:
            top = [r for r in relevant if r["score"] >= rcfg["min_score"]]
            for r in top[:rcfg["max_papers_per_day"]]:
                if store.load_json("research", r["id"]):
                    continue
                notes = research_paper(store.paper(r["id"]), store.load_json("deepread", r["id"]),
                                       store.load_json("enrich", r["id"]) or {},
                                       criteria, cfg, llm, scholar)
                store.save_json("research", r["id"], notes)

        # 3c. One image per paper: a figure from the paper, or a generated illustration
        if fmt.options.get("show_figures", True):
            if args.redo_figures:
                store.execute("DELETE FROM figures WHERE id IN "
                              "(SELECT id FROM papers WHERE run_date=?)", (run_date,))
            make_figures([r["id"] for r in relevant], run_date, fmt, cfg, llm, store)
    except BudgetExceeded as e:
        note = f"Spending limit reached ({e}); this digest covers only what finished before it."
        log.warning(note)

    log.info("arXiv: %d requests this run", arxiv_http.request_count())
    if arxiv_http.blocked_reason():
        blocked = (f"arXiv pushed back ({arxiv_http.blocked_reason()}), so the agent stopped "
                   "contacting arXiv for the rest of the run. Some papers were summarized "
                   "from the abstract only.")
        note = f"{note} {blocked}" if note else blocked

    # 4. Digest (written even after a budget stop, from whatever finished)
    fetched = len(store.papers_for_run(run_date))
    path = write_digest(run_date, criteria, fmt, cfg, store, llm,
                        {"fetched": fetched, "usd": store.spend_for_run(run_date)}, note)
    n_matched = store.query(
        "SELECT COUNT(*) n FROM deepread d JOIN papers p ON p.id=d.id "
        "WHERE p.run_date=? AND d.relevant=1", (run_date,))[0]["n"]
    usd = store.spend_for_run(run_date)
    store.save_run(run_date, status="ok", finished=datetime.now().isoformat(timespec="seconds"),
                   n_matched=n_matched, usd=usd, digest=str(path))
    log.info("digest written: %s (%d papers, $%.2f)", path, n_matched, usd)
    if cfg["output"]["notify"]:
        notify("arXiv digest ready", f"{n_matched} papers matched today (${usd:.2f})")


def cmd_status(args):
    os.chdir(ROOT)
    cfg = load_config()
    store = Store(ROOT / "state.db")
    today = date.today()
    b = cfg["budget"]
    print(f"Spent today:      ${store.spend_since(today):.2f}  (limit ${b['daily_usd']})")
    print(f"Spent this month: ${store.spend_since(today.replace(day=1)):.2f}  "
          f"(limit ${b['monthly_usd']})")
    print("\nRecent runs:")
    for r in store.query("SELECT * FROM runs ORDER BY run_date DESC LIMIT 10"):
        print(f"  {r['run_date']}  {r['status']:<8} scanned {r['n_fetched'] or 0:>5}  "
              f"matched {r['n_matched'] or 0:>3}  ${r['usd'] or 0:.2f}")


def main():
    parser = argparse.ArgumentParser(prog="research_agent")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run today's pipeline and write the digest")
    run.add_argument("--limit", type=int, help="only process this many papers (for testing)")
    run.add_argument("--no-research", action="store_true", help="skip the web research agent")
    run.add_argument("--no-enrich", action="store_true", help="skip author/similar lookups")
    run.add_argument("--resummarize", action="store_true",
                     help="redo today's summaries (after editing summary_format.md)")
    run.add_argument("--redo-figures", action="store_true",
                     help="redo today's figure choices and generated images")
    run.set_defaults(func=cmd_run)
    status = sub.add_parser("status", help="show spending and recent runs")
    status.set_defaults(func=cmd_status)
    args = parser.parse_args()
    try:
        args.func(args)
    except Exception:
        log.exception("run failed")
        if args.command == "run":
            notify("arXiv digest FAILED", "See the logs folder in research-agent")
        sys.exit(1)

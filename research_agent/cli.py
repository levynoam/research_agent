"""Command line entry point: `python -m research_agent run` / `status`.

`run` with no options makes today's daily digest. With --from/--to it makes an
ad-hoc digest of papers submitted in that window, optionally narrowed by --focus.
"""

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import date, datetime

from research_agent import arxiv_http
from research_agent.config import ROOT, load_config, load_criteria, load_format
from research_agent.deepread import deepread
from research_agent.digest import write_digest
from research_agent.fetch import fetch_api_window, fetch_new
from research_agent.figures import make_figures
from research_agent.llm import LLM, BudgetExceeded
from research_agent.progress import Progress
from research_agent.research import research_paper
from research_agent.scholar import Scholar, enrich_paper
from research_agent.store import Store
from research_agent.triage import triage

log = logging.getLogger("research_agent")

MAX_ADHOC_DAYS = 14
MAX_LOCK_WAIT_S = 3 * 3600
FOCUS_TOPIC = "Ad-hoc focus"


def notify(title: str, message: str):
    script = f'display notification "{message}" with title "{title}"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def setup_logging(run_id: str):
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(ROOT / "logs" / f"run-{run_id}.log")]
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    for noisy in ("httpx", "httpx2", "openai", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def wait_for_lock(progress: Progress):
    """Only one run at a time, so arXiv never sees two of our processes at once.

    A run that finds another one going waits for it (up to 3 hours) instead of quitting,
    so a scheduled daily run is never lost to an ad-hoc one.
    """
    lock_file = open(ROOT / "logs" / ".run.lock", "w")
    start = time.time()
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except BlockingIOError:
            if time.time() - start > MAX_LOCK_WAIT_S:
                raise RuntimeError("another run has been going for over 3 hours; giving up")
            if progress.data["stage"] != "queued":
                log.info("another run is in progress; waiting for it to finish")
                progress.stage("queued")
            time.sleep(10)


def adhoc_criteria(criteria, focus: str, categories: list[str] | None):
    if categories:
        criteria = replace(criteria, categories=categories)
    if focus:
        criteria = replace(
            criteria, topics=[FOCUS_TOPIC] + criteria.topics,
            text=criteria.text + f"\n\n# {FOCUS_TOPIC}\nThis is an ad-hoc digest about: "
                 f"{focus}\nFor this digest a paper counts as relevant only if it matches "
                 "this focus. Score papers by how well they match the focus: a paper that "
                 "fits the topics above but not this focus scores 3 at most. Put relevant "
                 f"papers under the topic '{FOCUS_TOPIC}', or under one of the topics above "
                 "when they fit there better.")
    return criteria


def cmd_run(args):
    os.chdir(ROOT)
    adhoc = bool(args.date_from)
    if adhoc:
        d_from, d_to = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
        if not d_from <= d_to <= date.today() or (d_to - d_from).days >= MAX_ADHOC_DAYS:
            raise SystemExit(f"--from/--to must be a range of at most {MAX_ADHOC_DAYS} days, "
                             "ending today or earlier")
        run_id = args.run_id or f"adhoc-{datetime.now():%Y%m%d-%H%M%S}"
        label = (args.focus or "My topics") + f" · {d_from:%d %b} – {d_to:%d %b %Y}"
    else:
        run_id = date.today().isoformat()
        label = f"{date.today():%A %d %B %Y}"
    kind = "adhoc" if adhoc else "daily"
    setup_logging(run_id)

    store = Store(ROOT / "state.db")
    progress = Progress(run_id, kind, label, spend_fn=lambda: store.spend_for_run(run_id))
    try:
        lock = wait_for_lock(progress)  # noqa: F841 (held until the process exits)
        run_pipeline(args, run_id, kind, label, store, progress)
    except BaseException as e:
        progress.finish("failed", error=f"{type(e).__name__}: {e}")
        store.save_run(run_id, status="failed", finished=datetime.now().isoformat(timespec="seconds"))
        raise


def _no_papers_note(adhoc: bool) -> str:
    """Why a run found nothing, in words for the log and the web app."""
    if adhoc:
        return "No papers in these categories were submitted in this date range."
    if date.today().weekday() >= 5:
        return "arXiv doesn't publish new papers on weekends."
    # arXiv announces Sunday to Thursday evenings (US Eastern), so a weekday morning
    # normally has a batch. An empty feed means arXiv skipped this announcement
    # (a holiday or maintenance). Nothing is lost: if papers appear later, the next
    # run sees the gap and fetches them from the arXiv API.
    return ("arXiv published no new papers today (its feed is empty for all categories, "
            "usually a holiday or maintenance). The next run will pick up anything missed.")


def run_pipeline(args, run_id: str, kind: str, label: str, store: Store, progress: Progress):
    adhoc = kind == "adhoc"
    cfg, criteria, fmt = load_config(), load_criteria(), load_format()
    if adhoc:
        cats = [c.strip() for c in (args.categories or "").split(",") if c.strip()] or None
        criteria = adhoc_criteria(criteria, args.focus or "", cats)
    llm = LLM(cfg, store, run_id)
    scholar = Scholar(cfg)
    params = {k: getattr(args, k) for k in ("date_from", "date_to", "focus", "categories",
                                            "no_research", "no_images", "limit")}
    store.save_run(run_id, started=datetime.now().isoformat(timespec="seconds"),
                   status="running", kind=kind, label=label, params=json.dumps(params))
    note = None
    log.info("run %s (%s): categories %s, topics %s", run_id, kind, criteria.categories,
             criteria.topics)

    try:
        # 1. Fetch
        progress.stage("fetch")
        if adhoc:
            papers = fetch_api_window(criteria.categories, date.fromisoformat(args.date_from),
                                      date.fromisoformat(args.date_to))
            announced = None
            log.info("arXiv API: %d papers submitted %s to %s", len(papers), args.date_from,
                     args.date_to)
        else:
            papers, announced = fetch_new(criteria.categories, store.last_announced(),
                                          cfg["catchup"]["max_days"], store.known_ids())
        if args.limit:
            papers = papers[:args.limit]
        for p in papers:
            store.add_paper(p, run_id)
        fetched = store.count_for_run(run_id)
        store.save_run(run_id, n_fetched=fetched, last_announced=announced)
        if fetched == 0:
            note = _no_papers_note(adhoc)
            log.info("no papers: %s", note)
            store.save_run(run_id, status="ok", note=note,
                           finished=datetime.now().isoformat(timespec="seconds"))
            progress.finish("done", detail=note)
            return

        # 2. Triage every abstract
        tcfg = cfg["triage"]
        todo = store.untriaged(run_id)
        progress.stage("triage", total=-(-len(todo) // tcfg["batch_size"]),
                       detail=f"{len(todo)} abstracts")
        triage(todo, criteria, cfg, llm, store, run_id, progress)
        candidates = store.triaged(run_id, tcfg["threshold"])[:tcfg["max_deepread_per_day"]]
        log.info("triage: %d candidates for full read", len(candidates))

        # 3. Full read of candidates
        if args.resummarize:
            # Redo this run's full reads, e.g. after editing summary_format.md. Paper text
            # is cached, so papers already downloaded are not fetched from arXiv again.
            store.execute("DELETE FROM deepread WHERE run_id=?", (run_id,))
            log.info("re-summarizing with the current summary_format.md")
        progress.stage("deepread", total=len(candidates))
        deepread(candidates, criteria, fmt, cfg, llm, store, run_id, progress)
        relevant = store.deepread_rows(run_id, relevant=True)
        log.info("deepread: %d of %d confirmed relevant", len(relevant), len(candidates))

        # 3a. Authors and similar papers
        if cfg["enrich"]["enabled"] and not args.no_enrich:
            progress.stage("enrich", total=len(relevant))
            for r in relevant:
                if not store.load_json("enrich", r["id"]):
                    store.save_json("enrich", r["id"],
                                    enrich_paper(store.paper(r["id"]), cfg, scholar, llm))
                progress.advance()

        # 3b. Research agent on the very top papers
        rcfg = cfg["research"]
        if rcfg["enabled"] and not args.no_research:
            top = [r for r in relevant if r["score"] >= rcfg["min_score"]]
            top = top[:rcfg["max_papers_per_day"]]
            progress.stage("research", total=len(top))
            for r in top:
                if not store.load_json("research", r["id"]):
                    p = store.paper(r["id"])
                    progress.advance(0, detail=p["title"][:90])
                    notes = research_paper(p, store.load_deepread(run_id, r["id"]),
                                           store.load_json("enrich", r["id"]) or {},
                                           criteria, cfg, llm, scholar)
                    store.save_json("research", r["id"], notes)
                progress.advance()

        # 3c. One image per paper: a figure from the paper, or a generated illustration
        if fmt.options.get("show_figures", True):
            if args.redo_figures:
                store.execute("DELETE FROM figures WHERE id IN "
                              "(SELECT id FROM run_papers WHERE run_id=?)", (run_id,))
            if args.no_images:
                cfg["images"]["enabled"] = False
            progress.stage("figures", total=len(relevant))
            make_figures([r["id"] for r in relevant], run_id, fmt, cfg, llm, store, progress)
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
    progress.stage("digest")
    if adhoc:
        title = f"Ad-hoc digest: {label}"
        scope = (f"papers submitted {args.date_from} to {args.date_to} in "
                 f"{', '.join(criteria.categories)}")
    else:
        title = f"arXiv digest: {label}"
        scope = f"new papers scanned in {', '.join(criteria.categories)}"
    path = write_digest(run_id, title, criteria, fmt, cfg, store, llm,
                        {"fetched": store.count_for_run(run_id), "scope": scope,
                         "usd": store.spend_for_run(run_id)}, note)
    n_matched = len(store.deepread_rows(run_id, relevant=True))
    usd = store.spend_for_run(run_id)
    store.save_run(run_id, status="ok", finished=datetime.now().isoformat(timespec="seconds"),
                   n_matched=n_matched, usd=usd, digest=str(path))
    progress.finish("done", digest=path.with_suffix(".html").name,
                    detail=f"{n_matched} papers matched")
    log.info("digest written: %s (%d papers, $%.2f)", path, n_matched, usd)
    if cfg["output"]["notify"]:
        what = "Ad-hoc digest" if adhoc else "arXiv digest"
        notify(f"{what} ready", f"{n_matched} papers matched (${usd:.2f})")


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
    for r in store.runs()[:10]:
        print(f"  {r['run_date']:<24} {r['status'] or '':<8} scanned {r['n_fetched'] or 0:>5}  "
              f"matched {r['n_matched'] or 0:>3}  ${r['usd'] or 0:.2f}")


def main():
    parser = argparse.ArgumentParser(prog="research_agent")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="make today's digest, or an ad-hoc one with --from/--to")
    run.add_argument("--from", dest="date_from", help="ad-hoc: first submission date (YYYY-MM-DD)")
    run.add_argument("--to", dest="date_to", help="ad-hoc: last submission date (YYYY-MM-DD)")
    run.add_argument("--focus", help="ad-hoc: what this digest is about (plain language)")
    run.add_argument("--categories", help="ad-hoc: comma-separated arXiv categories "
                                          "(default: those in criteria.md)")
    run.add_argument("--run-id", help=argparse.SUPPRESS)  # set by the web app
    run.add_argument("--limit", type=int, help="only process this many papers (for testing)")
    run.add_argument("--no-research", action="store_true", help="skip the web research agent")
    run.add_argument("--no-enrich", action="store_true", help="skip author/similar lookups")
    run.add_argument("--no-images", action="store_true", help="never generate images")
    run.add_argument("--resummarize", action="store_true",
                     help="redo this run's summaries (after editing summary_format.md)")
    run.add_argument("--redo-figures", action="store_true",
                     help="redo figure choices and generated images")
    run.set_defaults(func=cmd_run)
    status = sub.add_parser("status", help="show spending and recent runs")
    status.set_defaults(func=cmd_status)
    args = parser.parse_args()
    if args.command == "run" and bool(args.date_from) != bool(args.date_to):
        parser.error("--from and --to go together")
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception:
        log.exception("run failed")
        if args.command == "run":
            notify("arXiv digest FAILED", "See the logs folder in research-agent")
        sys.exit(1)

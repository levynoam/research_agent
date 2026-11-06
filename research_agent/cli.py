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
from zoneinfo import ZoneInfo

from openai import APIConnectionError

from research_agent import arxiv_http
from research_agent.config import ROOT, load_config, load_criteria, load_format
from research_agent.deepread import deepread
from research_agent.digest import write_digest
from research_agent.fetch import fetch_api_window, fetch_new
from research_agent.figures import make_figures
from research_agent.llm import LLM, BudgetExceeded
from research_agent.progress import Progress
from research_agent.refine import refine_criteria
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


def setup_logging(log_name: str):
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(ROOT / "logs" / f"{log_name}.log")]
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


def _connection_lost(e: BaseException) -> bool:
    while e is not None:
        if isinstance(e, APIConnectionError):
            return True
        e = e.__cause__ or e.__context__
    return False


def cmd_run(args):
    os.chdir(ROOT)
    if args.resume:
        # Finish an earlier run that was cut short: same papers, same settings.
        row = Store(ROOT / "state.db").run(args.resume)
        if not row:
            raise SystemExit(f"no run {args.resume} to resume")
        for k, v in json.loads(row.get("params") or "{}").items():
            if getattr(args, k, None) in (None, False):
                setattr(args, k, v)
    adhoc = bool(args.date_from)
    if args.resume:
        run_id, label = args.resume, row.get("label") or args.resume
    elif adhoc:
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
    setup_logging(f"run-{run_id}")

    store = Store(ROOT / "state.db")
    progress = Progress(run_id, kind, label, spend_fn=lambda: store.spend_for_run(run_id))
    try:
        lock = wait_for_lock(progress)  # noqa: F841 (held until the process exits)
        run_pipeline(args, run_id, kind, label, store, progress)
    except BaseException as e:
        progress.finish("failed", error=f"{type(e).__name__}: {e}")
        store.save_run(run_id, status="failed", finished=datetime.now().isoformat(timespec="seconds"),
                       incomplete=1 if _connection_lost(e) else 0)
        raise


def _no_papers_note(adhoc: bool, announced: str | None = None) -> str:
    """Why a run found nothing, in words for the log and the web app."""
    if adhoc:
        return "No papers in these categories were submitted in this date range."
    if announced:
        return (f"Nothing new since the last digest: arXiv's latest batch (announced "
                f"{announced}) is already in an earlier digest. New batches come out Sunday "
                "to Thursday at midnight US Eastern (9 pm Pacific).")
    # arXiv's feed gets a new batch at midnight US Eastern, dated Monday to Friday, and
    # is empty on Eastern Saturdays and Sundays, wherever the Mac happens to be.
    if datetime.now(ZoneInfo("America/New_York")).weekday() >= 5:
        return "arXiv doesn't publish new papers on weekends."
    # A weekday with an empty feed means arXiv skipped this announcement (a holiday or
    # maintenance). Nothing is lost: if papers appear later, the next run sees the gap
    # and fetches them from the arXiv API.
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
    if args.resume:
        store.save_run(run_id, status="running")
    else:
        store.save_run(run_id, started=datetime.now().isoformat(timespec="seconds"),
                       status="running", kind=kind, label=label, params=json.dumps(params),
                       resumes=0)
    note = None
    log.info("run %s (%s): categories %s, topics %s", run_id, kind, criteria.categories,
             criteria.topics)

    try:
        # 1. Fetch (a resumed run keeps the papers it already has)
        progress.stage("fetch")
        if args.resume:
            papers, announced = [], store.run(run_id).get("last_announced")
            log.info("resuming run %s: finishing the steps that were skipped", run_id)
        elif adhoc:
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
            note = _no_papers_note(adhoc, announced)
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
                    try:
                        store.save_json("enrich", r["id"],
                                        enrich_paper(store.paper(r["id"]), cfg, scholar, llm))
                    except APIConnectionError as e:  # skipped now, done when resumed
                        log.error("author lookup skipped for %s: %s", r["id"], e)
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
                    try:
                        notes = research_paper(p, store.load_deepread(run_id, r["id"]),
                                               store.load_json("enrich", r["id"]) or {},
                                               criteria, cfg, llm, scholar)
                        store.save_json("research", r["id"], notes)
                    except APIConnectionError as e:  # skipped now, done when resumed
                        log.error("research skipped for %s: %s", r["id"], e)
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

    if llm.connection_failures:
        lost = (f"The model service couldn't be reached for part of this run (for example "
                f"because the Mac went to sleep or lost its connection), so "
                f"{llm.connection_failures} step(s) were skipped.")
        note = f"{note} {lost}" if note else lost

    # 4. Digest (written even after a budget stop, from whatever finished)
    progress.stage("digest")
    if adhoc:
        title = f"Ad-hoc digest: {label}"
        scope = (f"papers submitted {args.date_from} to {args.date_to} in "
                 f"{', '.join(criteria.categories)}")
    else:
        title = f"arXiv digest: {label}"
        scope = f"new papers scanned in {', '.join(criteria.categories)}"
    finish_hint = ("If the web app is running, it finishes this digest automatically once "
                   "the connection is back, redoing only the skipped steps.")
    path, overview_ok = write_digest(run_id, title, criteria, fmt, cfg, store, llm,
                                     {"fetched": store.count_for_run(run_id), "scope": scope,
                                      "usd": store.spend_for_run(run_id)}, note, finish_hint)
    incomplete = bool(llm.connection_failures) or not overview_ok
    n_matched = len(store.deepread_rows(run_id, relevant=True))
    usd = store.spend_for_run(run_id)
    store.save_run(run_id, status="ok", finished=datetime.now().isoformat(timespec="seconds"),
                   n_matched=n_matched, usd=usd, digest=str(path),
                   note="Incomplete: see the note at the top of the digest." if incomplete
                   else None, incomplete=int(incomplete))
    progress.finish("done", digest=path.with_suffix(".html").name,
                    detail=f"{n_matched} papers matched" + (" (incomplete)" if incomplete else ""))
    log.info("digest written: %s (%d papers, $%.2f)", path, n_matched, usd)
    if cfg["output"]["notify"]:
        what = "Ad-hoc digest" if adhoc else "arXiv digest"
        notify(f"{what} ready", f"{n_matched} papers matched (${usd:.2f})")


def cmd_refine(args):
    """Revises criteria.md from the +/- marks and message given on one digest."""
    os.chdir(ROOT)
    store = Store(ROOT / "state.db")
    if not store.run(args.run):
        raise SystemExit(f"no digest {args.run}")
    rid = args.rid
    if rid is None:  # started from the command line rather than the web app
        marks = [v["verdict"] for v in store.feedback_for_run(args.run).values() if v["verdict"]]
        if not marks and not args.message:
            raise SystemExit("nothing to go on: mark papers in the digest or give --message")
        rid = store.add_refinement(args.run, args.message or "", sum(v > 0 for v in marks),
                                   sum(v < 0 for v in marks))
        store.claim_feedback(args.run, rid)
        store.update_refinement(rid, pid=os.getpid())
    setup_logging(f"refine-{rid}")
    row = store.refinement(rid)
    cfg = load_config()
    spent = store.spend_for_run(args.run)
    try:
        out = refine_criteria(args.run, row["text"] or "", cfg, store, LLM(cfg, store, args.run),
                              rid)
    except Exception as e:
        store.update_refinement(rid, status="failed", error=f"{type(e).__name__}: {e}",
                                usd=store.spend_for_run(args.run) - spent)
        store.release_feedback(rid)  # the marks go back on the digest, to send again
        raise
    store.update_refinement(rid, status="done" if out["changed"] else "unchanged",
                            changes=json.dumps(out["changes"]), notes=out["notes"],
                            backup=out["backup"], after_hash=out["after_hash"],
                            diff=out["diff"], usd=store.spend_for_run(args.run) - spent)
    if out["changed"]:
        print("criteria.md revised:")
        for c in out["changes"]:
            print(f"  - {c}")
        print(f"The previous version is in {out['backup']}.")
    else:
        print("criteria.md left as it is.")
    if out["notes"]:
        print(f"Notes: {out['notes']}")


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
    run.add_argument("--resume", metavar="RUN_ID",
                     help="finish an earlier run that was cut short (e.g. 2026-11-03)")
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
    refine = sub.add_parser("refine", help="revise criteria.md from the feedback on a digest")
    refine.add_argument("--run", required=True, metavar="RUN_ID",
                        help="the digest the feedback was given on (e.g. 2026-11-05)")
    refine.add_argument("--message", help="what to tell the agent, besides the +/- marks")
    refine.add_argument("--rid", type=int, help=argparse.SUPPRESS)  # set by the web app
    refine.set_defaults(func=cmd_refine)
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

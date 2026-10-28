"""Gets the list of new arXiv papers in the chosen categories."""

import logging
import re
from datetime import date, timedelta
from email.utils import parsedate_to_datetime

import feedparser

from research_agent import arxiv_http

log = logging.getLogger(__name__)

RSS_URL = "https://rss.arxiv.org/rss/{cats}"
API_URL = "https://export.arxiv.org/api/query"
ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")


def _split_id(text: str) -> tuple[str, str]:
    m = ID_RE.search(text)
    if not m:
        raise ValueError(f"no arXiv id in {text!r}")
    return m.group(1), m.group(2) or "v1"


def fetch_rss(categories: list[str]) -> tuple[list[dict], str | None]:
    """Today's announcement: new submissions and cross-lists (not revisions).

    Returns (papers, announcement date). The date is None when arXiv published
    nothing today (weekends and holidays).
    """
    r = arxiv_http.get(RSS_URL.format(cats="+".join(categories)))
    r.raise_for_status()
    feed = feedparser.parse(r.text)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"could not read arXiv RSS feed: {feed.bozo_exception}")

    papers, announced = [], None
    for e in feed.entries:
        if e.get("arxiv_announce_type") not in ("new", "cross"):
            continue
        pid, version = _split_id(e.id)
        announced = parsedate_to_datetime(e.published).date().isoformat()
        abstract = e.summary.split("Abstract:", 1)[-1].strip()
        papers.append({
            "id": pid, "version": version, "title": " ".join(e.title.split()),
            "abstract": " ".join(abstract.split()),
            "authors": [a.strip() for a in e.get("author", "").split(",") if a.strip()],
            "categories": [t["term"] for t in e.get("tags", [])],
            "announced": announced, "url": f"https://arxiv.org/abs/{pid}",
        })
    return papers, announced


def fetch_api_window(categories: list[str], since: date, until: date) -> list[dict]:
    """Papers submitted between two dates, via the arXiv search API.

    Used to catch up when daily runs were missed (the RSS feed only has the
    latest announcement). Only first versions are kept.
    """
    cats = " OR ".join(f"cat:{c}" for c in categories)
    window = f"submittedDate:[{since:%Y%m%d}0000 TO {until:%Y%m%d}2359]"
    query = f"({cats}) AND {window}"
    papers, start, page = [], 0, 500
    while True:
        r = arxiv_http.get(API_URL, params={"search_query": query, "start": start,
                                            "max_results": page, "sortBy": "submittedDate",
                                            "sortOrder": "ascending"}, timeout=120)
        r.raise_for_status()
        feed = feedparser.parse(r.text)
        for e in feed.entries:
            pid, version = _split_id(e.id)
            if version != "v1":
                continue
            papers.append({
                "id": pid, "version": version, "title": " ".join(e.title.split()),
                "abstract": " ".join(e.summary.split()),
                "authors": [a.name for a in e.get("authors", [])],
                "categories": [t["term"] for t in e.get("tags", [])],
                "announced": e.published[:10], "url": f"https://arxiv.org/abs/{pid}",
            })
        if len(feed.entries) < page:
            break
        start += page  # arxiv_http spaces the page requests as arXiv asks
    return papers


def fetch_new(categories: list[str], last_announced: str | None, max_catchup_days: int,
              known: set[str]) -> tuple[list[dict], str | None]:
    """All papers not seen before: today's feed plus a catch-up window if runs were missed."""
    papers, announced = fetch_rss(categories)
    log.info("RSS: %d new/cross-listed papers (announced %s)", len(papers), announced)

    if last_announced and announced:
        gap = (date.fromisoformat(announced) - date.fromisoformat(last_announced)).days
        # Normal gaps are 1 day, or 3 over a weekend. Anything longer means missed runs.
        if gap > 3 or (gap > 1 and date.fromisoformat(announced).weekday() != 0):
            since = max(date.fromisoformat(last_announced) - timedelta(days=2),
                        date.today() - timedelta(days=max_catchup_days))
            log.info("catching up on missed runs: submissions since %s", since)
            papers += fetch_api_window(categories, since, date.today())

    unique = {}
    for p in papers:
        if p["id"] not in known and p["id"] not in unique:
            unique[p["id"]] = p
    return list(unique.values()), announced

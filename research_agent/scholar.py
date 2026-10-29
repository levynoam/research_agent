"""Free scholarly APIs: Semantic Scholar, OpenAlex and arXiv search.

Used both by the automatic enrichment step and as tools for the research agent.
All functions return plain dicts/lists and never raise on network trouble; they
return an {"error": ...} entry instead, so one flaky API can't break a run.
"""

import json
import logging
import os
import re
import threading
import time

import feedparser
import httpx

from research_agent import arxiv_http
from research_agent.config import ROOT
from research_agent.fulltext import get_author_block

log = logging.getLogger(__name__)

S2 = "https://api.semanticscholar.org"
OPENALEX = "https://api.openalex.org"
ARXIV_API = "https://export.arxiv.org/api/query"
MAX_FAILS_IN_A_ROW = 3


class Scholar:
    def __init__(self, cfg: dict):
        ecfg = cfg["enrich"]
        key = os.environ.get(ecfg.get("semantic_scholar_key_env") or "", "")
        self.s2_headers = {"x-api-key": key} if key else {}
        self.mailto = ecfg.get("openalex_mailto") or ""
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()  # one request at a time, so the spacing always holds
        self._s2_paper_blocked = False
        self._exhausted: set[str] = set()  # APIs that told us to come back much later
        self._fails: dict[str, int] = {}  # requests in a row that failed after all retries

    def _get(self, url: str, params=None, headers=None, tries: int = 4):
        with self._lock:
            out = self._get_locked(url, params, headers, tries)
        host = S2 if url.startswith(S2) else OPENALEX
        if out is not None and "error" in out:
            self._fails[host] = self._fails.get(host, 0) + 1
            if self._fails[host] >= MAX_FAILS_IN_A_ROW and host not in self._exhausted:
                log.warning("%s keeps failing; skipping it for the rest of this run", host)
                self._exhausted.add(host)
        else:
            self._fails[host] = 0
        return out

    def _pace(self, host: str, gap: float):
        wait = self._last.get(host, 0.0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _get_locked(self, url: str, params, headers, tries: int):
        # Space calls out. Semantic Scholar allows 1 request per second across all its
        # endpoints (with or without a key), so wait a bit more than 1 s after each reply.
        # OpenAlex asks for no more than 10 requests per second.
        host, gap = (S2, 1.1) if url.startswith(S2) else (OPENALEX, 0.2)
        if host in self._exhausted:
            return {"error": "rate limit reached earlier in this run"}
        for attempt in range(tries):
            self._pace(host, gap)
            try:
                r = httpx.get(url, params=params, headers=headers, timeout=30)
            except httpx.HTTPError as e:
                if attempt == tries - 1:
                    log.info("request failed %s: %s", url, e)
                    return {"error": str(e)}
                time.sleep(2)
                continue
            finally:
                self._last[host] = time.monotonic()
            if r.status_code == 429:
                try:
                    retry_after = int(r.headers.get("retry-after", "0"))
                except ValueError:
                    retry_after = 0
                if retry_after > 30:
                    # A daily quota, not a burst limit: stop asking until the next run.
                    log.warning("%s rate limit reached (retry after %ss); skipping it "
                                "for the rest of this run", host, retry_after)
                    self._exhausted.add(host)
                    return {"error": "rate limited"}
                if attempt == tries - 1:
                    # Semantic Scholar sometimes refuses a share of requests even when
                    # they are well spaced. Skip this one; a streak of these stops the host.
                    log.info("%s still rate limiting after %d tries: %s", host, tries, url)
                    return {"error": "rate limited"}
                time.sleep(max(retry_after, 2 ** (attempt + 1)))
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (401, 403) and host == S2 and headers:
                # A wrong or revoked key: don't keep sending it.
                log.warning("Semantic Scholar rejected the API key (HTTP %s); skipping it "
                            "for the rest of this run", r.status_code)
                self._exhausted.add(host)
                return {"error": "API key rejected"}
            try:
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as e:
                if attempt == tries - 1:
                    log.info("request failed %s: %s", url, e)
                    return {"error": str(e)}
                time.sleep(2)
        return {"error": "rate limited"}

    # --- Semantic Scholar ---------------------------------------------------
    def s2_paper(self, arxiv_id: str) -> dict | None:
        if self._s2_paper_blocked:
            return None
        fields = ("title,citationCount,url,authors.name,authors.hIndex,"
                  "authors.citationCount,authors.affiliations,authors.url")
        data = self._get(f"{S2}/graph/v1/paper/arXiv:{arxiv_id}", {"fields": fields},
                         self.s2_headers)
        if data and "error" in data and not self.s2_headers:
            # Without an API key this endpoint is often rate-limited for long stretches.
            # Stop trying for the rest of the run instead of waiting on every paper.
            log.info("Semantic Scholar is rate limiting; skipping its author data this run "
                     "(a free API key fixes this, see README)")
            self._s2_paper_blocked = True
        return data

    def similar_papers(self, arxiv_id: str, limit: int = 5) -> list[dict]:
        data = self._get(f"{S2}/recommendations/v1/papers/forpaper/arXiv:{arxiv_id}",
                         {"limit": limit, "fields": "title,year,url,citationCount,externalIds"},
                         self.s2_headers)
        if not data or "error" in data:
            return [data] if data else []
        out = []
        for p in data.get("recommendedPapers", []):
            arxiv = (p.get("externalIds") or {}).get("ArXiv")
            out.append({"title": p.get("title"), "year": p.get("year"),
                        "citations": p.get("citationCount"),
                        "url": f"https://arxiv.org/abs/{arxiv}" if arxiv else p.get("url")})
        return out

    # --- OpenAlex -----------------------------------------------------------
    def author_profile(self, name: str, candidates: int = 3) -> list[dict]:
        """Author lookup by name. Names can be ambiguous, so several candidates are returned."""
        params = {"search": name, "per-page": candidates}
        if self.mailto:
            params["mailto"] = self.mailto
        data = self._get(f"{OPENALEX}/authors", params)
        if not data or "error" in data:
            return [data] if data else []
        out = []
        for a in data.get("results", []):
            inst = a.get("last_known_institutions") or []
            topics = a.get("topics") or []
            out.append({
                "name": a.get("display_name"),
                "h_index": (a.get("summary_stats") or {}).get("h_index"),
                "citations": a.get("cited_by_count"),
                "works": a.get("works_count"),
                "institution": inst[0].get("display_name") if inst else None,
                "top_topics": [t.get("display_name") for t in topics[:3]],
                "openalex": a.get("id"),
            })
        return out

    # --- arXiv search -------------------------------------------------------
    def search_arxiv(self, query: str, max_results: int = 5) -> list[dict]:
        try:
            r = arxiv_http.get(ARXIV_API, params={"search_query": f"all:{query}",
                                                  "max_results": max_results,
                                                  "sortBy": "relevance"})
            r.raise_for_status()
        except (httpx.HTTPError, arxiv_http.ArxivBlocked) as e:
            return [{"error": str(e)}]
        feed = feedparser.parse(r.text)
        return [{"title": " ".join(e.title.split()), "url": e.link,
                 "published": e.published[:10], "abstract": " ".join(e.summary.split())[:600]}
                for e in feed.entries]


EXTRACT_AUTHORS = """Extract the authors and their affiliations from the header of an
arXiv paper. The author list, in order, is: {authors}

Reply with ONLY a JSON list, one entry per author in that order:
[{{"name": "...", "affiliations": [{{"full": "Carnegie Mellon University", "short": "CMU"}}]}}]
- "short" is the name people commonly use (e.g. CMU, MIT, NVIDIA, Google DeepMind, Tsinghua,
  KAIST, Meta); otherwise a shortened form of the full name.
- If the header lists affiliations without saying which author has which, give each author
  the affiliations you can attribute. If that is impossible, give all of them to everyone.
- Use [] when the header gives no affiliation for an author. Never guess from outside
  knowledge."""

MAX_NAME_MATCH_H_INDEX = 100
LOOKUP_FAILED = {"error": "lookup failed"}  # the API didn't answer; unlike "no match", not cached
AUTHOR_CACHE = ROOT / "cache" / "authors.json"
CACHE_DAYS = {"found": 30, "not_found": 7}


class AuthorCache:
    """Remembers author lookups between runs, so the same people aren't looked up daily."""

    def __init__(self):
        try:
            self.data = json.loads(AUTHOR_CACHE.read_text())
        except (OSError, ValueError):
            self.data = {}

    @staticmethod
    def key(name: str, affiliations: list[str]) -> str:
        return f"{name.lower()}|{'|'.join(sorted(a.lower() for a in affiliations))}"

    def get(self, key: str) -> dict | None:
        hit = self.data.get(key)
        if not hit:
            return None
        days = CACHE_DAYS["found" if hit.get("h_index") is not None else "not_found"]
        return hit if time.time() - hit["ts"] < days * 86400 else None

    def put(self, key: str, h_index, citations):
        self.data[key] = {"h_index": h_index, "citations": citations, "ts": time.time()}
        AUTHOR_CACHE.parent.mkdir(parents=True, exist_ok=True)
        AUTHOR_CACHE.write_text(json.dumps(self.data))

GENERIC_WORDS = {"university", "univ", "institute", "of", "the", "and", "for", "technology",
                 "science", "sciences", "inc", "ltd", "llc", "corporation", "corp", "lab",
                 "labs", "laboratory", "research", "school", "college", "department", "dept",
                 "national", "center", "centre", "academy", "state", "group", "ai", "at"}


def _key_words(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return {w for w in words if w not in GENERIC_WORDS and len(w) > 1}


def _same_institution(a: str, b: str) -> bool:
    a_l, b_l = a.lower(), b.lower()
    if a_l in b_l or b_l in a_l:
        return True
    return bool(_key_words(a) & _key_words(b))


def _openalex_matched(scholar: "Scholar", name: str, affiliations: list[str]) -> dict | None:
    """OpenAlex author with this name AND a matching institution, else None.

    Name-only matches are not trusted: common names have many namesakes.
    """
    if not affiliations:
        return None
    params = {"search": name, "per-page": 8,
              "select": "display_name,summary_stats,cited_by_count,works_count,"
                        "affiliations,last_known_institutions"}
    if scholar.mailto:
        params["mailto"] = scholar.mailto
    data = scholar._get(f"{OPENALEX}/authors", params)
    if not data or "error" in data:
        return LOOKUP_FAILED
    matches = []
    for c in data.get("results", []):
        insts = [i.get("display_name", "") for i in c.get("last_known_institutions") or []]
        insts += [a.get("institution", {}).get("display_name", "")
                  for a in c.get("affiliations") or []]
        if any(_same_institution(x, y) for x in affiliations for y in insts if x and y):
            matches.append(c)
    if len(matches) != 1:
        return None  # nobody, or several people with this name at that institution
    h = (matches[0].get("summary_stats") or {}).get("h_index") or 0
    if h >= MAX_NAME_MATCH_H_INDEX:
        # Very high values from a name search are almost always several people merged
        # into one profile (common names). Real cases come through Semantic Scholar.
        return None
    return matches[0]


def enrich_paper(p: dict, cfg: dict, scholar: Scholar, llm) -> dict:
    """Authors (with affiliations from the paper itself, and h-index) and similar papers."""
    ecfg = cfg["enrich"]
    author_cache = AuthorCache()
    limit = ecfg["max_authors_per_paper"]

    # 1. Who the authors are and where they work, read from the paper's own header.
    people = [{"name": n, "affiliations": []} for n in p["authors"]]
    block = get_author_block(p["id"], p["version"])
    if block:
        try:
            extracted = llm.chat_json(
                "enrich", cfg["models"]["authors"],
                EXTRACT_AUTHORS.format(authors=", ".join(p["authors"])), block, max_tokens=4000)
            if isinstance(extracted, list) and extracted:
                people = extracted
        except ValueError as e:
            log.info("could not read affiliations for %s: %s", p["id"], e)

    # 2. h-index: Semantic Scholar if it has the paper, else OpenAlex matched by name AND
    # institution (so a namesake elsewhere isn't picked up).
    s2 = scholar.s2_paper(p["id"])
    s2_authors = s2.get("authors", []) if s2 and "error" not in s2 else []
    # The first authors plus the last one (often the senior author).
    picks = list(range(min(limit - 1, len(people))))
    if len(people) > len(picks):
        picks.append(len(people) - 1)
    authors = []
    for i in picks:
        person = people[i]
        affs = person.get("affiliations") or []
        entry = {"name": person.get("name"), "h_index": None, "citations": None,
                 "affiliations": [a.get("short") or a.get("full") for a in affs],
                 "source": None}
        full_affs = [a.get("full") or a.get("short") or "" for a in affs]
        key = AuthorCache.key(entry["name"] or "", full_affs)
        cached = author_cache.get(key)
        if i < len(s2_authors) and s2_authors[i].get("hIndex") is not None:
            entry |= {"h_index": s2_authors[i]["hIndex"],
                      "citations": s2_authors[i].get("citationCount"),
                      "source": "semantic-scholar"}
        elif cached:
            entry |= {"h_index": cached["h_index"], "citations": cached["citations"],
                      "source": "openalex" if cached["h_index"] is not None else None}
        elif affs:
            match = _openalex_matched(scholar, entry["name"] or "", full_affs)
            if match is not LOOKUP_FAILED:
                h = (match.get("summary_stats") or {}).get("h_index") if match else None
                cites = match.get("cited_by_count") if match else None
                author_cache.put(key, h, cites)
                if match:
                    entry |= {"h_index": h, "citations": cites, "source": "openalex"}
        authors.append(entry)

    all_affiliations = []
    for person in people:
        for a in person.get("affiliations") or []:
            short = a.get("short") or a.get("full")
            if short and short not in all_affiliations:
                all_affiliations.append(short)

    similar = scholar.similar_papers(p["id"], ecfg["similar_papers"])
    return {"authors": authors, "n_authors": len(p["authors"]),
            "affiliations": all_affiliations,
            "similar": [s for s in similar if "error" not in s]}

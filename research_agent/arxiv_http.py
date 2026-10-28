"""The only way this project talks to arXiv. Every arXiv request MUST go through get().

arXiv's rules (checked 2026-10-28):
- API terms (https://info.arxiv.org/help/api/tou.html), for the API, RSS and OAI-PMH:
  "make no more than one request every three seconds" and "limit requests to a single
  connection at a time". The limits apply to all your machines combined.
- arxiv.org/robots.txt: "Crawl-delay: 15" for pages such as /abs, /pdf and /html,
  and /api, /search and others are disallowed.
- export.arxiv.org/robots.txt disallows everything, so only its /api/ endpoint is used.
- https://info.arxiv.org/help/robots.html: rapid requests that continue after a
  403 "Access denied" are treated as an attack.

How this module enforces that:
- One lock for all arXiv hosts, so there is only ever one request in flight.
- Spacing is measured from the END of the previous request: 3 s between API/RSS
  calls, 15 s whenever an arxiv.org page request is involved.
- The time of the last request is saved to disk, so back-to-back runs also stay spaced.
- Redirects are followed by hand, and each hop waits its turn.
- A 403 stops all arXiv traffic for the rest of the run. A 429/503 waits for the
  server's Retry-After (up to 5 min) and retries once. If that fails too, arXiv
  traffic stops for the run.
- Only paths that robots.txt allows are requested.
"""

import json
import logging
import threading
import time
from urllib.parse import urljoin, urlparse

import httpx

from research_agent.config import ROOT

log = logging.getLogger(__name__)

API_DELAY = 3.0    # API terms of use: one request every three seconds
SITE_DELAY = 15.0  # arxiv.org robots.txt: Crawl-delay 15
SITE_HOSTS = {"arxiv.org", "www.arxiv.org"}
ALLOWED = {
    "arxiv.org": ("/abs/", "/pdf/", "/html/"),
    "www.arxiv.org": ("/abs/", "/pdf/", "/html/"),
    "export.arxiv.org": ("/api/query",),
    "rss.arxiv.org": ("/rss/", "/atom/"),
}
USER_AGENT = "research-agent/0.1 (personal daily paper digest; low volume)"
STATE_FILE = ROOT / "logs" / ".arxiv_last_request.json"


class ArxivBlocked(Exception):
    """arXiv refused us (or is overloaded); no more arXiv requests this run."""


_lock = threading.Lock()
_blocked_reason: str | None = None
_count = 0


def is_arxiv(host: str | None) -> bool:
    return bool(host) and (host == "arxiv.org" or host.endswith(".arxiv.org"))


def blocked_reason() -> str | None:
    return _blocked_reason


def request_count() -> int:
    return _count


def _load_last() -> tuple[float, str]:
    try:
        d = json.loads(STATE_FILE.read_text())
        return float(d["t"]), d["host"]
    except (OSError, ValueError, KeyError):
        return 0.0, ""


def _save_last(host: str):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({"t": time.time(), "host": host}))


def _block(reason: str):
    global _blocked_reason
    _blocked_reason = reason
    log.error("arXiv: %s. Stopping all arXiv requests for this run.", reason)
    raise ArxivBlocked(reason)


def _check_allowed(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or host not in ALLOWED:
        raise ValueError(f"not an allowed arXiv URL: {url}")
    if not parsed.path.startswith(ALLOWED[host]):
        raise ValueError(f"arXiv robots.txt does not allow automated access to {url}")
    return host


def get(url: str, params: dict | None = None, timeout: float = 60) -> httpx.Response:
    """Rate-limited GET to arXiv. Raises ArxivBlocked if arXiv has pushed back this run."""
    global _count
    for _ in range(4):  # the original request plus at most 3 redirect hops
        host = _check_allowed(url)
        with _lock:
            if _blocked_reason:
                raise ArxivBlocked(_blocked_reason)
            for attempt in range(2):
                last_t, last_host = _load_last()
                delay = SITE_DELAY if (host in SITE_HOSTS or last_host in SITE_HOSTS) \
                    else API_DELAY
                wait = last_t + delay - time.time()
                if wait > 0:
                    time.sleep(wait)
                try:
                    r = httpx.get(url, params=params, timeout=timeout, follow_redirects=False,
                                  headers={"User-Agent": USER_AGENT})
                finally:
                    _count += 1
                    _save_last(host)
                if r.status_code == 403:
                    _block(f"access denied (HTTP 403) for {url}")
                if r.status_code in (429, 503):
                    if attempt == 1:
                        _block(f"still HTTP {r.status_code} after waiting")
                    try:
                        retry_after = min(int(r.headers.get("retry-after", "60")), 300)
                    except ValueError:
                        retry_after = 60
                    log.warning("arXiv: HTTP %s, waiting %ss as asked", r.status_code,
                                retry_after)
                    time.sleep(retry_after)  # still holding the lock: nothing else goes out
                    continue
                break
        if r.is_redirect:
            url, params = urljoin(url, r.headers.get("location", "")), None
            continue
        return r
    raise ValueError(f"too many redirects for {url}")

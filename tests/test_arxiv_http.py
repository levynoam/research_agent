"""Checks that the arXiv rate limiter behaves as arXiv requires.

Uses a fake HTTP layer, so no requests reach arXiv. Delays are scaled down to keep
the test fast. Run with:  .venv/bin/python -m unittest discover tests
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from research_agent import arxiv_http as ax


class FakeArxiv:
    """Records when each request starts and ends, and replies with scripted statuses."""

    def __init__(self, statuses=None, duration=0.05):
        self.statuses = list(statuses or [])
        self.duration = duration
        self.calls = []  # (url, start, end)
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def get(self, url, params=None, **kwargs):
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        start = time.time()
        time.sleep(self.duration)
        status = self.statuses.pop(0) if self.statuses else 200
        with self._lock:
            self.in_flight -= 1
        self.calls.append((url, start, time.time()))
        headers = {"retry-after": "0"} if status in (429, 503) else {}
        return httpx.Response(status, headers=headers, request=httpx.Request("GET", url))


class LimiterTest(unittest.TestCase):
    API, SITE = 0.3, 1.0  # scaled-down stand-ins for 3 s and 15 s

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(ax, "API_DELAY", self.API),
            mock.patch.object(ax, "SITE_DELAY", self.SITE),
            mock.patch.object(ax, "STATE_FILE", Path(self.tmp.name) / "last.json"),
            mock.patch.object(ax, "_blocked_reason", None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_with(self, fake, fn):
        with mock.patch.object(ax.httpx, "get", fake.get):
            return fn()

    def gaps(self, fake):
        """Time from the end of each request to the start of the next."""
        return [b[1] - a[2] for a, b in zip(fake.calls, fake.calls[1:])]

    def test_api_requests_spaced(self):
        fake = FakeArxiv()
        self.run_with(fake, lambda: [ax.get("https://export.arxiv.org/api/query")
                                     for _ in range(4)])
        self.assertTrue(all(g >= self.API - 0.01 for g in self.gaps(fake)), self.gaps(fake))

    def test_site_requests_use_crawl_delay(self):
        fake = FakeArxiv()
        self.run_with(fake, lambda: [ax.get("https://export.arxiv.org/api/query"),
                                     ax.get("https://arxiv.org/pdf/2610.00001v1"),
                                     ax.get("https://export.arxiv.org/api/query")])
        # Any gap next to an arxiv.org page request uses the longer delay.
        self.assertTrue(all(g >= self.SITE - 0.01 for g in self.gaps(fake)), self.gaps(fake))

    def test_parallel_threads_never_overlap(self):
        fake = FakeArxiv()

        def worker():
            ax.get("https://export.arxiv.org/api/query")

        def go():
            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.run_with(fake, go)
        self.assertEqual(fake.max_in_flight, 1)
        self.assertTrue(all(g >= self.API - 0.01 for g in self.gaps(fake)), self.gaps(fake))

    def test_spacing_survives_restart(self):
        fake = FakeArxiv()
        self.run_with(fake, lambda: ax.get("https://export.arxiv.org/api/query"))
        # A brand-new run still has to wait: the last request time is on disk.
        t0 = time.time()
        self.run_with(fake, lambda: ax.get("https://export.arxiv.org/api/query"))
        self.assertGreaterEqual(fake.calls[1][1] - fake.calls[0][2], self.API - 0.01)
        self.assertLess(t0, fake.calls[1][1])

    def test_403_stops_all_arxiv_traffic(self):
        fake = FakeArxiv(statuses=[403])
        with self.assertRaises(ax.ArxivBlocked):
            self.run_with(fake, lambda: ax.get("https://arxiv.org/pdf/2610.00001v1"))
        with self.assertRaises(ax.ArxivBlocked):
            self.run_with(fake, lambda: ax.get("https://export.arxiv.org/api/query"))
        self.assertEqual(len(fake.calls), 1)  # nothing was sent after the 403

    def test_429_retries_once_then_stops(self):
        fake = FakeArxiv(statuses=[429, 200])
        r = self.run_with(fake, lambda: ax.get("https://export.arxiv.org/api/query"))
        self.assertEqual(r.status_code, 200)
        fake = FakeArxiv(statuses=[503, 503])
        with self.assertRaises(ax.ArxivBlocked):
            self.run_with(fake, lambda: ax.get("https://export.arxiv.org/api/query"))
        self.assertEqual(len(fake.calls), 2)

    def test_robots_disallowed_paths_refused(self):
        fake = FakeArxiv()
        for url in ["https://arxiv.org/search/?query=x", "https://arxiv.org/api/query",
                    "https://export.arxiv.org/abs/2610.00001", "http://arxiv.org/abs/1",
                    "https://arxiv.org/e-print/2610.00001"]:
            with self.assertRaises(ValueError, msg=url):
                self.run_with(fake, lambda: ax.get(url))
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()

"""Gets the full text of an arXiv paper (HTML version first, PDF as fallback).

Downloads go through arxiv_http (one at a time, 15 s apart, per arXiv's robots.txt)
and are cached on disk, so re-running a day never downloads the same paper twice.
"""

import io
import json
import logging
import re
from urllib.parse import urljoin

import trafilatura
from bs4 import BeautifulSoup
from pypdf import PdfReader

from research_agent import arxiv_http
from research_agent.config import ROOT

log = logging.getLogger(__name__)

CACHE = ROOT / "cache" / "fulltext"
CODE_LINK_RE = re.compile(r"https?://(?:www\.)?(?:github\.com|gitlab\.com|huggingface\.co)"
                          r"/[\w.\-]+/[\w.\-]+")


def get_fulltext(pid: str, version: str) -> tuple[str, str]:
    """Returns (text, source) where source is 'html', 'pdf' or 'abstract-only'."""
    CACHE.mkdir(parents=True, exist_ok=True)
    for source in ("html", "pdf"):
        cached = CACHE / f"{pid}{version}.{source}.txt"
        if cached.exists():
            return cached.read_text(), source

    text, source = "", "abstract-only"
    try:
        r = arxiv_http.get(f"https://arxiv.org/html/{pid}{version}")
        _save_html_extras(pid, version, r)
        if r.status_code == 200:
            html_text = trafilatura.extract(r.text, include_tables=True, include_links=False)
            if html_text and len(html_text) > 5000:
                text, source = html_text, "html"
        if not text:
            r = arxiv_http.get(f"https://arxiv.org/pdf/{pid}{version}", timeout=120)
            r.raise_for_status()
            reader = PdfReader(io.BytesIO(r.content))
            pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
            if len(pdf_text) > 2000:
                text, source = pdf_text, "pdf"
    except arxiv_http.ArxivBlocked:
        pass  # summarize from the abstract instead
    except Exception as e:
        log.warning("full text unavailable for %s: %s", pid, e)

    if text:
        (CACHE / f"{pid}{version}.{source}.txt").write_text(text)
    return text, source


def extract_figures(html: str, page_url: str) -> list[dict]:
    """The paper's figures from arXiv's HTML version: label, image URL and caption."""
    soup = BeautifulSoup(html, "html.parser")
    figures = []
    for fig in soup.find_all("figure", class_="ltx_figure"):
        if fig.find_parent("figure"):
            continue  # a sub-panel; its parent figure is listed instead
        media = fig.find("img", src=True) or fig.find("object", attrs={"data": True})
        if not media:
            continue  # e.g. a boxed prompt or algorithm typeset as a "figure"
        src = media.get("src") or media.get("data")
        caption_el = fig.find("figcaption")
        caption = " ".join(caption_el.get_text(" ").split()) if caption_el else ""
        tag = caption_el.find(class_="ltx_tag_figure") if caption_el else None
        label = figure_label(tag.get_text() if tag else "", len(figures) + 1)
        figures.append({"label": label, "url": urljoin(page_url, src),
                        "caption": caption[:700]})
    return figures


def figure_label(tag_text: str, position: int) -> str:
    """'Fig. 3', 'Figure 3.:' and similar all become 'Figure 3'."""
    m = re.search(r"\d+", tag_text)
    return f"Figure {m.group()}" if m else f"Figure {position}"


def extract_author_block(html: str) -> str:
    """Raw text of the author/affiliation block at the top of arXiv's HTML version."""
    block = BeautifulSoup(html, "html.parser").find(class_="ltx_authors")
    return " ".join(block.get_text(" ").split())[:4000] if block else ""


def _save_html_extras(pid: str, version: str, r):
    """From the HTML page (or its absence): the figure list and the author block."""
    CACHE.mkdir(parents=True, exist_ok=True)
    ok = r.status_code == 200
    figures = extract_figures(r.text, str(r.url)) if ok else []
    (CACHE / f"{pid}{version}.figures.json").write_text(json.dumps(figures))
    (CACHE / f"{pid}{version}.authors.txt").write_text(extract_author_block(r.text) if ok else "")


def _ensure_html_extras(pid: str, version: str, name: str):
    """Makes sure a cached HTML extra exists: one rate-limited request if it doesn't."""
    if not (CACHE / f"{pid}{version}.{name}").exists():
        _save_html_extras(pid, version, arxiv_http.get(f"https://arxiv.org/html/{pid}{version}"))


def get_figures(pid: str, version: str) -> list[dict]:
    """Figure list for a paper (empty if the paper has no HTML version)."""
    try:
        _ensure_html_extras(pid, version, "figures.json")
    except arxiv_http.ArxivBlocked:
        return []
    return json.loads((CACHE / f"{pid}{version}.figures.json").read_text())


def get_author_block(pid: str, version: str) -> str:
    """Author names and affiliations as raw text, from the HTML page or the PDF's first page."""
    try:
        _ensure_html_extras(pid, version, "authors.txt")
    except arxiv_http.ArxivBlocked:
        return ""
    block = (CACHE / f"{pid}{version}.authors.txt").read_text()
    if block:
        return block
    # No author block on the HTML page: the PDF's title page lists authors and affiliations.
    pdf_text = CACHE / f"{pid}{version}.pdf.txt"
    title_page = CACHE / f"{pid}{version}.titlepage.txt"
    if pdf_text.exists():
        return pdf_text.read_text()[:3000]
    if not title_page.exists():
        text = ""
        try:
            r = arxiv_http.get(f"https://arxiv.org/pdf/{pid}{version}", timeout=120)
            if r.status_code == 200:
                text = (PdfReader(io.BytesIO(r.content)).pages[0].extract_text() or "")[:3000]
        except arxiv_http.ArxivBlocked:
            return ""
        except Exception as e:
            log.info("no title page for %s: %s", pid, e)
        title_page.write_text(text)  # cached even if empty, so it's tried only once
    return title_page.read_text()


def code_links(text: str) -> list[str]:
    links = []
    for m in CODE_LINK_RE.findall(text):
        m = m.rstrip(".")
        if m not in links:
            links.append(m)
    return links[:15]

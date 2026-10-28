"""Step 3: read the full text of each candidate, re-check relevance, summarize.

What the summary contains is set by summary_format.md, read fresh on every run.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from research_agent.fulltext import code_links, get_fulltext
from research_agent.llm import BudgetExceeded

log = logging.getLogger(__name__)

SYSTEM = """You are a careful research assistant reading a new arXiv paper for a researcher.
Their research criteria:

<criteria>
{criteria}
</criteria>

First decide whether the paper really matches one of the topics, now that you can see the
full text (the screening step only saw the abstract and leans toward false positives).
Then summarize it following the researcher's format instructions:

<format>
TL;DR: {tldr}

Sections, in this order:
{sections}

Style:
{style}
</format>

Reply with ONLY this JSON:
{{
  "relevant": true/false,
  "score": 0-10,
  "topics": ["exact topic names from: {topic_names}"],
  "tldr": "the TL;DR, as described above",
  "sections": {{{section_keys}}},
  "confidence": "high/medium/low - how sure you are about relevance and the summary",
  "code_url": "URL of the authors' own released code for THIS paper, or null. Repo links
               found in the text are listed below; most are usually cited third-party work."
}}
Each section value is Markdown text (use "- " bullets where the section asks for a list).
Use the section names exactly as given."""


def build_system(criteria, fmt) -> str:
    sections = "\n".join(f"- {name}: {desc}" for name, desc in fmt.sections)
    keys = ", ".join(f'"{name}": "..."' for name in fmt.section_names())
    return SYSTEM.format(criteria=criteria.text, topic_names=json.dumps(criteria.topics),
                         tldr=fmt.tldr, sections=sections, style=fmt.style, section_keys=keys)


def summarize(p: dict, criteria, fmt, cfg: dict, llm) -> tuple[dict, str]:
    """Full-reads one paper. Returns (result, full-text source)."""
    text, source = get_fulltext(p["id"], p["version"])
    body = text[:cfg["deepread"]["max_chars"]] if text else "(Full text unavailable; abstract only.)"
    links = code_links(text)
    user = (f"Title: {p['title']}\nAuthors: {', '.join(p['authors'])}\n"
            f"Abstract: {p['abstract']}\n\n<full_text source=\"{source}\">\n{body}\n</full_text>"
            f"\n\nRepository links found in the text: {json.dumps(links) if links else 'none'}")
    result = llm.chat_json("deepread", cfg["models"]["deepread"], build_system(criteria, fmt),
                           user, max_tokens=3000)
    result["topics"] = [t for t in result.get("topics", []) if t in criteria.topics]
    if result.get("code_url") not in links:
        result["code_url"] = None  # only trust links that really appear in the paper
    sections = result.get("sections") or {}
    # Keep the sections in the order of summary_format.md, dropping any the model invented.
    result["sections"] = {n: sections[n] for n in fmt.section_names() if sections.get(n)}
    return result, source


def deepread(candidates: list[dict], criteria, fmt, cfg: dict, llm, store, run_id: str,
             progress) -> None:
    dcfg = cfg["deepread"]

    def read_one(c):
        if store.load_deepread(run_id, c["id"]):
            return  # already done in an earlier (interrupted) run
        p = store.paper(c["id"])
        result, source = summarize(p, criteria, fmt, cfg, llm)
        score = int(result.get("score", 0))
        relevant = bool(result.get("relevant")) and score >= dcfg["min_score"]
        store.save_deepread(run_id, p["id"], result, relevant, score, source)
        log.info("deepread %s: relevant=%s score=%s (%s)", p["id"], relevant, score, source)

    with ThreadPoolExecutor(max_workers=dcfg["workers"]) as pool:
        futures = [pool.submit(read_one, c) for c in candidates]
        for f in as_completed(futures):
            try:
                f.result()
                progress.advance()
            except BudgetExceeded:
                raise
            except Exception as e:
                log.error("deepread failed: %s", e)

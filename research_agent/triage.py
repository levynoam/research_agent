"""Step 2: score every new abstract against the topics in criteria.md."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from research_agent.llm import BudgetExceeded

log = logging.getLogger(__name__)

SYSTEM = """You screen new arXiv papers for a researcher. Below are their research criteria.

<criteria>
{criteria}
</criteria>

You will get a JSON list of papers (id, title, abstract). For EACH paper, judge how well
it matches the topics above, from the title and abstract alone.

Scoring (0-10):
- 9-10: squarely on one of the topics; the researcher would definitely want to read it
- 6-8: clearly relevant to a topic, or a strong partial match
- 3-5: tangential (same broad area, different focus)
- 0-2: not relevant

Favor recall: if a paper might plausibly match, score it at least 6. A later step reads the
full text and filters out false positives.

Reply with ONLY a JSON list, one entry per paper, same order:
[{{"id": "...", "score": 0-10, "topics": ["exact topic name", ...], "reason": "<= 20 words"}}]
Use topic names exactly as written: {topic_names}. Use [] when nothing matches."""


def _batch_user(papers: list[dict]) -> str:
    items = [{"id": p["id"], "title": p["title"], "abstract": p["abstract"][:2000]}
             for p in papers]
    return json.dumps(items, ensure_ascii=False)


def triage(papers: list[dict], criteria, cfg: dict, llm, store) -> int:
    """Scores papers in batches and saves results. Returns how many were scored."""
    tcfg = cfg["triage"]
    system = SYSTEM.format(criteria=criteria.text, topic_names=json.dumps(criteria.topics))
    batches = [papers[i:i + tcfg["batch_size"]]
               for i in range(0, len(papers), tcfg["batch_size"])]

    def run_batch(batch):
        result = llm.chat_json("triage", cfg["models"]["triage"], system, _batch_user(batch),
                               max_tokens=100 * len(batch) + 500)
        by_id = {str(r.get("id")): r for r in result if isinstance(r, dict)}
        for p in batch:
            r = by_id.get(p["id"])
            if r is None:
                log.warning("triage: no answer for %s", p["id"])
                continue
            topics = [t for t in r.get("topics", []) if t in criteria.topics]
            store.save_triage(p["id"], int(r.get("score", 0)), topics, r.get("reason", ""))
        return len(by_id)

    done = 0
    with ThreadPoolExecutor(max_workers=tcfg["workers"]) as pool:
        futures = [pool.submit(run_batch, b) for b in batches]
        for f in as_completed(futures):
            try:
                done += f.result()
            except BudgetExceeded:
                raise
            except Exception as e:  # one bad batch shouldn't sink the run
                log.error("triage batch failed: %s", e)
    log.info("triage: scored %d of %d papers", done, len(papers))
    return done

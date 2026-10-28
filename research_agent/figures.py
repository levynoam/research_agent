"""Step 3c: one image per paper. Reuse a figure from the paper if it shows the main idea
well; otherwise generate an illustration of the main claim.

The rules for choosing are in the '# Figure' section of summary_format.md. Generated
images are checked by a second model against the summary (wrong numbers or labels mean
one retry, then the image is dropped), and the digest labels them as AI-generated.
"""

import base64
import json
import logging
from pathlib import Path

from research_agent.fulltext import figure_label, get_figures
from research_agent.llm import BudgetExceeded

log = logging.getLogger(__name__)

CHOOSE = """You pick the one image that best conveys a new paper's main claim, for a research
digest read by an expert. The researcher's rules:

<figure_rules>
{rules}
</figure_rules>

You get the paper's summary and the captions of the paper's own figures.
Reply with ONLY this JSON:
{{"choice": "paper" or "generate",
 "figure": number in [brackets] of the chosen paper figure (only if choice is paper),
 "image_prompt": "prompt for an image model (only if choice is generate)",
 "caption": "one sentence: what the image shows"}}

If you generate, write image_prompt for a clean scientific diagram: white background,
flat vector style, legible labels. Show the main claim, for example a sketch of the
method next to the headline result against its baseline. Use ONLY facts and numbers
stated in the summary. Keep text in the image short (about 25 words at most). No
logos, no people, no paper title."""

VERIFY = """You check an AI-generated illustration for a research digest against the paper's
summary. It must be accurate and legible: every number, label and relationship shown
must match the summary, and nothing may be invented. Small stylistic simplifications
are fine. Reply with ONLY this JSON:
{"ok": true or false, "problems": "what is wrong in at most 40 words, or empty"}"""


def _summary_text(p: dict, s: dict) -> str:
    parts = [f"Title: {p['title']}", f"TL;DR: {s.get('tldr', '')}"]
    for name, text in (s.get("sections") or {}).items():
        parts.append(f"{name}: {text}")
    for key in ("contributions", "method", "results"):  # summaries in the older format
        if s.get(key):
            parts.append(f"{key}: {s[key]}")
    return "\n".join(parts)


def _generate_checked(prompt: str, summary: str, cfg, llm) -> tuple[bytes | None, str]:
    """Generates an image and has it checked; one retry with the problems fixed."""
    icfg = cfg["images"]
    problems = ""
    for attempt in range(2):
        full_prompt = prompt + (f"\n\nFix these problems from a previous attempt: {problems}"
                                if problems else "")
        png = llm.generate_image(icfg["model"], full_prompt, icfg["size"])
        if not icfg.get("verify", True):
            return png, ""
        b64 = base64.b64encode(png).decode()
        try:
            verdict = llm.chat_json("figures", cfg["models"]["figures"], VERIFY, [
                {"type": "text", "text": f"Summary:\n{summary}"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}],
                max_tokens=1500)
        except ValueError:  # unreadable verdict: don't trust the image
            verdict = {"ok": False, "problems": "the accuracy check gave no clear answer"}
        if verdict.get("ok"):
            return png, ""
        problems = verdict.get("problems", "inaccurate")
        log.info("generated image rejected (attempt %d): %s", attempt + 1, problems)
    return None, problems


def make_figures(paper_ids: list[str], run_date: str, fmt, cfg: dict, llm, store) -> None:
    icfg = cfg["images"]
    out_dir = Path(cfg["output"]["digest_dir"]) / "images" / run_date
    generated = sum(1 for pid in paper_ids
                    if (store.load_json("figures", pid) or {}).get("choice") == "generate")

    for pid in paper_ids:
        if store.load_json("figures", pid):
            continue
        p, s = store.paper(pid), store.load_json("deepread", pid)
        try:
            figs = get_figures(p["id"], p["version"])
            summary = _summary_text(p, s)
            listing = "\n".join(f"[{i}] {f['caption']}" for i, f in enumerate(figs, 1)) or \
                "(none available: the paper has no HTML version, or no figures)"
            pick = llm.chat_json("figures", cfg["models"]["figures"],
                                 CHOOSE.format(rules=fmt.figure),
                                 f"{summary}\n\nThe paper's figures:\n{listing}", max_tokens=800)

            record = {"choice": "none", "caption": pick.get("caption", "")}
            try:
                index = int(str(pick.get("figure", "")).strip("[] "))
                chosen = figs[index - 1] if 1 <= index <= len(figs) else None
            except ValueError:
                chosen = None
            if pick.get("choice") == "paper" and chosen:
                record |= {"choice": "paper", "label": figure_label(chosen["label"], index),
                           "url": chosen["url"]}
            elif pick.get("image_prompt") and icfg["enabled"] \
                    and generated < icfg["max_generated_per_day"]:
                png, problems = _generate_checked(pick["image_prompt"], summary, cfg, llm)
                if png:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    (out_dir / f"{pid}.png").write_bytes(png)
                    record |= {"choice": "generate", "path": f"images/{run_date}/{pid}.png",
                               "prompt": pick["image_prompt"]}
                    generated += 1
                else:
                    record["dropped"] = problems
            store.save_json("figures", pid, record)
            log.info("figure %s: %s", pid, record["choice"] if record["choice"] != "paper"
                     else f"paper {record['label']}")
        except BudgetExceeded:
            raise
        except Exception as e:
            log.error("figure step failed for %s: %s", pid, e)

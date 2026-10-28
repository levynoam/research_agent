"""Step 4: write the daily digest as Markdown and HTML."""

import html
import json
import logging
from datetime import date
from pathlib import Path

import markdown

log = logging.getLogger(__name__)

SYSTEM = """You write the opening of a daily research digest for an expert researcher.
You get today's matched arXiv papers (already summarized). Follow their instructions:

<overview_instructions>
{overview}
</overview_instructions>

<contents_table_instructions>
{contents}
</contents_table_instructions>

<style>
{style}
</style>

Reply with ONLY this JSON:
{{"brief": "the 'Today in brief' text",
 "top_picks": [{{"id": "...", "why": "why to read this first"}}],
 "affiliations": {{"<paper id>": "what the contents table's Affiliations cell shows", ...}}}}
Pick at most {top_picks} top picks (an empty list if 0), ordered by importance. Each paper's
"affiliations" field lists its authors' institutions as read from the paper; use only those."""

DEFAULT_LAYOUT = ["brief", "top_picks", "contents", "papers", "screened_out"]
COLUMNS = {  # contents table columns that summary_format.md can choose from
    "paper": "Paper", "topic": "Topic", "score": "Score", "h_index": "Avg h-index",
    "affiliations": "Affiliations", "tldr": "TL;DR",
}

# Summaries written before summary_format.md existed used fixed fields.
LEGACY_SECTIONS = [("Contributions", "contributions"), ("Method", "method"),
                   ("Results", "results"), ("Why it matters", "why_it_matters"),
                   ("Caveats", "limitations")]

CSS = """body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:860px;
margin:2em auto;padding:0 16px;line-height:1.5;color:#1d1d1f;background:#fff}
h1{font-size:1.6em}h2{border-bottom:1px solid #ddd;padding-bottom:.2em;margin-top:2em}
h3{font-size:1.05em;margin-bottom:.2em}a{color:#0a58ca}.meta{color:#666}
@media (prefers-color-scheme:dark){body{background:#1c1c1e;color:#e5e5e7}
a{color:#6ea8fe}.meta{color:#999}h2{border-color:#444}}
figure.paper-fig{margin:1em 0}figure.paper-fig img{max-width:100%;max-height:520px;
display:block;background:#fff;border:1px solid #ddd;border-radius:6px;padding:6px}
figure.paper-fig figcaption{font-size:.88em;color:#666;margin-top:.4em}
table{border-collapse:collapse;font-size:.88em;width:100%;display:block;overflow-x:auto}
th,td{border-bottom:1px solid #ddd;padding:.35em .5em;text-align:left;vertical-align:top}
th{white-space:nowrap}@media (prefers-color-scheme:dark){th,td{border-color:#444}}"""


def _authors_line(authors: list[str]) -> str:
    return ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")


def _sections(s: dict) -> list[tuple[str, str]]:
    if "sections" in s:
        return list(s["sections"].items())
    out = []
    for name, key in LEGACY_SECTIONS:
        value = s.get(key)
        if isinstance(value, list):
            value = "\n".join(f"- {v}" for v in value)
        if value:
            out.append((name, value))
    return out


def _render_figure(fig: dict | None) -> str:
    if not fig or fig.get("choice") not in ("paper", "generate"):
        return ""
    caption = html.escape(fig.get("caption", ""))
    if fig["choice"] == "paper":
        src, label = fig["url"], f"{html.escape(fig['label'])} from the paper"
    else:
        src, label = fig["path"], "AI-generated illustration (not from the paper)"
    return (f"<figure class=\"paper-fig\"><img src=\"{html.escape(src)}\" alt=\"{caption}\" "
            f"loading=\"lazy\"><figcaption><b>{label}:</b> {caption}</figcaption></figure>\n")


def _render_paper(p: dict, s: dict, e: dict | None, r: dict | None, fig: dict | None,
                  opts: dict) -> str:
    lines = [f"<a id=\"p-{p['id']}\"></a>\n", f"### [{p['title']}]({p['url']})"]
    meta = [_authors_line(p["authors"]), f"arXiv:{p['id']}", f"score {s.get('score')}/10"]
    if s.get("code_url"):
        meta.append(f"[code]({s['code_url']})")
    lines.append(f"<span class=\"meta\">{' · '.join(meta)}</span>\n")
    lines.append(f"**TL;DR** {s.get('tldr', '')}\n")
    if opts.get("show_figures", True):
        lines.append(_render_figure(fig))
    for name, text in _sections(s):
        text = str(text).strip()
        label = name if name[-1:] in "?:!." else f"{name}:"
        if "\n" in text or text.startswith("- "):  # lists and multi-paragraph text
            lines.append(f"**{label}**\n\n{text}\n")
        else:
            lines.append(f"**{label}** {text}\n")
    if not opts.get("show_authors", True):
        e = {**(e or {}), "authors": []}
    if not opts.get("show_similar_papers", True):
        e = {**(e or {}), "similar": []}
    if not opts.get("show_research_notes", True):
        r = None
    if e and e.get("authors"):
        parts = []
        for a in e["authors"]:
            bits = [f"h-index {a['h_index']}"] if a.get("h_index") is not None else []
            if a.get("affiliations"):
                bits.append(a["affiliations"][0])
            parts.append(a["name"] + (f" ({', '.join(bits)})" if bits else ""))
        lines.append(f"**Authors:** {'; '.join(parts)}\n")
    if e and e.get("similar"):
        rel = ", ".join(f"[{x['title']}]({x['url']})" for x in e["similar"][:3] if x.get("url"))
        if rel:
            lines.append(f"**Similar papers:** {rel}\n")
    if r:
        lines.append(f"**Context (research agent):** {r.get('novelty', '')}")
        for w in (r.get("prior_work") or [])[:4]:
            lines.append(f"- [{w.get('title')}]({w.get('url')}): {w.get('relation', '')}")
        for key, label in [("author_context", "Authors"), ("outside_signals", "Signals")]:
            if r.get(key):
                lines.append(f"- *{label}:* {r[key]}")
        lines.append("")
    return "\n".join(lines)


def _cell(text) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _avg_h_index(e: dict | None) -> str:
    known = [a["h_index"] for a in (e or {}).get("authors", []) if a.get("h_index") is not None]
    if not known:
        return "—"
    looked_up = len((e or {}).get("authors", []))
    return f"{sum(known) / len(known):.0f} ({len(known)}/{looked_up})"


def _contents_table(ordered: list, columns: list[str], affiliations: dict) -> list[str]:
    columns = [c for c in columns if c in COLUMNS] or ["paper", "topic", "score"]
    rows = ["## Contents", "| # | " + " | ".join(COLUMNS[c] for c in columns) + " |",
            "|---|" + "---|" * len(columns)]
    for i, (p, s, e, _r, _f) in enumerate(ordered, 1):
        values = {
            "paper": f"[{_cell(p['title'])}](#p-{p['id']})",
            "topic": _cell((s.get("topics") or ["Other"])[0]),
            "score": s.get("score", ""),
            "h_index": _avg_h_index(e),
            "affiliations": _cell(affiliations.get(p["id"], "")),
            "tldr": _cell(s.get("tldr", "")),
        }
        rows.append(f"| {i} | " + " | ".join(str(values[c]) for c in columns) + " |")
    if "h_index" in columns:
        rows.append("\n<span class=\"meta\">Avg h-index: average over the authors whose "
                    "profile was found (found/looked up). Profiles are matched by name and "
                    "institution, and ambiguous or implausible matches are skipped; sources differ, so "
                    "treat it as a rough signal.</span>")
    return rows + [""]


def write_digest(run_date: str, criteria, fmt, cfg: dict, store, llm, stats: dict,
                 note: str | None = None) -> Path:
    opts = fmt.options
    layout = opts.get("layout") or DEFAULT_LAYOUT
    columns = opts.get("contents_columns") or ["paper", "topic", "score", "h_index",
                                               "affiliations"]
    max_picks = int(opts.get("top_picks", 5))
    rows = store.query(
        "SELECT d.id, d.data FROM deepread d JOIN papers p ON p.id=d.id "
        "WHERE p.run_date=? AND d.relevant=1 ORDER BY d.score DESC", (run_date,))
    items = []
    for row in rows:
        p = store.paper(row["id"])
        items.append((p, json.loads(row["data"]), store.load_json("enrich", p["id"]),
                      store.load_json("research", p["id"]), store.load_json("figures", p["id"])))
    rejected = store.query(
        "SELECT p.id, p.title, p.url FROM deepread d JOIN papers p ON p.id=d.id "
        "WHERE p.run_date=? AND d.relevant=0 ORDER BY d.score DESC", (run_date,))

    brief, picks, affiliations = "", [], {}
    if items:
        try:
            listing = [{"id": p["id"], "title": p["title"], "topics": s.get("topics"),
                        "score": s.get("score"), "tldr": s.get("tldr"),
                        "affiliations": (e or {}).get("affiliations", [])}
                       for p, s, e, *_ in items]
            system = SYSTEM.format(overview=fmt.overview, contents=fmt.contents,
                                   style=fmt.style, top_picks=max_picks)
            out = llm.chat_json("digest", cfg["models"]["digest"], system,
                                json.dumps(listing, ensure_ascii=False), max_tokens=3000)
            brief, picks = out.get("brief", ""), out.get("top_picks", [])[:max_picks]
            affiliations = out.get("affiliations") or {}
        except Exception as e:  # budget hit or model trouble: digest without the overview
            log.warning("digest overview skipped: %s", e)

    # Each paper appears once, under its first matched topic.
    groups: dict[str, list] = {t: [] for t in criteria.topics}
    groups["Other"] = []
    for item in items:
        topics = item[1].get("topics") or ["Other"]
        groups.setdefault(topics[0], []).append(item)
    ordered = [item for group in groups.values() for item in group]

    by_id = {p["id"]: p for p, *_ in items}
    picks = [x for x in picks if x.get("id") in by_id]
    blocks: dict[str, list[str]] = {}
    if brief:
        blocks["brief"] = ["## Today in brief", brief, ""]
    if picks:
        blocks["top_picks"] = ["## Top picks"] + [
            f"{i}. **[{by_id[x['id']]['title']}](#p-{x['id']})**: {x.get('why', '')}"
            for i, x in enumerate(picks, 1)] + [""]
    if items:
        blocks["contents"] = _contents_table(ordered, columns, affiliations)
        blocks["papers"] = []
        for topic, group in groups.items():
            if group:
                blocks["papers"].append(f"## {topic}")
                blocks["papers"] += [_render_paper(*item, opts) for item in group]
    if rejected and opts.get("show_screened_out", True):
        blocks["screened_out"] = [
            "## Screened out after full read",
            "These looked relevant from the abstract but not from the full text. "
            "If any should have made it, add them to criteria.md as examples.\n"
        ] + [f"- [{r['title']}]({r['url']})" for r in rejected]

    d = date.fromisoformat(run_date)
    md = [f"# arXiv digest: {d:%A %d %B %Y}",
          f"<span class=\"meta\">{stats.get('fetched', 0)} new papers scanned in "
          f"{', '.join(criteria.categories)} · {len(items)} matched · "
          f"${stats.get('usd', 0):.2f} spent</span>\n"]
    if note:
        md.append(f"> **Note:** {note}\n")
    if not items:
        md.append("No papers matched your topics today.\n")
    for name in layout:
        md += blocks.get(name, [])

    text = "\n".join(md) + "\n"
    out_dir = Path(cfg["output"]["digest_dir"])
    out_dir.mkdir(exist_ok=True)
    md_path = out_dir / f"{run_date}.md"
    md_path.write_text(text)
    body = markdown.markdown(text, extensions=["extra", "sane_lists"])
    (out_dir / f"{run_date}.html").write_text(
        f"<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>arXiv digest {run_date}</title><style>{CSS}</style></head>"
        f"<body>{body}</body></html>")
    return md_path

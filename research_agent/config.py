"""Loads config.yaml (technical settings) and criteria.md (research interests)."""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CRITERIA = ROOT / "criteria.md"
FORMAT = ROOT / "summary_format.md"


@dataclass
class Criteria:
    categories: list[str]
    topics: list[str]
    text: str  # the full markdown body, given to the models verbatim


@dataclass
class SummaryFormat:
    options: dict                 # display switches from the '---' block
    tldr: str                     # how to write the TL;DR line
    sections: list[tuple[str, str]]  # (section name, what to write there), in display order
    style: str
    overview: str                 # how to write "Today in brief" and the top picks
    figure: str                   # how to pick or generate each paper's image
    contents: str                 # how to fill the contents table

    def section_names(self) -> list[str]:
        return [name for name, _ in self.sections]


def ensure_from_template(path: Path) -> Path:
    """Your own criteria.md and summary_format.md are not tracked by git (they say what
    you work on). On a fresh copy they start out as copies of the *.example.md files."""
    template = path.with_name(f"{path.stem}.example.md")
    if not path.exists() and template.exists():
        shutil.copyfile(template, path)
    return path


def _split_front_matter(path: Path) -> tuple[dict, str]:
    raw = path.read_text()
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    if not match:
        raise ValueError(f"{path} must start with a '---' block")
    return yaml.safe_load(match.group(1)) or {}, match.group(2).strip()


def _top_sections(body: str) -> dict[str, str]:
    """Splits markdown into its '# ' sections: {heading (lowercase): text}."""
    out, name = {}, None
    for line in body.splitlines():
        if line.startswith("# "):
            name = line[2:].strip().lower()
            out[name] = ""
        elif name:
            out[name] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


def load_format(path: Path = FORMAT) -> SummaryFormat:
    options, body = _split_front_matter(ensure_from_template(path))
    parts = _top_sections(body)
    sections, name = [], None
    for line in parts.get("sections", "").splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            sections.append([name, ""])
        elif name:
            sections[-1][1] += line + "\n"
    if not sections:
        raise ValueError(f"{path}: no '## ' sections found under '# Sections'")
    return SummaryFormat(
        options=options, tldr=parts.get("tl;dr", ""),
        sections=[(n, d.strip()) for n, d in sections],
        style=parts.get("style", ""), overview=parts.get("daily overview", ""),
        figure=parts.get("figure", ""), contents=parts.get("contents table", ""))


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_criteria(path: Path = CRITERIA) -> Criteria:
    front, body = _split_front_matter(ensure_from_template(path))
    categories = front.get("categories") or []
    if not categories:
        raise ValueError(f"{path}: no categories listed")

    # Topic names are the '## ' headings inside the '# Topics' section.
    topics = []
    in_topics = False
    for line in body.splitlines():
        if line.startswith("# "):
            in_topics = line[2:].strip().lower() == "topics"
        elif in_topics and line.startswith("## "):
            topics.append(line[3:].strip())
    if not topics:
        raise ValueError(f"{path}: no '## ' topics found under '# Topics'")

    return Criteria(categories=categories, topics=topics, text=body)

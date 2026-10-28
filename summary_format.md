---
# The parts of the digest, top to bottom. Reorder them, or delete one to hide it.
#   brief         "Today in brief" (see "# Daily overview" below)
#   top_picks     the most important papers of the day
#   contents      a table of all papers in the digest (see "# Contents table" below)
#   papers        the full summaries, grouped by topic
#   screened_out  papers dropped after the full read
layout: [brief, top_picks, contents, papers, screened_out]

# Columns of the contents table, left to right. Available:
#   paper, topic, score, h_index (average author h-index), affiliations, tldr
contents_columns: [paper, topic, score, h_index, affiliations]

# Display switches (true/false, or a number where noted).
top_picks: 5                 # max papers in "Top picks"
show_authors: true           # author h-index and affiliation line under each paper
show_similar_papers: true    # "Similar papers" links
show_research_notes: true    # the research agent's context notes (top papers only)
show_figures: true           # one image per paper (see "# Figure" below)
---

# Summary format

This file controls how papers are summarized and how the digest reads. The models
read it on every run, together with criteria.md. Plain language is fine.

Changes apply to papers summarized after you edit this file. To re-summarize papers
that were already summarized, see "Changing the format" in README.md.

# TL;DR
One or two sentences, shown in bold at the top of each paper: what they did and the
headline result, with the key number.

# Sections
Each `##` heading below is one section of a paper summary, shown in this order with
that name. Add, remove, rename or reorder sections freely. The text under each heading
tells the model what to write there.

## Contributions
2-4 bullet points listing what is new in this paper.

## Method
2-4 sentences on how it works.

## Results
The key quantitative results, with baselines and setup.

## Why it matters
1-2 sentences on why this matters for my topics in criteria.md.

## Caveats
The main caveats or open questions, in 1-2 sentences.

# Figure
Each paper gets one image, shown right under the TL;DR.
- If one of the paper's own figures clearly shows the main idea or the main result on
  its own (for example a method overview diagram, or the headline result plot), reuse it.
- Otherwise, generate one image that clearly illustrates the paper's main claim.
- Generated images are always labeled "AI-generated illustration (not from the paper)"
  in the digest, and are checked against the summary before they are used.

# Style
- Write for an expert reader: precise, concrete, no hype.
- Use numbers from the paper where they exist.
- If something is unclear or missing in the paper, say so rather than guessing.

# Daily overview
The digest opens with "Today in brief": 3-5 sentences on the main themes and anything
notable today. Then come the top picks, ordered by importance, each with one sentence
on why to read it first.

# Contents table
A table of every paper in the digest. Each title links to its summary further down.
The numbers (score, average h-index) are computed, not written by a model.

Affiliations column: list only well-known institutions, by their common short name
(for example CMU, NVIDIA, Stanford, Google, MIT, Meta, OpenAI, DeepMind, Microsoft,
Berkeley, Tsinghua). Show at most 3 per paper. Leave the cell empty if none of the
authors' institutions is well known.

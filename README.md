# research-agent

A daily arXiv reading assistant. Every weekday morning it:

1. **Fetches** the new papers in the arXiv categories you chose.
2. **Triages** every abstract against your research topics (a cheap, fast model).
3. **Reads** the full text of the candidates, drops the ones that turn out not to fit,
   and writes a structured summary of the rest (a stronger model).
4. **Looks up context**: author track records and similar papers (free scholarly APIs).
5. **Researches** the very top papers with a small web-browsing agent: closest prior
   work, how novel it really is, code and outside discussion.
6. **Adds one image per paper**: one of the paper's own figures if it shows the main
   idea well, otherwise a generated illustration of the main claim. Generated images
   are checked against the summary by a second model and labeled as AI-generated.
7. **Writes a TL;DR digest** to `digests/YYYY-MM-DD.html` (and `.md`) and pops up a
   macOS notification.

Models run on any OpenAI-compatible API. The defaults target NVIDIA Inference Hub
(Claude Haiku for triage, Claude Sonnet for everything else).

## What you edit

| File | What it controls |
|---|---|
| `criteria.md` | **What you care about**: arXiv categories, topics, what to include and exclude, examples. Read fresh on every run. |
| `summary_format.md` | **How summaries look**: the TL;DR, which sections each summary has (add, remove, rename, reorder), when to reuse or generate an image, writing style, the daily overview, and which extras the digest shows. Read fresh on every run. |
| `config.yaml` | Technical knobs: which model per step, spending limits, thresholds, how many papers get the research agent. |

The first two can also be edited in the web app (**Edit prompts**, see below).

Your `criteria.md` and `summary_format.md` are not tracked by git, so your research
interests stay private even though the code is public. The repository has templates
instead (`criteria.example.md`, `summary_format.example.md`). The first run, or opening
**Edit prompts**, copies them to your own files if those don't exist yet. To start over
from a template, delete your file and it is copied again.

Tips for `criteria.md`:
- Each `##` heading under `# Topics` is one topic; its name is used to group the digest.
- Concrete include/exclude lists and example papers improve accuracy more than long prose.
- The digest ends with a "Screened out after full read" list. If something there should
  have made it, add it to the relevant topic as an example.

## Changing the format

Edit `summary_format.md`. Each `##` heading under `# Sections` becomes one section of
every paper summary, in that order; the text under it tells the model what to write.
The switches at the top turn digest extras (author line, similar papers, research
notes, screened-out list) on or off and set the number of top picks.

New summaries follow the file from the next run on. To redo today's summaries in the
new format (costs about $0.06 per paper; already-downloaded papers come from the cache):

```bash
scripts/run.sh --resummarize
```

The display switches and the daily overview don't need this: they apply whenever the
digest is written, so a plain `scripts/run.sh` refreshes them.

### Layout and contents table

At the top of `summary_format.md`, `layout:` lists the parts of the digest in order
(brief, top picks, contents table, papers, screened-out list); reorder or delete to
taste. `contents_columns:` picks the table's columns, and the `# Contents table` section
says how to fill the affiliations column. These apply whenever the digest is written.

Author affiliations come from the paper itself (its arXiv page, or the PDF's title page).
The average h-index is computed from author profiles on Semantic Scholar or OpenAlex. An
OpenAlex profile is only used when both the name and the institution match, so a
namesake elsewhere isn't counted. Coverage is shown as found/looked-up authors.

Without API keys both services are tight: Semantic Scholar's shared pool is usually
rate-limited, and OpenAlex allows roughly 100 author searches per day. The agent looks
up the first three authors and the last one per paper, remembers results for 30 days,
and stops calling a service for the rest of a run once it says its quota is used up
(or after three requests in a row fail). For reliable h-index coverage, get a free
Semantic Scholar API key (see Setup).

With a key, Semantic Scholar allows 1 request per second. The agent waits a little
over a second after each reply. Even so, Semantic Scholar refuses a share of requests
now and then; those are retried after a short wait, so lookups take about 3 s each.

### Images

The `# Figure` section of `summary_format.md` says when to reuse a paper figure and
when to generate one; `show_figures` turns images off. Settings in `config.yaml` under
`images:` choose the image model, cap generated images per day (default 10, about $0.08
each including the accuracy check), or disable generation so only paper figures are used.
To redo today's image choices: `scripts/run.sh --redo-figures`.

- Paper figures are linked from arXiv, not copied, so they need an internet connection
  to display and they stay under the paper's own license.
- Generated images are saved next to the digest in `digests/images/`.
- Papers with no HTML version on arXiv have no figure list, so they get a generated image.

## Setup (macOS)

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo> research-agent && cd research-agent
uv venv .venv && uv pip install --python .venv/bin/python -e .
```

Set your API key as an environment variable in `~/.zshrc`:

```bash
export RESEARCH_AGENT_API_KEY=...
```

Optional: a free [Semantic Scholar API key](https://www.semanticscholar.org/product/api#api-key-form)
gives better author data. Without it, that API is often rate-limited and the agent falls
back to OpenAlex, which indexes new papers a few days late. If you have one, add it to
`~/.zshrc` the same way:

```bash
export SEMANTIC_SCHOLAR_API_KEY=...
```

`scripts/run.sh` (and so the schedule and the web app) picks up these two lines from
`~/.zshrc` and nothing else from it. If there's no Semantic Scholar line, the agent
runs without it.

## Running

Run once by hand (the first run is a good test):

```bash
scripts/run.sh                 # full run
scripts/run.sh --limit 30      # only 30 papers, for a quick, cheap test
scripts/run.sh --no-research   # skip the web research agent
```

### Web app

A local web page to browse digests, start an ad-hoc digest, and watch the agent work:

```bash
scripts/install_web.sh      # keep it running at http://localhost:8765 whenever you're logged in
scripts/uninstall_web.sh    # remove it
scripts/web.sh              # or just run it in a terminal for now
```

After updating the code, restart the installed web app so it picks up the change
(running `scripts/install_web.sh` again does the same):

```bash
launchctl kickstart -k gui/$(id -u)/local.research-agent.web
```

- **Browse:** every daily and ad-hoc digest is listed on the left; click one to read it.
- **Ad-hoc digest:** pick a date range (up to 14 days) and, optionally, a focus in plain
  language ("speculative decoding for long-context serving"). The agent scans the papers
  submitted in that range, fully reads the 60 most relevant at most, and writes a separate
  digest. Typical cost is $2–6.
- **Run today's digest** starts the daily run now instead of waiting for 07:00.
- **Live status:** while a run is going, a bar at the top shows its stage, progress, cost
  so far and the latest log lines.
- **Edit prompts:** opens `criteria.md` and `summary_format.md` in an editor. Saving
  checks the file the same way the agent reads it (categories, topics, sections), and
  refuses to save a file the agent couldn't use. The previous version is kept in
  `cache/prompt-backups/`. Changes apply from the next run. Cmd+S saves.
- **Days with no papers:** arXiv publishes nothing on weekends and occasionally skips a
  weekday (a holiday or maintenance). Those runs show "No new papers" with the reason.
  Nothing is lost on a skipped weekday: the next run notices the gap and fetches any
  missed papers from the arXiv API.

Only one run happens at a time; a second one waits for the first (a 07:00 run that finds
an ad-hoc run going waits for it rather than being skipped). The page only accepts
requests from this Mac's own browser, so other websites can't start runs.

From the command line, an ad-hoc digest is:

```bash
scripts/run.sh --from 2026-10-20 --to 2026-10-27 --focus "speculative decoding"
```

Check spending and recent runs:

```bash
.venv/bin/python -m research_agent status
```

Turn the weekday 07:00 schedule on or off:

```bash
scripts/install_schedule.sh
scripts/uninstall_schedule.sh
```

If the Mac is asleep at 07:00, the run happens when it wakes. If it was off, the next run
catches up on missed announcements (up to 7 days back).

## Costs

The model provider reports the cost of every call, and the agent logs it in `state.db`.
Hard daily and monthly limits are set in `config.yaml`. When a limit is reached the run
stops spending and still writes a digest from what it finished.

Rough numbers with the default models, for four busy categories (about 1,000 new papers
a day): triage about $0.10, full reads about $0.06 per candidate, research agent about
$0.03–0.15 per paper. Typically **a few dollars per day**.

## Where things are

```
criteria.md            your research interests (yours, not tracked by git)
summary_format.md      how summaries and the digest look (yours, not tracked by git)
*.example.md           the templates the two files above start from
config.yaml            technical settings
research_agent/        the code: fetch, triage, deepread, scholar, research, digest, cli, web
research_agent/static/ the web app's page, script and styles
scripts/               run.sh and the schedule install/uninstall scripts
scheduling/            the macOS schedule definition (template)
digests/               daily outputs (not tracked by git)
logs/                  run logs (not tracked by git)
state.db               memory between runs: papers seen, results, spend (not tracked)
```

## Being polite to arXiv

All arXiv traffic goes through one module, `research_agent/arxiv_http.py`, which follows
arXiv's [API terms of use](https://info.arxiv.org/help/api/tou.html) and
[robots.txt](https://arxiv.org/robots.txt):

- One request at a time, never in parallel.
- At least 3 s between API/RSS requests, and 15 s whenever an arxiv.org page (HTML/PDF)
  is involved. The time of the last request is stored on disk, so separate runs stay
  spaced too.
- Only paths robots.txt allows (`/abs`, `/pdf`, `/html`, and the `export.arxiv.org` API).
- A 403 from arXiv stops all arXiv requests for the rest of the run. A 429/503 waits
  for the server's Retry-After and retries once, then stops.
- Full texts are cached in `cache/`, so re-runs never download a paper twice.
- Only one run can happen at a time.

The cost is speed: full-reading 30 papers takes about 8–15 minutes, mostly waiting.
That's fine for an unattended morning job. The tests in `tests/` check the limiter
without contacting arXiv:

```bash
.venv/bin/python -m unittest discover tests
```

## Safety notes

- The research agent's tools only read; it cannot write, post or send anything.
- Web pages are treated as untrusted data. Page fetching refuses non-public addresses
  (localhost, private networks, intranet hosts that resolve to private IPs).
- The API key is read from the environment and never shown to the models.

## License

MIT. See [LICENSE](LICENSE).

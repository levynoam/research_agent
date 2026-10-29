#!/bin/zsh
# Runs the daily pipeline. Used by the macOS scheduler, and fine to run by hand.
# Extra arguments are passed through, e.g.:  scripts/run.sh --limit 20
set -e
cd "$(dirname "$0")/.."

# The scheduler doesn't load ~/.zshrc, so pick up just the API key lines from it.
# SEMANTIC_SCHOLAR_API_KEY is optional: if it isn't there, nothing is set.
for var in RESEARCH_AGENT_API_KEY SEMANTIC_SCHOLAR_API_KEY; do
  if [[ -z "${(P)var}" && -f ~/.zshrc ]]; then
    eval "$(grep -E "^export ${var}=" ~/.zshrc | tail -n 1)"
  fi
done

exec .venv/bin/python -m research_agent run "$@"

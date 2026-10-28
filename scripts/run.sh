#!/bin/zsh
# Runs the daily pipeline. Used by the macOS scheduler, and fine to run by hand.
# Extra arguments are passed through, e.g.:  scripts/run.sh --limit 20
set -e
cd "$(dirname "$0")/.."

# The scheduler doesn't load ~/.zshrc, so pick up just the API key line from it.
if [[ -z "$RESEARCH_AGENT_API_KEY" && -f ~/.zshrc ]]; then
  eval "$(grep -E '^export RESEARCH_AGENT_API_KEY=' ~/.zshrc)"
fi

exec .venv/bin/python -m research_agent run "$@"

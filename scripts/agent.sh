#!/bin/zsh
# Runs any research_agent command with the API keys loaded, e.g.:
#   scripts/agent.sh status
#   scripts/agent.sh refine --run 2026-11-05 --message "fewer training papers"
# scripts/run.sh is the shortcut for the "run" command.
set -e
cd "$(dirname "$0")/.."

# The scheduler and the web app don't load ~/.zshrc, so pick up just the API key lines
# from it. SEMANTIC_SCHOLAR_API_KEY is optional: if it isn't there, nothing is set.
for var in RESEARCH_AGENT_API_KEY SEMANTIC_SCHOLAR_API_KEY; do
  if [[ -z "${(P)var}" && -f ~/.zshrc ]]; then
    eval "$(grep -E "^export ${var}=" ~/.zshrc | tail -n 1)"
  fi
done

exec .venv/bin/python -m research_agent "$@"

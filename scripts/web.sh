#!/bin/zsh
# Runs the web app at http://localhost:8765 (only reachable from this Mac).
cd "$(dirname "$0")/.."
exec .venv/bin/python -m research_agent.web

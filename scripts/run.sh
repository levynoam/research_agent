#!/bin/zsh
# Runs the daily pipeline. Used by the macOS scheduler, and fine to run by hand.
# Extra arguments are passed through, e.g.:  scripts/run.sh --limit 20
# caffeinate keeps the Mac from going to sleep on its own while the run is going.
# (It can't stop sleep when the lid is closed on battery.)
exec /usr/bin/caffeinate -i "$(dirname "$0")/agent.sh" run "$@"

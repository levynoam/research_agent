#!/bin/zsh
# Turns on the daily run. macOS only looks for scheduled jobs in ~/Library/LaunchAgents,
# so this is the one file that lives outside the project folder.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="local.research-agent.daily"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"
sed "s|__ROOT__|$ROOT|g" "$ROOT/scheduling/research-agent.plist.template" > "$TARGET"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"
echo "Daily run installed: weekdays at 07:00. Remove with scripts/uninstall_schedule.sh"

#!/bin/zsh
# Keeps the web app running at http://localhost:8765 whenever you're logged in.
# macOS only looks for such jobs in ~/Library/LaunchAgents, so that's the one file
# that lives outside the project folder.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="local.research-agent.web"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"
sed "s|__ROOT__|$ROOT|g" "$ROOT/scheduling/research-agent-web.plist.template" > "$TARGET"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"
echo "Web app installed: http://localhost:8765 (remove with scripts/uninstall_web.sh)"

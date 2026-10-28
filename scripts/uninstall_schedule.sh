#!/bin/zsh
# Turns off the daily run.
LABEL="local.research-agent.daily"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Daily run removed."

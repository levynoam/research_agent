#!/bin/zsh
# Stops the web app and keeps it from starting at login.
LABEL="local.research-agent.web"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Web app removed."

#!/bin/bash
# (e) untick removes the job. Not shipped.
source "$HOME/builds/kobo-service-test/env.sh"
cd "$T"
echo "--- $(ts) before: launchctl list | grep TEST: [$(launchctl list | grep com.calibreader.sync.kobo.TEST)]"
echo "--- $(ts) (e) Settings: untick Keep syncing when calibre is closed, OK"
plugin settings "$LIB" --app-db "$APPDB" --user admin --cw "$CW" --push on --service off | grep -E "PREFS|status line|ERROR"
echo "$(ts) launchctl list | grep TEST: [$(launchctl list | grep com.calibreader.sync.kobo.TEST)]"
echo "$(ts) launchctl print: $(launchctl print "gui/$(id -u)/com.calibreader.sync.kobo.TEST" 2>&1 | head -1)"
ls -la "$HOME/Library/LaunchAgents/com.calibreader.sync.kobo.TEST.plist" 2>&1

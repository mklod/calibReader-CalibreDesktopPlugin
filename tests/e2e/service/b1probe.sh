#!/bin/bash
# B1 itself: does launchd start a user job whose StandardOutPath folder does not exist? A throwaway
# job com.calibreader.sync.kobo.TESTLOG (/bin/sh -c true), log path in a folder that is not there,
# RunAtLoad; launchd's verdict from `launchctl print`; then bootout + remove. Not shipped.
W="$HOME/builds/kobo-service-test/b1"
mkdir -p "$W"
LABEL=com.calibreader.sync.kobo.TESTLOG
PL="$HOME/Library/LaunchAgents/$LABEL.plist"
MISSING="$W/no-such-folder/kobo-launchd.log"
python3 - "$PL" "$LABEL" "$MISSING" "$W/ran.txt" <<'PY'
import plistlib, sys
pl, label, out, ran = sys.argv[1:]
plistlib.dump({'Label': label, 'ProgramArguments': ['/bin/sh', '-c', 'date > ' + ran], 'RunAtLoad': True,
               'StandardOutPath': out, 'StandardErrorPath': out}, open(pl, 'wb'))
PY
launchctl bootstrap "gui/$(id -u)" "$PL"
sleep 6
echo "log folder exists: $(test -d "$W/no-such-folder" && echo yes || echo no); the job's command ran: $(test -e "$W/ran.txt" && echo yes || echo no)"
launchctl print "gui/$(id -u)/$LABEL" | grep -E "^\s*(state|last exit code|runs) =" | tr -s '\t ' ' '
launchctl bootout "gui/$(id -u)/$LABEL"
python3 -c "import os,sys; os.remove(sys.argv[1])" "$PL"
echo "TESTLOG removed: [$(launchctl list | grep TESTLOG)] plist exists: $(test -e "$PL" && echo yes || echo no)"

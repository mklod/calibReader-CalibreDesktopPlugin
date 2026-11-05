#!/bin/bash
# Does launchd's WatchPaths fire again and again on IN-PLACE writes (as SQLite writes app.db)?
# A throwaway job com.calibreader.sync.kobo.TESTWP (/bin/sh appending the time to a log), a test
# file written in place three times 75 s apart, then a delete+recreate, then bootout + remove.
# Not shipped.
T="$HOME/builds/kobo-service-test/wp"
mkdir -p "$T"
LABEL=com.calibreader.sync.kobo.TESTWP
PL="$HOME/Library/LaunchAgents/$LABEL.plist"
printf 'start\n' > "$T/x.db"
: > "$T/fired.log"
python3 - "$PL" "$T" "$LABEL" <<'PY'
import plistlib, sys
pl, t, label = sys.argv[1:]
plistlib.dump({'Label': label, 'ProgramArguments': ['/bin/sh', '-c', 'date +%H:%M:%S >> ' + t + '/fired.log'],
               'WatchPaths': [t + '/x.db', t + '/x.db-wal'], 'ThrottleInterval': 10}, open(pl, 'wb'))
PY
launchctl bootstrap "gui/$(id -u)" "$PL"
echo "$(date +%H:%M:%S) loaded"
sleep 15
for i in 1 2 3; do
    python3 -c "import sys; f=open(sys.argv[1],'r+b'); f.seek(0); f.write(b'write$i'); f.close()" "$T/x.db"
    echo "$(date +%H:%M:%S) in-place write $i"
    sleep 75
done
python3 -c "import os,sys; os.remove(sys.argv[1]); open(sys.argv[1],'w').write('new')" "$T/x.db"
echo "$(date +%H:%M:%S) delete + recreate"
sleep 30
echo "fired at:"; cat "$T/fired.log"
launchctl bootout "gui/$(id -u)/$LABEL"
python3 -c "import os,sys; os.remove(sys.argv[1])" "$PL"
echo "$(date +%H:%M:%S) TESTWP removed: [$(launchctl list | grep TESTWP)] plist exists: $(test -e "$PL" && echo yes || echo no)"

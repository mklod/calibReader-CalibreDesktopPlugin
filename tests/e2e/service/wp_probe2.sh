#!/bin/bash
# WatchPaths experiment 2: the job's own launchd keys (RunAtLoad, StartInterval 300, ThrottleInterval
# THROTTLE, ProcessType Background, LowPriorityIO, Nice 10), a trivial program, in-place writes:
# one inside the throttle window after the load, then two more 75 s apart. Throwaway job
# com.calibreader.sync.kobo.TESTWP2, removed at the end. Not shipped.
THROTTLE="${1:-60}"
T="$HOME/builds/kobo-service-test/wp2"
mkdir -p "$T"
LABEL=com.calibreader.sync.kobo.TESTWP2
PL="$HOME/Library/LaunchAgents/$LABEL.plist"
printf 'start\n' > "$T/x.db"
: > "$T/fired.log"
python3 - "$PL" "$T" "$LABEL" "$THROTTLE" <<'PY'
import plistlib, sys
pl, t, label, throttle = sys.argv[1:]
plistlib.dump({'Label': label, 'ProgramArguments': ['/bin/sh', '-c', 'date +%H:%M:%S >> ' + t + '/fired.log'],
               'WatchPaths': [t + '/x.db', t + '/x.db-wal'], 'ThrottleInterval': int(throttle),
               'RunAtLoad': True, 'StartInterval': 300, 'ProcessType': 'Background', 'LowPriorityIO': True,
               'Nice': 10}, open(pl, 'wb'))
PY
sleep 3
launchctl bootstrap "gui/$(id -u)" "$PL"
echo "$(date +%H:%M:%S) loaded (ThrottleInterval $THROTTLE)"
sleep 5
for i in 1 2 3; do
    python3 -c "import sys; f=open(sys.argv[1],'r+b'); f.seek(0); f.write(b'write$i'); f.close()" "$T/x.db"
    echo "$(date +%H:%M:%S) in-place write $i"
    sleep 75
done
sleep 10
echo "fired at:"; cat "$T/fired.log"
launchctl bootout "gui/$(id -u)/$LABEL"
python3 -c "import os,sys; os.remove(sys.argv[1])" "$PL"
echo "$(date +%H:%M:%S) TESTWP2 removed: [$(launchctl list | grep TESTWP2)] plist exists: $(test -e "$PL" && echo yes || echo no)"

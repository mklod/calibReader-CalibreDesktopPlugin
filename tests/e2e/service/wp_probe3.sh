#!/bin/bash
# WatchPaths experiment 3, against the e2e's REAL Calibre-Web: a throwaway job watching app.db and
# app.db-journal (SQLite's rollback journal, created and deleted by every write transaction) with
# the job's own keys; two simulated Kobo PUTs 90 s apart. Then bootout + remove. Not shipped.
source "$HOME/builds/kobo-service-test/env.sh"
cd "$T"
D=$(uuid_of Dracula)
W="$T/wp3"
mkdir -p "$W"
LABEL=com.calibreader.sync.kobo.TESTWP3
PL="$HOME/Library/LaunchAgents/$LABEL.plist"
: > "$W/fired.log"
python3 - "$PL" "$W" "$LABEL" "$APPDB" <<'PY'
import plistlib, sys
pl, w, label, app_db = sys.argv[1:]
plistlib.dump({'Label': label, 'ProgramArguments': ['/bin/sh', '-c', 'date +%H:%M:%S >> ' + w + '/fired.log'],
               'WatchPaths': [app_db, app_db + '-journal'], 'ThrottleInterval': 60,
               'RunAtLoad': True, 'StartInterval': 300, 'ProcessType': 'Background', 'LowPriorityIO': True,
               'Nice': 10}, open(pl, 'wb'))
PY
launchctl bootstrap "gui/$(id -u)" "$PL"
echo "$(ts) loaded (watching app.db + app.db-journal)"
sleep 65
for v in kobo.27.1 kobo.28.1; do
    kobo put "$D" KoboSpan "$v" OEBPS/1617581820713377646_345-h-10.htm.xhtml 27.0 60.0 | cut -c1-60
    echo "$(ts) Kobo PUT $v"
    sleep 90
done
echo "fired at:"; cat "$W/fired.log"
launchctl bootout "gui/$(id -u)/$LABEL"
python3 -c "import os,sys; os.remove(sys.argv[1])" "$PL"
echo "$(ts) TESTWP3 removed: [$(launchctl list | grep TESTWP3)] plist exists: $(test -e "$PL" && echo yes || echo no)"

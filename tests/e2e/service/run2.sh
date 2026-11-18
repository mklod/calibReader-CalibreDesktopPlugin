#!/bin/bash
# Fix round 1 e2e on the REAL default folders (B1), the healthy status line (S1), the job list (S5),
# a 1.5.0 rollback (S5), a Kobo PUT imported with calibre closed, the journal race on macOS (S4),
# then untick (e). Not shipped.
source "$HOME/builds/kobo-service-test/env2.sh"
cd "$T"
D=$(uuid_of Dracula)
wait_run() {   # wait for a new run line after $1 runs, at most $2 seconds
    local n="$1" end=$(( $(date +%s) + $2 ))
    until [ "$(runs)" -gt "$n" ] || [ "$(date +%s)" -gt "$end" ]; do sleep 2; done
}
echo "--- $(ts) before: logs folder $(test -d "$HOME/Library/Logs/CalibReaderSync" && echo exists || echo absent); support folder $(test -d "$SUPPORT" && echo exists || echo absent); calibre processes $(pgrep -f 'calibre-debug|MacOS/calibre$' | wc -l | tr -d ' ')"
echo "--- $(ts) Settings OK: service ON, push OFF (library: $LIB)"
plugin settings "$LIB" --app-db "$APPDB" --user admin --cw "$CW" --push off --service on | grep -E "PREFS|status line|ERROR|background jobs"
echo "$(ts) folders now: logs $(test -d "$HOME/Library/Logs/CalibReaderSync" && echo exists || echo absent); support $(test -d "$SUPPORT" && echo exists || echo absent)"
wait_run 0 90
echo "$(ts) launchd: $(lc)"
echo "$(ts) first job line in $JOBLOG:"; cat "$JOBLOG"
ls -la "$HOME/Library/Logs/CalibReaderSync" "$SUPPORT" "$SUPPORT/state"

echo; echo "--- $(ts) the Kobo reads Dracula (PUT kobo.17.6), calibre closed"
sleep 62
R=$(runs)
kobo put "$D" KoboSpan kobo.17.6 OEBPS/1617581820713377646_345-h-10.htm.xhtml 24.0 40.0 | cut -c1-110
T0=$(date +%s)
wait_run "$R" 330
echo "$(ts) next job run after $(( $(date +%s) - T0 )) s:"; since_run "$R"
echo "$(ts) launchd: $(lc)"

echo; echo "--- $(ts) Settings again (status line, job list)"
plugin settings "$LIB" --app-db "$APPDB" --user admin --cw "$CW" --push off --service on | grep -E "status line after|background jobs|ERROR"

echo; echo "--- $(ts) S5 rollback: calibre's plugin replaced by the 1.5.0 zip, then the job starts"
"$BIN/calibre-customize" -a "$T/CalibReaderSync-1.5.0.zip"
R=$(runs)
launchctl kickstart "gui/$(id -u)/com.calibreader.sync.kobo.TEST"
wait_run "$R" 150
since_run "$R"
echo "--- $(ts) back to the fixed 1.6.0 zip"
"$BIN/calibre-customize" -a "$T/CalibReaderSync-1.6.0-fix1.zip"
R=$(runs)
launchctl kickstart "gui/$(id -u)/com.calibreader.sync.kobo.TEST"
wait_run "$R" 150
since_run "$R"

echo; echo "--- $(ts) S4 on macOS: the reviewer's journal race, 2 processes x 100 saves, against the job's copy"
python3 "$T/p_journal_race2.py" "$SUPPORT/service" "$T/s4-journal.json" 100 20
SAME_IDENT=1 python3 "$T/p_journal_race2.py" "$SUPPORT/service" "$T/s4-journal-b.json" 100 20
python3 -c "import shutil; [shutil.rmtree(p, ignore_errors=True) for p in ('/tmp/libA', '/tmp/libB')]"

echo; echo "--- $(ts) (e) Settings: untick"
plugin settings "$LIB" --app-db "$APPDB" --user admin --cw "$CW" --push off --service off | grep -E "PREFS|status line after|ERROR"
echo "$(ts) launchctl list | grep TEST: [$(launchctl list | grep com.calibreader.sync.kobo.TEST)]; plist: $(test -e "$HOME/Library/LaunchAgents/com.calibreader.sync.kobo.TEST.plist" && echo present || echo gone)"

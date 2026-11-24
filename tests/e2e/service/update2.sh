#!/bin/bash
# The final 1.6.0 zip installed over the e2e build (a plugin update), Settings OK refreshes the
# job's copy, the copy runs (and never the zip) under calibre-debug, and one more Kobo move is
# imported by launchd's job with calibre closed. Not shipped.
source "$HOME/builds/kobo-service-test/env.sh"
cd "$T"
D=$(uuid_of Dracula)
runs() { grep -c '^2026' logs/kobo.log; }
echo "--- $(ts) plugin update: calibre-customize -a CalibReaderSync-1.6.0.zip"
"$BIN/calibre-customize" -a "$T/CalibReaderSync-1.6.0.zip"
echo "copy before OK: kobo_service.py $(shasum -a 256 home/service/kobo_service.py | cut -c1-16)"
echo "--- $(ts) Settings OK (box still ticked)"
plugin settings "$LIB" --app-db "$APPDB" --user admin --cw "$CW" --push on --service on | grep -E "PREFS|status line|ERROR"
echo "copy after OK:  kobo_service.py $(shasum -a 256 home/service/kobo_service.py | cut -c1-16)"
echo "launchctl list: $(launchctl list | grep com.calibreader.sync.kobo.TEST)"
launchctl print "gui/$(id -u)/com.calibreader.sync.kobo.TEST" | grep -A5 '"WatchPaths"'
echo "$(ts) waiting 65 s (past the reload's RunAtLoad run and the 60 s throttle)"; sleep 65
echo "--- $(ts) the job's copy under calibre-debug, with the plugin installed in calibre:"
"$BIN/calibre-debug" -e "$T/home/service/kobo_service.py" -- version 2>&1 | grep -v -E '^(QPA|devicePixelRatio|logicalDpi|physicalDpi)'
echo "calibre processes: $(pgrep -f "calibre-debug|MacOS/calibre$" | wc -l | tr -d ' ')"
R=$(runs)
echo "--- $(ts) (a, final zip, WatchPaths on the journal) the Kobo reads on in Dracula: PUT kobo.29.1"
kobo put "$D" KoboSpan kobo.29.1 OEBPS/1617581820713377646_345-h-10.htm.xhtml 27.5 63.0 | cut -c1-120
T0=$(date +%s)
until grep -q "1 place imported" <(awk -v n="$R" '/^2026/{c++} c>n' logs/kobo.log); do sleep 3; done
echo "--- $(ts) imported after $(( $(date +%s) - T0 )) s"
awk -v n="$R" '/^2026/{c++} c>n' logs/kobo.log
python3 -c "
import json; e=json.load(open('$LIB/CalibReader_positions.json'))['positions']['$D']
print('sidecar entry:', e['deviceId'], 'chapter', e['chapter'], 'textAnchor', e['textAnchor'], e['kobo']['value'], e['kobo']['precision'])"

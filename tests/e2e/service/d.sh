#!/bin/bash
# (d) one echo memory: the job's push is not a Kobo move for calibre, and calibre's push is not one
# for the job. Not shipped.
source "$HOME/builds/kobo-service-test/env.sh"
cd "$T"
D=$(uuid_of Dracula)
runs() { grep -c '^2026' logs/kobo.log; }
pos() { shasum -a 256 "$LIB/CalibReader_positions.json" | cut -c1-16; }
echo "--- $(ts) (d1) calibre opened after the job's push: the plugin path (Sync Read Status)"
P0=$(pos)
plugin run "$LIB"
echo "$(ts) positions file before/after calibre's run: $P0 / $(pos)"
echo "--- $(ts) (d2) the app reads on (chapter 11, 15205); calibre (Sync Read Status) sends it"
python3 app_reads.py "$LIB" "$D" 11 15205 0.252
plugin run "$LIB"
P2=$(pos)
echo "--- $(ts) calibre closed; launchd starts the job (kickstart)"
R=$(runs)
launchctl kickstart -k "gui/$(id -u)/com.calibreader.sync.kobo.TEST"
until [ "$(runs)" -gt "$R" ]; do sleep 0.5; done
awk -v n="$R" '/^2026/{c++} c>n' logs/kobo.log
echo "$(ts) positions file before/after the job's run: $P2 / $(pos)"

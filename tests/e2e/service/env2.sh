# source env2.sh — fix round 1 e2e: the job's REAL default folders (~/Library/Logs/CalibReaderSync,
# ~/Library/Application Support/CalibReaderSync: no CALIBREADER_SYNC_HOME/LOGS), a test label only,
# an isolated calibre config, a library whose name has an accent and a space. Not shipped.
export T="$HOME/builds/kobo-service-test"
export BIN=/Applications/calibre.app/Contents/MacOS
export CALIBRE_CONFIG_DIRECTORY="$T/calibre-config"
unset CALIBREADER_SYNC_HOME CALIBREADER_SYNC_LOGS
export CALIBREADER_SYNC_LABEL=com.calibreader.sync.kobo.TEST
export QT_QPA_PLATFORM=offscreen
export LIB="$T/Bibliothèque calibre"
export APPDB="$T/cw/app.db"
export CW=http://127.0.0.1:8083
export JOBLOG="$HOME/Library/Logs/CalibReaderSync/kobo.log"
export SUPPORT="$HOME/Library/Application Support/CalibReaderSync"
kobo() { python3 "$T/kobo_sim.py" --cw "$CW" --token-file "$T/cw/token.txt" --state "$T/kobo" "$@"; }
plugin() { "$BIN/calibre-debug" -e "$T/e2e_plugin.py" -- "$@" 2>&1 | grep --line-buffered -v -E '^(QPA|devicePixelRatio|logicalDpi|physicalDpi|ApplicationPaletteChange|Using calibre Qt style|qt.qpa.fonts)'; }
uuid_of() { sqlite3 "$LIB/metadata.db" "select uuid from books where title like '$1%'"; }
runs() { grep -c '^2026' "$JOBLOG" 2>/dev/null || echo 0; }
since_run() { awk -v n="$1" '/^2026/{c++} c>n' "$JOBLOG"; }
ts() { date +"%H:%M:%S"; }
lc() { launchctl print "gui/$(id -u)/com.calibreader.sync.kobo.TEST" 2>&1 | grep -E "^\s*(state|last exit code|runs) =" | tr -s '\t ' ' '; }

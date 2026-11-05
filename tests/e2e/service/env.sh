# source env.sh — the e2e's paths. The job's support/log folders and its label are the TEST ones
# (CALIBREADER_SYNC_HOME / _LOGS / _LABEL, passed on into the plist by kobo_service.plist_for), so
# nothing lands in ~/Library/Application Support or ~/Library/Logs; the plist itself goes to the
# real ~/Library/LaunchAgents, as the plugin puts it. Not shipped.
export T="$HOME/builds/kobo-service-test"
export BIN=/Applications/calibre.app/Contents/MacOS
export CALIBRE_CONFIG_DIRECTORY="$T/calibre-config"
export CALIBREADER_SYNC_HOME="$T/home"
export CALIBREADER_SYNC_LOGS="$T/logs"
export CALIBREADER_SYNC_LABEL=com.calibreader.sync.kobo.TEST
export QT_QPA_PLATFORM=offscreen
export LIB="$T/library"
export APPDB="$T/cw/app.db"
export CW=http://127.0.0.1:8083
kobo() { python3 "$T/kobo_sim.py" --cw "$CW" --token-file "$T/cw/token.txt" --state "$T/kobo" "$@"; }
plugin() { "$BIN/calibre-debug" -e "$T/e2e_plugin.py" -- "$@" 2>&1 | grep --line-buffered -v -E '^(QPA|devicePixelRatio|logicalDpi|physicalDpi)'; }
joblog() { cat "$T/logs/kobo.log"; }
uuid_of() { sqlite3 "$LIB/metadata.db" "select uuid from books where title like '$1%'"; }
ts() { date +"%H:%M:%S"; }

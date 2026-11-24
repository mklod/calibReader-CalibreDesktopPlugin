#!/bin/bash
# Fix round 1 set-up: a fresh isolated calibre config with the fixed 1.6.0 zip, a fresh library
# "Bibliothèque calibre" (3 SAFE EPUB copies), Calibre-Web's app.db copied again from the step-1 rig,
# emptied of Kobo rows and pointed at it. The job's real folders are NOT created here. Not shipped.
set -e
source "$HOME/builds/kobo-service-test/env2.sh"
R="$HOME/builds/kobo-sync"
python3 -c "import shutil,sys; [shutil.rmtree(p, ignore_errors=True) for p in sys.argv[1:]]" \
    "$T/calibre-config" "$T/library" "$LIB" "$T/kobo" "$T/logs" "$T/home" "$T/wp" "$T/wp2" "$T/wp3"
mkdir -p "$T/calibre-config" "$T/kobo"
"$BIN/calibre-debug" -e "$T/make_library.py" -- "$LIB" "$T/src/dracula.epub" "$T/src/metamorphosis.epub" "$T/src/persuasion.epub" 2>&1 | grep -v -E '^(QPA|device|logical|physical)'
"$BIN/calibre-customize" -a "$T/CalibReaderSync-1.6.0-fix1.zip"
cp "$R/cw-data/app.db" "$T/cw/app.db"
sqlite3 "$T/cw/app.db" "DELETE FROM kobo_bookmark; DELETE FROM kobo_statistics; DELETE FROM kobo_reading_state; DELETE FROM kobo_synced_books; DELETE FROM book_read_link; DELETE FROM archived_book; DELETE FROM user_session; DELETE FROM remote_auth_token;"
python3 "$T/cw_setup.py" --app-db "$T/cw/app.db" --library "$LIB" --kepubify "$R/bin/kepubify-linux-64bit" --token-file "$T/cw/token.txt"
echo "the job's real folders before anything: $(ls -d "$HOME/Library/Logs/CalibReaderSync" "$SUPPORT" 2>&1 | tr '\n' ' ')"

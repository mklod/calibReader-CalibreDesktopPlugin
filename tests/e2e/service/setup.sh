#!/bin/bash
# Step 3 e2e on the Mac mini: a throwaway library (3 SAFE EPUB copies), the 1.6.0 plugin in an
# ISOLATED calibre config folder, and a copy of the step-1 Calibre-Web app.db emptied of the old
# library's Kobo rows, pointed at the new library (kepubify on, Kobo sync on, a fresh Kobo token in
# cw/token.txt, never printed). Not shipped.
set -e
T="$HOME/builds/kobo-service-test"
R="$HOME/builds/kobo-sync"
BIN=/Applications/calibre.app/Contents/MacOS
export CALIBRE_CONFIG_DIRECTORY="$T/calibre-config"
mkdir -p "$T/calibre-config" "$T/cw" "$T/kobo" "$T/logs" "$T/home" "$T/src"
python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$T/library"
cp "$R/library/Bram Stoker/Dracula (70)/Dracula - Bram Stoker.epub" "$T/src/dracula.epub"
cp "$R/library/Franz Kafka/The Metamorphosis (60)/The Metamorphosis - Franz Kafka.epub" "$T/src/metamorphosis.epub"
cp "$R/library/Jane Austen/Persuasion (40)/Persuasion - Jane Austen.epub" "$T/src/persuasion.epub"
"$BIN/calibre-debug" -e "$T/make_library.py" -- "$T/library" "$T/src/dracula.epub" "$T/src/metamorphosis.epub" "$T/src/persuasion.epub"
"$BIN/calibre-customize" -a "$T/CalibReaderSync-1.6.0-e2e.zip"
ls -la "$T/calibre-config/plugins"
cp "$R/cw-data/app.db" "$T/cw/app.db"
sqlite3 "$T/cw/app.db" "DELETE FROM kobo_bookmark; DELETE FROM kobo_statistics; DELETE FROM kobo_reading_state; DELETE FROM kobo_synced_books; DELETE FROM book_read_link; DELETE FROM archived_book; DELETE FROM user_session; DELETE FROM remote_auth_token;"
python3 "$T/cw_setup.py" --app-db "$T/cw/app.db" --library "$T/library" --kepubify "$R/bin/kepubify-linux-64bit" --token-file "$T/cw/token.txt"
shasum -a 256 "$T/cw/app.db" "$T/library/metadata.db"

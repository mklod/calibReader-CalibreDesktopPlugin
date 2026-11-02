#!/bin/sh
# Last modified: 2026-11-02--1416
# Runs every CalibReader Sync test under calibre's own python (calibre-debug), with a
# throwaway calibre config directory (the plugin's JSONConfig never touches the real one)
# and TZ=America/Los_Angeles (calibre hands last_modified back in local time, -08:00 in
# November). CALIBRE_DEBUG overrides the calibre-debug binary; CR_SAFE_LIBRARY the library
# template (a local copy of the SAFE library). Exit code: 0 only when tests ran and passed.
HERE="$(cd "$(dirname "$0")" && pwd)"
DEBUG="${CALIBRE_DEBUG:-/Applications/calibre.app/Contents/MacOS/calibre-debug}"
CFG="$(mktemp -d "${TMPDIR:-/tmp}/cr-calibre-config.XXXXXX")"
CALIBRE_CONFIG_DIRECTORY="$CFG" TZ="${TZ:-America/Los_Angeles}" QT_QPA_PLATFORM=offscreen \
    "$DEBUG" -e "$HERE/run_tests.py" -- "$@"
CODE=$?
python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$CFG"
exit $CODE

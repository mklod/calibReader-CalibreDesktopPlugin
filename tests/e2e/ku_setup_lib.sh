#!/bin/sh
# Last modified: 2026-11-05--1033
# Win10 Git Bash, paths of this e2e (C:/dev/cr-kobo-reverse/e2e-ku). Not shipped.
# Fresh scratch library (copies of SAFE books) + KU-style columns filled by hand, and a fresh
# isolated calibre config with the 1.7.0 zip installed. Never touches the SAFE library or K:\.
set -e
cd /c/dev/cr-kobo-reverse/e2e-ku
python -c "import shutil; [shutil.rmtree(p, ignore_errors=True) for p in ('lib', 'calibre-config')]"
mkdir -p lib calibre-config
python ../e2e-ku/build_zip.py CalibReaderSync-1.7.0-e2e.zip
export CALIBRE_CONFIG_DIRECTORY="C:/dev/cr-kobo-reverse/e2e-ku/calibre-config"
"/c/Program Files/Calibre2/calibre-customize.exe" -a "C:/dev/cr-kobo-reverse/e2e-ku/CalibReaderSync-1.7.0-e2e.zip" 2>&1 | grep -v DEBUG
CB="/c/Program Files/Calibre2/calibredb.exe"; L="C:/dev/cr-kobo-reverse/e2e-ku/lib"
for f in "Dracula - Bram Stoker" "El Dorado - Baroness Orczy" "Emma - Jane Austen" "Persuasion - Jane Austen" "The Metamorphosis - Franz Kafka" "Through the Looking-Glass - Lewis Carroll"; do
  "$CB" add --library-path "$L" "C:/dev/cr-kobo-reverse/e2e-ku/src/$f.epub" 2>&1 | grep -v DEBUG
done
"$CB" add_custom_column --library-path "$L" kobo_reading_location "Kobo location" text 2>&1 | grep -v DEBUG
"$CB" add_custom_column --library-path "$L" kobo_percent_read "Kobo % read" int 2>&1 | grep -v DEBUG
"$CB" add_custom_column --library-path "$L" kobo_last_read "Kobo last read" datetime 2>&1 | grep -v DEBUG
set_ku() {
  [ -n "$2" ] && "$CB" set_custom --library-path "$L" kobo_reading_location "$1" "$2" 2>&1 | grep -v DEBUG
  "$CB" set_custom --library-path "$L" kobo_percent_read "$1" "$3" 2>&1 | grep -v DEBUG
  "$CB" set_custom --library-path "$L" kobo_last_read "$1" "$4" 2>&1 | grep -v DEBUG
}
# 1 Dracula: device-prefixed (!!) span; 2 El Dorado: the sample's shape with a span calibre numbers;
# 3 Emma: device-prefixed (!) chapter + an unknown fragment; 4 Persuasion: % only;
# 5 Metamorphosis: chapter only; 6 Looking-Glass: the MobileRead sample verbatim
set_ku 1 'file:///mnt/onboard/Bram Stoker/Dracula - Bram Stoker.kepub.epub!!OEBPS/1617581820713377646_345-h-10.htm.xhtml#kobo.18.8' 34 '2026-11-04T20:20:00+00:00'
set_ku 2 'text/titlepage.xhtml#kobo.2.1' 1 '2026-11-04T20:00:00+00:00'
set_ku 3 'file:///mnt/onboard/Jane Austen/Emma - Jane Austen.kepub.epub!OEBPS!801687786609727734_158-h-4.htm.xhtml#point(/1/4/2/1:0)' 6 '2026-11-04T20:50:00+00:00'
set_ku 4 '' 40 '2026-11-04T20:40:00+00:00'
set_ku 5 'OEBPS/7446794606207039498_5200-h-2.htm.xhtml' 45 '2026-11-04T20:30:00+00:00'
set_ku 6 'text/titlepage.xhtml#kobo.1.1' 1 '2026-11-04T20:10:00+00:00'
"$CB" list --library-path "$L" -f 'title,*kobo_reading_location,*kobo_percent_read,*kobo_last_read' --line-width 300 2>&1 | grep -v DEBUG

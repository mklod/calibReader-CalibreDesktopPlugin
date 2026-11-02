# Last modified: 2026-11-02--1440
#
# Writes an iOS parity fixture: what kobo_position computes for one book, so
# the app's KoboSyncTests can check that the reader (EPUBExtractor + WebKit
# TreeWalker after ChapterHTML.prepare) numbers chapters and counts text the
# same way.
#
#   python3 tests/make_ios_parity_fixture.py <book.epub> <book.kepub or -> <out.json> [--every N]
#
# Places: spans spread over EVERY chapter (every Nth kobo span of the KEPUB),
# chapter % at 0 / 37.5 / 99 in each chapter, a CFI and a book %. Excerpts are
# 40 UTF-16 units of the book's text: keep fixtures of copyrighted books OUT of
# git (the app test reads them from TEST_RUNNER_CR_KOBO_PARITY_DIR).
import json, os, re, sys, zipfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get('KOBO_POSITION_DIR') or os.path.dirname(HERE))
from kobo_position import Book, map_bookmark

epub_path, kepub_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
every = int(sys.argv[sys.argv.index('--every') + 1]) if '--every' in sys.argv else 40
epub = Book(epub_path)
kepub = Book(kepub_path) if kepub_path != '-' else None
cases = []
def add(name, b, kp):
    r = map_bookmark(epub, b, kp)
    if r is None:
        return
    units = epub.text(r['chapter']).text.encode('utf-16-le')
    cases.append({'name': name, 'chapter': r['chapter'], 'textAnchor': r['textAnchor'], 'precision': r['precision'],
                  'excerpt': units[2 * r['textAnchor']:2 * (r['textAnchor'] + 40)].decode('utf-16-le', 'replace')})
for ch, member in enumerate(epub.chapters):
    if kepub is not None:
        try:
            with zipfile.ZipFile(kepub.path) as z:
                ids = re.findall(r'id="(kobo\.\d+\.\d+)"', z.read(member).decode('utf-8', 'replace'))
        except KeyError:
            ids = []
        for sid in ids[::every]:
            add('span ch%d %s' % (ch, sid), {'location_source': member, 'location_type': 'KoboSpan',
                'location_value': sid, 'progress_percent': 50.0, 'content_source_progress_percent': 50.0}, kepub)
    for p in (0.0, 37.5, 99.0):
        add('chapter%% ch%d %s' % (ch, p), {'location_source': member, 'location_type': None, 'location_value': None,
            'progress_percent': 50.0, 'content_source_progress_percent': p}, None)
add('book% 64', {'location_source': None, 'location_type': None, 'location_value': None,
    'progress_percent': 64.0, 'content_source_progress_percent': None}, None)
if 'metamorphosis' in os.path.basename(epub_path).lower():
    add('cfi Part I second paragraph', {'location_source': epub.chapters[2], 'location_type': 'EpubCfi',
        'location_value': 'epubcfi(/6/6!/4/2/6/1:1)', 'progress_percent': 12.0,
        'content_source_progress_percent': None}, None)
out = {'book': os.path.basename(epub_path), 'epub': os.path.basename(epub_path),
       'chapters': epub.chapters, 'lengths': epub.lengths(), 'cases': cases}
with open(out_path, 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print('%s: %d chapters, %d cases' % (os.path.basename(epub_path), len(epub.chapters), len(cases)))

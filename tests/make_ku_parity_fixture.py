# Last modified: 2026-11-05--1033
#
# make_ku_parity_fixture.py — the span parity run of KOBO-UTILITIES.md 8.4 (plugin 1.7.0):
# calibre's OWN kepubify (what calibre's Kobo driver sends) against the EPUB CalibReader opens.
# Run it under calibre-debug, on COPIES of books (never a library's own files):
#
#   calibre-debug -e tests/make_ku_parity_fixture.py -- --books <folder of .epub copies>
#       [--adversarial] --out <report.json>
#
# Per book, per chapter, per span (kobo.N.M of the regenerated KEPUB, prefer-justification as
# calibre's KEPUB output defaults say, i.e. as the driver does):
#   1. structural: the KEPUB chapter's text == the EPUB chapter's text (whitespace-collapsed; and
#      exactly);
#   2. every span resolves with kobo_position.resolve_span as 'span' (not 'span~'), and at the
#      right place: its first visible character's index in the collapsed KEPUB text equals the
#      landed anchor's index in the collapsed EPUB text;
#   3. miss classes, with examples; plus: how many spans are blank (calibre's paragraph-start
#      "\n" spans), whether prefer justification renumbers the chapter, whether calibre's
#      send-to-device metadata update (OPF + cover) changes any span, kepubify seconds and
#      KEPUB size (the cache), and a digest of the span map to compare calibre versions/machines.
# The report records calibre's version, Python's, ICU's (sentence breaking) and a fingerprint
# of the converter's output on a fixed sample. --adversarial adds a synthetic book (poetry,
# a table, nested inline markup, Greek/Russian/Japanese, characters outside the BMP, a very
# long paragraph, an image page, <pre>, entities).
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

import argparse
import bisect
import collections
import glob
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from kobo_position import SPAN, SPAN_APPROX, Book, _norm_index, resolve_span  # noqa: E402
import kobo_ku  # noqa: E402

SAMPLE = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body><p>\nOne morning, when '
          'Gregor Samsa woke from troubled dreams. He lay on his back! "What has happened?" he thought.</p>'
          '<p>Mr. Smith met Dr. Watson at 3 p.m. on the U.S. coast. Ἐν ἀρχῇ ἦν ὁ λόγος. 吾輩は猫である。名前はまだ無い。</p>'
          '</body></html>')


def environment(options):
    from calibre.constants import __version__
    from calibre.ebooks.oeb.polish.kepubify import kepubify_html_data, make_options, serialize_html
    try:
        from calibre.utils.icu import _icu
        icu = getattr(_icu, 'icu_version', None)
    except Exception:
        icu = None
    out = kepubify_html_data(SAMPLE, opts=make_options(prefer_justification=bool(options.get('prefer_justification'))))
    out = out if isinstance(out, bytes) else out.encode('utf-8') if isinstance(out, str) else serialize_html(out)
    return {'calibre': __version__, 'python': sys.version.split()[0], 'icu': icu, 'platform': platform.platform(),
            'options': options, 'converter_fingerprint': hashlib.sha256(out).hexdigest()[:16],
            'driver': dict(zip(('format_map', 'kepubify'), kobo_ku.driver_settings()))}


def visible(norm, idx, offset):
    '''Index in the collapsed text of the first visible character at or after `offset` (a blank
    span's offset sits on whitespace that collapses into the space before the next word).'''
    i = bisect.bisect_left(idx, offset)
    while i < len(norm) and norm[i] == ' ':
        i += 1
    return i


def kepub_chapter(epub, kepub, chapter):
    src = epub.chapters[chapter]
    lowered = [p.lower() for p in kepub.chapters]
    if src.lower() in lowered:
        return lowered.index(src.lower())
    return kepub.chapter_for_source(src)


def span_map(kepub):
    '''{member: [(span id, collapsed text)]} of a KEPUB, in document order.'''
    out = {}
    for i, member in enumerate(kepub.chapters):
        ct = kepub.text(i)
        out[member] = [(sid, ' '.join(ct.id_text.get(sid, '').split())) for sid in ct.ids if sid.startswith('kobo.')]
    return out


def simulate_send(src, tmpdir):
    '''calibre's send to device updates the book's metadata (OPF, cover) in a temp copy before
    the driver kepubifies it: do the same with calibre's own EPUB metadata writer.'''
    from calibre.ebooks.metadata.meta import get_metadata, set_metadata
    copy = os.path.join(tmpdir, 'sent.epub')
    shutil.copy2(src, copy)
    with open(copy, 'r+b') as f:
        mi = get_metadata(f, 'epub')
        mi.title = (mi.title or 'Untitled') + ' (sent to the Kobo)'
        mi.tags = list(mi.tags or []) + ['CalibReader parity']
        f.seek(0)
        set_metadata(f, mi, stream_type='epub')
    return copy


def check_book(path, regenerate, regenerate_pj, tmpdir, examples=5):
    row = {'file': os.path.basename(path), 'epub_bytes': os.path.getsize(path)}
    out = os.path.join(tmpdir, 'out.kepub')
    t = time.monotonic()
    regenerate(path, out)
    row['seconds'] = round(time.monotonic() - t, 3)
    row['kepub_bytes'] = os.path.getsize(out)
    epub, kepub = Book(path), Book(out)
    misses = collections.Counter()
    shown = collections.defaultdict(list)
    counts = collections.Counter()
    digest = hashlib.sha256()
    try:
        row['title_page_added'] = any(kobo_ku.DUMMY_TITLE_PAGE in m for m in kepub.chapters)
        row['chapters'] = len(epub.chapters)
        for ch in range(len(epub.chapters)):
            kch = kepub_chapter(epub, kepub, ch)
            if kch is None:
                misses['no KEPUB chapter for this EPUB chapter'] += 1
                continue
            et, kt = epub.text(ch), kepub.text(kch)
            en, ei = _norm_index(et.text)
            kn, ki = _norm_index(kt.text)
            same = en == kn
            counts['structural_ok'] += same
            counts['exact_equal'] += et.text == kt.text
            if not same:
                misses['chapter text differs (structural)'] += 1
                j = next((k for k, (a, b) in enumerate(zip(en, kn)) if a != b), min(len(en), len(kn)))
                if len(shown['structural']) < examples:
                    shown['structural'].append({'member': epub.chapters[ch], 'at': j, 'epub': en[max(0, j - 30):j + 30],
                                                'kepub': kn[max(0, j - 30):j + 30]})
            for sid, koff in kt.ids.items():
                if not sid.startswith('kobo.'):
                    continue
                counts['spans'] += 1
                text = kt.id_text.get(sid, '')
                digest.update('{}\0{}\0{}\n'.format(epub.chapters[ch], sid, ' '.join(text.split())).encode('utf-8'))
                if not text.strip():
                    counts['blank'] += 1
                hit = resolve_span(epub, kepub, ch, sid)
                if hit is None:
                    cls = 'not resolved'
                elif hit[1] == SPAN_APPROX:
                    # no text of its own or after it (an image, a run of empty blocks): scaled offset
                    right = same and visible(kn, ki, koff) == visible(en, ei, hit[0])
                    cls = ('span~ (no text to match: image / empty blocks), at the right place' if right
                           else 'span~ (its text not found in the EPUB), elsewhere')
                elif not same:
                    cls = 'span, unverifiable place (chapter text differs)'
                elif visible(kn, ki, koff) != visible(en, ei, hit[0]):
                    cls = 'span at the wrong place (another occurrence of its text)'
                else:
                    counts['span_ok'] += 1
                    if not text.strip():
                        counts['blank_ok'] += 1
                    continue
                misses[cls] += 1
                if len(shown[cls]) < examples:
                    shown[cls].append({'member': epub.chapters[ch], 'span': sid, 'text': ' '.join(text.split())[:60],
                                       'blank': not text.strip()})
        # prefer justification on: does it renumber the spans?
        alt = os.path.join(tmpdir, 'pj.kepub')
        regenerate_pj(path, alt)
        alt_book = Book(alt)
        try:
            base, other = span_map(kepub), span_map(alt_book)
        finally:
            alt_book.close()
        row['pj_chapters_renumbered'] = sum(1 for m in base if base[m] and base[m] != other.get(m))
        row['pj_chapters_with_spans'] = sum(1 for m in base if base[m])
        # calibre's send-to-device metadata update before kepubify: any span different?
        sent = os.path.join(tmpdir, 'sent.kepub')
        regenerate(simulate_send(path, tmpdir), sent)
        sent_book = Book(sent)
        try:
            after = span_map(sent_book)
        finally:
            sent_book.close()
        row['send_metadata_chapters_changed'] = sum(1 for m in base if kobo_ku.DUMMY_TITLE_PAGE not in m
                                                    and base[m] != after.get(m))
    finally:
        epub.close()
        kepub.close()
    row.update(counts)
    row['misses'] = dict(misses)
    row['examples'] = dict(shown)
    row['span_map_digest'] = digest.hexdigest()[:16]
    return row


ADVERSARIAL = [
    ('poetry.xhtml', '<h2>Poetry</h2><div class="poem"><p>Line one, the first,<br/>line two; and then<br/>line three.</p>'
                     '</div><p class="verse">Twas brillig, and the slithy toves<br/>Did gyre and gimble in the wabe:<br/>'
                     'All mimsy were the borogoves,<br/>And the mome raths outgrabe.</p>'),
    ('table.xhtml', '<h2>A table</h2><table><tr><th>Name</th><th>Age</th></tr><tr><td>Alice. Bob.</td><td>7</td></tr>'
                    '<tr><td>Carol? Dave!</td><td>12</td></tr></table><p>After the table. The end.</p>'),
    ('inline.xhtml', '<h2>Nested inline</h2><p>He said <i>"Never <b>ever</b>, <span class="x">not once</span>."</i> '
                     'Then left. <a href="#n1" id="r1">1</a> She <em>smiled; <strong>briefly</strong></em>. Done.</p>'
                     '<p><span><span>Deep <i>deeper <b>deepest.</b></i></span></span> Out again.</p>'),
    ('scripts.xhtml', '<h2>Scripts</h2><p lang="el" xml:lang="el">Ἐν ἀρχῇ ἦν ὁ λόγος. Καὶ ὁ λόγος ἦν πρὸς τὸν θεόν.</p>'
                      '<p lang="ru" xml:lang="ru">Все счастливые семьи похожи друг на друга. Каждая несчастливая семья '
                      'несчастлива по-своему.</p><p lang="ja" xml:lang="ja">吾輩は猫である。名前はまだ無い。どこで生れたか'
                      'とんと見当がつかぬ。</p><p>Smile \U0001F600 now. Then \U0001D538 math. Fin.</p>'),
    ('long.xhtml', '<h2>One long paragraph</h2><p>' + ' '.join(
        'Sentence number {} goes on, with a clause, and another clause; then it ends.'.format(i) for i in range(400))
        + '</p>'),
    ('image.xhtml', '<div><img src="../images/plate.png" alt="A plate"/></div>'),
    ('entities.xhtml', '<h2>Entities</h2><p>A&#160;B &amp; C — “quoted”. Mr.&#160;Smith arrived.</p>'
                       '<pre>code  block\n  kept as is</pre><div>  Text straight in a div. Another sentence.</div>'
                       '<p>   </p><p>Last.</p>'),
]
def _png():
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xff\xff\xff')) + chunk(b'IEND', b''))


PNG = _png()


def make_adversarial(path):
    manifest = ['<item id="cover" href="images/cover.png" media-type="image/png" properties="cover-image"/>',
                '<item id="plate" href="images/plate.png" media-type="image/png"/>',
                '<item id="coverpage" href="text/cover.xhtml" media-type="application/xhtml+xml"/>']
    spine = ['<itemref idref="coverpage"/>']
    for i, (name, _) in enumerate(ADVERSARIAL):
        manifest.append('<item id="c{}" href="text/{}" media-type="application/xhtml+xml"/>'.format(i, name))
        spine.append('<itemref idref="c{}"/>'.format(i))
    opf = ('<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
           'unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Adversarial'
           '</dc:title><dc:language>en</dc:language><dc:identifier id="uid">cr-ku-adversarial</dc:identifier>'
           '<meta name="cover" content="cover"/></metadata><manifest>{}</manifest><spine>{}</spine></package>').format(
               ''.join(manifest), ''.join(spine))
    page = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en" xml:lang="en">'
            '<head><title>{}</title></head><body>{}</body></html>')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:'
                   'tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="'
                   'application/oebps-package+xml"/></rootfiles></container>')
        z.writestr('OEBPS/content.opf', opf)
        z.writestr('OEBPS/images/cover.png', PNG)
        z.writestr('OEBPS/images/plate.png', PNG)
        z.writestr('OEBPS/text/cover.xhtml', page.format('Cover', '<div><img src="../images/cover.png" alt="cover"/></div>'))
        for name, body in ADVERSARIAL:
            z.writestr('OEBPS/text/' + name, page.format(name, body))
    return path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--books', required=True, help='a folder of .epub COPIES')
    ap.add_argument('--adversarial', action='store_true')
    ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    options = kobo_ku.calibre_options()
    regenerate = kobo_ku.calibre_kepubify(options)
    regenerate_pj = kobo_ku.calibre_kepubify(dict(options, prefer_justification=not options['prefer_justification']))
    if regenerate is None:
        raise SystemExit('run this under calibre-debug')
    report = {'environment': environment(options), 'books': []}
    paths = sorted(glob.glob(os.path.join(a.books, '*.epub')))
    work = tempfile.mkdtemp(prefix='cr-ku-parity-')
    try:
        if a.adversarial:
            paths.append(make_adversarial(os.path.join(work, 'Adversarial (synthetic).epub')))
        for p in paths:
            tmp = tempfile.mkdtemp(dir=work)
            try:
                row = check_book(p, regenerate, regenerate_pj, tmp)
            except Exception as exc:
                row = {'file': os.path.basename(p), 'error': repr(exc)}
            report['books'].append(row)
            print('{:<60} spans {:>6} ok {:>6} miss {:>4} struct {}/{} {:.2f}s'.format(
                row['file'][:60], row.get('spans', 0), row.get('span_ok', 0), sum(row.get('misses', {}).values()),
                row.get('structural_ok', 0), row.get('chapters', 0), row.get('seconds', 0)), flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    totals = collections.Counter()
    classes = collections.Counter()
    for row in report['books']:
        for k in ('chapters', 'structural_ok', 'exact_equal', 'spans', 'span_ok', 'blank', 'blank_ok', 'epub_bytes',
                  'kepub_bytes', 'pj_chapters_renumbered', 'pj_chapters_with_spans', 'send_metadata_chapters_changed'):
            totals[k] += row.get(k, 0) or 0
        totals['seconds'] += row.get('seconds', 0) or 0
        classes.update(row.get('misses', {}))
        totals['errors'] += 'error' in row
    report['totals'] = dict(totals)
    report['miss_classes'] = dict(classes)
    with open(a.out, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(report, f, indent=1, ensure_ascii=False)
    print(json.dumps({'environment': report['environment'], 'totals': report['totals'],
                      'miss_classes': report['miss_classes']}, indent=1, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())

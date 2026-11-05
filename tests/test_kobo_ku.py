# Last modified: 2026-11-05--1033
#
# test_kobo_ku.py — the Kobo Utilities (USB) route, Kobo -> CalibReader (plugin 1.7.0, tester
# preview): kobo_ku.py. Design: docs/handoffs/104-kobo-sync/KOBO-UTILITIES.md section 8.
#
#   * the location parser (8.3), as a TABLE: every stored shape -> what it parses as. A new
#     sample shape from a tester is one row here and, when it is a new fragment form, one
#     row in kobo_ku.FRAGMENT_RULES;
#   * the mapping to CalibReader's place, with the precision labels the app's 8.3 rule reads;
#   * the import (8.5 stamps, 8.6 change test, newer-wins merge into CalibReader_positions.json
#     through step 1's writer), the kepub cache (ruling 6: 200 MB, LRU), the column check
#     (8.2: < 50 % parse = refused) and the staleness text (8.6).
#
# Plain Python: the KEPUB is either the committed kepubify-4.0.4 Metamorphosis KEPUB (a
# stored KEPUB, as calibre's Kobo driver sends it) or a small synthetic pair. calibre's own
# kepubify runs in tests/test_real_calibre.py (calibre-debug) and the parity run.

import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import kobo_ku  # noqa: E402
from kobo_position import Book  # noqa: E402

EPUB = os.path.join(HERE, 'fixtures', 'metamorphosis.epub')
KEPUB = os.path.join(HERE, 'fixtures', 'metamorphosis.kepub')
CH = 'OEBPS/7446794606207039498_5200-h-{}.htm.xhtml'   # spine 1..5 = h-0..h-4 (chapter index 1..5)
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
SOURCE = {'id': 'ku', 'label': 'Kobo Utilities', 'location': '#kobo_reading_location',
          'percent': '#kobo_percent_read', 'last_read': '#kobo_last_read', 'boundReader': None}


def norm(s):
    return ' '.join(s.split())


def kepub_span_text(member, span_id, path=KEPUB):
    with zipfile.ZipFile(path) as z:
        html = z.read(member).decode('utf-8')
    m = re.search(r'id="' + re.escape(span_id) + r'">([^<]*)<', html)
    return norm(m.group(1)) if m else None


def epub_text_at(chapter, anchor, n=40, path=EPUB):
    b = Book(path)
    try:
        return norm(b.text(chapter).text[anchor:anchor + n])
    finally:
        b.close()


CONTAINER = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
             '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
             '</rootfiles></container>')


def make_book(path, chapters):
    '''A tiny EPUB (or KEPUB): chapters = [(file name under OEBPS/text/, body html)].'''
    manifest = ''.join('<item id="c{}" href="text/{}" media-type="application/xhtml+xml"/>'.format(i, n)
                       for i, (n, _) in enumerate(chapters))
    spine = ''.join('<itemref idref="c{}"/>'.format(i) for i in range(len(chapters)))
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', CONTAINER)
        z.writestr('OEBPS/content.opf', '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="3.0"><metadata/><manifest>{}</manifest><spine>{}</spine></package>'.format(manifest, spine))
        for name, body in chapters:
            z.writestr('OEBPS/text/' + name, '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/'
                       'xhtml"><head><title>t</title></head><body>{}</body></html>'.format(body))
    return path


def span(sid, text):
    return '<span class="koboSpan" id="{}">{}</span>'.format(sid, text)


# --------------------------------------------------------------------------- 8.3 parser table

# (stored value, expected form, expected href, expected span / cfi value or None)
PARSE_TABLE = [
    # the MobileRead sample (enuddleyarbl): chapter href (relative to the OPF) + KoboSpan
    ('text/titlepage.xhtml#kobo.1.1', 'span', 'text/titlepage.xhtml', 'kobo.1.1'),
    ('  text/titlepage.xhtml#kobo.1.1\n', 'span', 'text/titlepage.xhtml', 'kobo.1.1'),
    # device-prefixed ContentID shapes: the book's file, then '!' or '!!', then the chapter
    ('file:///mnt/onboard/Kafka, Franz/The Metamorphosis - Franz Kafka.kepub.epub!!' + CH.format(1) + '#kobo.2.3',
     'span', CH.format(1), 'kobo.2.3'),
    ('file:///mnt/onboard/Dracula.kepub.epub!OEBPS!ch08.xhtml', 'chapter', 'OEBPS/ch08.xhtml', None),
    ('/mnt/onboard/Dracula.kepub.epub!OEBPS/ch08.xhtml#kobo.17.6', 'span', 'OEBPS/ch08.xhtml', 'kobo.17.6'),
    ('file:///mnt/onboard/x.kepub.epub!!text/ch.xhtml', 'chapter', 'text/ch.xhtml', None),
    # URL-encoded hrefs; the fragment is split off BEFORE decoding (an encoded # stays in the href)
    ('OEBPS/Text/chapter%2008.xhtml#kobo.3.2', 'span', 'OEBPS/Text/chapter 08.xhtml', 'kobo.3.2'),
    ('text/a%23b.xhtml#kobo.1.1', 'span', 'text/a#b.xhtml', 'kobo.1.1'),
    # chapter only (ChapterIDBookmarked without a fragment)
    ('OEBPS/ch08.xhtml', 'chapter', 'OEBPS/ch08.xhtml', None),
    ('Text/ch1.html', 'chapter', 'Text/ch1.html', None),
    ('ch1.htm', 'chapter', 'ch1.htm', None),
    # the span written the way the Kobo's Bookmark table / CSS escapes it
    ('OEBPS/ch08.xhtml#kobo\\.17\\.6', 'span', 'OEBPS/ch08.xhtml', 'kobo.17.6'),
    ('OEBPS/ch08.xhtml#span#kobo\\.17\\.6', 'span', 'OEBPS/ch08.xhtml', 'kobo.17.6'),
    # an EPUB CFI fragment (if any firmware writes one)
    ('OEBPS/ch08.xhtml#epubcfi(/6/22!/4/2/14/1:0)', 'cfi', 'OEBPS/ch08.xhtml', 'epubcfi(/6/22!/4/2/14/1:0)'),
    # old-firmware EPUB: chapter |@ @| adobe_location (used only when it is a CFI)
    ('OEBPS/ch08.xhtml|@ @|/6/22[ch08]!/4/2/14/1:0', 'chapter', 'OEBPS/ch08.xhtml', None),
    ('OEBPS/ch08.xhtml|@ @|epubcfi(/6/22!/4/2/14/1:0)', 'cfi', 'OEBPS/ch08.xhtml', 'epubcfi(/6/22!/4/2/14/1:0)'),
    # a fragment of a shape we have not seen: the chapter, the fragment kept for the testers' reports
    ('OEBPS/ch08.xhtml#point(/1/4/2:0)', 'chapter', 'OEBPS/ch08.xhtml', None),
    # a comments column hand-edited in calibre wraps the value in HTML
    ('<p>text/titlepage.xhtml#kobo.1.1</p>', 'span', 'text/titlepage.xhtml', 'kobo.1.1'),
    ('<div><p>OEBPS/ch08.xhtml</p></div>', 'chapter', 'OEBPS/ch08.xhtml', None),
]

NOT_LOCATIONS = [None, '', '   ', 'Fantasy', '42', '#kobo.1.1', 'https://example.com/', 'Read twice; loved it.',
                 'kobo.1.1', '2026-11-01']


class ParserTableTests(unittest.TestCase):

    def test_the_table(self):
        for value, form, href, extra in PARSE_TABLE:
            with self.subTest(value=value):
                p = kobo_ku.parse_location(value)
                self.assertIsNotNone(p, value)
                self.assertEqual((p['form'], p['href']), (form, href))
                if form == 'span':
                    self.assertEqual(p['span'], extra)
                elif form == 'cfi':
                    self.assertEqual(p['cfi'], extra)

    def test_not_locations(self):
        for value in NOT_LOCATIONS:
            with self.subTest(value=value):
                self.assertIsNone(kobo_ku.parse_location(value))

    def test_a_device_prefix_is_remembered_and_the_last_part_is_a_second_candidate(self):
        p = kobo_ku.parse_location('file:///mnt/onboard/Dracula.kepub.epub!OEBPS!ch08.xhtml')
        self.assertEqual(p['shape'], 'device')
        self.assertIn('ch08.xhtml', p['hrefs'])
        self.assertEqual(kobo_ku.parse_location('text/titlepage.xhtml#kobo.1.1')['shape'], 'bare')

    def test_unknown_fragment_and_adobe_location_are_kept(self):
        self.assertEqual(kobo_ku.parse_location('OEBPS/ch08.xhtml#point(/1/4/2:0)')['fragment'], 'point(/1/4/2:0)')
        self.assertEqual(kobo_ku.parse_location('OEBPS/ch08.xhtml|@ @|/6/22[ch08]!/4/2/14/1:0')['adobe'],
                         '/6/22[ch08]!/4/2/14/1:0')

    def test_a_new_sample_shape_is_a_one_line_rule(self):
        # 8.11 (5): real samples come later; a new fragment form is one row in FRAGMENT_RULES.
        rules = list(kobo_ku.FRAGMENT_RULES)
        try:
            kobo_ku.FRAGMENT_RULES.append(('test: kobo point', re.compile(r'^kpt:(kobo\.\d+\.\d+)$'), 'span'))
            p = kobo_ku.parse_location('OEBPS/ch08.xhtml#kpt:kobo.4.2')
            self.assertEqual((p['form'], p['span'], p['rule']), ('span', 'kobo.4.2', 'test: kobo point'))
        finally:
            kobo_ku.FRAGMENT_RULES[:] = rules

    def test_describe_is_the_live_preview(self):
        self.assertEqual(kobo_ku.describe('text/titlepage.xhtml#kobo.1.1'), 'text/titlepage.xhtml#kobo.1.1 -> span')
        self.assertEqual(kobo_ku.describe('OEBPS/ch08.xhtml'), 'OEBPS/ch08.xhtml -> chapter')
        self.assertEqual(kobo_ku.describe('Fantasy'), 'Fantasy -> not a location')
        self.assertTrue(kobo_ku.describe('x' * 300).endswith('-> not a location'))
        self.assertLess(len(kobo_ku.describe('text/' + 'a' * 300 + '.xhtml#kobo.1.1')), 120)


class ColumnCheckTests(unittest.TestCase):
    '''8.2: a column where fewer than half of the non-empty values parse is refused.'''

    def test_mostly_locations_is_accepted(self):
        r = kobo_ku.column_check(['text/a.xhtml#kobo.1.1', 'text/b.xhtml', None, 'junk', 'OEBPS/c.xhtml#kobo.2.2'])
        self.assertEqual((r['ok'], r['parsed'], r['nonempty']), (True, 3, 4))

    def test_half_is_enough(self):
        self.assertTrue(kobo_ku.column_check(['text/a.xhtml', 'junk'])['ok'])

    def test_a_wrong_column_is_refused_with_a_message(self):
        r = kobo_ku.column_check(['Fantasy', 'Horror', 'text/a.xhtml', 'Romance'])
        self.assertFalse(r['ok'])
        self.assertIn('does not look like a Kobo Utilities location column', r['message'])

    def test_an_empty_column_is_accepted(self):
        r = kobo_ku.column_check([None, '', '  '])
        self.assertEqual((r['ok'], r['nonempty']), (True, 0))
        self.assertIn('empty', r['message'])


# --------------------------------------------------------------------------- mapping

class MappingTests(unittest.TestCase):
    '''parsed location (+ the Kobo's %) -> CalibReader's place, on The Metamorphosis.'''

    @classmethod
    def setUpClass(cls):
        cls.epub = Book(EPUB)
        cls.kepub = Book(KEPUB)
        cls.lengths = cls.epub.lengths()
        cls.total = float(sum(cls.lengths))

    @classmethod
    def tearDownClass(cls):
        cls.epub.close()
        cls.kepub.close()

    def kepub_of(self):
        return lambda: (self.kepub, None)

    def pct_of(self, place):
        return 100.0 * (sum(self.lengths[:place['chapter']]) + place['textAnchor']) / self.total

    def test_a_span_lands_on_its_sentence(self):
        p = kobo_ku.parse_location(CH.format(1) + '#kobo.10.2')
        place, why = kobo_ku.map_ku(self.epub, p, kepub=self.kepub_of())
        self.assertIsNone(why)
        self.assertEqual((place['chapter'], place['precision']), (2, kobo_ku.SPAN))
        want = kepub_span_text(CH.format(1), 'kobo.10.2')
        self.assertTrue(epub_text_at(2, place['textAnchor'], len(want) + 10).startswith(want[:30]), want)

    def test_an_href_relative_to_the_opf_or_a_bare_file_name_is_found(self):
        for href in (CH.format(1)[len('OEBPS/'):], '7446794606207039498_5200-h-1.htm.xhtml'):
            place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location(href + '#kobo.10.2'), kepub=self.kepub_of())
            self.assertEqual((place['chapter'], place['precision']), (2, kobo_ku.SPAN), href)

    def test_a_device_prefixed_value_lands_like_the_bare_one(self):
        bare, _ = kobo_ku.map_ku(self.epub, kobo_ku.parse_location(CH.format(2) + '#kobo.5.1'), kepub=self.kepub_of())
        dev, _ = kobo_ku.map_ku(self.epub, kobo_ku.parse_location(
            'file:///mnt/onboard/Kafka/Metamorphosis.kepub.epub!!' + CH.format(2) + '#kobo.5.1'), kepub=self.kepub_of())
        self.assertEqual(bare, dev)

    def test_chapter_only_is_the_chapter_start(self):
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location(CH.format(3)))
        self.assertEqual((place['chapter'], place['textAnchor'], place['precision']), (4, 0, kobo_ku.CHAPTER))

    def test_chapter_only_with_a_percent_keeps_the_start_and_takes_the_percent_as_progress(self):
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location(CH.format(3)), percent=63)
        self.assertEqual((place['chapter'], place['textAnchor'], place['precision']), (4, 0, kobo_ku.CHAPTER))
        self.assertAlmostEqual(place['progress'], 0.63)

    def test_percent_only(self):
        place, why = kobo_ku.map_ku(self.epub, None, percent=40)
        self.assertEqual(place['precision'], kobo_ku.PERCENT)
        self.assertAlmostEqual(self.pct_of(place), 40, delta=0.5)
        for pct in (0, 100, None):
            place, why = kobo_ku.map_ku(self.epub, None, percent=pct)
            self.assertIsNone(place, pct)
            self.assertTrue(why)

    def test_an_href_not_in_this_book_is_no_place(self):
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location('text/ch99.xhtml#kobo.1.1'), percent=40,
                                    kepub=self.kepub_of())
        self.assertIsNone(place)
        self.assertIn('not in this book', why)

    def test_calibres_generated_title_page_is_not_a_reading_place(self):
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location(
            'kobo-title-page-generated-by-calibre.xhtml#kobo.1.1'), kepub=self.kepub_of())
        self.assertIsNone(place)
        self.assertIn('title page', why)

    def test_a_span_not_in_the_kepub_is_no_place(self):
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location(CH.format(1) + '#kobo.999.1'), kepub=self.kepub_of())
        self.assertIsNone(place)
        self.assertIn('not in', why)

    def test_without_a_kepub_a_span_is_the_chapter(self):
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location(CH.format(1) + '#kobo.10.2'),
                                    kepub=lambda: (None, 'calibre\'s KEPUB converter is not available here'))
        self.assertEqual((place['chapter'], place['textAnchor'], place['precision']), (2, 0, kobo_ku.CHAPTER))
        self.assertIn('not available', place['note'])
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location(CH.format(1) + '#kobo.10.2'))
        self.assertEqual(place['precision'], kobo_ku.CHAPTER)

    def test_a_span_far_from_the_kobos_percent_is_not_trusted(self):
        p = kobo_ku.parse_location(CH.format(1) + '#kobo.10.2')
        ok, _ = kobo_ku.map_ku(self.epub, p, percent=round(self.pct_of(
            kobo_ku.map_ku(self.epub, p, kepub=self.kepub_of())[0])), kepub=self.kepub_of())
        self.assertEqual(ok['precision'], kobo_ku.SPAN)
        # 45 %: the span (~4 %) is not trusted; the chapter (0.8-28 %) still fits within 25 points
        place, why = kobo_ku.map_ku(self.epub, p, percent=45, kepub=self.kepub_of())
        self.assertEqual((place['chapter'], place['textAnchor'], place['precision']), (2, 0, kobo_ku.CHAPTER))
        self.assertIn('span not trusted', place['note'])
        # 95 %: not even the chapter fits: no place (M5)
        place, why = kobo_ku.map_ku(self.epub, p, percent=95, kepub=self.kepub_of())
        self.assertIsNone(place)
        self.assertIn('does not fit', why)

    def test_a_cfi_resolves_in_its_chapter(self):
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location(CH.format(1) + '#epubcfi(/6/6!/4/2/6/1:1)'))
        self.assertEqual((place['chapter'], place['precision']), (2, kobo_ku.CFI))
        self.assertGreater(place['textAnchor'], 0)

    def test_old_firmware_adobe_location_is_the_chapter(self):
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location(CH.format(1) + '|@ @|/6/6[x]!/4/2/6/1:1'))
        self.assertEqual((place['chapter'], place['textAnchor'], place['precision']), (2, 0, kobo_ku.CHAPTER))

    def test_an_unknown_fragment_is_the_chapter(self):
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location(CH.format(1) + '#point(/1/4/2:0)'))
        self.assertEqual((place['chapter'], place['textAnchor'], place['precision']), (2, 0, kobo_ku.CHAPTER))


class BlankSpanTests(unittest.TestCase):
    '''calibre's converter (prefer justification off, its default) puts a whitespace-only span
    at the start of most paragraphs ("\\n" in kobo.N.1). It is where a page that starts a
    paragraph begins: it lands on the text that follows it (ruling, DESIGN.md step 4).'''

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='cr-ku-blank-')
        body = '<h2>One</h2>\n<p>\nIt was a dark night. The wind blew hard.</p>\n<p>\nMorning came. Nobody slept.</p>'
        kbody = ('<div id="book-columns"><div id="book-inner"><h2>' + span('kobo.1.1', 'One') + '</h2>\n<p>'
                 + span('kobo.2.1', '\n') + span('kobo.2.2', 'It was a dark night. ')
                 + span('kobo.2.3', 'The wind blew hard.') + '</p>\n<p>' + span('kobo.3.1', '\n')
                 + span('kobo.3.2', 'Morning came. ') + span('kobo.3.3', 'Nobody slept.') + '</p></div></div>')
        self.epub = Book(make_book(os.path.join(self.dir, 'b.epub'), [('ch1.xhtml', body)]))
        self.kepub = Book(make_book(os.path.join(self.dir, 'b.kepub'), [('ch1.xhtml', kbody)]))

    def tearDown(self):
        self.epub.close()
        self.kepub.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_blank_span_lands_on_the_text_after_it(self):
        text = self.epub.text(0).text
        for sid, want in (('kobo.2.1', 'It was a dark'), ('kobo.3.1', 'Morning came'), ('kobo.3.3', 'Nobody slept')):
            place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location('text/ch1.xhtml#' + sid),
                                        kepub=lambda: (self.kepub, None))
            self.assertEqual(place['precision'], kobo_ku.SPAN, sid)
            self.assertTrue(text[place['textAnchor']:].startswith(want), (sid, text[place['textAnchor']:][:20]))


# --------------------------------------------------------------------------- driver plan

class DriverPlanTests(unittest.TestCase):
    '''Which KEPUB the spans are numbered in: the one calibre's Kobo driver sends today.'''

    def test_a_stored_kepub_is_sent_as_it_is_when_the_driver_lists_kepub_first(self):
        self.assertEqual(kobo_ku.driver_plan({'EPUB', 'KEPUB'}, ['kepub', 'epub'], True), 'kepub')

    def test_an_epub_alone_is_kepubified_by_the_driver(self):
        self.assertEqual(kobo_ku.driver_plan({'EPUB'}, ['kepub', 'epub'], True), 'kepubify')

    def test_the_users_order_and_the_kepubify_switch_are_followed(self):
        self.assertEqual(kobo_ku.driver_plan({'EPUB', 'KEPUB'}, ['epub', 'kepub'], True), 'kepubify')
        self.assertEqual(kobo_ku.driver_plan({'EPUB'}, ['kepub', 'epub'], False), 'epub')
        self.assertIsNone(kobo_ku.driver_plan({'PDF'}, ['kepub', 'epub', 'pdf'], True))


# --------------------------------------------------------------------------- import

class ImportBase(unittest.TestCase):

    def setUp(self):
        self.lib = tempfile.mkdtemp(prefix='cr-ku-lib-')
        self.path = os.path.join(self.lib, 'CalibReader_positions.json')
        self.uuids = {i: '0000000{}-aaaa-bbbb-cccc-dddddddddddd'.format(i) for i in range(1, 10)}
        self.memory = {}
        self.kepub_calls = []

    def tearDown(self):
        shutil.rmtree(self.lib, ignore_errors=True)

    def lookup(self, book_id):
        if book_id not in self.uuids:
            return None
        return self.uuids[book_id], EPUB, None

    def kepub_for(self, book_id, epub_path, kepub_path):
        self.kepub_calls.append(book_id)
        return KEPUB, None, {'plan': 'kepub'}

    def write_positions(self, entries):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'schema': 1, 'positions': entries}, f)

    def positions(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path, encoding='utf-8') as f:
            return json.load(f)['positions']

    def raw(self):
        with open(self.path, 'rb') as f:
            return f.read()

    def run_import(self, values, source=SOURCE, now=NOW, **kw):
        kw.setdefault('kepub_for', self.kepub_for)
        return kobo_ku.import_ku(self.lib, source, values, self.lookup, self.memory, now=now, **kw)


SPAN_VALUE = CH.format(1) + '#kobo.10.2'


class ImportTests(ImportBase):

    def test_a_ku_span_lands_in_the_positions_file(self):
        res = self.run_import({1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')})
        self.assertEqual(res.written, [self.uuids[1]], (res.skipped, res.notes))
        e = self.positions()[self.uuids[1]]
        self.assertEqual((e['deviceId'], e['chapter'], e['page'], e['scrollY']), ('kobo-utilities', 2, 0, 0))
        self.assertEqual(e['updated_at'], '2026-11-01T10:00:00.000Z')
        self.assertEqual(e['kobo']['precision'], 'span')
        self.assertEqual(e['kobo']['location'], SPAN_VALUE)
        self.assertEqual(e['kobo']['stamp'], 'last-read')
        self.assertAlmostEqual(e['progress'], 0.04)
        want = kepub_span_text(CH.format(1), 'kobo.10.2')
        self.assertTrue(epub_text_at(2, e['textAnchor'], 60).startswith(want[:30]))
        self.assertTrue(res.memory_changed)

    def test_a_re_run_writes_nothing_and_maps_nothing(self):
        values = {1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z'), 2: (CH.format(3), 70, '2026-11-02T10:00:00Z')}
        self.run_import(values)
        before = self.raw()
        calls = len(self.kepub_calls)
        res = self.run_import(values)
        self.assertEqual((res.written, res.memory_changed), ([], False))
        self.assertEqual(self.raw(), before)
        self.assertEqual(len(self.kepub_calls), calls)          # 8.6: only changed books are mapped

    def test_a_newer_app_place_wins_and_is_not_asked_again(self):
        self.write_positions({self.uuids[1]: {'chapter': 4, 'textAnchor': 100, 'progress': 0.6, 'page': 0,
                                              'scrollY': 0, 'updated_at': '2026-11-03T10:00:00.000Z',
                                              'deviceId': 'phone-1'}})
        res = self.run_import({1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')})
        self.assertEqual(res.written, [])
        self.assertEqual(self.positions()[self.uuids[1]]['deviceId'], 'phone-1')
        res = self.run_import({1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')})
        self.assertEqual((res.written, len(self.kepub_calls)), ([], 0))     # decided without mapping it

    def test_an_older_app_place_is_replaced_and_a_tie_goes_to_the_app(self):
        app = {'chapter': 1, 'textAnchor': 10, 'progress': 0.01, 'page': 0, 'scrollY': 0,
               'updated_at': '2026-11-01T10:00:00.000Z', 'deviceId': 'phone-1'}
        self.write_positions({self.uuids[1]: dict(app)})
        res = self.run_import({1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')})          # same second: tie
        self.assertEqual(res.written, [])
        res = self.run_import({1: (SPAN_VALUE, 4, '2026-11-01T10:00:01Z')})          # a later Kobo read
        self.assertEqual(res.written, [self.uuids[1]])

    def test_a_kobo_move_with_a_new_date_is_imported_again(self):
        self.run_import({1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')})
        res = self.run_import({1: (CH.format(2) + '#kobo.5.1', 33, '2026-11-02T09:00:00Z')})
        self.assertEqual(res.written, [self.uuids[1]])
        self.assertEqual(self.positions()[self.uuids[1]]['chapter'], 3)

    def test_a_changed_location_under_the_same_last_read_date_is_not_a_new_place(self):
        # 8.5: with a last-read column, a new location must come with a new date (M5 analog)
        self.run_import({1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')})
        before = self.raw()
        res = self.run_import({1: (CH.format(2) + '#kobo.5.1', 33, '2026-11-01T10:00:00Z')})
        self.assertEqual(res.written, [])
        self.assertIn('date', res.skipped[self.uuids[1]])
        self.assertEqual(self.raw(), before)

    def test_a_kobo_date_in_the_future_is_skipped_and_looked_at_again(self):
        res = self.run_import({1: (SPAN_VALUE, 4, '2026-11-05T13:00:00Z')})          # an hour ahead
        self.assertEqual(res.written, [])
        self.assertTrue(any('future' in n for n in res.notes), res.notes)
        res = self.run_import({1: (SPAN_VALUE, 4, '2026-11-05T13:00:00Z')},
                              now=datetime.datetime(2026, 11, 5, 14, 0, tzinfo=UTC))
        self.assertEqual(res.written, [self.uuids[1]])

    def test_last_read_as_a_calibre_datetime(self):
        lr = datetime.datetime(2026, 11, 1, 2, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-8)))
        self.run_import({1: (SPAN_VALUE, 4, lr)})
        self.assertEqual(self.positions()[self.uuids[1]]['updated_at'], '2026-11-01T10:00:00.000Z')

    def test_a_finer_calibre_web_place_in_the_same_chapter_is_kept(self):
        cw = {'chapter': 2, 'textAnchor': 5000, 'progress': 0.05, 'page': 0, 'scrollY': 0,
              'updated_at': '2026-10-30T10:00:00.000Z', 'deviceId': 'kobo-calibre-web',
              'kobo': {'type': 'KoboSpan', 'value': 'kobo.10.2', 'source': CH.format(1), 'precision': 'span'}}
        self.write_positions({self.uuids[1]: cw})
        res = self.run_import({1: (CH.format(1), 4, '2026-11-01T10:00:00Z')})
        self.assertEqual(res.written, [])
        self.assertIn('Calibre-Web', res.skipped[self.uuids[1]])
        # newest wins otherwise: a KU span (as fine) or a later chapter replaces it
        res = self.run_import({1: (CH.format(2), 30, '2026-11-02T10:00:00Z')})
        self.assertEqual(res.written, [self.uuids[1]])

    def test_percent_only(self):
        res = self.run_import({1: (None, 40, '2026-11-01T10:00:00Z'), 2: (None, 100, '2026-11-01T10:00:00Z'),
                               3: (None, 0, '2026-11-01T10:00:00Z')})
        self.assertEqual(res.written, [self.uuids[1]])
        self.assertEqual(self.positions()[self.uuids[1]]['kobo']['precision'], 'percent')
        self.assertIn(self.uuids[2], res.skipped)

    def test_an_unreadable_location_is_skipped_with_the_value_named(self):
        res = self.run_import({1: ('Fantasy', 40, '2026-11-01T10:00:00Z')})
        self.assertEqual(res.written, [])
        self.assertIn('Fantasy', res.skipped[self.uuids[1]])

    def test_books_not_in_this_library_and_books_without_an_epub(self):
        self.uuids.pop(3)
        res = self.run_import({3: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')})
        self.assertIn(3, res.skipped)
        res = kobo_ku.import_ku(self.lib, SOURCE, {1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')},
                                lambda b: (self.uuids[b], None, None), self.memory, now=NOW)
        self.assertIn('EPUB', res.skipped[self.uuids[1]])

    def test_a_failed_book_is_not_tried_again_until_its_value_changes(self):
        broken = os.path.join(self.lib, 'broken.epub')
        with open(broken, 'wb') as f:
            f.write(b'not a zip')
        lookup = lambda b: (self.uuids[b], broken, None)  # noqa: E731
        res = kobo_ku.import_ku(self.lib, SOURCE, {1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')}, lookup,
                                self.memory, now=NOW, kepub_for=self.kepub_for)
        self.assertIn('could not read', res.skipped[self.uuids[1]])
        res = kobo_ku.import_ku(self.lib, SOURCE, {1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')}, lookup,
                                self.memory, now=NOW, kepub_for=self.kepub_for)
        self.assertEqual((res.skipped, res.written, res.more), ({}, [], False))

    def test_a_damaged_positions_file_is_never_overwritten(self):
        with open(self.path, 'w') as f:
            f.write('{not json')
        res = self.run_import({1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')})
        self.assertEqual(res.written, [])
        self.assertTrue(res.notes)
        with open(self.path) as f:
            self.assertEqual(f.read(), '{not json')
        self.assertEqual(self.memory, {})                     # tried again next run

    def test_at_most_max_books_are_mapped_per_run_then_more(self):
        self.uuids = {i: '{:08d}-aaaa-bbbb-cccc-dddddddddddd'.format(i) for i in range(1, 31)}
        values = {i: (CH.format(i % 5), None, '2026-11-01T10:00:{:02d}Z'.format(i)) for i in range(1, 31)}
        res = self.run_import(values, max_books=25)
        self.assertEqual((len(res.written), res.more), (25, True))
        res = self.run_import(values, max_books=25)
        self.assertEqual((len(res.written), res.more), (5, False))

    def test_the_regeneration_budget_stops_starting_new_kepubs(self):
        ticks = iter([0.0, 0.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        values = {1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z'), 2: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')}
        res = self.run_import(values, budget=3.0, clock=lambda: next(ticks))
        self.assertEqual((len(res.written), res.more), (1, True))
        res = self.run_import(values, budget=3.0)
        self.assertEqual(len(res.written), 1)

    def test_metadata_is_never_written(self):
        # nothing but the positions file appears in the library folder
        self.run_import({1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')})
        self.assertEqual(sorted(os.listdir(self.lib)), ['CalibReader_positions.json'])


class FirstSeenStampTests(ImportBase):
    '''8.5: without a last-read column the stamp is the plugin's first-seen time; values that
    were already there when the column was picked are not dated, so they never override a
    place CalibReader already has (ruling, DESIGN.md step 4).'''

    NOLR = dict(SOURCE, last_read='')

    def test_first_run_baselines_books_the_app_has_and_imports_the_others(self):
        self.write_positions({self.uuids[1]: {'chapter': 1, 'textAnchor': 0, 'progress': 0.0, 'page': 0, 'scrollY': 0,
                                              'updated_at': '2026-10-01T10:00:00.000Z', 'deviceId': 'phone-1'}})
        res = self.run_import({1: (SPAN_VALUE, 4, None), 2: (CH.format(3), 70, None)}, source=self.NOLR)
        self.assertEqual(res.written, [self.uuids[2]])
        self.assertIn('before', res.skipped[self.uuids[1]])
        e = self.positions()[self.uuids[2]]
        self.assertEqual((e['updated_at'], e['kobo']['stamp']), ('2026-11-05T12:00:00.000Z', 'first-seen'))
        # a later Kobo move is dated when it is first seen
        later = NOW + datetime.timedelta(hours=1)
        res = self.run_import({1: (CH.format(2) + '#kobo.5.1', 33, None), 2: (CH.format(3), 70, None)},
                              source=self.NOLR, now=later)
        self.assertEqual(res.written, [self.uuids[1]])
        self.assertEqual(self.positions()[self.uuids[1]]['updated_at'], '2026-11-05T13:00:00.000Z')

    def test_changing_the_chosen_columns_is_a_first_run_again(self):
        self.write_positions({self.uuids[1]: {'chapter': 1, 'textAnchor': 0, 'progress': 0.0, 'page': 0, 'scrollY': 0,
                                              'updated_at': '2026-10-01T10:00:00.000Z', 'deviceId': 'phone-1'}})
        memories = {}
        sig1 = kobo_ku.source_signature(self.NOLR)
        self.memory = memories.setdefault(sig1, {})
        self.run_import({1: (SPAN_VALUE, 4, None)}, source=self.NOLR)
        other = dict(self.NOLR, percent='')
        self.assertNotEqual(kobo_ku.source_signature(other), sig1)
        self.memory = memories.setdefault(kobo_ku.source_signature(other), {})
        res = self.run_import({1: (SPAN_VALUE, None, None)}, source=other)
        self.assertEqual(res.written, [])                       # not a mass re-date of old values

    def test_an_unchanged_value_keeps_its_first_seen_time(self):
        res = self.run_import({2: (CH.format(3), 70, None)}, source=self.NOLR)
        first = self.positions()[self.uuids[2]]['updated_at']
        res = self.run_import({2: (CH.format(3), 70, None)}, source=self.NOLR, now=NOW + datetime.timedelta(days=1))
        self.assertEqual((res.written, self.positions()[self.uuids[2]]['updated_at']), ([], first))


# --------------------------------------------------------------------------- columns read-only

class SqliteColumnsTests(unittest.TestCase):
    '''The CLI / e2e reader: metadata.db opened read-only, calibre's custom column tables.'''

    def setUp(self):
        self.lib = tempfile.mkdtemp(prefix='cr-ku-db-')
        db = os.path.join(self.lib, 'metadata.db')
        con = sqlite3.connect(db)
        con.executescript('''
            CREATE TABLE books (id INTEGER PRIMARY KEY, uuid TEXT, path TEXT, title TEXT);
            CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT);
            CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT, name TEXT, datatype TEXT,
                                         is_multiple BOOL, normalized BOOL);
            CREATE TABLE custom_column_1 (id INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE books_custom_column_1_link (id INTEGER PRIMARY KEY, book INTEGER, value INTEGER);
            CREATE TABLE custom_column_2 (id INTEGER PRIMARY KEY, book INTEGER, value INT);
            CREATE TABLE custom_column_3 (id INTEGER PRIMARY KEY, book INTEGER, value TIMESTAMP);
            CREATE TABLE custom_column_4 (id INTEGER PRIMARY KEY, book INTEGER, value TEXT);
            INSERT INTO books VALUES (1, 'u1', 'A/B (1)', 'B'), (2, 'u2', 'A/C (2)', 'C'), (3, 'u3', 'A/D (3)', 'D');
            INSERT INTO custom_columns VALUES (1, 'kobo_reading_location', 'Location', 'text', 0, 1),
                (2, 'kobo_percent_read', 'Percent', 'int', 0, 0), (3, 'kobo_last_read', 'Last', 'datetime', 0, 0),
                (4, 'kobo_loc_comments', 'Loc C', 'comments', 0, 0);
            INSERT INTO custom_column_1 VALUES (10, 'text/titlepage.xhtml#kobo.1.1'), (11, 'OEBPS/ch08.xhtml');
            INSERT INTO books_custom_column_1_link VALUES (1, 1, 10), (2, 2, 11);
            INSERT INTO custom_column_2 VALUES (1, 1, 3), (2, 3, 40);
            INSERT INTO custom_column_3 VALUES (1, 1, '2026-11-01 10:20:30+00:00');
            INSERT INTO custom_column_4 VALUES (1, 2, 'OEBPS/x.xhtml#kobo.2.2');
        ''')
        con.commit()
        con.close()
        with open(db, 'rb') as f:
            self.hash = hashlib.sha256(f.read()).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.lib, ignore_errors=True)

    def test_values_and_metadata_untouched(self):
        values = kobo_ku.read_columns_sqlite(self.lib, SOURCE)
        self.assertEqual(values[1], ('text/titlepage.xhtml#kobo.1.1', 3, '2026-11-01T10:20:30.000Z'))
        self.assertEqual(values[2], ('OEBPS/ch08.xhtml', None, None))
        self.assertEqual(values[3], (None, 40, None))
        comments = kobo_ku.read_columns_sqlite(self.lib, dict(SOURCE, location='#kobo_loc_comments', percent='',
                                                               last_read=''))
        self.assertEqual(comments, {2: ('OEBPS/x.xhtml#kobo.2.2', None, None)})
        with open(os.path.join(self.lib, 'metadata.db'), 'rb') as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), self.hash)

    def test_a_missing_column_is_an_error(self):
        with self.assertRaises(ValueError):
            kobo_ku.read_columns_sqlite(self.lib, dict(SOURCE, location='#nope'))


# --------------------------------------------------------------------------- the kepub cache

class CacheTests(unittest.TestCase):
    '''Ruling 6: regenerated kepubs are cached, 200 MB in all, least recently used first out;
    keyed by book + format file mtime/size + calibre version + driver options; safe to delete.'''

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='cr-ku-cache-')
        self.cache = os.path.join(self.dir, 'cache')
        self.made = []
        self.src = os.path.join(self.dir, 'a.epub')
        shutil.copy2(EPUB, self.src)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def regenerate(self, src, out):
        self.made.append(src)
        shutil.copy2(KEPUB, out)

    def make(self, cap=200 * 1024 * 1024, version='9.6.0', options=None):
        return kobo_ku.KepubCache(self.cache, regenerate=self.regenerate, cap=cap, version=version,
                                  options=options or {'prefer_justification': False})

    def test_made_once_then_reused(self):
        c = self.make()
        p1, why = c.path_for(7, self.src)
        p2, why = c.path_for(7, self.src)
        self.assertEqual((p1, len(self.made)), (p2, 1))
        self.assertTrue(os.path.isfile(p1))
        self.assertTrue(p1.startswith(self.cache))

    def test_the_key(self):
        c = self.make()
        k = c.key(7, self.src)
        self.assertNotEqual(k, c.key(8, self.src))
        self.assertNotEqual(k, self.make(version='9.14.0').key(7, self.src))
        self.assertNotEqual(k, self.make(options={'prefer_justification': True}).key(7, self.src))
        st = os.stat(self.src)
        os.utime(self.src, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        self.assertNotEqual(k, c.key(7, self.src))

    def test_an_edited_epub_is_made_again(self):
        c = self.make()
        c.path_for(7, self.src)
        with open(self.src, 'ab') as f:
            f.write(b'\0')
        c.path_for(7, self.src)
        self.assertEqual(len(self.made), 2)

    def test_least_recently_used_goes_first_and_the_total_stays_under_the_cap(self):
        size = os.path.getsize(KEPUB)
        c = self.make(cap=int(size * 2.5))
        a, _ = c.path_for(1, self.src)
        b, _ = c.path_for(2, self.src)
        old = time.time() - 100
        os.utime(b, (old, old))                    # b not used lately
        os.utime(a, (old + 50, old + 50))
        c.path_for(1, self.src)                     # a used again (touched)
        d, _ = c.path_for(3, self.src)              # needs room: b goes
        self.assertTrue(os.path.exists(a) and os.path.exists(d))
        self.assertFalse(os.path.exists(b))
        total = sum(os.path.getsize(os.path.join(self.cache, n)) for n in os.listdir(self.cache))
        self.assertLessEqual(total, int(size * 2.5))

    def test_safe_to_delete_at_any_time(self):
        c = self.make()
        c.path_for(7, self.src)
        shutil.rmtree(self.cache)
        p, why = c.path_for(7, self.src)
        self.assertTrue(os.path.isfile(p))
        self.assertEqual(len(self.made), 2)

    def test_a_failed_regeneration_leaves_nothing_behind(self):
        def boom(src, out):
            with open(out, 'wb') as f:
                f.write(b'half')
            raise RuntimeError('DRM')
        c = kobo_ku.KepubCache(self.cache, regenerate=boom, version='9.6.0', options={})
        p, why = c.path_for(7, self.src)
        self.assertIsNone(p)
        self.assertIn('DRM', why)
        self.assertEqual([n for n in os.listdir(self.cache) if not n.startswith('.')], [])

    def test_without_calibre_there_is_no_converter(self):
        if kobo_ku.calibre_kepubify() is not None:
            self.skipTest('calibre is here')
        c = kobo_ku.KepubCache(self.cache)
        p, why = c.path_for(7, self.src)
        self.assertIsNone(p)
        self.assertIn('not available', why)


# --------------------------------------------------------------------------- memory, staleness, fingerprint

class MemoryFileTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='cr-ku-mem-')
        self.path = os.path.join(self.dir, 'calibreader_sync_ku_seen.json')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_per_library_and_column_set(self):
        m = kobo_ku.MemoryFile(self.path)
        self.assertEqual(m.load('/lib/a', 'sig1'), {})
        m.save('/lib/a', 'sig1', {'u1': {'h': 'x'}})
        m.save('/lib/b', 'sig1', {'u2': {'h': 'y'}})
        m.save('/lib/a', 'sig2', {'u3': {'h': 'z'}})
        self.assertEqual(m.load('/lib/a', 'sig1'), {})          # a new column set replaces the old one
        self.assertEqual(m.load('/lib/a', 'sig2'), {'u3': {'h': 'z'}})
        self.assertEqual(m.load('/lib/b', 'sig1'), {'u2': {'h': 'y'}})
        self.assertFalse(m.had_part('/lib/c', 'sig1'))
        self.assertTrue(m.had_part('/lib/b', 'sig1'))

    def test_a_damaged_file_is_empty_memory(self):
        with open(self.path, 'w') as f:
            f.write('{x')
        self.assertEqual(kobo_ku.MemoryFile(self.path).load('/lib/a', 'sig1'), {})


class StalenessTests(unittest.TestCase):

    def test_last_store_seen(self):
        values = {1: ('a.xhtml', 3, '2026-10-01T10:00:00.000Z'), 2: ('b.xhtml', 3, '2026-10-20T10:00:00.000Z'),
                  3: ('c.xhtml', None, None)}
        self.assertEqual(kobo_ku.last_store_seen(values, {}), '2026-10-20T10:00:00.000Z')
        self.assertEqual(kobo_ku.last_store_seen({1: ('a.xhtml', 3, None)}, {'u': {'seen': '2026-10-02T00:00:00.000Z'}}),
                         '2026-10-02T00:00:00.000Z')
        self.assertIsNone(kobo_ku.last_store_seen({}, {}))

    def test_the_hint(self):
        last = '2026-10-01T10:00:00.000Z'
        self.assertIn('The newest Kobo reading Kobo Utilities stored is from 2026-10-01',  # NIT 6
                      kobo_ku.staleness_hint(last, '2026-11-04T10:00:00.000Z', NOW))
        self.assertEqual(kobo_ku.staleness_hint(last, None, NOW), '')                       # no Kobo seen
        self.assertEqual(kobo_ku.staleness_hint(last, '2026-09-30T10:00:00.000Z', NOW), '')  # not since
        self.assertEqual(kobo_ku.staleness_hint('2026-10-30T10:00:00.000Z', '2026-11-04T10:00:00.000Z', NOW), '')

    def test_status_line(self):
        self.assertIn('never', kobo_ku.store_line(None))
        self.assertIn('2026-10-20', kobo_ku.store_line('2026-10-20T10:00:00.000Z'))


class FingerprintTests(unittest.TestCase):

    def test_changes_with_any_value(self):
        a = {1: ('x.xhtml#kobo.1.1', 3, '2026-10-01T10:00:00.000Z')}
        self.assertEqual(kobo_ku.fingerprint(a), kobo_ku.fingerprint(dict(a)))
        self.assertNotEqual(kobo_ku.fingerprint(a), kobo_ku.fingerprint({1: ('x.xhtml#kobo.1.2', 3,
                                                                                '2026-10-01T10:00:00.000Z')}))
        self.assertNotEqual(kobo_ku.fingerprint(a), kobo_ku.fingerprint({1: ('x.xhtml#kobo.1.1', 4,
                                                                                '2026-10-01T10:00:00.000Z')}))


class SourceTests(unittest.TestCase):
    '''8.8: the settings are a list of sources, one today; off until a location column is picked.'''

    def test_off_until_a_location_column_is_picked(self):
        self.assertIsNone(kobo_ku.source_from_prefs(None))
        self.assertIsNone(kobo_ku.source_from_prefs({'sources': [dict(SOURCE, location='')]}))
        self.assertIsNone(kobo_ku.source_from_prefs({'sources': [dict(SOURCE, location='not a lookup')]}))
        s = kobo_ku.source_from_prefs({'sources': [SOURCE]})
        self.assertEqual((s['location'], s['percent'], s['last_read'], s['boundReader']),
                         ('#kobo_reading_location', '#kobo_percent_read', '#kobo_last_read', None))

    def test_the_stored_shape(self):
        stored = kobo_ku.prefs_entry('#kobo_reading_location', '#kobo_percent_read', '')
        self.assertEqual(len(stored['sources']), 1)
        self.assertEqual(stored['sources'][0]['id'], 'ku')
        self.assertIsNone(stored['sources'][0]['boundReader'])
        self.assertIsNone(kobo_ku.prefs_entry('', '#kobo_percent_read', ''))


# --------------------------------------------------------------------------- review step 4, round 1

class TwoNumberingsTests(unittest.TestCase):
    '''S1: a stored KEPUB may not be the Kobo's copy (a KEPUB format added after the book was sent
    by USB). Map the span through both numberings (the stored KEPUB and calibre's own) and never
    land after the Kobo's real place: when they disagree, the EARLIER landing, marked span~.'''

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='cr-ku-two-')
        body = '<p>\nFirst sentence here. Second sentence there.</p>\n<p>\nThird one. Fourth one.</p>'
        calibre_like = ('<div id="book-columns"><div id="book-inner"><p>' + span('kobo.1.1', '\n')
                        + span('kobo.1.2', 'First sentence here. ') + span('kobo.1.3', 'Second sentence there.')
                        + '</p>\n<p>' + span('kobo.2.1', '\n') + span('kobo.2.2', 'Third one. ')
                        + span('kobo.2.3', 'Fourth one.') + '</p></div></div>')
        kepubify_like = ('<p>' + span('kobo.1.1', 'First sentence here. ') + span('kobo.1.2', 'Second sentence there.')
                         + '</p>\n<p>' + span('kobo.2.1', 'Third one. ') + span('kobo.2.2', 'Fourth one.') + '</p>')
        self.epub = Book(make_book(os.path.join(self.dir, 'b.epub'), [('ch1.xhtml', body)]))
        self.calibre = Book(make_book(os.path.join(self.dir, 'c.kepub'), [('ch1.xhtml', calibre_like)]))
        self.stored = Book(make_book(os.path.join(self.dir, 's.kepub'), [('ch1.xhtml', kepubify_like)]))
        self.text = self.epub.text(0).text

    def tearDown(self):
        for b in (self.epub, self.calibre, self.stored):
            b.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def at(self, place):
        return self.text[place['textAnchor']:]

    def test_disagreeing_numberings_take_the_earlier_place_marked_approximate(self):
        # kobo.1.2 = "First…" in calibre's numbering, "Second…" in the stored one
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location('text/ch1.xhtml#kobo.1.2'),
                                    kepub=lambda: ([self.stored, self.calibre], None))
        self.assertTrue(self.at(place).startswith('First sentence'), self.at(place)[:20])
        self.assertEqual(place['precision'], kobo_ku.SPAN_APPROX)
        self.assertIn('disagree', place['note'])

    def test_agreeing_numberings_stay_exact(self):
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location('text/ch1.xhtml#kobo.1.1'),
                                    kepub=lambda: ([self.stored, self.calibre], None))
        self.assertTrue(self.at(place).startswith('First sentence'))
        self.assertEqual(place['precision'], kobo_ku.SPAN)

    def test_a_span_only_one_numbering_has_decides_the_numbering(self):
        place, why = kobo_ku.map_ku(self.epub, kobo_ku.parse_location('text/ch1.xhtml#kobo.2.3'),
                                    kepub=lambda: ([self.stored, self.calibre], None))
        self.assertTrue(self.at(place).startswith('Fourth one'))
        self.assertEqual(place['precision'], kobo_ku.SPAN)

    def test_the_driver_plan_for_a_stored_kepub_with_an_epub_gives_both(self):
        made = []

        def regenerate(src, out):
            made.append(src)
            shutil.copy2(KEPUB, out)
        cache = kobo_ku.KepubCache(os.path.join(self.dir, 'cache'), regenerate=regenerate, version='9.6.0',
                                   options={'prefer_justification': False})
        kepub_for = kobo_ku.make_kepub_for(lambda b: {'EPUB', 'KEPUB'}, cache, ['kepub', 'epub'], True, kte=False)
        paths, why, info = kepub_for(1, EPUB, KEPUB)
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0], KEPUB)
        self.assertTrue(paths[1].startswith(cache.folder))
        self.assertNotIn('uncertain', info)
        kepub_for = kobo_ku.make_kepub_for(lambda b: {'EPUB'}, cache, ['kepub', 'epub'], True, kte=True)
        paths, why, info = kepub_for(1, EPUB, None)
        self.assertIn('Kobo Touch Extended', info['uncertain'])


class KoboTouchExtendedImportTests(ImportBase):
    '''S1: with the Kobo Touch Extended driver plugin installed, a span is approximate.'''

    def kepub_for(self, book_id, epub_path, kepub_path):
        return KEPUB, None, {'kepub': 'stored KEPUB', 'uncertain': 'the Kobo Touch Extended driver plugin is installed'}

    def test_a_span_is_marked_approximate_and_said_once(self):
        res = self.run_import({1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z')})
        e = self.positions()[self.uuids[1]]
        self.assertEqual(e['kobo']['precision'], kobo_ku.SPAN_APPROX)
        self.assertIn('Kobo Touch Extended', e['kobo']['note'])
        self.assertIn('Kobo Touch Extended', res.uncertain)


@unittest.skipUnless(kobo_ku.calibre_kepubify() is not None, 'needs calibre\'s kepubify (calibre-debug)')
class CalibreNumberingTests(unittest.TestCase):
    '''S1, the reviewer's Metamorphosis probe: the Kobo has calibre's numbering (sent while the
    book had only an EPUB), the library now also holds a kepubify-4.0.4 KEPUB. Through the plugin
    no place may land after the Kobo's real one (874 did in 1.7.0 / 5d99d2e).'''

    def test_never_after_the_kobos_place(self):
        work = tempfile.mkdtemp(prefix='cr-ku-s1-')
        try:
            cache = kobo_ku.KepubCache(os.path.join(work, 'cache'))
            kepub_for = kobo_ku.make_kepub_for(lambda b: {'EPUB', 'KEPUB'}, cache, ['kepub', 'epub'], True, kte=False)
            paths, why, info = kepub_for(1, EPUB, KEPUB)
            self.assertEqual(len(paths), 2, why)
            epub, stored, calibre = Book(EPUB), Book(paths[0]), Book(paths[1])
            after = exact = early = 0
            try:
                for ch in range(len(epub.chapters)):
                    kch = [p.lower() for p in calibre.chapters].index(epub.chapters[ch].lower())
                    for sid in calibre.text(kch).ids:
                        if not re.match(r'^kobo\.\d+\.\d+$', sid):
                            continue
                        true = kobo_ku.resolve_span(epub, calibre, ch, sid)
                        place, why = kobo_ku.map_ku(epub, kobo_ku.parse_location(epub.chapters[ch] + '#' + sid),
                                                    kepub=lambda: ([stored, calibre], None))
                        if true is None or place is None:
                            continue
                        if place['textAnchor'] > true[0]:
                            after += 1
                        elif place['textAnchor'] == true[0]:
                            exact += 1
                        else:
                            early += 1
            finally:
                for b in (epub, stored, calibre):
                    b.close()
            self.assertEqual(after, 0, (after, exact, early))
            self.assertGreater(exact, 0)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_S2_a_conversion_leaves_no_temp_folder_behind(self):
        from calibre.ptempfile import base_dir
        work = tempfile.mkdtemp(prefix='cr-ku-s2-')
        try:
            before = set(os.listdir(base_dir()))
            cache = kobo_ku.KepubCache(os.path.join(work, 'cache'))
            path, why = cache.path_for(1, EPUB)
            self.assertTrue(path, why)
            self.assertEqual(set(os.listdir(base_dir())) - before, set())
            self.assertEqual(sorted(os.listdir(cache.folder)), [os.path.basename(path)])
        finally:
            shutil.rmtree(work, ignore_errors=True)


class CacheReviewTests(unittest.TestCase):
    '''S2: everything the plugin keeps counts against the cap; stale temp files and work folders
    (a calibre killed mid-conversion) are removed. NIT 8: a damaged cache file is made again.'''
    setUp, tearDown, regenerate, make = CacheTests.setUp, CacheTests.tearDown, CacheTests.regenerate, CacheTests.make

    def test_stale_temp_files_and_work_folders_count_and_go(self):
        c = self.make()
        p, _ = c.path_for(7, self.src)
        old = time.time() - 3600
        stale_file = os.path.join(self.cache, 'abc.123.456.tmp')
        stale_dir = os.path.join(self.cache, 'work-123-456')
        os.makedirs(os.path.join(stale_dir, 'OEBPS'))
        for f in (stale_file, os.path.join(stale_dir, 'OEBPS', 'x.html')):
            with open(f, 'wb') as fh:
                fh.write(b'x' * 1000)
            os.utime(f, (old, old))
        os.utime(stale_dir, (old, old))
        self.assertGreaterEqual(c.used(), os.path.getsize(p) + 2000)
        c.evict()
        self.assertFalse(os.path.exists(stale_file))
        self.assertFalse(os.path.exists(stale_dir))
        self.assertTrue(os.path.exists(p))

    def test_a_damaged_cache_file_is_made_again(self):
        c = self.make()
        p, _ = c.path_for(7, self.src)
        with open(p, 'wb') as f:
            f.write(b'not a zip')
        p2, why = c.path_for(7, self.src)
        self.assertEqual(p2, p)
        self.assertEqual(len(self.made), 2)
        self.assertTrue(zipfile.is_zipfile(p2))


class SummaryNoiseTests(ImportBase):
    '''NIT 7: a 0 % / 100 % status with no location is a status, not a problem to report.'''

    def test_status_rows_are_not_problems(self):
        res = self.run_import({1: (None, 100, '2026-11-01T10:00:00Z'), 2: ('Fantasy', 40, '2026-11-01T10:00:00Z')})
        self.assertIn(self.uuids[1], res.skipped)
        self.assertNotIn(self.uuids[1], res.problems)
        self.assertIn(self.uuids[2], res.problems)


if __name__ == '__main__':
    unittest.main()

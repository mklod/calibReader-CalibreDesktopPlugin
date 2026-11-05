# Last modified: 2026-11-05--1315
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


def drop_paragraph(src, dst):
    """The reviewer's hand edit (p_r2_edits.py): in every chapter, delete the 3rd <p>."""
    b = Book(src)
    chapters = set(c.lower() for c in b.chapters)
    b.close()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename.lower() in chapters:
                text = data.decode('utf-8')
                ps = [m.start() for m in re.finditer(r'<p[\s>]', text)]
                if len(ps) > 3:
                    end = text.find('</p>', ps[2]) + 4
                    text = text[:ps[2]] + text[end:]
                data = text.encode('utf-8')
            zout.writestr(info, data)
    return dst


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
        # NIT 3 ruling (review step 4): from the Kobo's % minus 2 points, snapped to the START of the
        # chapter that position falls in; precision stays "percent"; never skip unread text
        place, why = kobo_ku.map_ku(self.epub, None, percent=40)
        self.assertEqual(place['precision'], kobo_ku.PERCENT)
        self.assertEqual((place['chapter'], place['textAnchor']), (3, 0))      # Part II: 28.2-57.3 %
        self.assertAlmostEqual(place['progress'], self.pct_of(place) / 100.0, places=5)
        for pct in (0, 100, None):
            place, why = kobo_ku.map_ku(self.epub, None, percent=pct)
            self.assertIsNone(place, pct)
            self.assertTrue(why)

    def test_a_percent_only_place_never_lands_after_the_kobos(self):
        # The Kobo at the very start of Part III (57.3 % of the text) reports 58 % (its own measure is
        # not the text's). The old text-fraction mapping put the reader ~1,000 characters into Part
        # III, past the Kobo's place; the ruling lands at the start of Part II, before it.
        real = (4, 0)
        place, why = kobo_ku.map_ku(self.epub, None, percent=58)
        self.assertLessEqual((place['chapter'], place['textAnchor']), real)
        self.assertEqual(place['textAnchor'], 0)
        self.assertEqual(place['precision'], kobo_ku.PERCENT)
        # every whole %: a chapter start, at or before (the Kobo's % - 2) of the text
        for pct in range(1, 100):
            place, why = kobo_ku.map_ku(self.epub, None, percent=pct)
            self.assertEqual(place['textAnchor'], 0, pct)
            self.assertLessEqual(self.pct_of(place), max(0, pct - 2) + 1e-9, pct)

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
        # a library already judged calibre-numbered (re-review B1-R2: until then, chapter starts)
        self.memory['.'] = {'since': '2026-10-01T00:00:00.000Z', 'spans': 5, 'missing': 0, 'foreign': False,
                            'decided': True}
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
    '''Re-review B1-R1 (supersedes round 1's span~): with the Kobo Touch Extended driver plugin
    installed, a span goes to the START of its chapter, precision "chapter" (the app compares span~
    strictly and would move readers past unread text: up to 27 % of Gatsby late, measured).'''

    def kepub_for(self, book_id, epub_path, kepub_path):
        return KEPUB, None, {'kepub': 'stored KEPUB', 'uncertain': kobo_ku.KTE_NOTE}

    def test_a_span_goes_to_its_chapter_start_and_it_is_said_once(self):
        res = self.run_import({1: (SPAN_VALUE, 4, '2026-11-01T10:00:00Z'),
                               2: ('OEBPS/7446794606207039498_5200-h-1.htm.xhtml#kobo.999.1', 4,
                                   '2026-11-01T10:00:00Z')})
        for u in (self.uuids[1], self.uuids[2]):          # a span not in the KEPUB: its chapter too
            e = self.positions()[u]
            self.assertEqual((e['chapter'], e['textAnchor'], e['kobo']['precision']), (2, 0, kobo_ku.CHAPTER))
            self.assertIn('Kobo Touch Extended', e['kobo']['note'])
        self.assertIn('Kobo Touch Extended', res.uncertain)
        self.assertIn('start of its chapter', res.uncertain)     # NIT-R1-2: the wording says what happened
        # NIT-R2-3: the sentence calibre's numbering gives is kept for the testers' reports (the app ignores it)
        self.assertGreater(self.positions()[self.uuids[1]]['kobo']['spanAnchor'], 0)
        self.assertNotIn('spanAnchor', self.positions()[self.uuids[2]]['kobo'])


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

    def kte_values(self, epub_path, kte_path, every=40):
        """(true place, location) for every `every`-th Kobo Touch Extended span of the book."""
        epub, dev = Book(epub_path), Book(kte_path)
        out = []
        try:
            n = 0
            for ch in range(len(epub.chapters)):
                kch = [p.lower() for p in dev.chapters].index(epub.chapters[ch].lower())
                for sid in dev.text(kch).ids:
                    if not re.match(r'^kobo\.\d+\.\d+$', sid):
                        continue
                    n += 1
                    if n % every:
                        continue
                    true = kobo_ku.resolve_span(epub, dev, ch, sid)
                    if true is not None:
                        out.append(((ch, true[0]), epub.chapters[ch] + '#' + sid))
        finally:
            epub.close()
            dev.close()
        return out

    def run_kte(self, kte):
        """Metamorphosis on a Kobo with a Kobo Touch Extended KEPUB; the library has the EPUB only."""
        import ku_kte_port
        work = tempfile.mkdtemp(prefix='cr-ku-kte-')
        try:
            kte_path = ku_kte_port.kte_kepub(EPUB, os.path.join(work, 'kte.kepub'))
            cache = kobo_ku.KepubCache(os.path.join(work, 'cache'))
            kepub_for = kobo_ku.make_kepub_for(lambda b: {'EPUB'}, cache, ['kepub', 'epub'], True, kte=kte)
            pairs = self.kte_values(EPUB, kte_path)
            uuids = {i: '{:08d}-aaaa-bbbb-cccc-dddddddddddd'.format(i) for i in range(len(pairs))}
            lib = os.path.join(work, 'lib')
            os.makedirs(lib)
            values = {i: (loc, None, '2026-11-01T10:{:02d}:{:02d}Z'.format(i // 60, i % 60)) for i, (_, loc) in enumerate(pairs)}
            memory = {}
            res = kobo_ku.import_ku(lib, SOURCE, values, lambda b: (uuids[b], EPUB, None), memory, kepub_for=kepub_for,
                                    now=NOW, max_books=10 ** 6, budget=float('inf'))
            with open(os.path.join(lib, 'CalibReader_positions.json'), encoding='utf-8') as f:
                table = json.load(f)['positions']
            after = sum(1 for i, (true, _) in enumerate(pairs)
                        if uuids[i] in table and (table[uuids[i]]['chapter'], table[uuids[i]]['textAnchor']) > true)
            return res, memory, after, len(pairs), table
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_B1R1_kobo_touch_extended_installed_never_lands_after(self):
        res, memory, after, n, table = self.run_kte(kte=True)
        self.assertGreater(n, 20)
        self.assertEqual(after, 0)
        self.assertEqual({e['kobo']['precision'] for e in table.values()}, {kobo_ku.CHAPTER})

    def test_S1R1_kobo_touch_extended_numbering_is_detected_without_the_driver(self):
        res, memory, after, n, table = self.run_kte(kte=False)
        self.assertTrue(memory['.']['foreign'])
        self.assertEqual(after, 0)
        self.assertTrue(res.foreign)

    def kte_library_places(self, books, work):
        import ku_kte_port
        cache = kobo_ku.KepubCache(os.path.join(work, 'cache'))
        out = {}
        for i, src in enumerate(books, 1):
            ep = os.path.join(work, 'b{}.epub'.format(i))
            shutil.copy(src, ep)
            kp = ku_kte_port.kte_kepub(ep, os.path.join(work, 'b{}.kte.kepub'.format(i)))
            e, d = Book(ep), Book(kp)
            places = []
            for ch in range(len(e.chapters)):
                kch = [q.lower() for q in d.chapters].index(e.chapters[ch].lower())
                for sid in d.text(kch).ids:
                    if re.match(r'^kobo\.\d+\.\d+$', sid):
                        t = kobo_ku.resolve_span(e, d, ch, sid)
                        if t is not None:
                            places.append((e.chapters[ch] + '#' + sid, ch, t[0]))
            e.close()
            d.close()
            out[i] = {'epub': ep, 'uuid': '{:08d}-aaaa-bbbb-cccc-dddddddddddd'.format(i), 'places': places}
        return out, cache

    def test_B1R2_one_book_per_connect_never_lands_after_the_kobo(self):
        # the reviewer's p_r2_trickle.py part 3, smaller: Kobo Touch Extended copies (uninstalled),
        # one changed value per run, random places; 27 % of such libraries wrote a late place at c7d1dc4
        import random
        work = tempfile.mkdtemp(prefix='cr-ku-trickle-')
        try:
            books, cache = self.kte_library_places([EPUB], work)
            kepub_for = kobo_ku.make_kepub_for(lambda b: {'EPUB'}, cache, ['kepub', 'epub'], True, kte=False)
            lookup = lambda b: (books[b]['uuid'], books[b]['epub'], None)  # noqa: E731
            rng = random.Random(4)
            late = libraries = 0
            for trial in range(40):
                lib = os.path.join(work, 'lib{}'.format(trial))
                os.makedirs(lib)
                memory, values = {}, {}
                for k in range(1, 9):
                    loc, ch, true = rng.choice(books[1]['places'])
                    values[1] = (loc, None, '2026-11-05T11:{:02d}:00Z'.format(k))
                    kobo_ku.import_ku(lib, SOURCE, dict(values), lookup, memory, kepub_for=kepub_for,
                                      now=datetime.datetime(2026, 11, 5, 12, k, tzinfo=UTC), max_books=10 ** 6,
                                      budget=1e9)
                    path = os.path.join(lib, 'CalibReader_positions.json')
                    if os.path.exists(path):
                        e = json.load(open(path, encoding='utf-8'))['positions'].get(books[1]['uuid'])
                        if e and e['kobo']['location'] == loc and (e['chapter'], e['textAnchor']) > (ch, true):
                            late += 1
                libraries += 1
            self.assertEqual(late, 0)
            self.assertEqual(libraries, 40)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_S1R2_an_edited_book_never_lands_after_once_a_missing_id_flagged_it(self):
        # the reviewer's p_r2_editdir.py: the Kobo has calibre's KEPUB of the ORIGINAL book; the library's
        # EPUB had one <p> deleted per chapter, later than the Kobo's reading
        work = tempfile.mkdtemp(prefix='cr-ku-edited-')
        try:
            orig = os.path.join(work, 'orig.epub')
            shutil.copy(EPUB, orig)
            dev_path, _ = kobo_ku.KepubCache(os.path.join(work, 'dev')).path_for(1, orig)
            edited = drop_paragraph(orig, os.path.join(work, 'edited.epub'))
            t = datetime.datetime(2026, 11, 4, tzinfo=UTC).timestamp()
            os.utime(edited, (t, t))
            cache = kobo_ku.KepubCache(os.path.join(work, 'plug'))
            kepub_for = kobo_ku.make_kepub_for(lambda b: {'EPUB'}, cache, ['kepub', 'epub'], True, kte=False)
            plug_path, _ = cache.path_for(1, edited)
            epub, dev, plug = Book(edited), Book(dev_path), Book(plug_path)
            missing, found = None, []
            for ch in range(len(epub.chapters)):
                kch = [q.lower() for q in dev.chapters].index(epub.chapters[ch].lower())
                for sid in dev.text(kch).ids:
                    if not re.match(r'^kobo\.\d+\.\d+$', sid):
                        continue
                    if len(' '.join((dev.text(kch).id_text.get(sid) or '').split())) < 24:
                        continue
                    true = kobo_ku.resolve_span(epub, dev, ch, sid)
                    got = kobo_ku.resolve_span(epub, plug, ch, sid)
                    if true is None or true[1] != kobo_ku.SPAN:
                        continue
                    if got is None and missing is None:
                        missing = epub.chapters[ch] + '#' + sid
                    elif got is not None and got[0] > true[0]:
                        found.append((epub.chapters[ch] + '#' + sid, ch, true[0]))
            for b in (epub, dev, plug):
                b.close()
            self.assertTrue(missing and len(found) > 20)
            lib = os.path.join(work, 'lib')
            os.makedirs(lib)
            memory = {'.': {'since': '2026-10-01T00:00:00.000Z', 'spans': 10, 'missing': 0, 'foreign': False,
                            'decided': True}}
            lookup = lambda b: ('00000001-aaaa-bbbb-cccc-dddddddddddd', edited, None)  # noqa: E731

            def run(loc, k):
                kobo_ku.import_ku(lib, SOURCE, {1: (loc, None, '2026-11-03T10:{:02d}:00Z'.format(k))}, lookup, memory,
                                  kepub_for=kepub_for, now=NOW, max_books=10 ** 6, budget=1e9)
                return json.load(open(os.path.join(lib, 'CalibReader_positions.json'), encoding='utf-8'))[
                    'positions']['00000001-aaaa-bbbb-cccc-dddddddddddd']
            run(missing, 0)
            late = 0
            for k, (loc, ch, true) in enumerate(found[::max(1, len(found) // 25)][:25], 1):
                e = run(loc, k)
                late += (e['chapter'], e['textAnchor']) > (ch, true)
            self.assertEqual(late, 0)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_S1R2_E3_edit_then_keep_reading_writes_nothing_after_the_kobo(self):
        # the reviewer's p_r3_e3.py: a calibre-numbered library; the Kobo keeps calibre's copy of the
        # ORIGINAL Metamorphosis; the library's EPUB had one <p> deleted per chapter (2026-11-02). Phase 1:
        # Kobo dates before the edit, the first store a missing id; phase 2: the Kobo reads on after the
        # edit (no re-send), where the old rule cleared the flag (135 of 240 late). And the first-seen
        # variant: the plugin saw the original file, the edit replaces it, the Kobo reads on.
        import random
        work = tempfile.mkdtemp(prefix='cr-ku-e3-')
        try:
            orig = os.path.join(work, 'orig.epub')
            shutil.copy(EPUB, orig)
            dev_path, _ = kobo_ku.KepubCache(os.path.join(work, 'dev')).path_for(9, orig)
            edited = drop_paragraph(orig, os.path.join(work, 'edited.epub'))
            t = datetime.datetime(2026, 11, 2, 12, tzinfo=UTC).timestamp()
            os.utime(edited, (t, t))
            epub, dev = Book(edited), Book(dev_path)
            places = []
            for ch in range(len(epub.chapters)):
                kch = [q.lower() for q in dev.chapters].index(epub.chapters[ch].lower())
                for sid in dev.text(kch).ids:
                    if re.match(r'^kobo\.\d+\.\d+$', sid):
                        tr = kobo_ku.resolve_span(epub, dev, ch, sid)
                        if tr is not None and tr[1] == kobo_ku.SPAN:
                            places.append((epub.chapters[ch] + '#' + sid, ch, tr[0]))
            epub.close()
            dev.close()
            cache = kobo_ku.KepubCache(os.path.join(work, 'plug'))
            kepub_for = kobo_ku.make_kepub_for(lambda b: {'EPUB'}, cache, ['kepub', 'epub'], True, kte=False)
            plug_path, _ = cache.path_for(1, edited)
            e2, plug = Book(edited), Book(plug_path)
            missing = [p for p in places if kobo_ku.resolve_span(e2, plug, p[1], p[0].split('#')[1]) is None]
            e2.close()
            plug.close()
            rng = random.Random(9)
            uuid = '00000001-aaaa-bbbb-cccc-dddddddddddd'

            def trial(lib, book_path, sequence):
                memory = {'.': {'since': '2026-10-01T00:00:00.000Z', 'spans': 20, 'missing': 0, 'foreign': False,
                                'decided': True}}
                late = []
                for k, (loc, ch, true, day, before) in enumerate(sequence, 1):
                    if before:
                        before()
                    kobo_ku.import_ku(lib, SOURCE, {1: (loc, None, '2026-11-{:02d}T{:02d}:00:00Z'.format(day, 8 + k))},
                                      lambda b: (uuid, book_path, None), memory, kepub_for=kepub_for, now=NOW,
                                      max_books=10 ** 6, budget=1e9)
                    path = os.path.join(lib, 'CalibReader_positions.json')
                    e = json.load(open(path, encoding='utf-8'))['positions'].get(uuid) if os.path.exists(path) else None
                    if e and e['kobo']['location'] == loc and (e['chapter'], e['textAnchor']) > (ch, true):
                        late.append((k, day))
                return late
            late_e3 = []
            for n in range(12):
                lib = os.path.join(work, 'e3-{}'.format(n))
                os.makedirs(lib)
                m = rng.choice(missing)
                seq = [(m[0], m[1], m[2], 1, None)] + [(p[0], p[1], p[2], 1, None) for p in rng.sample(places, 3)]
                seq += [(p[0], p[1], p[2], 4, None) for p in rng.sample(places, 4)]
                late_e3 += trial(lib, edited, seq)
            self.assertEqual(late_e3, [])
            late_seen = []
            for n in range(12):
                lib = os.path.join(work, 'seen-{}'.format(n))
                os.makedirs(lib)
                book = os.path.join(lib, 'book.epub')
                shutil.copy(orig, book)
                os.utime(book, (t - 86400 * 30, t - 86400 * 30))
                swap = lambda book=book: (shutil.copy(edited, book), os.utime(book, (t, t)))  # noqa: E731
                first = rng.choice([p for p in places if p not in missing])
                seq = [(first[0], first[1], first[2], 1, None)]
                later = rng.sample(places, 4)
                seq += [(later[0][0], later[0][1], later[0][2], 4, swap)]
                seq += [(p[0], p[1], p[2], 4, None) for p in later[1:]]
                late_seen += [x for x in trial(lib, book, seq) if x[0] > 1]
            self.assertEqual(late_seen, [])
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_S1R4_both_paths_write_nothing_after_the_kobo(self):
        # the reviewer's p_r4.py, smaller. A: a book first seen unedited, edited, then edited AGAIN (no
        # re-send); B: an undecided chapter start, the book then edited, the library decided by other books.
        import random
        work = tempfile.mkdtemp(prefix='cr-ku-r4-')
        try:
            orig = os.path.join(work, 'orig.epub')
            shutil.copy(EPUB, orig)
            dev_path, _ = kobo_ku.KepubCache(os.path.join(work, 'dev')).path_for(77, orig)
            cache = kobo_ku.KepubCache(os.path.join(work, 'plug'))
            kepub_for = kobo_ku.make_kepub_for(lambda b: {'EPUB'}, cache, ['kepub', 'epub'], True, kte=False)

            def truth(book):
                e, d = Book(book), Book(dev_path)
                out = []
                for ch in range(len(e.chapters)):
                    kch = [q.lower() for q in d.chapters].index(e.chapters[ch].lower())
                    for sid in d.text(kch).ids:
                        if re.match(r'^kobo\.\d+\.\d+$', sid):
                            tr = kobo_ku.resolve_span(e, d, ch, sid)
                            if tr is not None and tr[1] == kobo_ku.SPAN:
                                out.append((e.chapters[ch] + '#' + sid, ch, tr[0]))
                e.close()
                d.close()
                return out

            def edit(book, when):
                tmp = book + '.tmp.epub'
                drop_paragraph(book, tmp)
                os.replace(tmp, book)
                os.utime(book, (when, when))

            def run(lib, books, values, memory, minute):
                kobo_ku.import_ku(lib, SOURCE, dict(values), lambda b: (books[b][1], books[b][0], None), memory,
                                  kepub_for=kepub_for, now=datetime.datetime(2026, 11, 5, 12, minute, tzinfo=UTC),
                                  max_books=10 ** 6, budget=1e9)
                path = os.path.join(lib, 'CalibReader_positions.json')
                return json.load(open(path, encoding='utf-8'))['positions'] if os.path.exists(path) else {}
            rng = random.Random(3)
            late_a = []
            for trial in range(8):
                lib = os.path.join(work, 'a{}'.format(trial))
                os.makedirs(lib)
                book = os.path.join(lib, 'a.epub')
                shutil.copy(orig, book)
                books = {1: (book, '00000001-aaaa-bbbb-cccc-dddddddddddd')}
                memory = {'.': {'since': '2026-10-01T00:00:00.000Z', 'spans': 20, 'missing': 0, 'foreign': False,
                                'decided': True}}
                minute = 0
                for edits in (0, 1, 2):
                    if edits:
                        edit(book, time.time() - 1000 + edits * 100)
                    places = truth(book)
                    for k in range(3):
                        minute += 1
                        loc, ch, true = rng.choice(places)
                        e = run(lib, books, {1: (loc, None, '2026-11-05T09:{:02d}:00Z'.format(minute))}, memory,
                                minute).get(books[1][1])
                        if e and e['kobo']['location'] == loc and (e['chapter'], e['textAnchor']) > (ch, true):
                            late_a.append((trial, edits))
            self.assertEqual(late_a, [])
            others = {}
            for i in range(2, 6):
                others[i] = (EPUB, '{:08d}-aaaa-bbbb-cccc-dddddddddddd'.format(i))
            other_places = truth(EPUB)
            late_b = []
            for trial in range(8):
                lib = os.path.join(work, 'b{}'.format(trial))
                os.makedirs(lib)
                book = os.path.join(lib, 'b.epub')
                shutil.copy(orig, book)
                books = dict(others)
                books[1] = (book, '00000001-aaaa-bbbb-cccc-dddddddddddd')
                memory = {}
                loc, ch, true = rng.choice(truth(book))
                values = {1: (loc, None, '2026-11-05T09:00:00Z')}
                run(lib, books, values, memory, 0)                   # undecided: a chapter start
                edit(book, time.time() - 500)                        # edited, not sent again
                after = {q[0]: q for q in truth(book)}
                for k, i in enumerate(others, 1):
                    values[i] = (rng.choice(other_places)[0], None, '2026-11-05T09:{:02d}:00Z'.format(k))
                    table = run(lib, books, values, memory, k)
                e = table.get(books[1][1])
                q = after.get(loc)
                if q is not None and e and (e['chapter'], e['textAnchor']) > (q[1], q[2]):
                    late_b.append(trial)
            self.assertEqual(late_b, [])
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
        stale_file = os.path.join(self.cache, '0123456789abcdef01234567.123.456.tmp')
        stale_dir = os.path.join(self.cache, 'work-123-456-x7k2')
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


class CacheOwnNamesTests(unittest.TestCase):
    """Re-review NIT-R1-1: evict removes only the cache's own names (a CLI --cache pointed at a
    folder that is not dedicated to it must not lose anything else)."""
    setUp, tearDown, regenerate, make = CacheTests.setUp, CacheTests.tearDown, CacheTests.regenerate, CacheTests.make

    def test_foreign_files_and_folders_survive(self):
        c = self.make(cap=1)
        c.path_for(7, self.src)
        old = time.time() - 3600
        keep_file = os.path.join(self.cache, 'notes.txt')
        keep_dir = os.path.join(self.cache, 'photos')
        os.makedirs(keep_dir)
        for f in (keep_file, os.path.join(keep_dir, 'a.jpg')):
            with open(f, 'wb') as fh:
                fh.write(b'x')
            os.utime(f, (old, old))
        os.utime(keep_dir, (old, old))
        c.evict()
        self.assertTrue(os.path.exists(keep_file) and os.path.exists(os.path.join(keep_dir, 'a.jpg')))


class ForeignNumberingTests(ImportBase):
    """Re-review S1-R1: a library whose Kobo copies were numbered by another converter (Kobo Touch
    Extended, even uninstalled): most of the Kobo's span ids are not in calibre's numbering (76 % of
    Kobo Touch Extended's, measured; 0 of 343,676 calibre-made ones). Over at least FOREIGN_MIN
    span values with FOREIGN_SHARE or more missing, the library is remembered as "foreign" and its
    spans go to the start of their chapter, the earlier span entries we wrote included; with fewer
    values, a missing span makes the run's spans coarse without remembering anything."""

    PRESENT = ('OEBPS/7446794606207039498_5200-h-1.htm.xhtml#kobo.10.2', 'OEBPS/7446794606207039498_5200-h-2.htm.xhtml#kobo.5.1')

    def setUp(self):
        ImportBase.setUp(self)
        self.uuids = {i: '{:08d}-aaaa-bbbb-cccc-dddddddddddd'.format(i) for i in range(1, 13)}

    def missing(self, i):
        return 'OEBPS/7446794606207039498_5200-h-1.htm.xhtml#kobo.9{:02d}.1'.format(i)

    def entry(self, i):
        return self.positions()[self.uuids[i]]

    def test_a_calibre_numbered_library_stays_exact(self):
        values = {i: (self.PRESENT[i % 2], None, '2026-11-01T10:00:{:02d}Z'.format(i)) for i in range(1, 7)}
        res = self.run_import(values)
        self.assertEqual(len(res.written), 6)
        self.assertEqual({self.entry(i)['kobo']['precision'] for i in range(1, 7)}, {kobo_ku.SPAN})
        self.assertFalse(res.foreign)
        self.assertFalse(self.memory['.'].get('foreign'))

    def test_mostly_missing_span_ids_mark_the_library_foreign(self):
        values = {i: (self.missing(i), None, '2026-11-01T10:00:{:02d}Z'.format(i)) for i in range(1, 5)}
        values.update({i: (self.PRESENT[i % 2], None, '2026-11-01T10:00:{:02d}Z'.format(i)) for i in (5, 6)})
        res = self.run_import(values)
        self.assertTrue(self.memory['.']['foreign'])
        self.assertIn('another', res.foreign)
        self.assertEqual(len(res.written), 6)              # the missing ones too: their chapter is right
        for i in range(1, 7):
            e = self.entry(i)
            self.assertEqual((e['textAnchor'], e['kobo']['precision']), (0, kobo_ku.CHAPTER), i)
        # sticky: a later span value of this library is coarse too, and said again
        res = self.run_import({9: (self.PRESENT[0], None, '2026-11-02T10:00:00Z')})
        self.assertEqual(self.entry(9)['kobo']['precision'], kobo_ku.CHAPTER)
        self.assertTrue(res.foreign)

    def test_few_values_with_a_missing_span_are_coarse_but_not_remembered(self):
        res = self.run_import({1: (self.missing(1), None, '2026-11-01T10:00:01Z'),
                               2: (self.PRESENT[0], None, '2026-11-01T10:00:02Z')})
        self.assertEqual(self.entry(2)['kobo']['precision'], kobo_ku.CHAPTER)
        self.assertFalse(self.memory['.'].get('foreign'))
        self.assertTrue(res.undecided)                  # B1-R2: undecided, said as such

    def stamp(self, i, day=1):
        return '2026-11-{:02d}T10:00:{:02d}Z'.format(day, i)

    def decided_library(self):
        """Five sentence places, all in calibre's numbering: judged calibre-numbered (exact)."""
        values = {i: (self.PRESENT[i % 2], None, self.stamp(i)) for i in range(1, 6)}
        self.run_import(values)
        self.assertEqual({self.entry(i)['kobo']['precision'] for i in range(1, 6)}, {kobo_ku.SPAN})
        return values

    def test_once_foreign_our_earlier_span_entries_go_back_to_their_chapter_start(self):
        values = self.decided_library()
        values.update({6: (self.missing(6), None, self.stamp(6)), 7: (self.missing(7), None, self.stamp(7))})
        res = self.run_import(values)                       # 2 of 7 missing: 28 % >= 20 %
        self.assertTrue(self.memory['.']['foreign'])
        e = self.entry(1)
        self.assertEqual((e['textAnchor'], e['kobo']['precision'], e['updated_at']),
                         (0, kobo_ku.CHAPTER, '2026-11-01T10:00:01.000Z'))
        self.assertIn(self.uuids[1], res.written)

    def test_an_app_place_after_our_entry_is_not_touched_by_the_correction(self):
        values = self.decided_library()
        app = {'chapter': 4, 'textAnchor': 5, 'progress': 0.6, 'page': 0, 'scrollY': 0,
               'updated_at': '2026-11-03T10:00:00.000Z', 'deviceId': 'phone-1'}
        data = json.load(open(self.path, encoding='utf-8'))
        data['positions'][self.uuids[1]] = app
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        values.update({6: (self.missing(6), None, self.stamp(6)), 7: (self.missing(7), None, self.stamp(7))})
        self.run_import(values)
        self.assertEqual(self.entry(1), app)

    # -- re-review round 2, B1-R2 --------------------------------------------------------------------

    def test_B1R2_undecided_runs_write_chapter_starts_even_with_nothing_missing(self):
        values = {}
        for i in range(1, 5):                                # one book per USB connect, all found
            values[i] = (self.PRESENT[i % 2], None, self.stamp(i))
            res = self.run_import(dict(values))
            e = self.entry(i)
            self.assertEqual((e['textAnchor'], e['kobo']['precision']), (0, kobo_ku.CHAPTER), i)
            self.assertFalse(self.memory['.'].get('foreign'))
            self.assertTrue(res.foreign or res.undecided)

    def test_B1R2_once_judged_calibre_numbered_our_chapter_starts_are_upgraded_to_their_sentence(self):
        values = {}
        for i in range(1, 5):
            values[i] = (self.PRESENT[i % 2], None, self.stamp(i))
            self.run_import(dict(values))
        values[5] = (self.PRESENT[1], None, self.stamp(5))
        res = self.run_import(dict(values))                  # 5 places, none missing: calibre-numbered
        self.assertTrue(self.memory['.'].get('decided'))
        exact = self.positions()
        for i in range(1, 6):
            e = self.entry(i)
            self.assertEqual(e['kobo']['precision'], kobo_ku.SPAN, i)
            self.assertGreater(e['textAnchor'], 0, i)
        # the upgrade keeps the Kobo's own date, one millisecond on, so an app that already took the
        # chapter start (same stamp) takes the sentence; a place read in the app after it stays newer
        self.assertEqual(self.entry(1)['updated_at'], '2026-11-01T10:00:01.001Z')
        self.assertEqual(self.entry(1)['kobo']['lastRead'], '2026-11-01T10:00:01.000Z')
        self.assertEqual(self.entry(5)['updated_at'], '2026-11-01T10:00:05.000Z')
        for i in range(1, 5):
            self.assertIn(self.uuids[i], res.written)

    def test_B1R2_the_upgrade_leaves_entries_that_are_not_ours(self):
        values = {}
        for i in range(1, 5):
            values[i] = (self.PRESENT[i % 2], None, self.stamp(i))
            self.run_import(dict(values))
        app = {'chapter': 2, 'textAnchor': 0, 'progress': 0.01, 'page': 0, 'scrollY': 0,
               'updated_at': '2026-11-01T10:00:01.000Z', 'deviceId': 'phone-1'}     # the app's, same stamp
        data = json.load(open(self.path, encoding='utf-8'))
        data['positions'][self.uuids[1]] = app
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        values[5] = (self.PRESENT[1], None, self.stamp(5))
        self.run_import(dict(values))
        self.assertEqual(self.entry(1), app)
        self.assertEqual(self.entry(2)['kobo']['precision'], kobo_ku.SPAN)

    def test_B1R2_twenty_percent_missing_is_foreign(self):
        values = {i: (self.PRESENT[i % 2], None, self.stamp(i)) for i in range(1, 5)}
        values[5] = (self.missing(5), None, self.stamp(5))
        self.run_import(values)                             # 1 of 5 = 20 %
        self.assertTrue(self.memory['.']['foreign'])


class EditedBookTests(ImportBase):
    """Re-review round 3, S1-R2 as finally ruled: a book whose Kobo copy is numbered differently from
    calibre's numbering of its EPUB today (edited after it was sent) is flagged on ANY missing span id
    (calibre-made copies never miss one), and also when its EPUB file changes after the plugin first saw
    it (mtime / size per book). The flag stays until "Re-check numbering" or the EPUB changes again; a
    Kobo reading after the edit does NOT clear it. While flagged, the book's places go to the chapter start."""

    PRESENT = 'OEBPS/7446794606207039498_5200-h-1.htm.xhtml#kobo.10.2'
    OTHER = 'OEBPS/7446794606207039498_5200-h-2.htm.xhtml#kobo.5.1'
    MISSING = 'OEBPS/7446794606207039498_5200-h-1.htm.xhtml#kobo.950.1'

    def setUp(self):
        ImportBase.setUp(self)
        self.epub = os.path.join(self.lib, 'book.epub')
        shutil.copy2(EPUB, self.epub)
        self.edited('2026-10-01')
        self.memory['.'] = {'since': '2026-10-01T00:00:00.000Z', 'spans': 10, 'missing': 0, 'foreign': False,
                            'decided': True}

    def lookup(self, book_id):
        return self.uuids[book_id], self.epub, None

    def edited(self, iso_day):
        t = datetime.datetime.strptime(iso_day, '%Y-%m-%d').replace(tzinfo=UTC).timestamp()
        os.utime(self.epub, (t, t))

    def entry(self):
        return self.positions()[self.uuids[1]]

    def test_S1R2_any_missing_id_flags_the_book_whatever_the_dates(self):
        # the EPUB is OLDER than the Kobo's reading: the old rule skipped it and kept trusting the book
        self.run_import({1: (self.MISSING, None, '2026-11-01T10:00:00Z')})
        e = self.entry()
        self.assertEqual((e['chapter'], e['textAnchor'], e['kobo']['precision']), (2, 0, kobo_ku.CHAPTER))
        self.assertIn('edited', e['kobo']['note'])
        self.run_import({1: (self.PRESENT, None, '2026-11-02T10:00:00Z')})
        self.assertEqual((self.entry()['textAnchor'], self.entry()['kobo']['precision']), (0, kobo_ku.CHAPTER))

    def test_S1R2_a_kobo_reading_after_the_edit_does_not_clear_the_flag(self):
        # the reviewer's E3: flagged while the Kobo's dates were before the edit, then it reads on after it
        self.edited('2026-11-03')
        self.run_import({1: (self.MISSING, None, '2026-11-01T10:00:00Z')})
        for day, loc in (('2026-11-04', self.PRESENT), ('2026-11-05', self.OTHER)):
            self.run_import({1: (loc, None, day + 'T10:00:00Z')})
            self.assertEqual((self.entry()['textAnchor'], self.entry()['kobo']['precision']), (0, kobo_ku.CHAPTER), day)

    def test_S1R2_an_epub_changed_after_the_plugin_first_saw_it_flags_the_book(self):
        self.run_import({1: (self.PRESENT, None, '2026-11-01T10:00:00Z')})
        self.assertEqual(self.entry()['kobo']['precision'], kobo_ku.SPAN)      # first seen: trusted
        self.edited('2026-11-02')                                               # edited in calibre afterwards
        self.run_import({1: (self.OTHER, None, '2026-11-03T10:00:00Z')})       # the Kobo reads on, nothing missing
        e = self.entry()
        self.assertEqual((e['textAnchor'], e['kobo']['precision']), (0, kobo_ku.CHAPTER))
        self.assertIn('edited', e['kobo']['note'])

    def test_S1R4a_another_epub_change_keeps_the_flag_only_recheck_clears_it(self):
        # re-review round 4 S1-R4a (supersedes round 3's "the EPUB changes again clears it"): a second edit
        # is not a re-send; the Kobo still has the old copy
        self.run_import({1: (self.MISSING, None, '2026-11-01T10:00:00Z')})
        self.edited('2026-11-03')
        self.run_import({1: (self.PRESENT, None, '2026-11-04T10:00:00Z')})
        self.assertEqual((self.entry()['textAnchor'], self.entry()['kobo']['precision']), (0, kobo_ku.CHAPTER))
        kobo_ku.reset_numbering(self.memory)                                     # "Re-check numbering"
        self.memory['.'].update(spans=10, missing=0, decided=True)              # (the library decided again)
        self.run_import({1: (self.OTHER, None, '2026-11-05T09:00:00Z')})
        self.assertEqual(self.entry()['kobo']['precision'], kobo_ku.SPAN)

    def test_S1R4b_the_upgrade_skips_a_book_edited_since_it_was_first_seen_and_flags_it(self):
        self.memory.clear()                                                     # a new library: undecided
        self.run_import({1: (self.PRESENT, None, '2026-11-01T10:00:00Z')})      # book 1: chapter start
        self.assertEqual(self.entry()['kobo']['precision'], kobo_ku.CHAPTER)
        self.edited('2026-11-02')                                               # edited, not sent again
        others = {i: (self.OTHER, None, '2026-11-02T10:00:{:02d}Z'.format(i)) for i in range(2, 6)}
        values = {1: (self.PRESENT, None, '2026-11-01T10:00:00Z')}
        values.update(others)
        self.run_import(values)                                                 # 5 places: decided
        self.assertTrue(self.memory['.']['decided'])
        e = self.entry()
        self.assertEqual((e['textAnchor'], e['kobo']['precision'], e['updated_at']),
                         (0, kobo_ku.CHAPTER, '2026-11-01T10:00:00.000Z'))    # not upgraded
        self.assertTrue(self.memory[self.uuids[1].lower()].get('edited'))

    def test_b_a_kobo_copy_older_than_the_epub_flags_the_book_before_any_missing_id(self):
        # residual (b): calibre's own in-memory list of the connected Kobo's books gives each copy's date
        copies = {1: datetime.datetime(2026, 9, 1, tzinfo=UTC).timestamp()}     # sent 2026-09-01; EPUB of 2026-10-01
        self.run_import({1: (self.PRESENT, None, '2026-11-01T10:00:00Z')}, kobo_copies=copies)
        e = self.entry()
        self.assertEqual((e['textAnchor'], e['kobo']['precision']), (0, kobo_ku.CHAPTER))
        self.assertTrue(self.memory[self.uuids[1].lower()].get('edited'))

    def test_b_after_recheck_only_an_epub_newer_than_the_recheck_too_is_flagged(self):
        # re-review round 5: a Kobo probably keeps a replaced book's original DateCreated, so a re-sent
        # edited book still "looks" older than its EPUB; after "Re-check numbering" the EPUB must also be
        # newer than the time Re-check was clicked
        copies = {1: datetime.datetime(2026, 9, 1, tzinfo=UTC).timestamp()}     # the Kobo's (original) date
        self.run_import({1: (self.PRESENT, None, '2026-11-01T08:00:00Z')}, kobo_copies=copies)
        self.assertEqual(self.entry()['kobo']['precision'], kobo_ku.CHAPTER)     # flagged (EPUB of 2026-10-01)
        kobo_ku.reset_numbering(self.memory, now=datetime.datetime(2026, 10, 15, tzinfo=UTC))   # sent again, Re-check
        self.memory['.'].update(spans=10, missing=0, decided=True)
        self.run_import({1: (self.OTHER, None, '2026-11-01T09:00:00Z')}, kobo_copies=copies)
        self.assertEqual(self.entry()['kobo']['precision'], kobo_ku.SPAN)        # not flagged again
        self.assertEqual(self.memory['.']['rechecked_at'], '2026-10-15T00:00:00.000Z')
        self.edited('2026-10-20')                                                # edited after the Re-check
        self.run_import({1: (self.PRESENT, None, '2026-11-01T10:00:00Z')}, kobo_copies=copies)
        self.assertEqual(self.entry()['kobo']['precision'], kobo_ku.CHAPTER)

    def test_b_a_kobo_copy_newer_than_the_epub_changes_nothing(self):
        copies = {1: datetime.datetime(2026, 10, 15, tzinfo=UTC).timestamp()}   # sent after the last edit
        self.run_import({1: (self.PRESENT, None, '2026-11-01T10:00:00Z')}, kobo_copies=copies)
        self.assertEqual(self.entry()['kobo']['precision'], kobo_ku.SPAN)
        self.run_import({1: (self.OTHER, None, '2026-11-02T10:00:00Z')}, kobo_copies=None)  # no Kobo: as before
        self.assertEqual(self.entry()['kobo']['precision'], kobo_ku.SPAN)

    def test_S1R2_recheck_numbering_clears_the_books_flags_too(self):
        self.run_import({1: (self.MISSING, None, '2026-11-01T10:00:00Z')})
        self.assertTrue(self.memory[self.uuids[1].lower()].get('edited'))
        kobo_ku.reset_numbering(self.memory)
        self.assertNotIn('edited', self.memory[self.uuids[1].lower()])
        self.assertNotIn('epub_seen', self.memory[self.uuids[1].lower()])
        self.assertEqual((self.memory['.']['foreign'], self.memory['.']['spans']), (False, 0))

    def test_NITR33_the_undecided_line_says_how_many_places_are_still_needed(self):
        self.memory.clear()
        res = self.run_import({1: (self.PRESENT, None, '2026-11-01T10:00:00Z')})
        self.assertIn('4 more', res.undecided)


class KoboCopyTimesTests(unittest.TestCase):
    """Residual (b): kobo_copy_times reads calibre's own in-memory list of the connected Kobo's books
    (Main.booklists(): memory, card A, card B; each calibre device Book has application_id = the
    calibre book id calibre matched, and datetime = the Kobo's DateCreated, a UTC struct_time)."""

    class DeviceBook(object):
        def __init__(self, book_id, when, other=None):
            self.application_id = book_id
            self.datetime = time.gmtime(when)
            if other:
                self.__dict__.update(other)

    def test_the_newest_copy_per_matched_book(self):
        b = self.DeviceBook
        lists = ([b(1, 1000), b(2, 5000), b(None, 9000), b(1, 3000)], [b(2, 4000)], [])
        self.assertEqual(kobo_ku.kobo_copy_times(lists), {1: 3000, 2: 5000})

    def test_nothing_usable_is_none(self):
        self.assertIsNone(kobo_ku.kobo_copy_times(None))
        self.assertIsNone(kobo_ku.kobo_copy_times(([], [], [])))

        class Odd(object):
            application_id = 4
            datetime = 'not a date'
        self.assertIsNone(kobo_ku.kobo_copy_times(([Odd()], [], [])))


if __name__ == '__main__':
    unittest.main()

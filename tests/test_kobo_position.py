# Last modified: 2026-11-05--0358
#
# Kobo position -> CalibReader position (kobo_position.map_bookmark), on a real
# public-domain book from the SAFE library: Kafka, The Metamorphosis (Project
# Gutenberg EPUB3, 6 spine items, a cover wrapper first) and the KEPUB
# kepubify 4.0.4 makes of it (what Calibre-Web stores next to the EPUB when
# kepubify is configured).
#
#   python3 -m unittest discover -s tests -v      (from the plugin folder)

import os
import re
import sys
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from kobo_position import (  # noqa: E402
    BOOK_PERCENT, CFI, CHAPTER_PERCENT, CHAPTER_START, SPAN, Book, ChapterText, map_bookmark,
)
import kobo_position  # noqa: E402

EPUB = os.path.join(HERE, 'fixtures', 'metamorphosis.epub')
KEPUB = os.path.join(HERE, 'fixtures', 'metamorphosis.kepub')
CH = 'OEBPS/7446794606207039498_5200-h-{}.htm.xhtml'   # spine 1..5 = h-0..h-4


def bm(source=None, ltype=None, value=None, pct=None, cpct=None):
    return {'location_source': source, 'location_type': ltype, 'location_value': value,
            'progress_percent': pct, 'content_source_progress_percent': cpct}


def norm(s):
    return ' '.join(s.split())


def kepub_span_text(chapter_member, span_id):
    '''The span's text straight from the KEPUB bytes (independent of the code
    under test).'''
    with zipfile.ZipFile(KEPUB) as z:
        html = z.read(chapter_member).decode('utf-8')
    m = re.search(r'id="' + re.escape(span_id) + r'">([^<]*)<', html)
    return norm(m.group(1)) if m else None


class SpacerParityTests(unittest.TestCase):
    '''M3: the app collapses spacer blocks (ChapterHTML.collapseSpacers) before
    WebKit counts text; the plugin must count the same.'''

    def text(self, body):
        return ChapterText('<html><head><title>t</title></head><body>' + body + '</body></html>').text

    def test_a_spacer_counts_nothing(self):
        self.assertEqual(self.text('<p>a</p>\n<p>&#160;</p>\n<p>b</p>'), 'a\n\nb')
        self.assertEqual(self.text('<p>a</p>\n<p>\u00a0</p>\n<p>b</p>'), 'a\n\nb')
        self.assertEqual(self.text('<p>a</p>\n<div class="x"> <br/> <br /> </div>\n<p>b</p>'), 'a\n\nb')

    def test_a_run_of_spacers_loses_the_whitespace_between_them(self):
        self.assertEqual(self.text('<p>a</p>\n<p>&nbsp;</p>\n  <p>&emsp;</p>\n<p>b</p>'), 'a\n\nb')

    def test_not_spacers(self):
        self.assertEqual(self.text('<p>a</p><p> </p><p>b</p>'), 'a b')                 # whitespace only
        self.assertEqual(self.text('<p>a</p><p><br clear="all"/></p><p>b</p>'), 'ab')    # clears a float: kept, no text
        self.assertEqual(self.text('<p>a</p><p>&nbsp;x</p>'), 'a\u00a0x')                # ink
        self.assertEqual(self.text('<p>a</p><p><span>&nbsp;</span></p>'), 'a\u00a0')     # an element inside

    def test_a_spacer_inside_a_comment_or_script_is_ignored(self):
        self.assertEqual(self.text('<!-- <p>&nbsp;</p> --><p>a</p>'), 'a')

    def test_junk_after_body_is_dropped_as_the_app_does(self):
        ct = ChapterText('<html><body><p>a</p></body>\n<p> z/body&gt;</p></html></html>')
        self.assertEqual(ct.text, 'a\n')


class MappingTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.epub = Book(EPUB)
        cls.kepub = Book(KEPUB)

    @classmethod
    def tearDownClass(cls):
        cls.epub.close()
        cls.kepub.close()

    def at(self, res, n=60):
        return norm(self.epub.text(res['chapter']).excerpt(res['textAnchor'], n))

    # -- spine convention -----------------------------------------------------
    def test_chapters_follow_the_apps_spine_numbering(self):
        # EPUBExtractor: every html spine item, the leading cover wrapper KEPT
        # (index 0), so h-1 (Part I) is chapter 2.
        self.assertEqual(self.epub.chapters[0], 'OEBPS/wrap0000.xhtml')
        self.assertEqual(self.epub.chapters[2], CH.format(1))
        self.assertEqual(len(self.epub.chapters), 6)

    # -- KoboSpan -----------------------------------------------------------------
    def test_span_at_chapter_start(self):
        r = map_bookmark(self.epub, bm(CH.format(1), 'KoboSpan', 'kobo.1.1', 10.0, 0.0), self.kepub)
        self.assertEqual((r['chapter'], r['precision']), (2, SPAN))
        self.assertTrue(self.at(r).startswith('I One morning, when Gregor Samsa'), self.at(r))
        self.assertLess(r['textAnchor'], 5)

    def test_span_mid_chapter(self):
        r = map_bookmark(self.epub, bm(CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0), self.kepub)
        self.assertEqual((r['chapter'], r['precision']), (2, SPAN))
        want = kepub_span_text(CH.format(1), 'kobo.3.5')
        self.assertTrue(want.startswith('A collection of textile samples'))
        self.assertTrue(self.at(r, 2 * len(want)).startswith(want), (self.at(r), want))

    def test_span_in_last_chapter(self):
        # The story's last sentence (Part III, chapter 4) and the last spine
        # item (the PG licence, chapter 5).
        r = map_bookmark(self.epub, bm(CH.format(3), 'KoboSpan', 'kobo.39.11', 92.0, 99.0), self.kepub)
        self.assertEqual((r['chapter'], r['precision']), (4, SPAN))
        want = kepub_span_text(CH.format(3), 'kobo.39.11')
        self.assertTrue(want.startswith('And, as if in confirmation of their new dreams'))
        self.assertTrue(self.at(r, 2 * len(want)).startswith(want))
        self.assertAlmostEqual(r['progress'], 0.92)
        r = map_bookmark(self.epub, bm(CH.format(4), 'KoboSpan', 'kobo.2.78', 99.0, 98.0), self.kepub)
        self.assertEqual((r['chapter'], r['precision']), (5, SPAN))
        self.assertTrue(self.at(r).startswith('This website includes information about Project Gutenberg'))

    def test_every_span_of_the_book_lands_on_its_own_sentence(self):
        # The whole-book property: for every kobo span with real text, the
        # mapped EPUB offset starts with that span's text.
        checked = 0
        with zipfile.ZipFile(KEPUB) as z:
            for ch in range(len(self.epub.chapters)):
                member = self.epub.chapters[ch]
                html = z.read(member).decode('utf-8')
                for sid, txt in re.findall(r'id="(kobo\.\d+\.\d+)">([^<]*)<', html):
                    t = norm(txt)
                    if len(t) < 3:
                        continue
                    r = map_bookmark(self.epub, bm(member, 'KoboSpan', sid, 50.0, 50.0), self.kepub)
                    self.assertEqual(r['precision'], SPAN, sid)
                    self.assertTrue(self.at(r, 2 * len(t) + 10).startswith(t), (sid, t, self.at(r)))
                    checked += 1
        self.assertGreater(checked, 900)

    def test_span_without_kepub_falls_back_to_chapter_percent(self):
        r = map_bookmark(self.epub, bm(CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 50.0), None)
        self.assertEqual((r['chapter'], r['precision']), (2, CHAPTER_PERCENT))
        L = self.epub.text(2).length
        self.assertLess(abs(r['textAnchor'] - L // 2), 40)

    def test_unknown_span_id_falls_back_to_chapter_percent(self):
        r = map_bookmark(self.epub, bm(CH.format(1), 'KoboSpan', 'kobo.9999.1', 12.0, 25.0), self.kepub)
        self.assertEqual(r['precision'], CHAPTER_PERCENT)

    # -- EpubCfi --------------------------------------------------------------------
    def test_cfi_resolves_to_the_same_place_as_the_span(self):
        # /6/6 = spine itemref 3 (h-1); /4 body, /2 div.chapter, /6 the second
        # <p>, /1:1 its text after the leading newline.
        cfi = map_bookmark(self.epub, bm(CH.format(1), 'EpubCfi', 'epubcfi(/6/6!/4/2/6/1:1)', 12.0, 3.0))
        span = map_bookmark(self.epub, bm(CH.format(1), 'KoboSpan', 'kobo.3.1', 12.0, 3.0), self.kepub)
        self.assertEqual((cfi['chapter'], cfi['precision']), (2, CFI))
        self.assertTrue(self.at(cfi).startswith('“What’s happened to me?”'), self.at(cfi))
        self.assertEqual(cfi['textAnchor'], span['textAnchor'])

    def test_cfi_without_source_takes_its_chapter_from_the_spine_step(self):
        r = map_bookmark(self.epub, bm(None, 'EpubCfi', 'epubcfi(/6/6[item4]!/4/2[pgepubid00002]/6/1:1)', 12.0, None))
        self.assertEqual((r['chapter'], r['precision']), (2, CFI))
        self.assertTrue(self.at(r).startswith('“What’s happened'))

    def test_cfi_that_does_not_resolve_falls_back(self):
        r = map_bookmark(self.epub, bm(CH.format(1), 'EpubCfi', 'epubcfi(/6/6!/4/2/999/1:0)', 12.0, 40.0))
        self.assertEqual(r['precision'], CHAPTER_PERCENT)

    # -- percentages ------------------------------------------------------------------
    def test_chapter_percent_mid_chapter_is_a_word_start(self):
        r = map_bookmark(self.epub, bm(CH.format(2), 'KoboSpan', 'kobo.5.1', 40.0, 50.0), None)
        self.assertEqual((r['chapter'], r['precision']), (3, CHAPTER_PERCENT))
        t = self.epub.text(3).text
        a = r['textAnchor']
        self.assertFalse(t[a].isspace())
        self.assertTrue(a == 0 or t[a - 1].isspace())

    def test_chapter_percent_zero_and_hundred(self):
        r0 = map_bookmark(self.epub, bm(CH.format(4), None, None, 97.0, 0.0))
        self.assertEqual((r0['chapter'], r0['precision']), (5, CHAPTER_PERCENT))
        self.assertTrue(self.at(r0).startswith('*** END OF THE PROJECT GUTENBERG'), self.at(r0))
        r1 = map_bookmark(self.epub, bm(CH.format(4), None, None, 100.0, 100.0))
        self.assertEqual(r1['textAnchor'], self.epub.text(5).length)   # end-of-chapter marker

    def test_percent_only_maps_book_wide(self):
        r = map_bookmark(self.epub, bm(None, None, None, 50.0, None))
        self.assertEqual(r['precision'], BOOK_PERCENT)
        lengths = self.epub.lengths()
        place = sum(lengths[:r['chapter']]) + r['textAnchor']
        self.assertLess(abs(place - sum(lengths) / 2.0), 30)
        self.assertAlmostEqual(r['progress'], 0.5)
        r0 = map_bookmark(self.epub, bm(None, None, None, 0.0, None))
        self.assertEqual((r0['chapter'], r0['textAnchor']), (0, 0))

    def test_unknown_href_is_not_mapped(self):
        # M4: a Source that is not in this book means the wrong book (another
        # library, a reused calibre id): never a confident place by book %.
        r = map_bookmark(self.epub, bm('OEBPS/not-in-this-book.xhtml', 'KoboSpan', 'kobo.3.5', 60.0, 10.0), self.kepub)
        self.assertIsNone(r)

    def test_source_spellings(self):
        for s in ('/' + CH.format(1), CH.format(1).upper(), '7446794606207039498_5200-h-1.htm.xhtml',
                  'OEBPS!' + CH.format(1)[6:]):
            r = map_bookmark(self.epub, bm(s, 'KoboSpan', 'kobo.3.5', 12.0, 3.0), self.kepub)
            self.assertEqual((r['chapter'], r['precision']), (2, SPAN), s)

    def test_source_with_nothing_else_is_chapter_start(self):
        r = map_bookmark(self.epub, bm(CH.format(2), None, None, None, None))
        self.assertEqual((r['chapter'], r['textAnchor'], r['precision']), (3, 0, CHAPTER_START))

    def test_nothing_usable_is_none(self):
        self.assertIsNone(map_bookmark(self.epub, bm(None, None, None, None, None)))
        self.assertIsNone(map_bookmark(self.epub, bm('', 'KoboSpan', '', None, None)))

class ReverseMappingTests(unittest.TestCase):
    '''Step 2 (CalibReader -> Kobo): the app's chapter + textAnchor -> the Kobo
    bookmark to PUT (kobo_position.kobo_location). Round trip through step 1's
    map_bookmark: lands at or just before the app's place.'''

    @classmethod
    def setUpClass(cls):
        cls.epub = Book(EPUB)
        cls.kepub = Book(KEPUB)

    @classmethod
    def tearDownClass(cls):
        cls.epub.close()
        cls.kepub.close()

    def back(self, loc, kepub='same'):
        '''Where step 1's import puts the pushed bookmark.'''
        row = {'location_type': loc['type'], 'location_value': loc['value'], 'location_source': loc['source'],
               'progress_percent': loc['progressPercent'],
               'content_source_progress_percent': loc['contentSourceProgressPercent']}
        return map_bookmark(self.epub, row, self.kepub if kepub == 'same' else kepub)

    def spans(self, chapter):
        kct = self.kepub.text(chapter)
        return [i for i in kct.ids if re.match(r'^kobo\.\d+\.\d+$', i)]

    def starts(self, chapter):
        out = set()
        for s in self.spans(chapter):
            hit = kobo_position.resolve_span(self.epub, self.kepub, chapter, s)
            if hit is not None and hit[1] == SPAN:
                out.add(hit[0])
        return sorted(out)

    def test_every_span_start_round_trips_exactly(self):
        # anchor = a span's own place -> that span (or one at the same place) -> the same anchor
        checked = 0
        for ch in (1, 2, 3, 4, 5):
            for sid in self.spans(ch):
                hit = kobo_position.resolve_span(self.epub, self.kepub, ch, sid)
                if hit is None or hit[1] != SPAN:
                    continue
                loc = kobo_position.kobo_location(self.epub, ch, hit[0], self.kepub)
                self.assertEqual((loc['type'], loc['precision']), ('KoboSpan', SPAN), sid)
                self.assertEqual(loc['source'], self.epub.chapters[ch])
                back = self.back(loc)
                self.assertEqual((back['chapter'], back['textAnchor']), (ch, hit[0]), (sid, loc['value']))
                self.assertEqual(loc['anchor'], hit[0])
                checked += 1
        self.assertGreater(checked, 900)

    def test_any_place_lands_at_or_just_before_it(self):
        # Every 23rd character of the story: the span the character is in; back <= place,
        # and the next span starts after the place (it is the sentence the place is in).
        for ch in (2, 3, 4):
            starts = self.starts(ch)
            text = self.epub.text(ch).text
            for anchor in range(0, self.epub.text(ch).length, 23):
                loc = kobo_position.kobo_location(self.epub, ch, anchor, self.kepub)
                back = self.back(loc)
                self.assertEqual(back['chapter'], ch)
                if back['textAnchor'] > anchor:
                    # only before the chapter's first span, with nothing but whitespace between
                    self.assertEqual(back['textAnchor'], starts[0])
                    self.assertFalse(text[anchor:back['textAnchor']].strip(), (ch, anchor))
                    continue
                self.assertEqual(back['textAnchor'], max(s for s in starts if s <= anchor), (ch, anchor))

    def test_the_end_of_chapter_marker_is_the_last_span(self):
        L = self.epub.text(2).length
        loc = kobo_position.kobo_location(self.epub, 2, L, self.kepub)
        self.assertEqual(self.back(loc)['textAnchor'], self.starts(2)[-1])

    def test_the_sentence_is_the_apps_sentence(self):
        # "A collection of textile samples..." (kobo.3.5): any character inside it maps to it.
        start = kobo_position.resolve_span(self.epub, self.kepub, 2, 'kobo.3.5')[0]
        for inside in (start, start + 5, start + 40):
            self.assertEqual(kobo_position.kobo_location(self.epub, 2, inside, self.kepub)['value'], 'kobo.3.5')

    def test_percentages_are_text_fractions(self):
        loc = kobo_position.kobo_location(self.epub, 3, 20000, self.kepub)
        lengths = self.epub.lengths()
        a = loc['anchor']
        self.assertAlmostEqual(loc['contentSourceProgressPercent'], 100.0 * a / lengths[3], places=2)
        self.assertAlmostEqual(loc['progressPercent'], 100.0 * (sum(lengths[:3]) + a) / sum(lengths), places=2)

    def test_without_a_kepub_source_and_percentages_only(self):
        text = self.epub.text(3).text
        for anchor in (0, 1, 777, 20000, 40208):
            loc = kobo_position.kobo_location(self.epub, 3, anchor, None)
            self.assertEqual((loc['type'], loc['value'], loc['precision']), ('', '', CHAPTER_PERCENT))
            self.assertEqual(loc['source'], CH.format(2))
            back = self.back(loc, kepub=None)
            self.assertEqual((back['chapter'], back['precision']), (3, CHAPTER_PERCENT))
            self.assertEqual(back['textAnchor'], loc['anchor'], anchor)
            # at or before the place, never past a word boundary before it
            if back['textAnchor'] <= anchor:
                self.assertNotIn(' ', text[back['textAnchor']:anchor].strip(), anchor)
            else:
                self.assertFalse(text[anchor:back['textAnchor']].strip(), anchor)   # leading whitespace only

    def test_a_chapter_without_spans_uses_the_percentages(self):
        loc = kobo_position.kobo_location(self.epub, 0, 3, self.kepub)      # the cover wrapper
        self.assertEqual((loc['type'], loc['value'], loc['precision']), ('', '', CHAPTER_PERCENT))
        self.assertEqual(loc['source'], 'OEBPS/wrap0000.xhtml')

    def test_the_source_mirrors_the_kobos_own_spelling(self):
        anchor = kobo_position.resolve_span(self.epub, self.kepub, 3, 'kobo.5.1')[0]
        kobo_said = '7446794606207039498_5200-h-1.htm.xhtml'          # the Kobo's Source for chapter 2
        loc = kobo_position.kobo_location(self.epub, 3, anchor, self.kepub, kobo_source=kobo_said)
        self.assertEqual(loc['source'], '7446794606207039498_5200-h-2.htm.xhtml')
        loc = kobo_position.kobo_location(self.epub, 3, anchor, self.kepub, kobo_source='/' + CH.format(1))
        self.assertEqual(loc['source'], '/' + CH.format(2))
        loc = kobo_position.kobo_location(self.epub, 3, anchor, self.kepub, kobo_source='not-in-this-book.xhtml')
        self.assertEqual(loc['source'], CH.format(2))
        self.assertEqual(self.back(loc)['chapter'], 3)

    def test_a_chapter_out_of_range_is_none(self):
        self.assertIsNone(kobo_position.kobo_location(self.epub, 6, 0, self.kepub))
        self.assertIsNone(kobo_position.kobo_location(self.epub, -1, 0, self.kepub))


if __name__ == '__main__':
    unittest.main()

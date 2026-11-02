# Last modified: 2026-11-02--1447
#
# kobo_position.py — a Kobo reading position -> a CalibReader position.
#
# Calibre-Web's Kobo sync stores the Kobo's place per book in its app.db
# (kobo_bookmark): a Location {Type, Value, Source} plus two percentages,
# ProgressPercent (whole book) and ContentSourceProgressPercent (inside the
# file named by Source). CalibReader's place is {chapter, textAnchor}:
#   chapter    = index into the book's spine items whose manifest media-type
#                contains "html" (EPUBExtractor.SpineParser; leading cover pages
#                are KEPT in the list, only flagged);
#   textAnchor = a character offset into that chapter's text: the summed
#                .length (UTF-16 code units) of the text nodes under <body>, in
#                document order, as WebKit's TreeWalker(SHOW_TEXT) counts them
#                over the reader's #_dp_inner wrapper (ReaderViewModel
#                `_crAnchorOffset`). Whitespace is NOT collapsed.
#
# Resolution, best first (see docs/handoffs/104-kobo-sync/DESIGN.md):
#   1. KoboSpan  "kobo.P.S" found in the book's KEPUB (Calibre-Web's kepubify
#      output sits next to the EPUB) -> that span's text located in the EPUB
#      chapter -> the span's first character. ~sentence.
#   2. EpubCfi   epubcfi(/6/N!/4/...:c) resolved in the EPUB chapter. ~element/char.
#   3. Source + ContentSourceProgressPercent -> that fraction of the chapter's
#      text, snapped back to a word start. ~a page or two (Kobo's % is its own
#      measure).
#   4. ProgressPercent only -> the book-wide fraction over all chapters' text.
#
# Pure stdlib (zipfile, html.parser, re, posixpath); nothing from calibre, so it
# is unit-tested outside calibre (tests/test_kobo_position.py).
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

from __future__ import annotations

__license__   = 'GPL v3'
__copyright__ = '2026, CalibReader'

import posixpath
import re
import zipfile
from html.parser import HTMLParser
from urllib.parse import unquote
from xml.etree import ElementTree

# Precision labels written into the sidecar's `kobo` block (and the DESIGN table).
SPAN = 'span'            # KoboSpan resolved through the KEPUB: the sentence
SPAN_APPROX = 'span~'    # KoboSpan found, its text not found in the EPUB: kepub offset scaled
CFI = 'cfi'              # EpubCfi resolved in the chapter
CHAPTER_PERCENT = 'chapter%'   # Source + ContentSourceProgressPercent
BOOK_PERCENT = 'book%'         # ProgressPercent only
CHAPTER_START = 'chapter-start'  # Source known, no usable in-chapter place

_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param',
         'source', 'track', 'wbr', 'basefont', 'bgsound', 'frame', 'keygen', 'image'}


def u16len(s):
    '''JavaScript string length (UTF-16 code units): what a text node's .length is.'''
    return len(s.encode('utf-16-le')) // 2



# -- the app's chapter hygiene that changes text (ChapterHTML.prepare) ---------------

_WS = ' \t\n\r\f'
_BLANK_CHARS = ' ­    ​'
_BLANK_NAMED = ('&nbsp;', '&ensp;', '&emsp;', '&thinsp;', '&hairsp;', '&shy;', '&zerowidthspace;')
_BLANK_CODES = {0xA0, 0xAD, 0x2002, 0x2003, 0x2009, 0x200A, 0x200B}
_RAW = ('script', 'style', 'title', 'textarea', 'xmp', 'noscript', 'iframe')


def drop_after_body(html):
    """ChapterHTML.dropAfterBody: junk after the LAST </body> (old calibre
    MOBI->EPUB) is cut, unless the tail is only whitespace and </html>."""
    matches = list(re.finditer(r'</body\s*>', html, re.I))
    if not matches:
        return html
    end = matches[-1].end()
    if re.match(r'^\s*(</html\s*>\s*)?$', html[end:], re.I):
        return html
    return html[:end] + '\n</html>'


def _blank_len(s, j):
    c = s[j]
    if c in _BLANK_CHARS:
        return 1
    if c != '&' or j + 1 >= len(s):
        return 0
    if s[j + 1] == '#':
        k = j + 2
        hexa = k < len(s) and s[k] in 'xX'
        if hexa:
            k += 1
        v = digits = 0
        while k < len(s) and digits < 7:
            d = s[k]
            if d.isdigit() and d.isascii():
                v = v * (16 if hexa else 10) + int(d)
            elif hexa and d.lower() in 'abcdef':
                v = v * 16 + int(d, 16)
            else:
                break
            k += 1
            digits += 1
        if digits == 0 or k >= len(s) or s[k] != ';':
            return 0
        return k + 1 - j if v in _BLANK_CODES else 0
    low = s[j:j + 18].lower()
    for name in _BLANK_NAMED:
        if low.startswith(name):
            return len(name)
    return 0


def _tag_end(s, frm):
    j, quote = frm, ''
    while j < len(s):
        c = s[j]
        if quote:
            if c == quote:
                quote = ''
        elif c in '"\'':
            quote = c
        elif c == '>':
            return j
        j += 1
    return len(s)


def _spacer(s, st):
    """ChapterHTML.collapseSpacers' spacer test at `st` ('<p' / '<div'):
    -> end index (one past its '>') or None."""
    n = len(s)
    if st + 2 >= n or s[st] != '<':
        return None
    e = st + 1
    while e < n and s[e].isascii() and s[e].isalpha():
        e += 1
    name = s[st + 1:e].lower()
    if name not in ('p', 'div') or e >= n or not (s[e] in _WS or s[e] == '>'):
        return None
    gt = _tag_end(s, e)
    if gt >= n or s[gt - 1] == '/':
        return None
    j, brs, text_after_br = gt + 1, 0, False
    while j < n:
        c = s[j]
        if c in _WS:
            j += 1
            continue
        b = _blank_len(s, j)
        if b:
            text_after_br = True
            j += b
            continue
        if c == '&' or c != '<' or j + 1 >= n:
            return None
        if s[j:j + 3].lower() == '<br' and j + 3 < n and (s[j + 3] in _WS or s[j + 3] in '/>'):
            g = j + 3
            while g < n and s[g] in _WS:
                g += 1
            if g < n and s[g] == '/':
                g += 1
            if g >= n or s[g] != '>':
                return None
            brs += 1
            text_after_br = False
            j = g + 1
            continue
        if s[j + 1] == '/':
            ln = e - st - 1
            if j + 2 + ln >= n or s[j + 2:j + 2 + ln].lower() != s[st + 1:st + 1 + ln].lower():
                return None
            k = j + 2 + ln
            while k < n and s[k] in _WS:
                k += 1
            if k >= n or s[k] != '>':
                return None
            if brs + (1 if text_after_br else 0) == 0:
                return None
            return k + 1
        return None
    return None


def spacer_runs(s):
    """[(start, end)] of the spacer runs ChapterHTML.collapseSpacers empties
    (a run = spacers separated only by whitespace; the whitespace between them
    goes too). Same scan: comments and raw-text elements are skipped."""
    out, n, i = [], len(s), 0
    while i < n:
        i = s.find('<', i)
        if i < 0 or i + 1 >= n:
            break
        c1 = s[i + 1]
        if c1 == '!':
            if s.startswith('<!--', i):
                k = s.find('-->', i + 4)
                i = n if k < 0 else k + 3
            else:
                i += 2
            continue
        if not (c1.isascii() and c1.isalpha()):
            i += 1
            continue
        e = i + 1
        while e < n and s[e].isascii() and s[e].isalnum():
            e += 1
        name = s[i + 1:e].lower()
        if name in _RAW:
            p = _tag_end(s, e) + 1
            closer = '</' + name
            while p < n and s[p:p + len(closer)].lower() != closer:
                q = s.find('<', p + 1)
                p = n if q < 0 else q
            i = p
            continue
        if name not in ('p', 'div'):
            i = e
            continue
        end = _spacer(s, i)
        if end is None:
            i = e
            continue
        while True:
            w = end
            while w < n and s[w] in _WS:
                w += 1
            nxt = _spacer(s, w)
            if nxt is None:
                break
            end = nxt
        out.append((i, end))
        i = end
    return out


class _Element(object):
    __slots__ = ('tag', 'id', 'children', 'start', 'parent')

    def __init__(self, tag, el_id, parent, start):
        self.tag, self.id, self.parent, self.start = tag, el_id, parent, start
        self.children = []


class _Text(object):
    __slots__ = ('data', 'start')

    def __init__(self, data, start):
        self.data, self.start = data, start


class ChapterText(HTMLParser):
    '''One chapter's text as the reader counts it, plus a light element tree
    (for CFI steps) and each id's offset.'''

    def __init__(self, html):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = _Element('#document', None, None, 0)
        self._cur = self.root
        self._in_body = False
        self._has_body = re.search(r'<body\b', html, re.I) is not None
        self._in_head = False
        self.length = 0
        self.parts = []            # body text, in order (joined = .text)
        self.ids = {}              # id -> text offset at the element's start
        self.id_text = {}          # id -> the element's own text (filled at close)
        self._open_ids = {}        # id(element) -> parts index at open
        # The HTML parser's input preprocessing: CR LF and lone CR become LF.
        # Then what the app does before WebKit sees the chapter and that
        # changes text (M3): junk after </body> cut, spacer runs emptied.
        src = drop_after_body(html.replace('\r\n', '\n').replace('\r', '\n'))
        self._runs = spacer_runs(src)
        self._line_starts = [0] + [m.end() for m in re.finditer('\n', src)]
        self.feed(src)
        self.close()
        self.text = ''.join(self.parts)

    # -- tree ------------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower().split(':')[-1]
        if tag == 'head':
            self._in_head = True
        if tag == 'body':
            self._in_body = True
        el_id = None
        for k, v in attrs:
            if k.lower() == 'id' and v is not None:
                el_id = v
        el = _Element(tag, el_id, self._cur, self.length)
        self._cur.children.append(el)
        if el_id is not None and self._counting() and el_id not in self.ids:
            self.ids[el_id] = self.length
            self._open_ids[id(el)] = len(self.parts)
        if tag not in _VOID:
            self._cur = el

    def handle_startendtag(self, tag, attrs):
        tag_l = tag.lower().split(':')[-1]
        self.handle_starttag(tag, attrs)
        if tag_l not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower().split(':')[-1]
        if tag == 'head':
            self._in_head = False
        n = self._cur
        while n is not None and n is not self.root and n.tag != tag:
            n = n.parent
        if n is None or n is self.root:
            return                 # stray end tag: ignored, as the HTML parser does
        while True:
            closing = self._cur
            self._close_id(closing)
            self._cur = closing.parent
            if closing is n:
                break

    def _close_id(self, el):
        idx = self._open_ids.pop(id(el), None)
        if idx is not None:
            self.id_text[el.id] = ''.join(self.parts[idx:])

    def _counting(self):
        if self._has_body:
            return self._in_body
        return not self._in_head

    def _in_spacer_run(self):
        if not self._runs:
            return False
        line, col = self.getpos()
        at = self._line_starts[line - 1] + col
        for a, b in self._runs:
            if a <= at < b:
                return True
            if a > at:
                break
        return False

    def handle_data(self, data):
        if not self._counting() or self._in_spacer_run():
            return
        self._cur.children.append(_Text(data, self.length))
        self.parts.append(data)
        self.length += u16len(data)

    # -- queries ----------------------------------------------------------------
    def html_element(self):
        for c in self.root.children:
            if isinstance(c, _Element) and c.tag == 'html':
                return c
        return self.root

    def word_start(self, offset):
        '''Snap `offset` back to the start of the word it falls in (a place that
        starts mid-word reads badly in a log; the reader shows the same page).'''
        offset = max(0, min(offset, self.length))
        units = self.text.encode('utf-16-le')
        def ch(i):
            return units[2 * i:2 * i + 2].decode('utf-16-le', 'replace')
        if offset >= self.length:
            return self.length
        # Skip forward over whitespace first: an offset in the gap between
        # paragraphs belongs to the next word.
        i = offset
        while i < self.length and ch(i).isspace():
            i += 1
        if i > offset:
            return i if i < self.length else offset
        while i > 0 and not ch(i - 1).isspace():
            i -= 1
        return i

    def excerpt(self, offset, n=60):
        units = self.text.encode('utf-16-le')
        return units[2 * offset:2 * (offset + n)].decode('utf-16-le', 'replace').replace('\n', ' ')


# -- the book -----------------------------------------------------------------

class Book(object):
    '''An EPUB (or KEPUB) opened from a path: spine chapters as CalibReader numbers them.'''

    def __init__(self, path):
        self.path = path
        self.zf = zipfile.ZipFile(path)
        self.names = {n.lower(): n for n in self.zf.namelist()}
        opf = self._opf_path()
        self.opf_dir = posixpath.dirname(opf)
        root = ElementTree.fromstring(self.zf.read(opf))
        def local(t):
            return t.rsplit('}', 1)[-1]
        manifest = {}
        itemrefs = []
        for el in root.iter():
            name = local(el.tag)
            if name == 'item' and el.get('id') and el.get('href') is not None:
                manifest[el.get('id')] = (el.get('href'), el.get('media-type') or '')
            elif name == 'itemref' and el.get('idref'):
                itemrefs.append(el.get('idref'))
        self.itemref_chapter = []      # spine itemref index -> chapter index (or None)
        self.chapters = []             # zip member path per chapter
        for idref in itemrefs:
            item = manifest.get(idref)
            if item is None or 'html' not in item[1]:
                self.itemref_chapter.append(None)
                continue
            self.itemref_chapter.append(len(self.chapters))
            member = posixpath.normpath(posixpath.join(self.opf_dir, unquote(item[0].split('#')[0])))
            self.chapters.append(member)
        self._texts = {}

    def _opf_path(self):
        try:
            container = ElementTree.fromstring(self.zf.read('META-INF/container.xml'))
            for el in container.iter():
                if el.tag.rsplit('}', 1)[-1] == 'rootfile' and el.get('full-path'):
                    return el.get('full-path')
        except (KeyError, ElementTree.ParseError):
            pass
        for n in self.zf.namelist():
            if n.lower().endswith('.opf'):
                return n
        raise ValueError('no OPF in ' + self.path)

    def close(self):
        self.zf.close()

    def text(self, chapter):
        if chapter not in self._texts:
            member = self.names.get(self.chapters[chapter].lower(), self.chapters[chapter])
            try:
                raw = self.zf.read(member)
            except KeyError:
                raw = b''
            self._texts[chapter] = ChapterText(raw.decode('utf-8', 'replace'))
        return self._texts[chapter]

    def chapter_for_source(self, source):
        '''Kobo's Location.Source (a path inside the book, from its root) ->
        chapter index, or None.'''
        if not source:
            return None
        cands = [source]
        if '!' in source:
            cands += [source.split('!')[-1], source.replace('!', '/')]
        lowered = [c.lower() for c in self.chapters]
        for c in cands:
            c = posixpath.normpath(unquote(c.split('#')[0]).lstrip('/')).lower()
            if c in lowered:
                return lowered.index(c)
            # Relative to the OPF, or a path with a different prefix: unique tail match.
            tails = [i for i, p in enumerate(lowered) if p.endswith('/' + c) or c.endswith('/' + p)]
            if len(tails) == 1:
                return tails[0]
            base = posixpath.basename(c)
            bases = [i for i, p in enumerate(lowered) if posixpath.basename(p) == base]
            if len(bases) == 1:
                return bases[0]
        return None

    def lengths(self):
        return [self.text(i).length for i in range(len(self.chapters))]


# -- EPUB CFI -----------------------------------------------------------------

_CFI_RE = re.compile(r'^\s*epubcfi\((.*)\)\s*$', re.S)


def _cfi_steps(path):
    '''"/4[body]/2/10:5" -> ([4, 2, 10], 5). Assertions in [] are dropped.'''
    path = re.sub(r'\[[^\]]*\]', '', path)
    offset = None
    m = re.search(r':(\d+)', path)
    if m:
        offset = int(m.group(1))
        path = path[:m.start()]
    path = re.sub(r'~[\d.]+|@[\d.:]+', '', path)
    steps = [int(s) for s in path.split('/') if s.strip().isdigit()]
    return steps, offset


def parse_cfi(value):
    '''-> (spine itemref index, [content steps], char offset) or None.'''
    m = _CFI_RE.match(value or '')
    if not m:
        return None
    body = m.group(1)
    if ',' in body:                      # a range: parent path + its start
        parts = body.split(',')
        body = parts[0] + parts[1]
    if '!' not in body:
        return None
    pkg, content = body.split('!', 1)
    pkg_steps, _ = _cfi_steps(pkg)
    if len(pkg_steps) < 2 or pkg_steps[1] % 2:
        return None
    steps, offset = _cfi_steps(content)
    return pkg_steps[1] // 2 - 1, steps, offset


def resolve_cfi_steps(ct, steps, offset):
    '''Content steps (from the root element) -> text offset in `ct`, or None.'''
    node = ct.html_element()
    target_start = None
    for k, step in enumerate(steps):
        if not isinstance(node, _Element):
            return None
        elements = [c for c in node.children if isinstance(c, _Element)]
        if step % 2 == 0:
            idx = step // 2 - 1
            if idx < 0 or idx >= len(elements):
                return None
            node = elements[idx]
            target_start = node.start
        else:
            # Odd: the text between element (step-1)/2 and the next one.
            before = (step - 1) // 2
            if before == 0:
                prev_end = None
            else:
                if before > len(elements):
                    return None
                prev_end = elements[before - 1]
            texts = []
            seen_prev = prev_end is None
            for c in node.children:
                if c is prev_end:
                    seen_prev = True
                    continue
                if not seen_prev:
                    continue
                if isinstance(c, _Element):
                    break
                texts.append(c)
            if texts:
                target_start = texts[0].start
            else:
                target_start = prev_end_end(prev_end, ct) if prev_end is not None else node.start
            if k != len(steps) - 1:
                return None
            node = None
    if target_start is None:
        return None
    return min(ct.length, target_start + (offset or 0))


def prev_end_end(el, ct):
    '''Text offset just after element `el` (its subtree's end).'''
    end = el.start
    stack = [el]
    while stack:
        n = stack.pop()
        for c in n.children:
            if isinstance(c, _Text):
                end = max(end, c.start + u16len(c.data))
            else:
                stack.append(c)
    return end


# -- KoboSpan via the KEPUB ----------------------------------------------------

def _norm_index(s):
    '''Whitespace-collapsed copy of `s` plus, per kept char, its UTF-16 offset in `s`.'''
    out, idx = [], []
    pos = 0
    prev_space = True
    for ch in s:
        w = u16len(ch)
        if ch.isspace():
            if not prev_space:
                out.append(' ')
                idx.append(pos)
            prev_space = True
        else:
            out.append(ch)
            idx.append(pos)
            prev_space = False
        pos += w
    return ''.join(out), idx


def locate_text(ct, needle, near):
    '''Offset in `ct` of the occurrence of `needle` (whitespace-insensitive) whose
    start is nearest `near`; None if absent.'''
    hay, idx = _norm_index(ct.text)
    n = ' '.join(needle.split())
    if not n:
        return None
    best = None
    start = hay.find(n)
    while start >= 0:
        off = idx[start]
        if best is None or abs(off - near) < abs(best - near):
            best = off
        start = hay.find(n, start + 1)
    return best


def resolve_span(epub, kepub, chapter, span_id):
    '''-> (offset, precision) or None. `kepub` is a Book for the KEPUB (or None).'''
    if kepub is None:
        return None
    src = epub.chapters[chapter]
    try:
        kch = [p.lower() for p in kepub.chapters].index(src.lower())
    except ValueError:
        kch = kepub.chapter_for_source(src)
    if kch is None:
        return None
    kct = kepub.text(kch)
    if span_id not in kepub.text(kch).ids:
        return None
    koff = kct.ids[span_id]
    ect = epub.text(chapter)
    scale = (ect.length / kct.length) if kct.length else 1.0
    near = int(round(koff * scale))
    # Locate the span's sentence in the EPUB text; a short span ("“") is
    # widened with the spans after it so the match is unique enough.
    probe = kct.id_text.get(span_id, '')
    if len(' '.join(probe.split())) < 24:
        probe = kct.text[_char_index(kct.text, koff):][:80]
    probe = ' '.join(probe.split())[:80]
    found = locate_text(ect, probe, near) if probe else None
    if found is not None:
        return found, SPAN
    return min(near, ect.length), SPAN_APPROX


def _char_index(s, u16off):
    '''UTF-16 offset -> Python str index.'''
    pos = 0
    for i, ch in enumerate(s):
        if pos >= u16off:
            return i
        pos += u16len(ch)
    return len(s)


# -- the mapping ------------------------------------------------------------------

def _pct(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v != v or v < 0:
        return None
    return min(v, 100.0)


def map_bookmark(epub, bookmark, kepub=None):
    '''A Kobo bookmark -> {'chapter', 'textAnchor', 'progress', 'precision'} or None.

    `epub`: a Book (the format CalibReader opens). `kepub`: a Book for the
    KEPUB Calibre-Web sent the Kobo, or None. `bookmark`: a dict with
    location_type, location_value, location_source, progress_percent,
    content_source_progress_percent (app.db column names).'''
    ltype = (bookmark.get('location_type') or '').strip()
    value = (bookmark.get('location_value') or '').strip()
    source = (bookmark.get('location_source') or '').strip()
    book_pct = _pct(bookmark.get('progress_percent'))
    chap_pct = _pct(bookmark.get('content_source_progress_percent'))
    n = len(epub.chapters)
    if n == 0:
        return None

    chapter = epub.chapter_for_source(source)
    if source and chapter is None and (kepub is None or kepub.chapter_for_source(source) is None):
        # M4: a Source that is not in this book means the wrong book (another
        # library, a reused calibre id): no place at all, never one by book %.
        return None
    anchor = None
    precision = None

    if ltype.lower() == 'epubcfi' or value.lower().startswith('epubcfi('):
        parsed = parse_cfi(value)
        if parsed is not None:
            itemref, steps, offset = parsed
            cfi_chapter = epub.itemref_chapter[itemref] if 0 <= itemref < len(epub.itemref_chapter) else None
            if chapter is None:
                chapter = cfi_chapter
            if chapter is not None and cfi_chapter in (None, chapter):
                off = resolve_cfi_steps(epub.text(chapter), steps, offset)
                if off is not None:
                    anchor, precision = off, CFI

    if anchor is None and chapter is not None and ltype.lower() == 'kobospan' and value:
        hit = resolve_span(epub, kepub, chapter, value)
        if hit is not None:
            anchor, precision = hit

    if anchor is None and chapter is not None and chap_pct is not None:
        ct = epub.text(chapter)
        anchor = ct.word_start(int(ct.length * chap_pct / 100.0)) if chap_pct < 100 else ct.length
        precision = CHAPTER_PERCENT

    if anchor is None and chapter is None and book_pct is not None:
        lengths = epub.lengths()
        total = sum(lengths)
        if total > 0:
            target = total * book_pct / 100.0
            acc = 0
            chapter = n - 1
            for i, L in enumerate(lengths):
                if target < acc + L or i == n - 1:
                    chapter = i
                    break
                acc += L
            ct = epub.text(chapter)
            within = max(0, int(target - acc))
            anchor = ct.word_start(within) if within < ct.length else ct.length
            precision = BOOK_PERCENT

    if chapter is None:
        return None
    if anchor is None:
        anchor, precision = 0, CHAPTER_START

    if book_pct is not None:
        progress = book_pct / 100.0
    else:
        lengths = epub.lengths()
        total = sum(lengths) or 1
        progress = (sum(lengths[:chapter]) + anchor) / float(total)
    return {
        'chapter': chapter,
        'textAnchor': int(anchor),
        'progress': round(max(0.0, min(1.0, progress)), 6),
        'precision': precision,
    }

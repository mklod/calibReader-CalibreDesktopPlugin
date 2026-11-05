# Last modified: 2026-11-05--1000
#
# kobo_ku.py — Kobo reading places stored by the Kobo Utilities calibre plugin (USB sync)
# into CalibReader_positions.json (plugin 1.7.0, tester preview). Kobo -> CalibReader only.
#
# Kobo Utilities ("KU", janlarres/kobo-utilities) copies, when the user runs "Store current
# reading position" (or its auto-store on connect), the Kobo's own ChapterIDBookmarked into a
# calibre column the user picked (default #kobo_reading_location), plus ___PercentRead and
# DateLastRead into a percent and a last-read column. This module:
#   1. reads those columns READ-ONLY (calibre's db API inside calibre; metadata.db opened with
#      sqlite mode=ro for the command line), never the Kobo or KoboReader.sqlite;
#   2. parses the stored value (parse_location: a table, FRAGMENT_RULES, so a new sample shape
#      from a tester is a one-line addition): device prefix, URL-encoding, chapter href, and a
#      KoboSpan / EPUB CFI / unknown fragment;
#   3. maps it to CalibReader's place (map_ku) with kobo_position's own span resolution, on the
#      KEPUB calibre's Kobo driver sends for that book: the book's stored KEPUB when the driver
#      sends that (its default format order), else calibre's own kepubify of the EPUB with the
#      driver's options, regenerated in-process and cached (KepubCache: 200 MB, LRU);
#   4. merges the places into CalibReader_positions.json with step 1's newer-wins rule and
#      writer (kobo_import), deviceId "kobo-utilities", the precision in kobo.precision
#      (span, span~, cfi, chapter, percent): the app never treats a chapter- or percent-precise
#      place in the same chapter as "behind" (DESIGN.md step 4, app change).
# Nothing is written to metadata.db, ever. The only output is the positions file.
#
# Stamps (KOBO-UTILITIES.md 8.5): the last-read column (the Kobo's DateLastRead, device clock)
# when the user picked one; otherwise the time this plugin first saw the value (a per-book hash
# of location + percent + date in the plugin's memory file keeps re-runs idempotent).
#
# Design and rulings: docs/handoffs/104-kobo-sync/KOBO-UTILITIES.md section 8 and DESIGN.md
# "Step 4". Pure standard library apart from calibre's kepubify, which is used when present.
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

from __future__ import annotations

__license__   = 'GPL v3'
__copyright__ = '2026, CalibReader'

import datetime as _dt
import hashlib
import html as _html
import json
import os
import re
import sqlite3
import threading
import time
from urllib.parse import unquote

try:
    from calibre_plugins.calibreader_sync.kobo_position import CFI as _POS_CFI, Book, _pct, map_bookmark, resolve_span
    from calibre_plugins.calibreader_sync.kobo_import import (
        KOBO_DEVICE_ID as CW_DEVICE_ID, POSITIONS_FILE, _connect_ro, _is_newer, _load_positions, _now_iso,
        _parse_iso, _write_atomic, kobo_stamp,
    )
    from calibre_plugins.calibreader_sync import kobo_service
except ImportError:  # pragma: no cover - standalone / test import
    from kobo_position import CFI as _POS_CFI, Book, _pct, map_bookmark, resolve_span
    from kobo_import import (
        KOBO_DEVICE_ID as CW_DEVICE_ID, POSITIONS_FILE, _connect_ro, _is_newer, _load_positions, _now_iso,
        _parse_iso, _write_atomic, kobo_stamp,
    )
    import kobo_service

KU_DEVICE_ID = 'kobo-utilities'
SEPARATOR = '|@ @|'                        # KU's BOOKMARK_SEPARATOR (old-firmware EPUB: chapter + adobe_location)

# Precision labels written into the entry's kobo.precision (KOBO-UTILITIES.md 8.3).
SPAN = 'span'               # the Kobo's KoboSpan, resolved through the KEPUB calibre sends: the sentence
SPAN_APPROX = 'span~'       # the span was found, its text not in the EPUB: the KEPUB offset scaled
CFI = 'cfi'                 # an EPUB CFI resolved in the chapter
CHAPTER = 'chapter'         # the chapter's start: no usable place inside it
PERCENT = 'percent'         # the Kobo's book-wide % only

DISTRUST_POINTS = 25.0      # M5: the mapped place vs the Kobo's own %, in % points
MAX_BOOKS = 25              # L1: books mapped per run inside calibre (the rest next run)
REGEN_BUDGET = 3.0          # seconds of KEPUB regeneration started per run inside calibre
CACHE_CAP = 200 * 1024 * 1024   # ruling 6: the kepub cache, in bytes
STALE_DAYS = 14             # 8.6: "Kobo Utilities has not stored positions since …"
FUTURE_SLACK = _dt.timedelta(minutes=10)   # M2: a Kobo date this far ahead of us is a wrong clock
DUMMY_TITLE_PAGE = 'kobo-title-page-generated-by-calibre'   # calibre's kepubify adds it before a coverless book

LOCATION_TYPES = ('text', 'comments')
PERCENT_TYPES = ('int', 'float')
DATE_TYPES = ('datetime',)
_LOOKUP_RE = re.compile(r'^#[a-z][a-z0-9_]*$')

# 8.3, the fragment after the chapter href: (name, regex on the URL-decoded fragment, form).
# group(1) is the value. A new shape from a tester's sample is ONE row here (tests/test_kobo_ku.py
# has the table of stored values). Anything else after '#' is kept and read as the chapter.
FRAGMENT_RULES = [
    ('KoboSpan', re.compile(r'^(?:span#)?(kobo\\?\.\d+\\?\.\d+)$', re.I), 'span'),
    ('EPUB CFI', re.compile(r'^(epubcfi\(.+\))$', re.S), 'cfi'),
]
_ADOBE_CFI = re.compile(r'^(epubcfi\(.+\))$', re.S)         # old firmware: adobe_location used only as a CFI
_CHAPTER_FILE = re.compile(r'\.(?:x?html?|xml)$', re.I)     # an href with no fragment must name a chapter file
_DEVICE_BOOK = re.compile(r'\.(?:kepub\.)?epub!+', re.I)    # file:///mnt/onboard/….kepub.epub!  or  …!!
_TAG = re.compile(r'<[^>]+>')


# -- 8.3: parsing ----------------------------------------------------------------------------

def _strip_html(value):
    '''A comments column edited by hand in calibre wraps its value in HTML.'''
    if '<' in value and '>' in value:
        value = _html.unescape(_TAG.sub(' ', value))
    return value.strip()


def parse_location(value):
    '''A stored KU location -> {form, href, hrefs, span, cfi, fragment, adobe, shape, rule, raw}
    or None when it is not a location. form: 'span' | 'cfi' | 'chapter'.'''
    if not isinstance(value, str):
        return None
    v = _strip_html(value)
    if not v:
        return None
    adobe = None
    if SEPARATOR in v:
        v, adobe = v.split(SEPARATOR, 1)
        v, adobe = v.strip(), adobe.strip() or None
    shape = 'bare'
    m = _DEVICE_BOOK.search(v)
    if m and (v.lower().startswith('file:') or v.startswith('/') or m.start() > 0):
        # the Kobo's ContentID of the chapter: <book file on the device>!<chapter> (or !!)
        v, shape = v[m.end():], 'device'
    elif '!' in v.split('#', 1)[0] and (v.lower().startswith('file:') or v.startswith('/mnt/')):
        v, shape = v.split('!', 1)[1].lstrip('!'), 'device'
    head, sep, frag = v.partition('#')
    candidates = [head.replace('!', '/')]
    if '!' in head:
        candidates.append(head.rsplit('!', 1)[-1])          # the design's "split on the last !"
    hrefs = []
    for c in candidates:
        c = unquote(c).strip()
        if c and c not in hrefs:
            hrefs.append(c)
    if not hrefs:
        return None
    out = {'form': 'chapter', 'href': hrefs[0], 'hrefs': hrefs, 'span': None, 'cfi': None,
           'fragment': unquote(frag).strip() if sep else '', 'adobe': adobe, 'shape': shape,
           'rule': 'chapter', 'raw': value}
    if out['fragment']:
        out['rule'] = 'unknown fragment'
        for name, rx, kind in FRAGMENT_RULES:
            hit = rx.match(out['fragment'])
            if not hit:
                continue
            out['rule'] = name
            if kind == 'span':
                sid = hit.group(1).replace('\\', '')
                out['span'], out['form'] = 'kobo.' + sid.split('.', 1)[1], 'span'
            elif kind == 'cfi':
                out['cfi'], out['form'] = hit.group(1), 'cfi'
            else:
                out['form'] = kind
            break
    elif adobe:
        hit = _ADOBE_CFI.match(adobe)
        if hit:
            out['cfi'], out['form'], out['rule'] = hit.group(1), 'cfi', 'adobe CFI'
        else:
            out['rule'] = 'adobe location'
    if out['form'] == 'chapter' and not _CHAPTER_FILE.search(out['href']):
        return None                                          # "Fantasy", "42", a URL: not a location
    return out


def _short(value, n=60):
    s = ' '.join((value or '').split()) if isinstance(value, str) else str(value)
    return s if len(s) <= n else s[:n - 3] + '...'


def describe(value):
    '''The Settings preview line: "text/titlepage.xhtml#kobo.1.1 -> span".'''
    p = parse_location(value)
    return '{} -> {}'.format(_short(value), p['form'] if p else 'not a location')


def column_check(values):
    '''8.2: {'ok', 'parsed', 'nonempty', 'message'} for a candidate location column's values.
    Fewer than half of the non-empty values parsing = refused.'''
    nonempty = [v for v in values if isinstance(v, str) and v.strip()]
    parsed = sum(1 for v in nonempty if parse_location(v) is not None)
    if not nonempty:
        return {'ok': True, 'parsed': 0, 'nonempty': 0,
                'message': 'This column is empty so far: Kobo Utilities fills it when it stores reading positions.'}
    ok = parsed * 2 >= len(nonempty)
    if ok:
        message = '{} of {} values read as Kobo locations.'.format(parsed, len(nonempty))
    else:
        message = ('Only {} of {} values read as Kobo locations: this does not look like a Kobo Utilities '
                   'location column.'.format(parsed, len(nonempty)))
    return {'ok': ok, 'parsed': parsed, 'nonempty': len(nonempty), 'message': message}


# -- 8.3: mapping ---------------------------------------------------------------------------------

def _for_lookup(href):
    # Book.chapter_for_source URL-decodes and cuts at '#': keep a literal '#' or '%' in the href.
    return href.replace('%', '%25').replace('#', '%23')


def map_ku(epub, parsed, percent=None, kepub=None):
    '''A parsed KU location (+ the Kobo's book %) -> ({chapter, textAnchor, progress, precision
    [, note]}, None) or (None, why). `kepub`: a callable -> (Book of the KEPUB calibre sends for
    this book, None) or (None, why); called only for a span.'''
    pct = _pct(percent)
    n = len(epub.chapters)
    if n == 0:
        return None, 'the EPUB has no chapters'
    if parsed is None:
        if pct is None or pct <= 0 or pct >= 100:
            return None, 'no location, and the Kobo\'s {} is a status, not a reading place'.format(
                'missing %' if pct is None else '{:.0f} %'.format(pct))
        place = map_bookmark(epub, {'progress_percent': pct})
        if place is None:
            return None, 'the book has no text'
        place['precision'] = PERCENT
        return place, None
    chapter = None
    for href in parsed['hrefs']:
        chapter = epub.chapter_for_source(_for_lookup(href))
        if chapter is not None:
            break
    if chapter is None:
        if any(DUMMY_TITLE_PAGE in h for h in parsed['hrefs']):
            return None, 'calibre\'s generated title page (the cover), not a reading place'
        return None, 'the Kobo\'s chapter {} is not in this book (another edition, or the wrong book?)'.format(
            _short(parsed['href']))
    lengths = epub.lengths()
    total = float(sum(lengths)) or 1.0

    def pct_at(ch, anchor):
        return 100.0 * (sum(lengths[:ch]) + anchor) / total

    anchor = precision = note = None
    if parsed['form'] == 'span':
        book, why = kepub() if kepub is not None else (None, 'calibre\'s KEPUB converter is not available here')
        if book is None:
            note = 'span not mapped: {}'.format(why or 'no KEPUB')
        else:
            hit = resolve_span(epub, book, chapter, parsed['span'])
            if hit is None:
                return None, ('span {} is not in the KEPUB calibre sends for this book (the book changed since it '
                              'was sent, or another tool made the Kobo\'s copy)'.format(parsed['span']))
            anchor, precision = hit
            if pct is not None and abs(pct_at(chapter, anchor) - pct) > DISTRUST_POINTS:
                note = 'span not trusted: it is {:.0f} % into the book, the Kobo says {:.0f} %'.format(
                    pct_at(chapter, anchor), pct)
                anchor = precision = None
    elif parsed['form'] == 'cfi':
        place = map_bookmark(epub, {'location_type': 'EpubCfi', 'location_value': parsed['cfi'],
                                    'location_source': epub.chapters[chapter]})
        if place is not None and place['precision'] == _POS_CFI and place['chapter'] == chapter:
            anchor, precision = place['textAnchor'], CFI
        else:
            note = 'CFI not resolved in this chapter'
    if anchor is None:
        anchor, precision = 0, CHAPTER
        if pct is not None:
            lo, hi = pct_at(chapter, 0), pct_at(chapter, lengths[chapter])
            if pct < lo - DISTRUST_POINTS or pct > hi + DISTRUST_POINTS:
                return None, ('the Kobo\'s chapter ({:.0f}-{:.0f} % of the book) does not fit its {:.0f} %'
                              .format(lo, hi, pct))
    progress = pct / 100.0 if pct is not None else pct_at(chapter, anchor) / 100.0
    place = {'chapter': chapter, 'textAnchor': int(anchor), 'progress': round(max(0.0, min(1.0, progress)), 6),
             'precision': precision}
    if note:
        place['note'] = note
    return place, None


# -- which KEPUB the Kobo has ------------------------------------------------------------------

def driver_plan(book_formats, format_map, kepubify):
    '''What calibre's Kobo driver (KOBOTOUCH) sends for a book with these formats: 'kepub' (the
    stored KEPUB as it is), 'kepubify' (the EPUB, through calibre's kepubify), 'epub' (a plain
    EPUB: the driver's KEPUB conversion is off) or None (neither).'''
    have = {str(f).upper() for f in (book_formats or ())}
    for f in format_map or ():
        f = str(f).upper()
        if f not in have:
            continue
        if f == 'KEPUB':
            return 'kepub'
        if f == 'EPUB':
            return 'kepubify' if kepubify else 'epub'
        return None
    return None


def driver_settings():
    '''(format order, kepubify on) of calibre's Kobo driver; calibre's defaults outside calibre.'''
    try:
        from calibre.devices.kobo.driver import KOBOTOUCH
        opts = KOBOTOUCH.settings()
        return [str(f) for f in (opts.format_map or [])] or ['kepub', 'epub'], bool(opts.kepubify)
    except Exception:
        return ['kepub', 'epub', 'pdf', 'txt', 'cbz', 'cbr'], True


def calibre_version():
    try:
        from calibre.constants import __version__
        return str(__version__)
    except Exception:
        return None


def calibre_options():
    '''The options calibre's Kobo driver passes to kepubify that change the text and therefore
    the span numbering: only "prefer justification" (calibre's KEPUB output defaults, which the
    driver reads; measured: 5 of 6 Metamorphosis chapters number differently with it on).
    The driver's other options (extra CSS, hyphenation, @page, widows/orphans) are CSS only.'''
    try:
        from calibre.ebooks.conversion.config import load_defaults
        return {'prefer_justification': bool(load_defaults('kepub_output').get('kepub_prefer_justification', False))}
    except Exception:
        return {'prefer_justification': False}


def calibre_kepubify(options=None):
    '''-> regenerate(src, out) running calibre's own kepubify in-process, or None outside calibre.'''
    try:
        from calibre.ebooks.oeb.polish.kepubify import kepubify_path
    except Exception:
        return None
    try:
        from calibre.ebooks.oeb.polish.kepubify import make_options
    except Exception:                       # pragma: no cover - every calibre 8.0-9.14 has it
        make_options = None
    justify = bool((options or {}).get('prefer_justification'))

    def regenerate(src, out):
        if make_options is None:            # pragma: no cover
            kepubify_path(src, outpath=out, allow_overwrite=True)
            return
        kepubify_path(src, outpath=out, allow_overwrite=True, opts=make_options(prefer_justification=justify))
    return regenerate


def cache_folder():
    '''calibre's cache folder (safe to delete; calibre's own caches live there) / CalibReaderSync / kepub.'''
    try:
        from calibre.constants import cache_dir
        base = cache_dir()
    except Exception:
        base = os.path.join(os.path.expanduser('~'), '.cache', 'calibre')
    return os.path.join(base, 'CalibReaderSync', 'kepub')


class KepubCache(object):
    '''Ruling 6: calibre's kepubify of a book's EPUB, cached in calibre's cache folder; 200 MB
    in all, least recently used out first; keyed by book + EPUB mtime/size + calibre version +
    the driver options that change spans. Deleting the folder at any time is safe.'''

    def __init__(self, folder=None, regenerate=None, cap=CACHE_CAP, version=None, options=None):
        self.folder = folder or cache_folder()
        self.options = dict(options if options is not None else calibre_options())
        self.regenerate = regenerate if regenerate is not None else calibre_kepubify(self.options)
        self.cap = cap
        self.version = version or calibre_version() or '?'
        self.made = 0

    def key(self, book_id, epub_path):
        st = os.stat(epub_path)
        raw = json.dumps([str(book_id), st.st_mtime_ns, st.st_size, self.version, sorted(self.options.items())])
        return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]

    def path_for(self, book_id, epub_path):
        '''-> (path of the KEPUB, None) or (None, why).'''
        if self.regenerate is None:
            return None, 'calibre\'s KEPUB converter is not available here'
        try:
            key = self.key(book_id, epub_path)
        except OSError as exc:
            return None, 'the EPUB could not be read ({})'.format(exc)
        out = os.path.join(self.folder, key + '.kepub')
        if os.path.isfile(out):
            try:
                os.utime(out, None)             # recently used
                return out, None
            except OSError:
                pass                            # deleted meanwhile: make it again
        try:
            os.makedirs(self.folder, exist_ok=True)
        except OSError as exc:
            return None, 'the KEPUB cache folder could not be made ({})'.format(exc)
        tmp = os.path.join(self.folder, '{}.{}.{}.tmp'.format(key, os.getpid(), threading.get_ident()))
        try:
            self.regenerate(epub_path, tmp)
            os.replace(tmp, out)
        except Exception as exc:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return None, 'calibre could not make its KEPUB of this book ({})'.format(exc)
        self.made += 1
        self.evict(keep=out)
        return out, None

    def evict(self, keep=None):
        try:
            names = os.listdir(self.folder)
        except OSError:
            return
        files = []
        for name in names:
            if not name.endswith('.kepub'):
                continue
            p = os.path.join(self.folder, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
        total = sum(size for _, size, _ in files)
        for _, size, p in sorted(files):
            if total <= self.cap:
                break
            if keep and os.path.normcase(p) == os.path.normcase(keep):
                continue
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass


def make_kepub_for(formats_of, cache, format_map=None, kepubify=None):
    '''-> kepub_for(book_id, epub_path, kepub_path) for import_ku: the KEPUB the Kobo got, by
    what calibre's Kobo driver sends for that book (driver_plan).'''
    if format_map is None or kepubify is None:
        fmap, kon = driver_settings()
        format_map = fmap if format_map is None else format_map
        kepubify = kon if kepubify is None else kepubify

    def kepub_for(book_id, epub_path, kepub_path):
        plan = driver_plan(formats_of(book_id), format_map, kepubify)
        if plan == 'kepub':
            if kepub_path and os.path.isfile(kepub_path):
                return kepub_path, None, {'kepub': 'stored KEPUB'}
            return None, 'the book\'s KEPUB file is missing', {}
        if plan == 'kepubify':
            path, why = cache.path_for(book_id, epub_path)
            return path, why, {'kepub': 'calibre kepubify', 'calibre': cache.version}
        if plan == 'epub':
            return None, 'calibre\'s Kobo driver sends this book as a plain EPUB (its KEPUB conversion is off)', {}
        return None, 'calibre\'s Kobo driver sends this book in another format', {}
    return kepub_for


# -- settings: the sources (8.2, 8.8) ------------------------------------------------------------

def _lookup(value):
    return value if isinstance(value, str) and _LOOKUP_RE.match(value) else ''


def source_from_prefs(entry):
    '''PREFS['kobo_ku'][library] -> the first source with a location column, normalised, or None
    (off). Stored as a list of sources (8.8: one today; boundReader for the multi-reader mode).'''
    if not isinstance(entry, dict):
        return None
    for raw in entry.get('sources') or []:
        if not isinstance(raw, dict):
            continue
        location = _lookup(raw.get('location'))
        if not location:
            continue
        return {'id': raw.get('id') or 'ku', 'label': raw.get('label') or 'Kobo Utilities', 'location': location,
                'percent': _lookup(raw.get('percent')), 'last_read': _lookup(raw.get('last_read')),
                'boundReader': raw.get('boundReader') if isinstance(raw.get('boundReader'), str) else None}
    return None


def prefs_entry(location, percent, last_read):
    '''What Settings stores for the library, or None (off) without a location column.'''
    if not _lookup(location):
        return None
    return {'sources': [{'id': 'ku', 'label': 'Kobo Utilities', 'location': location,
                         'percent': _lookup(percent), 'last_read': _lookup(last_read), 'boundReader': None}]}


def source_signature(source):
    return '|'.join((source.get('location') or '', source.get('percent') or '', source.get('last_read') or ''))


def eligible(custom_columns, types, exclude=()):
    '''[(lookup, heading)] of single-value custom columns of these datatypes, minus `exclude`.'''
    out = []
    for lookup, meta in (custom_columns or {}).items():
        if not isinstance(meta, dict) or meta.get('datatype') not in types or meta.get('is_multiple'):
            continue
        if lookup in exclude:
            continue
        out.append((lookup, meta.get('name') or lookup))
    return sorted(out, key=lambda x: (x[1].lower(), x[0]))


# -- reading the columns (read-only) -------------------------------------------------------------

def _stamp(value):
    '''A last-read value (calibre datetime, or the TEXT metadata.db holds) -> "…Z" or None.'''
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        if value.year < 1900:               # calibre's UNDEFINED_DATE (year 101)
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        t = value.astimezone(_dt.timezone.utc)
        return t.strftime('%Y-%m-%dT%H:%M:%S.') + '{:03d}Z'.format(t.microsecond // 1000)
    if isinstance(value, str):
        s = kobo_stamp(value.strip())
        if s and int(s[:4]) >= 1900:
            return s
    return None


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _values(locations, percents, dates):
    out = {}
    for book_id in set(locations) | set(percents) | set(dates):
        loc = locations.get(book_id)
        loc = loc if isinstance(loc, str) and loc.strip() else None
        pct = _number(percents.get(book_id))
        if loc is None and pct is None:
            continue
        out[book_id] = (loc, pct, _stamp(dates.get(book_id)))
    return out


def column_problem(custom_columns, source):
    '''None, or why the chosen columns cannot be read (gone, or the wrong kind).'''
    for key, types, what in (('location', LOCATION_TYPES, 'text or long text'),
                             ('percent', PERCENT_TYPES, 'number'), ('last_read', DATE_TYPES, 'date')):
        lookup = source.get(key)
        if not lookup:
            continue
        meta = (custom_columns or {}).get(lookup)
        if not isinstance(meta, dict):
            return 'The Kobo Utilities column {} is not in this library any more: pick it again in Settings.'.format(lookup)
        if meta.get('datatype') not in types or meta.get('is_multiple'):
            return 'The Kobo Utilities column {} is not a {} column: pick another in Settings.'.format(lookup, what)
    return None


def read_columns_db(api, source):
    '''Inside calibre: {book_id: (location, percent, last read "…Z")} through the db API
    (calibre's in-memory cache; nothing is written). Raises ValueError for a column problem.'''
    problem = column_problem(api.field_metadata.custom_field_metadata(), source)
    if problem:
        raise ValueError(problem)
    ids = api.all_book_ids()

    def column(key):
        lookup = source.get(key)
        return dict(api.all_field_for(lookup, ids)) if lookup else {}
    return _values(column('location'), column('percent'), column('last_read'))


def read_columns_sqlite(library_path, source):
    '''Outside calibre: the same from metadata.db opened READ-ONLY (sqlite mode=ro).'''
    con = _connect_ro(os.path.join(library_path, 'metadata.db'))
    try:
        cols = {}
        for cid, label, datatype, multiple, normalized in con.execute(
                'SELECT id, label, datatype, is_multiple, normalized FROM custom_columns'):
            cols['#' + label] = {'id': cid, 'datatype': datatype, 'is_multiple': bool(multiple),
                                 'normalized': bool(normalized)}
        problem = column_problem(cols, source)
        if problem:
            raise ValueError(problem)

        def column(key):
            lookup = source.get(key)
            if not lookup:
                return {}
            c = cols[lookup]
            if c['normalized']:
                q = ('SELECT l.book, v.value FROM books_custom_column_{0}_link l '
                     'JOIN custom_column_{0} v ON v.id = l.value'.format(c['id']))
            else:
                q = 'SELECT book, value FROM custom_column_{}'.format(c['id'])
            return {book: value for book, value in con.execute(q)}
        return _values(column('location'), column('percent'), column('last_read'))
    finally:
        con.close()


def fingerprint(values):
    '''One short hash of every chosen column value: the 5-minute poll's change test.'''
    raw = json.dumps(sorted((str(k), list(v)) for k, v in values.items()), default=str)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def row_hash(location, percent, last_read):
    raw = json.dumps([(location or '').strip(), percent, last_read or ''])
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]


# -- the plugin's memory (per library, per column set) --------------------------------------------

class MemoryFile(object):
    '''{library: {'signature': the chosen columns, 'books': {uuid: record}}} in one JSON file in
    calibre's config folder (plugins/calibreader_sync_ku_seen.json). A new column set replaces
    the library's old part (a first run again). Saves re-read the file under a lock and replace
    only their library's part.'''

    def __init__(self, path):
        self.path = path

    def _read(self):
        try:
            with open(self.path, 'rb') as f:
                data = json.loads(f.read().decode('utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _part(self, library, signature):
        part = self._read().get(library)
        if isinstance(part, dict) and part.get('signature') == signature and isinstance(part.get('books'), dict):
            return part
        return None

    def had_part(self, library, signature):
        return self._part(library, signature) is not None

    def load(self, library, signature):
        part = self._part(library, signature)
        return dict(part['books']) if part else {}

    def save(self, library, signature, memory):
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with kobo_service.FileLock(self.path + '.lock'):
            data = self._read()
            data[library] = {'signature': signature, 'books': memory}
            kobo_service._write_atomic(self.path, json.dumps(data, ensure_ascii=False).encode('utf-8'))


def memory_path(config_dir):
    return os.path.join(config_dir, 'plugins', 'calibreader_sync_ku_seen.json')


# -- 8.6: what the user sees ------------------------------------------------------------------------

def last_store_seen(values, memory):
    '''The newest Kobo last-read date in the columns (when Kobo Utilities last stored), else the
    newest time this plugin saw a value change; None when nothing is known.'''
    dates = [v[2] for v in values.values() if v[2]]
    if dates:
        return max(dates, key=lambda s: _parse_iso(s) or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc))
    seen = [r.get('seen') for r in (memory or {}).values() if isinstance(r, dict) and r.get('seen')]
    return max(seen) if seen else None


def store_line(last):
    if not last:
        return 'Last Kobo Utilities store seen: never.'
    return 'Last Kobo Utilities store seen: {} UTC.'.format(last[:16].replace('T', ' '))


def staleness_hint(last_store, device_seen, now=None):
    '''8.6: a hint when Kobo Utilities has not stored for 14 days though a Kobo was connected since.'''
    dev = _parse_iso(device_seen) if device_seen else None
    if dev is None:
        return ''
    now = now or _dt.datetime.now(_dt.timezone.utc)
    last = _parse_iso(last_store) if last_store else None
    if last is not None and ((now - last) < _dt.timedelta(days=STALE_DAYS) or dev <= last):
        return ''
    if last is None:
        return ('A Kobo was connected, but Kobo Utilities has not stored any reading positions yet. In Kobo '
                'Utilities, run "Store current reading position", or switch on its auto-store on connect.')
    return ('Kobo Utilities has not stored positions since {}, though a Kobo was connected since. In Kobo Utilities, '
            'run "Store current reading position", or switch on its auto-store on connect.'.format(last_store[:10]))


# -- the import -----------------------------------------------------------------------------------

class KuImportResult(object):
    def __init__(self):
        self.written = []          # uuids whose entry was added / replaced
        self.skipped = {}          # uuid (or book id) -> why, for books looked at this run
        self.mapped = {}           # uuid -> the entry written
        self.notes = []
        self.more = False          # L1: books left for the next run
        self.memory_changed = False
        self.busy = False          # the library's Kobo lock was taken (ui.py)
        self.last_store = None

    def status_line(self):
        if self.written:
            return 'Kobo (USB): {} reading place{} imported'.format(len(self.written),
                                                                    '' if len(self.written) == 1 else 's')
        return ''


def _finer_calibre_web(current, entry):
    '''Section 5: a Calibre-Web place to the sentence is never replaced by a coarser Kobo
    Utilities place in the same chapter (both are the Kobo's; the finer one is kept).'''
    if not isinstance(current, dict) or current.get('deviceId') != CW_DEVICE_ID:
        return False
    k = current.get('kobo') if isinstance(current.get('kobo'), dict) else {}
    if k.get('precision') not in ('span', 'cfi'):
        return False
    return entry['kobo']['precision'] not in (SPAN, CFI) and current.get('chapter') == entry['chapter']


def _iso(t):
    return t.strftime('%Y-%m-%dT%H:%M:%S.') + '{:03d}Z'.format(t.microsecond // 1000)


def import_ku(library_path, source, values, lookup, memory, kepub_for=None, now=None, log=None,
              max_books=MAX_BOOKS, budget=REGEN_BUDGET, clock=time.monotonic):
    '''One import of the Kobo Utilities columns. `values`: {book_id: (location, percent, last
    read)} (read_columns_db / read_columns_sqlite). `lookup(book_id)` -> (uuid, EPUB path,
    KEPUB path) or None. `memory`: this library's + column set's records {uuid: record}, updated
    in place (res.memory_changed: the caller saves it). `kepub_for(book_id, epub, kepub)` ->
    (KEPUB path, why, info) (make_kepub_for). Maps only books whose values changed (8.6), then
    re-reads the positions file and merges against that copy just before writing (M1).'''
    res = KuImportResult()
    now = now or _dt.datetime.now(_dt.timezone.utc)
    now_s = _iso(now)
    path = os.path.join(library_path, POSITIONS_FILE)
    data, err = _load_positions(path)
    if err:
        res.notes.append(err.replace('Kobo places', 'Kobo Utilities places'))
        return res
    first_run = '.' not in memory
    has_date_column = bool(source.get('last_read'))
    keys = {k.lower(): k for k in data['positions']}
    pending = {}                       # uuid -> record, saved once the run is through
    mapped = {}                        # uuid -> (uuid as in the library, entry, record)
    attempted = future = 0
    start = clock()
    for book_id in sorted(values, key=lambda b: (str(type(b)), b)):
        loc, pct, last_read = values[book_id]
        loc = loc.strip() if isinstance(loc, str) and loc.strip() else None
        pct = _pct(_number(pct))
        last_read = _stamp(last_read)
        if loc is None and pct is None:
            continue
        info = lookup(book_id)
        if not info or not info[0]:
            res.skipped[book_id] = 'not in this calibre library'
            continue
        uuid, epub_path = info[0], info[1]
        kepub_path = info[2] if len(info) > 2 else None
        u = uuid.lower()
        h = row_hash(loc, pct, last_read)
        before = memory.get(u)
        if isinstance(before, dict) and before.get('h') == h:
            continue                                   # 8.6: unchanged since it was last looked at
        record = {'h': h, 'lr': last_read, 'seen': now_s}
        current = data['positions'].get(keys.get(u, uuid))
        if last_read:
            stamp, kind = last_read, 'last-read'
            if _parse_iso(last_read) - now > FUTURE_SLACK:
                future += 1                            # M2: not remembered, looked at again next run
                continue
            if has_date_column and isinstance(before, dict) and before.get('lr') == last_read:
                pending[u] = dict(record, status='same-date')
                res.skipped[uuid] = ('the Kobo Utilities location changed but its last-read date did not: not a new '
                                     'reading place')
                continue
        else:
            stamp, kind = now_s, 'first-seen'
            if first_run and isinstance(current, dict):
                pending[u] = dict(record, status='baseline')
                res.skipped[uuid] = ('in Kobo Utilities\' column before it was picked here, with no date: CalibReader\'s '
                                     'own place kept')
                continue
        if isinstance(current, dict) and not _is_newer(stamp, current.get('updated_at')):
            pending[u] = dict(record, status='kept')
            res.skipped[uuid] = 'CalibReader\'s place is as new or newer'
            continue
        if not epub_path or not os.path.isfile(epub_path):
            pending[u] = dict(record, status='no-epub')
            res.skipped[uuid] = 'no EPUB (CalibReader opens the EPUB)'
            continue
        parsed = parse_location(loc) if loc else None
        if loc and parsed is None:
            pending[u] = dict(record, status='unreadable')
            res.skipped[uuid] = ('the location "{}" is not a shape CalibReader reads yet (please send it to us)'
                                 .format(_short(loc)))
            continue
        if attempted >= max_books:
            res.more = True
            continue
        if parsed is not None and parsed['form'] == 'span' and kepub_for is not None and clock() - start > budget:
            res.more = True                            # no new KEPUB after the run's budget
            continue
        attempted += 1
        epub = None
        opened = []
        kinfo = {}

        def kepub():
            if kepub_for is None:
                return None, 'calibre\'s KEPUB converter is not available here'
            kpath, why, extra = kepub_for(book_id, epub_path, kepub_path)
            kinfo.update(extra or {})
            if not kpath:
                return None, why
            book = Book(kpath)
            opened.append(book)
            return book, None
        try:
            epub = Book(epub_path)
            place, why = map_ku(epub, parsed, pct, kepub=kepub)
        except Exception as exc:
            pending[u] = dict(record, status='failed')
            res.skipped[uuid] = 'could not read the EPUB ({})'.format(exc)
            continue
        finally:
            for b in [epub] + opened:
                if b is not None:
                    b.close()
        if place is None:
            pending[u] = dict(record, status='no-place')
            res.skipped[uuid] = why
            continue
        kobo = {'source': KU_DEVICE_ID, 'location': loc, 'href': parsed['href'] if parsed else None,
                'value': (parsed['span'] or parsed['cfi'] or '') if parsed else '', 'percent': pct,
                'lastRead': last_read, 'stamp': kind, 'precision': place['precision']}
        if parsed and parsed['rule'] == 'unknown fragment':
            kobo['fragment'] = parsed['fragment']
        if place.get('note'):
            kobo['note'] = place['note']
        kobo.update(kinfo)
        entry = {'chapter': place['chapter'], 'page': 0, 'scrollY': 0, 'progress': place['progress'],
                 'textAnchor': place['textAnchor'], 'updated_at': stamp, 'deviceId': KU_DEVICE_ID, 'kobo': kobo}
        mapped[u] = (uuid, entry, record)
    if future:
        res.notes.append('{} Kobo Utilities place(s) have a last-read date more than 10 minutes in the future: the '
                         'Kobo\'s clock looks wrong. Not imported.'.format(future))
    if mapped:
        fresh, err = _load_positions(path)            # M1: merge against the file as it is now
        if err:
            res.notes.append(err.replace('Kobo places', 'Kobo Utilities places'))
            return res
        table = fresh['positions']
        keys = {k.lower(): k for k in table}
        for u, (uuid, entry, record) in mapped.items():
            key = keys.get(u, uuid)
            current = table.get(key)
            if isinstance(current, dict) and not _is_newer(entry['updated_at'], current.get('updated_at')):
                pending[u] = dict(record, status='kept')
                res.skipped[uuid] = 'CalibReader\'s place is as new or newer'
                continue
            if _finer_calibre_web(current, entry):
                pending[u] = dict(record, status='finer')
                res.skipped[uuid] = 'Calibre-Web already has a finer place for this Kobo reading in this chapter'
                continue
            table[key] = entry
            res.written.append(uuid)
            res.mapped[uuid] = entry
            pending[u] = dict(record, status='written')
            if log:
                log('Kobo Utilities place {} -> chapter {} textAnchor {} ({})'.format(
                    uuid, entry['chapter'], entry['textAnchor'], entry['kobo']['precision']))
        if res.written:
            fresh['updated_at'] = _now_iso()
            try:
                _write_atomic(path, fresh)
            except OSError as exc:
                res.notes.append('Could not write {} ({}).'.format(POSITIONS_FILE, exc))
                pending = {u: r for u, r in pending.items() if r.get('status') != 'written'}
                res.written, res.mapped = [], {}
    if first_run:
        memory['.'] = {'since': now_s}
    for u, record in pending.items():
        memory[u] = record
    res.memory_changed = bool(first_run or pending)
    return res


# -- command line (e2e, or a library without calibre open) ------------------------------------------

def main(argv=None):  # pragma: no cover - CLI
    import argparse
    try:
        from calibre_plugins.calibreader_sync.kobo_import import metadata_lookup
    except ImportError:
        from kobo_import import metadata_lookup
    ap = argparse.ArgumentParser(description='Import Kobo reading places stored by Kobo Utilities (calibre columns) '
                                             'into CalibReader_positions.json. metadata.db is only read.')
    ap.add_argument('--library', required=True)
    ap.add_argument('--location', required=True, help='lookup name of the location column, e.g. #kobo_reading_location')
    ap.add_argument('--percent', default='')
    ap.add_argument('--last-read', default='')
    ap.add_argument('--memory', default=memory_path(kobo_service.calibre_config_dir()),
                    help='where the values seen are remembered (default %(default)s)')
    ap.add_argument('--cache', default=None, help='KEPUB cache folder (default: calibre\'s cache folder)')
    a = ap.parse_args(argv)
    source = {'id': 'ku', 'label': 'Kobo Utilities', 'location': a.location, 'percent': a.percent,
              'last_read': a.last_read, 'boundReader': None}
    lock = kobo_service.LibraryLock(a.library, 'the command line')
    if not lock.acquire():
        print('note:', kobo_service.busy_note(lock.holder()))
        return kobo_service.BUSY
    try:
        values = read_columns_sqlite(a.library, source)
        con = _connect_ro(os.path.join(a.library, 'metadata.db'))
        try:
            formats = {}
            for book, fmt in con.execute('SELECT book, format FROM data'):
                formats.setdefault(book, set()).add(fmt.upper())
        finally:
            con.close()
        cache = KepubCache(a.cache)
        kepub_for = make_kepub_for(lambda b: formats.get(b, ()), cache)
        mem = MemoryFile(a.memory)
        sig = source_signature(source)
        memory = mem.load(a.library, sig)
        res = import_ku(a.library, source, values, metadata_lookup(a.library), memory, kepub_for=kepub_for,
                        log=print, max_books=10 ** 6, budget=float('inf'))
        if res.memory_changed:
            mem.save(a.library, sig, memory)
        for n in res.notes:
            print('note:', n)
        for k, why in res.skipped.items():
            print('skipped {}: {}'.format(k, why))
        print('calibre={} kepubs made={} written={}'.format(cache.version, cache.made, res.written))
        for u, e in res.mapped.items():
            print(json.dumps({u: e}, ensure_ascii=False))
        print(store_line(last_store_seen(values, memory)))
        return 0
    finally:
        lock.release()


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())

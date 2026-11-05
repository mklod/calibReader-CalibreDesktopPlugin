# Last modified: 2026-11-05--0556
#
# kobo_import.py — Kobo reading positions (Calibre-Web's app.db) into
# CalibReader_positions.json, the sidecar the CalibReader apps already read.
#
# Calibre-Web's Kobo sync keeps each book's Kobo place in app.db
# (kobo_reading_state per user+book, kobo_bookmark under it). This module:
#   1. opens app.db READ-ONLY (sqlite URI mode=ro; never a write, never a lock
#      held past the read),
#   2. takes the chosen Calibre-Web user's non-empty bookmarks,
#   3. maps each to a CalibReader place with kobo_position.map_bookmark,
#      using the book's EPUB (and KEPUB, when Calibre-Web stored one),
#   4. merges them into CalibReader_positions.json entry by entry with the
#      apps' own rule: an entry is replaced only when the Kobo's stamp is
#      STRICTLY newer than the entry's `updated_at` (an app position read
#      after the Kobo's wins). Every other entry and key is kept as-is. The
#      file is rewritten (temp file + rename) only when something changed.
#
# Nothing is written to calibre's metadata.db or to app.db. The stamp is
# kobo_bookmark.last_modified: Calibre-Web's SERVER clock (UTC) at the moment
# the Kobo's PUT changed the place, not the Kobo's own clock (Calibre-Web drops
# the client's LastModified).
#
# `python3 kobo_import.py --library <folder> --app-db <app.db> [--user <name>]`
# runs one import outside calibre (metadata.db opened read-only to find the
# books); inside calibre ui.py calls import_positions with the db API.
#
# 1.5.0 (step 2, CalibReader -> Kobo, kobo_push.py): `--push` also sends the
# app's places to the Kobo through Calibre-Web's own Kobo API. The import skips
# a Kobo bookmark that is our own push coming back (is_echo: Calibre-Web stamps
# it with server time, so it looks newer than the app's place).
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

from __future__ import annotations

__license__   = 'GPL v3'
__copyright__ = '2026, CalibReader'

import datetime as _dt
import json
import os
import re
import stat
import time as _time
import uuid as _uuid
import sqlite3

try:
    from calibre_plugins.calibreader_sync.kobo_position import SPAN, Book, map_bookmark
except ImportError:  # pragma: no cover - standalone / test import
    from kobo_position import SPAN, Book, map_bookmark

POSITIONS_FILE = 'CalibReader_positions.json'
KOBO_DEVICE_ID = 'kobo-calibre-web'


class KoboImportResult(object):
    def __init__(self):
        self.user = None
        self.written = []          # uuids whose entry was replaced / added
        self.skipped = {}          # uuid or book id -> why
        self.mapped = {}           # uuid -> the entry written (for logs)
        self.notes = []
        self.more = False          # L1: books left for the next run

    def status_line(self):
        if self.written:
            return 'Kobo: {} reading place{} imported'.format(len(self.written), '' if len(self.written) == 1 else 's')
        return ''


def kobo_stamp(raw):
    '''app.db DATETIME ("2026-10-30 21:03:48.356000", naive UTC, as Calibre-Web
    0.6.27 stores it) -> the sidecar's "2026-10-30T21:03:48.356Z"; None if
    unusable. A "+HH:MM" / "Z" suffix (another build or fork) is honoured and
    converted to UTC (M2).'''
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().replace('T', ' ')
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    tz = None
    m = re.search(r'([+-])(\d{2}):?(\d{2})$', s)
    if m and len(s) > 19:
        sign = 1 if m.group(1) == '+' else -1
        tz = _dt.timezone(sign * _dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
        s = s[:m.start()].strip()
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            t = _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if tz is not None:
            t = t.replace(tzinfo=tz).astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return t.strftime('%Y-%m-%dT%H:%M:%S.') + '{:03d}Z'.format(t.microsecond // 1000)
    return None


def _parse_iso(s):
    if not isinstance(s, str) or not s:
        return None
    t = s.strip()
    if t.endswith('Z'):
        t = t[:-1] + '+00:00'
    # fromisoformat takes 0-6 fractional digits only in some versions: trim to 6.
    if '.' in t:
        head, rest = t.split('.', 1)
        frac = ''.join(c for c in rest if c.isdigit())
        tz = rest[len(frac):]
        t = head + '.' + (frac[:6] or '0') + tz
    try:
        v = _dt.datetime.fromisoformat(t)
    except ValueError:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=_dt.timezone.utc)
    return v


def _is_newer(candidate, incumbent):
    '''The apps' rule (CloudSidecarSync.isNewer): an unparseable candidate never
    wins, an unparseable incumbent always loses, else strictly newer.'''
    c = _parse_iso(candidate)
    if c is None:
        return False
    i = _parse_iso(incumbent)
    return i is None or c > i


def _connect_ro(path):
    uri = 'file:{}?mode=ro'.format(path.replace('?', '%3f').replace('#', '%23'))
    return sqlite3.connect(uri, uri=True, timeout=5)


def read_kobo_bookmarks(app_db, user=None):
    '''-> (rows, user name, notes). rows: one dict per book with a usable Kobo
    place for that user (location or a percentage), app.db column names plus
    book_id and the sidecar-format stamp.'''
    notes = []
    if not app_db or not os.path.isfile(app_db):
        return [], None, ['Calibre-Web app.db not found: {}'.format(app_db)]
    try:
        con = _connect_ro(app_db)
    except sqlite3.Error as exc:
        return [], None, ['Could not open Calibre-Web app.db read-only ({}).'.format(exc)]
    try:
        cols = {r[1] for r in con.execute('PRAGMA table_info(user)')}
        users = con.execute('SELECT id, name, {} FROM user'.format('role' if 'role' in cols else '0')).fetchall()
        q = '''SELECT rs.user_id, rs.book_id, b.last_modified, b.location_source, b.location_type,
                      b.location_value, b.progress_percent, b.content_source_progress_percent
               FROM kobo_bookmark b JOIN kobo_reading_state rs ON rs.id = b.kobo_reading_state_id
               WHERE (b.location_value IS NOT NULL AND b.location_value != '')
                  OR (b.location_source IS NOT NULL AND b.location_source != '')
                  OR b.progress_percent IS NOT NULL
                  OR b.content_source_progress_percent IS NOT NULL'''
        raw = con.execute(q).fetchall()
    except sqlite3.Error as exc:
        return [], None, ['This does not look like a Calibre-Web app.db ({}).'.format(exc)]
    finally:
        con.close()
    names = {uid: name for uid, name, _ in users}
    uid, notes = pick_user(users, sorted({r[0] for r in raw}), user)
    if uid is None:
        return [], None, notes
    rows = {}
    for (u, book_id, lm, src, ltype, val, pct, cpct) in raw:
        if u != uid:
            continue
        stamp = kobo_stamp(lm)
        row = {'book_id': book_id, 'updated_at': stamp, 'location_source': src, 'location_type': ltype,
               'location_value': val, 'progress_percent': pct, 'content_source_progress_percent': cpct}
        prev = rows.get(book_id)
        if prev is None or _is_newer(stamp, prev['updated_at']):
            rows[book_id] = row
    return sorted(rows.values(), key=lambda r: r['book_id']), names.get(uid), notes


def pick_user(users, with_rows, user=None):
    """-> (user id or None, notes). `users`: (id, name, role) rows of app.db's
    user table; `with_rows`: ids of the users that have Kobo rows. An explicit
    `user` (Settings) is looked up by name; otherwise the single real person,
    when the rows are theirs. Several people: never picked silently (L4)."""
    names = {uid: name for uid, name, _ in users}
    # Calibre-Web's anonymous account ("Guest", ROLE_ANONYMOUS = 32) is nobody's Kobo.
    people = sorted(uid for uid, name, role in users
                    if not ((role or 0) & 32) and (name or '').lower() != 'guest')
    with_rows = sorted(set(with_rows))
    if user:
        uid = next((i for i, n in names.items() if (n or '').lower() == user.lower()), None)
        if uid is None:
            return None, ['No Calibre-Web user named "{}" in app.db.'.format(user)]
        return uid, []
    if not with_rows:
        return None, []
    if len(people) == 1 and with_rows == people:
        return with_rows[0], []
    # L4: never pick one silently (two people, one Kobo, one library).
    return None, ['Several Calibre-Web users ({}): choose in Settings whose Kobo places CalibReader '
                  'should follow.'.format(', '.join(sorted(names.get(u, str(u)) for u in people)))]


def _load_positions(path):
    '''-> (data, error). Missing file -> a fresh table; a damaged one -> error
    (and it is never overwritten).'''
    if not os.path.exists(path):
        return {'schema': 1, 'positions': {}}, None
    try:
        with open(path, 'rb') as f:
            data = json.loads(f.read().decode('utf-8'))
    except (OSError, ValueError) as exc:
        return None, '{} could not be read ({}); Kobo places not imported.'.format(POSITIONS_FILE, exc)
    if not isinstance(data, dict) or not isinstance(data.get('positions', {}), dict):
        return None, '{} has an unexpected layout; Kobo places not imported.'.format(POSITIONS_FILE)
    data.setdefault('positions', {})
    return data, None


def _write_atomic(path, data):
    """sync_core's writer (H1/L3): a temp file made with open() (the umask's
    mode, never mkstemp's 0600), given the old file's mode when there is one,
    then os.replace, with a rename-aside fallback when the replace fails
    (Windows, a cloud client holding the file)."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        mode = None
    tmp = '{}.crtmp.{}'.format(path, _uuid.uuid4().hex)
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        try:
            os.replace(tmp, path)
        except OSError:
            bak = '{}.crbak.{}.{}-1'.format(path, int(_time.time()), _uuid.uuid4().hex)
            os.rename(path, bak)
            os.rename(tmp, path)
            os.remove(bak)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def _now_iso():
    t = _dt.datetime.now(_dt.timezone.utc)
    return t.strftime('%Y-%m-%dT%H:%M:%S.') + '{:03d}Z'.format(t.microsecond // 1000)


def _library_uuid(folder):
    try:
        con = _connect_ro(os.path.join(folder, 'metadata.db'))
        try:
            row = con.execute('SELECT uuid FROM library_id').fetchone()
        finally:
            con.close()
        return row[0].lower() if row and row[0] else None
    except sqlite3.Error:
        return None


def served_library(app_db, library_path):
    """-> (state, folder, note). Calibre-Web's app.db book ids are calibre ids of
    the library IT serves (settings.config_calibre_dir). state:
      'same'    that folder is this library (same path, or a visible folder
                whose metadata.db has the same library_id);
      'other'   a visible folder with a different library_id (note says so);
      'unknown' not visible here (Docker's /books, another computer) or no
                library_id to compare."""
    try:
        con = _connect_ro(app_db)
        try:
            row = con.execute('SELECT config_calibre_dir FROM settings').fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return 'unknown', None, None
    served = row[0] if row else None
    if served and os.path.realpath(served) == os.path.realpath(library_path):
        return 'same', served, None
    if not served or not os.path.isfile(os.path.join(served, 'metadata.db')):
        return 'unknown', served, None
    theirs, ours = _library_uuid(served), _library_uuid(library_path)
    if theirs and ours and theirs != ours:
        return 'other', served, ('This Calibre-Web serves another calibre library ({}); Kobo places not imported. '
                                 'Open that library in calibre, or point Settings at the right app.db.'.format(served))
    if theirs and ours:
        return 'same', served, None
    return 'unknown', served, None


def other_library(app_db, library_path):
    """When Calibre-Web's library folder is visible here and holds a metadata.db
    with a different library_id, the ids belong to another library: a note (and
    nothing is imported). A path not visible here (Docker's /books) or the same
    library under another mount is accepted."""
    state, _, note = served_library(app_db, library_path)
    return note if state == 'other' else None


# N1: books whose Kobo place failed to map (or was refused) in this calibre
# session, by (library, app.db, uuid) -> the Kobo stamp that failed. They are
# not parsed again until the Kobo moves (a new stamp), so a failing set never
# re-parses EPUBs on calibre's GUI thread run after run.
_FAILED = {}
# Step 2 (S1): Kobo bookmarks recognised as our own push in the Kobo's terms, by
# the same key -> the Kobo stamp: not mapped again until the Kobo moves.
_ECHOED = {}


def forget_failures():
    _FAILED.clear()
    _ECHOED.clear()


def _failure_key(library_path, app_db, uuid):
    return (os.path.realpath(library_path), os.path.realpath(app_db), uuid.lower())


FUTURE_SLACK = _dt.timedelta(minutes=10)   # M2
MAX_BOOKS = 25                             # L1: books mapped per run (the rest next run)
DISTRUST_POINTS = 25.0                     # M5: location vs ProgressPercent, in % points


ECHO_POINTS = 1.0                          # step 2: % tolerance of a no-KEPUB echo
ECHO_WINDOW = 2000                         # step 2 (S1): characters, about a Kobo page and a half
ECHO_PERCENT_WINDOW = 2.0                  # step 2 (S1): book-% points BEHIND what we sent, after a %-only push
ECHO_PERCENT_AHEAD = 0.5                   # re-review R1-S1: and only rounding ahead of it


def record_is_current(record, current, app_too=True):
    """Re-review R1-S1, corrected in R2-S1: an echo record speaks for our push until
    something has moved on SINCE it: the sidecar holds an entry stamped after the
    push (`pushed_at`): the app read on (app_too), or a real Kobo move was imported.
    An app place stamped before the push but uploaded after it (the app's ~30 s
    upload debounce) has not moved on: the record still holds."""
    if not isinstance(record, dict):
        return False
    if not isinstance(current, dict):
        return True
    if not app_too and current.get('deviceId') != KOBO_DEVICE_ID:
        return True
    return not _is_newer(current.get('updated_at'), record.get('pushed_at'))


def _percent_matches(got, sent, behind):
    try:
        diff = float(got) - float(sent)
    except (TypeError, ValueError):
        return False
    return -behind <= diff <= ECHO_PERCENT_AHEAD


def echo_by_place(place, row, record, current):
    """Review round 1 (S1), narrowed in re-review R1-S1: the Kobo's report OF OUR
    PUSH in its own terms (the first span of its page, its own spelling of Source,
    its own measure after a %-only push) is our echo, not a Kobo move: no "go
    there?" card and no silent jump. Only while the Kobo's place still matches
    what we sent:
      * a span push: in the chapter we sent, from ECHO_WINDOW characters before
        where our span lands up to it (a later span is a real move);
      * a push the Kobo got as % only: the Kobo's ProgressPercent from
        ECHO_PERCENT_WINDOW points below what we sent up to ECHO_PERCENT_AHEAD
        above it (further on is a real move);
    and only while nothing has moved on since the push (record_is_current)."""
    if place is None or not record_is_current(record, current):
        return False
    if record.get('precision') == SPAN:
        try:
            chapter, landed = int(record['chapter']), int(record['textAnchor'])
        except (KeyError, TypeError, ValueError):
            return False
        return place['chapter'] == chapter and landed - ECHO_WINDOW <= place['textAnchor'] <= landed
    return _percent_matches(row.get('progress_percent'), record.get('progressPercent'), ECHO_PERCENT_WINDOW)


_ANY = object()


def is_echo(row, record, current=_ANY):
    """Step 2's echo guard: is this Kobo bookmark (app.db row) our own push
    (`record`, kept per book by kobo_push) coming back? A KoboSpan push: the
    same Type, Value and Source (the span is the place; a Kobo may send its own
    % with it). A push without a KEPUB (Value empty): the same Location AND both
    % within ECHO_POINTS below and ECHO_PERCENT_AHEAD above what we sent,
    because Calibre-Web keeps our Location when a later Kobo PUT has none, and a
    Kobo that moved on by % only must still be imported. With `current` (the
    sidecar's entry for the book): not once a real Kobo move newer than the push
    has been imported (R2-S1: an app entry never retires the exact echo; one
    written after the push is newer than the Kobo row and never reaches it)."""
    if not isinstance(record, dict):
        return False
    if current is not _ANY and not record_is_current(record, current, app_too=False):
        return False
    if ((row.get('location_type') or ''), (row.get('location_value') or ''), (row.get('location_source') or '')) != (
            (record.get('type') or ''), (record.get('value') or ''), (record.get('source') or '')):
        return False
    if record.get('value'):
        return True
    for got, sent in ((row.get('progress_percent'), record.get('progressPercent')),
                      (row.get('content_source_progress_percent'), record.get('contentSourceProgressPercent'))):
        if not _percent_matches(got, sent, ECHO_POINTS):
            return False
    return True


def _row_has_location(row):
    return bool((row.get('location_source') or '').strip() or (row.get('location_value') or '').strip())


def _same_location(row, entry):
    k = entry.get('kobo') if isinstance(entry, dict) else None
    if not isinstance(k, dict) or entry.get('deviceId') != KOBO_DEVICE_ID:
        return False
    return (k.get('type'), k.get('value'), k.get('source')) == (
        row['location_type'], row['location_value'], row['location_source'])


def import_positions(library_path, app_db, lookup, user=None, log=None, max_books=MAX_BOOKS, now=None,
                     pushed=None):
    '''One import. `lookup(book_id)` -> (uuid, epub path or None, kepub path or
    None), or None for a book not in this library. Maps first (slow: EPUB
    parsing), then re-reads the positions file and merges against THAT copy
    just before writing (M1), so an app upload landing meanwhile is kept.
    `pushed`: kobo_push's memory {uuid (lower case): what we last PUT}; a Kobo
    bookmark equal to it is our own push, not a Kobo move (step 2 echo guard).'''
    res = KoboImportResult()
    mismatch = other_library(app_db, library_path) if app_db and os.path.isfile(app_db) else None
    if mismatch:
        res.notes.append(mismatch)
        return res
    rows, res.user, notes = read_kobo_bookmarks(app_db, user)
    res.notes.extend(notes)
    if not rows:
        return res
    path = os.path.join(library_path, POSITIONS_FILE)
    data, err = _load_positions(path)
    if err:
        res.notes.append(err)
        return res
    now = now or _dt.datetime.now(_dt.timezone.utc)
    future_skipped = 0
    with_source = unresolved = 0
    mapped = {}                       # uuid -> entry
    attempted = 0
    candidates = []                   # every row that would be mapped (this run or later)
    keys = {k.lower(): k for k in data['positions']}
    for row in rows:
        stamp = _parse_iso(row['updated_at']) if row['updated_at'] else None
        if stamp is None:
            continue
        if stamp - now > FUTURE_SLACK:
            future_skipped += 1
            continue
        info = lookup(row['book_id'])
        if not info:
            res.skipped[row['book_id']] = 'not in this calibre library'
            continue
        uuid, epub_path, kepub_path = info
        if not uuid:
            continue
        current = data['positions'].get(keys.get(uuid.lower(), uuid))
        if isinstance(current, dict) and not _is_newer(row['updated_at'], current.get('updated_at')):
            continue                      # the sidecar's place is as new or newer
        if pushed and is_echo(row, pushed.get(uuid.lower()), current):
            res.skipped[uuid] = 'our own push to the Kobo coming back (echo), not a Kobo move'
            continue
        has_loc = _row_has_location(row)
        pct = row['progress_percent']
        if not has_loc and (pct is None or pct <= 0 or pct >= 100):
            res.skipped[uuid] = 'Kobo status (0 % / 100 %), not a reading place'
            continue
        if has_loc and _same_location(row, current):
            # M5: Calibre-Web keeps the old Location when a later PUT has none;
            # an unchanged location under a fresh stamp is not a new place.
            res.skipped[uuid] = 'Kobo location unchanged since the last import'
            continue
        if not epub_path or not os.path.isfile(epub_path):
            res.skipped[uuid] = 'no EPUB (CalibReader opens the EPUB)'
            continue
        fkey = _failure_key(library_path, app_db, uuid)
        if _FAILED.get(fkey) == row['updated_at']:
            res.skipped[uuid] = 'failed before; tried again when the Kobo moves'
            continue
        if _ECHOED.get(fkey) == row['updated_at']:
            res.skipped[uuid] = 'our own push to the Kobo, reported in the Kobo\'s terms (echo)'
            continue
        candidates.append((fkey, row['updated_at']))
        if attempted >= max_books:
            res.more = True
            continue
        attempted += 1
        epub = kepub = None
        try:
            epub = Book(epub_path)
            if kepub_path and os.path.isfile(kepub_path):
                try:
                    kepub = Book(kepub_path)
                except Exception:          # a damaged KEPUB only costs precision
                    kepub = None
            place = map_bookmark(epub, row, kepub)
            if row['location_source']:
                with_source += 1
                if place is None:
                    unresolved += 1
            if pushed and echo_by_place(place, row, pushed.get(uuid.lower()), current):
                res.skipped[uuid] = 'our own push to the Kobo, reported in the Kobo\'s terms (echo)'
                _ECHOED[fkey] = row['updated_at']
                continue
            if place is not None and has_loc and pct is not None and 0 < pct < 100:
                lengths = epub.lengths()
                total = float(sum(lengths)) or 1.0
                ours = 100.0 * (sum(lengths[:place['chapter']]) + place['textAnchor']) / total
                if abs(ours - pct) > DISTRUST_POINTS:
                    res.skipped[uuid] = ('Kobo location ({:.0f} % into the book) does not fit its {:.0f} %'
                                         .format(ours, pct))
                    _FAILED[fkey] = row['updated_at']
                    continue
        except Exception as exc:
            res.skipped[uuid] = 'could not read the EPUB ({})'.format(exc)
            _FAILED[fkey] = row['updated_at']
            continue
        finally:
            for b in (epub, kepub):
                if b is not None:
                    b.close()
        if place is None:
            res.skipped[uuid] = 'Kobo place does not match this book'
            _FAILED[fkey] = row['updated_at']
            continue
        mapped[uuid] = {
            'chapter': place['chapter'],
            'page': 0,
            'scrollY': 0,
            'progress': place['progress'],
            'textAnchor': place['textAnchor'],
            'updated_at': row['updated_at'],
            'deviceId': KOBO_DEVICE_ID,
            # Provenance (additive; the apps ignore keys they do not know).
            'kobo': {
                'type': row['location_type'], 'value': row['location_value'], 'source': row['location_source'],
                'progressPercent': row['progress_percent'],
                'contentSourceProgressPercent': row['content_source_progress_percent'],
                'precision': place['precision'],
            },
        }
    if future_skipped:
        res.notes.append('{} Kobo place(s) are stamped more than 10 minutes in the future: Calibre-Web\'s clock '
                         'or timezone looks wrong. Not imported.'.format(future_skipped))
    # M4: most Kobo locations not found in these books = the wrong library.
    if with_source >= 2 and unresolved * 2 > with_source:
        res.notes.append('{} of {} Kobo places do not match the books here: this Calibre-Web looks like it serves '
                         'another calibre library. Nothing imported.'.format(unresolved, with_source))
        res.mapped = {}
        res.more = False              # N1: a refused run never continues
        # The whole set is the wrong library: none of it is tried again
        # until its Kobo stamp changes.
        for fkey, stamp in candidates:
            _FAILED[fkey] = stamp
        return res
    if not mapped:
        return res
    # M1: re-read, re-merge against the fresh copy, write at once.
    fresh, err = _load_positions(path)
    if err:
        res.notes.append(err)
        return res
    table = fresh['positions']
    keys = {k.lower(): k for k in table}
    for uuid, entry in mapped.items():
        key = keys.get(uuid.lower(), uuid)
        current = table.get(key)
        if isinstance(current, dict) and not _is_newer(entry['updated_at'], current.get('updated_at')):
            continue
        table[key] = entry
        res.written.append(uuid)
        res.mapped[uuid] = entry
        if log:
            log('Kobo place {} -> chapter {} textAnchor {} ({})'.format(
                uuid, entry['chapter'], entry['textAnchor'], entry['kobo']['precision']))
    if res.written:
        fresh['updated_at'] = _now_iso()
        try:
            _write_atomic(path, fresh)
        except OSError as exc:
            res.notes.append('Could not write {} ({}).'.format(POSITIONS_FILE, exc))
            res.written = []
            res.mapped = {}
    return res


def metadata_lookup(library_path):
    '''Standalone (outside calibre): book id -> (uuid, EPUB path, KEPUB path)
    from metadata.db, opened read-only.'''
    con = _connect_ro(os.path.join(library_path, 'metadata.db'))
    try:
        books = {i: (u, p) for i, u, p in con.execute('SELECT id, uuid, path FROM books')}
        data = {}
        for book, fmt, name in con.execute('SELECT book, format, name FROM data'):
            data[(book, fmt.upper())] = name
    finally:
        con.close()

    def lookup(book_id):
        if book_id not in books:
            return None
        uuid, rel = books[book_id]
        def fmt_path(fmt, ext):
            name = data.get((book_id, fmt))
            return os.path.join(library_path, rel, name + ext) if name else None
        return uuid, fmt_path('EPUB', '.epub'), fmt_path('KEPUB', '.kepub')
    return lookup


def main(argv=None):  # pragma: no cover - CLI
    import argparse
    try:
        from calibre_plugins.calibreader_sync import kobo_push
    except ImportError:  # standalone
        import kobo_push
    ap = argparse.ArgumentParser(description='Import Kobo reading places from Calibre-Web into CalibReader_positions.json'
                                             ' (and, with --push, send CalibReader\'s places to the Kobo)')
    ap.add_argument('--library', required=True)
    ap.add_argument('--app-db', required=True)
    ap.add_argument('--user')
    ap.add_argument('--push', action='store_true',
                    help='also send CalibReader\'s place to the Kobo through Calibre-Web (tester preview; off without it)')
    ap.add_argument('--calibre-web', default=kobo_push.DEFAULT_CALIBRE_WEB,
                    help='Calibre-Web address (default %(default)s)')
    ap.add_argument('--state', default=kobo_push.DEFAULT_STATE_FILE,
                    help='where the places sent to the Kobo are remembered (default %(default)s)')
    ap.add_argument('--since',
                    help='send only places read after this time (ISO 8601, UTC); default: the time of the first '
                         '--push run for this library, kept in --state')
    a = ap.parse_args(argv)
    state = kobo_push.load_state(a.state, a.library)
    since = a.since
    if a.push and not since:
        if not state.since:                 # B3: the first --push run is when sending was turned on
            state.since = kobo_push.now_iso()
            state.save()
            print('sending to the Kobo starts now ({}): places read before are not sent'.format(state.since))
        since = state.since
    res, pres = kobo_push.sync_kobo(a.library, a.app_db, metadata_lookup(a.library), user=a.user, push=a.push,
                                    base_url=a.calibre_web, memory=state.memory, log=print, since=since,
                                    persist=lambda memory: state.save())
    for n in res.notes:
        print('note:', n)
    for k, why in res.skipped.items():
        print('skipped {}: {}'.format(k, why))
    print('user={} written={}'.format(res.user, res.written))
    for u, e in res.mapped.items():
        print(json.dumps({u: e}, ensure_ascii=False))
    if pres is not None:
        for n in pres.notes:
            print('push note:', n)
        for k, why in sorted(pres.skipped.items()):
            print('not sent {}: {}'.format(k, why))
        for line in pres.lines:
            print(line)
        print('sent={}'.format(pres.sent))
        if pres.sent or pres.failure == 'timeout':
            state.save()
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())

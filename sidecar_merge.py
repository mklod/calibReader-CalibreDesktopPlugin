# Last modified: 2026-10-21--1751
#
# sidecar_merge.py — pure decision logic for CalibReader_sync.json.
#
# Deliberately imports NOTHING from calibre: this module is importable (and
# runnable: `python3 sidecar_merge.py`) outside the Calibre environment so the
# merge rules can be unit-tested without a GUI.
#
# ---------------------------------------------------------------------------
# Sidecar schema (schema stays 1; both forms are valid and may be mixed)
# ---------------------------------------------------------------------------
#
#   {
#     "schema": 1,
#     "updated_at": "2026-09-15T05:09:00Z",          <- file-level fallback
#     "books": {
#       "<uuid-legacy>": true,                        <- LEGACY bare bool
#       "<uuid-modern>": {                            <- per-entry stamp
#         "value": false,
#         "updated_at": "2026-11-02T18:31:12.482Z"
#       }
#     }
#   }
#
# A bare bool carries the FILE-level `updated_at`. An object carries its own.
# Fractional seconds are allowed; a trailing `Z` or a `+00:00` offset are both
# accepted, as is a naive stamp (assumed UTC).
#
# ---------------------------------------------------------------------------
# Why per-entry stamps exist
# ---------------------------------------------------------------------------
#
# The file used to carry ONE file-level stamp, and every client applied every
# entry unconditionally and forever. That meant a stale `false` written in
# September could re-un-read a book the user finished in November, and a write
# about book Y could revert an explicit later toggle on book X. Per-entry
# stamps + a per-book record of what this desktop last applied fix both.
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.


from __future__ import annotations

__license__   = 'GPL v3'
__copyright__ = '2026, CalibReader'

import datetime
import json
import math
import re

# Objects in `books` use these keys. Kept as constants so the reader and any
# future writer can't drift apart.
KEY_VALUE = 'value'
KEY_UPDATED_AT = 'updated_at'

# CalibReader_positions.json: `positions[uuid] = {progress (0-1), updated_at,
# chapter, page, scrollY, deviceId, textAnchor?}`. Both apps write it for EPUB
# and PDF (PDF progress = (page + 1) / pageCount). Only these two keys matter
# to the desktop.
KEY_POSITIONS = 'positions'
KEY_PROGRESS = 'progress'

UTC = datetime.timezone.utc

_ISO_RE = re.compile(
    r'^(\d{4})-(\d{2})-(\d{2})'          # date
    r'[Tt ](\d{2}):(\d{2}):(\d{2})'      # time
    r'(?:[.,](\d{1,9}))?'                # optional fractional seconds
    r'(Z|z|[+-]\d{2}:?\d{2})?$'          # optional zone
)


def parse_iso(stamp):
    '''Parse an ISO-8601 stamp into an aware UTC datetime, or None.

    Tolerant on purpose: iOS writes `2026-09-15T05:09:00Z`, Android's
    `toIso8601String()` adds microseconds, and a hand-edited file may carry an
    offset or no zone at all. Anything unrecognised returns None, which every
    caller below treats as "undated" rather than as "epoch" — an undated
    source must never win a comparison by accident.
    '''
    if isinstance(stamp, datetime.datetime):
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    if not isinstance(stamp, str):
        return None
    m = _ISO_RE.match(stamp.strip())
    if m is None:
        return None
    year, month, day, hour, minute, second, frac, zone = m.groups()
    micro = int((frac or '').ljust(6, '0')[:6]) if frac else 0
    try:
        dt = datetime.datetime(
            int(year), int(month), int(day),
            int(hour), int(minute), int(second), micro, tzinfo=UTC,
        )
    except ValueError:
        return None
    if zone and zone not in ('Z', 'z'):
        sign = 1 if zone[0] == '+' else -1
        zone = zone[1:].replace(':', '')
        offset = datetime.timedelta(hours=int(zone[:2]), minutes=int(zone[2:]))
        dt = dt - sign * offset
    return dt


def format_iso(dt):
    '''Render an aware datetime as `…Z`, millisecond precision.'''
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + '{:03d}Z'.format(dt.microsecond // 1000)


def trunc_ms(dt):
    '''Drop sub-millisecond precision. The bookkeeping stores `last_modified`
    via `format_iso` (milliseconds) while calibre hands back microseconds, so
    an untouched book would otherwise look up to 999 µs "newer" than the value
    recorded after our own write, i.e. like a desktop edit.'''
    if dt is None:
        return None
    return dt.replace(microsecond=dt.microsecond // 1000 * 1000)

class Entry(object):
    '''One `books[uuid]` entry, normalised.

    `stamp` is the EFFECTIVE stamp: the entry's own when it carried one, else
    the file-level stamp (legacy bare bools), else None.
    `dated` is True only when the entry carried its own stamp — used so a
    legacy file can't masquerade as a freshly-stamped explicit toggle.
    '''

    __slots__ = ('value', 'stamp', 'dated')

    def __init__(self, value, stamp=None, dated=False):
        self.value = bool(value)
        self.stamp = stamp
        self.dated = bool(dated)

    def __repr__(self):  # pragma: no cover - debugging aid
        return 'Entry(value={!r}, stamp={!r}, dated={!r})'.format(
            self.value, format_iso(self.stamp), self.dated)

    def __eq__(self, other):
        return (isinstance(other, Entry) and self.value == other.value
                and self.stamp == other.stamp and self.dated == other.dated)


def parse_sidecar(data):
    '''`{...}` (already-decoded JSON) -> `{lowercased uuid: Entry}`.

    Accepts both entry forms. Unusable entries (wrong type, object without a
    usable `value`) are dropped rather than guessed at — a dropped entry is
    a no-op, a guessed one silently rewrites the user's library.
    '''
    if not isinstance(data, dict):
        return {}
    file_stamp = parse_iso(data.get(KEY_UPDATED_AT))
    books = data.get('books')
    if not isinstance(books, dict):
        return {}

    out = {}
    for uuid, raw in books.items():
        if not isinstance(uuid, str) or not uuid:
            continue
        if isinstance(raw, bool):
            out[uuid.lower()] = Entry(raw, file_stamp, dated=False)
        elif isinstance(raw, dict):
            value = raw.get(KEY_VALUE)
            if not isinstance(value, bool):
                continue
            own = parse_iso(raw.get(KEY_UPDATED_AT))
            if own is None:
                out[uuid.lower()] = Entry(value, file_stamp, dated=False)
            else:
                out[uuid.lower()] = Entry(value, own, dated=True)
        # anything else (int, str, null, list) is ignored on purpose
    return out


class Target(object):
    '''A value the sidecars say should be in calibre, plus the stamp that
    decides whether it may be written. `Entry` is the read-flag case; this is
    the general one (`value` is not coerced), used for reading progress.'''

    __slots__ = ('value', 'stamp', 'dated')

    def __init__(self, value, stamp=None, dated=False):
        self.value = value
        self.stamp = stamp
        self.dated = bool(dated)


class Position(object):
    '''One `positions[uuid]` entry, reduced to what the desktop uses.'''

    __slots__ = ('progress', 'stamp', 'dated')

    def __init__(self, progress, stamp=None, dated=False):
        self.progress = progress
        self.stamp = stamp
        self.dated = bool(dated)


def parse_positions(data, threshold_percent=1):
    '''`CalibReader_positions.json` (decoded) -> `({lowercased uuid: Position}, below)`.

    Entries under `threshold_percent` % read are left out (`below` counts
    them): opening a book to look at it must not make it "in progress". An
    entry without a usable 0-1 `progress` is dropped, as `parse_sidecar` drops
    a flag without a bool. Stamps follow the read-flag rule: the entry's own
    `updated_at`, else the file-level one (then `dated` is False), else None.
    '''
    if not isinstance(data, dict):
        return {}, 0
    file_stamp = parse_iso(data.get(KEY_UPDATED_AT))
    positions = data.get(KEY_POSITIONS)
    if not isinstance(positions, dict):
        return {}, 0
    threshold = max(0.0, float(threshold_percent)) / 100.0
    out, below = {}, 0
    for uuid, raw in positions.items():
        if not isinstance(uuid, str) or not uuid or not isinstance(raw, dict):
            continue
        progress = raw.get(KEY_PROGRESS)
        # bool is an int subclass; a flag is not a progress value
        if isinstance(progress, bool) or not isinstance(progress, (int, float)):
            continue
        progress = float(progress)
        if math.isnan(progress) or progress < 0 or progress > 1:
            continue
        if progress < threshold or progress == 0:
            below += 1
            continue
        own = parse_iso(raw.get(KEY_UPDATED_AT))
        if own is None:
            out[uuid.lower()] = Position(progress, file_stamp, dated=False)
        else:
            out[uuid.lower()] = Position(progress, own, dated=True)
    return out, below


_FRACTION_FORMAT_RE = re.compile(r'\{[^}]*%\}')


def percent_for_column(progress, datatype, number_format=None):
    '''Progress (0-1) as the value a calibre column of `datatype` should hold.

    - Integer: 0-100, rounded half up (Kobo Utilities' "Percent Read" style).
    - Float shown with a percent format type such as `{:.0%}`: 0-1, because
      Python's `%` format multiplies by 100 itself (KOReader Sync's
      `#ko_progfloat` style).
    - Any other float: 0-100 with one decimal.
    '''
    if datatype == 'int':
        return min(100, max(0, int(math.floor(progress * 100 + 0.5))))
    if datatype == 'float':
        if number_format and _FRACTION_FORMAT_RE.search(number_format):
            return round(progress, 4)
        return round(progress * 100, 1)
    raise ValueError('not a number column: {!r}'.format(datatype))


def last_read_value(stamp):
    '''The value for a "last read" Date column: the position's stamp, whole
    seconds, UTC. None when the entry carried no usable stamp at all.'''
    if stamp is None:
        return None
    return stamp.astimezone(UTC).replace(microsecond=0)


# --- decisions -------------------------------------------------------------

APPLY = 'apply'        # write the value into #read_status, then record
RECORD = 'record'      # entry won, but the column already agrees: record only
SKIP = 'skip'          # entry lost (stale, or a desktop edit is newer)


def decide(entry, current_value, applied_stamp, applied_last_modified,
           book_last_modified):
    '''The one rule the plugin applies, per book. Returns APPLY/RECORD/SKIP.

    The same rule serves reading progress: there `entry` is a `Target` whose
    value is the tuple of column values to write, and `current_value` is the
    matching tuple read from calibre.

    Arguments
    ---------
    entry                  : Entry (or Target) from the sidecar (never None).
    current_value          : current `#read_status` (True/False/None-if-unset).
    applied_stamp          : stamp of the entry this plugin last applied to
                             this book, or None if it never has.
    applied_last_modified  : the book's calibre `last_modified` as observed
                             immediately AFTER that apply, or None.
    book_last_modified     : the book's calibre `last_modified` right now.

    Rules, in order
    ---------------
    1. Undated entry (legacy file with no usable stamp anywhere): bootstrap
       only. Applied when this plugin has no record for the book; once a
       record exists, an undated source never overrides it. This is the
       desktop mirror of the mobile rule "an undated source never overrides a
       stamped local value".
    2. Stale: stamp <= what we last applied -> SKIP. This is what stops a
       September `false` from re-un-reading a book forever: we applied the
       November entry, so the September one can never win again.
    3. Desktop edit wins: if the book has been touched in calibre since our
       last apply (last_modified moved), or we have never applied anything for
       it, then an entry whose stamp is not strictly newer than the book's
       `last_modified` is SKIPped — the desktop change is the later one.
    4. Otherwise the entry wins: APPLY if it actually changes the column,
       RECORD if the column already agrees (still worth recording so rule 2
       can retire the entry).
    '''
    if entry.stamp is None:
        if applied_stamp is None and applied_last_modified is None:
            return APPLY if current_value != entry.value else RECORD
        return SKIP

    if applied_stamp is not None and entry.stamp <= applied_stamp:
        return SKIP

    if book_last_modified is not None:
        desktop_touched = (
            applied_last_modified is None                      # never applied
            # edited since (compared at the millisecond precision the
            # bookkeeping is stored at; see trunc_ms)
            or trunc_ms(book_last_modified) > trunc_ms(applied_last_modified)
        )
        if desktop_touched and entry.stamp <= book_last_modified:
            return SKIP

    return APPLY if current_value != entry.value else RECORD


# --- the read flag, by value (v1.2.3) ------------------------------------------
#
# calibre bumps a book's `last_modified` on EVERY metadata edit (a rating, a tag,
# a cover). Using it as "the read flag was edited in calibre" meant an unrelated
# edit made after a phone toggle silently dropped that toggle. From 1.2.3 the
# plugin remembers, per book, the read value it last saw in calibre, and only a
# CHANGE of that value counts as a desktop edit of the read flag. When it sees
# one it remembers when (`desktop_at`, the book's last_modified at that moment),
# and a phone entry must be newer than that to win. Books the plugin has no value
# record for yet (first contact, or a 1.2.x record) use `decide` as before.

NOT_SEEN = object()   # no value record for this book yet


def decide_flag(entry, current_value, applied_stamp, applied_last_modified,
                book_last_modified, seen_value=NOT_SEEN, desktop_at=None):
    '''The read-flag rule. Returns (APPLY/RECORD/SKIP, desktop_at to store).'''
    if seen_value is NOT_SEEN:
        verdict = decide(entry, current_value, applied_stamp, applied_last_modified, book_last_modified)
        if (verdict == SKIP and entry.stamp is not None and book_last_modified is not None
                and (applied_stamp is None or entry.stamp > applied_stamp)):
            desktop_at = later(desktop_at, book_last_modified)   # it lost to a desktop edit
        return verdict, desktop_at
    if current_value != seen_value and book_last_modified is not None:
        desktop_at = later(desktop_at, book_last_modified)       # the flag itself changed in calibre
    if entry.stamp is None:
        return SKIP, desktop_at                                  # undated never overrides a record
    floor = later(applied_stamp, desktop_at)
    if floor is not None and entry.stamp <= floor:
        return SKIP, desktop_at
    return (APPLY if current_value != entry.value else RECORD), desktop_at


def later(a, b):
    '''The later of two optional datetimes.'''
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def load_flag_records(prefs_blob):
    '''`{uuid: (applied_stamp, applied_last_modified, seen_value | NOT_SEEN, desktop_at)}`.'''
    out = {}
    if not isinstance(prefs_blob, dict):
        return out
    for uuid, rec in prefs_blob.items():
        if not isinstance(uuid, str) or not isinstance(rec, dict):
            continue
        seen = rec['val'] if 'val' in rec and rec['val'] in (True, False, None) else NOT_SEEN
        out[uuid.lower()] = (parse_iso(rec.get('stamp')), parse_iso(rec.get('lm')), seen, parse_iso(rec.get('dt')))
    return out


def store_flag_record(prefs_blob, uuid, applied_stamp, applied_last_modified, seen_value, desktop_at):
    '''One book's read-flag bookkeeping (in place). Keeps the 1.2.x keys, so an older
    plugin installed over this one still reads `stamp` and `lm`.'''
    prefs_blob[uuid.lower()] = {
        'stamp': format_iso(applied_stamp),
        'lm': format_iso(applied_last_modified),
        'val': seen_value,
        'dt': format_iso(desktop_at),
    }
    return prefs_blob


def plan(entries, book_state):
    '''Run `decide` over a whole sidecar.

    `entries`    : {uuid: Entry} (from `parse_sidecar`).
    `book_state` : {uuid: (current_value, applied_stamp, applied_last_modified,
                           book_last_modified)} for the uuids present in this
                   library. uuids missing from it are counted as unmatched.

    Returns (updates, records, skipped, unmatched):
      updates  : {uuid: bool}      -> write these into #read_status
      records  : {uuid: Entry}     -> stamp bookkeeping for updates + RECORDs
      skipped  : [uuid]
      unmatched: [uuid]
    '''
    updates, records, skipped, unmatched = {}, {}, [], []
    for uuid, entry in entries.items():
        state = book_state.get(uuid)
        if state is None:
            unmatched.append(uuid)
            continue
        current_value, applied_stamp, applied_lm, book_lm = state
        verdict = decide(entry, current_value, applied_stamp, applied_lm, book_lm)
        if verdict == APPLY:
            updates[uuid] = entry.value
            records[uuid] = entry
        elif verdict == RECORD:
            records[uuid] = entry
        else:
            skipped.append(uuid)
    return updates, records, skipped, unmatched


def load_applied(prefs_blob):
    '''Normalise the plugin's stored bookkeeping into
    `{uuid: (applied_stamp, applied_last_modified)}`.'''
    out = {}
    if not isinstance(prefs_blob, dict):
        return out
    for uuid, rec in prefs_blob.items():
        if not isinstance(uuid, str) or not isinstance(rec, dict):
            continue
        out[uuid.lower()] = (parse_iso(rec.get('stamp')), parse_iso(rec.get('lm')))
    return out


def store_applied(prefs_blob, uuid, entry_stamp, book_last_modified):
    '''Write one book's bookkeeping back into the prefs dict (in place).'''
    prefs_blob[uuid.lower()] = {
        'stamp': format_iso(entry_stamp),
        'lm': format_iso(book_last_modified),
    }
    return prefs_blob


def refresh_lm(prefs_blob, uuid, lm_before, lm_after):
    '''Carry a book's record over one of OUR OWN writes made for another kind
    of entry (writing % read bumps the same `last_modified` the read-flag
    record is keyed on, and vice versa).

    Only when the record still holds `lm_before`, i.e. nothing changed the
    book between our previous write and this one. If the book was edited in
    calibre in between, the record is left alone so that edit keeps counting
    as a desktop edit. Returns True when the record was moved on.'''
    rec = prefs_blob.get(uuid.lower())
    if not isinstance(rec, dict) or lm_before is None or lm_after is None:
        return False
    if rec.get('lm') != format_iso(lm_before):
        return False
    rec['lm'] = format_iso(lm_after)
    return True


def build_entry_object(value, stamp):
    '''The object form a WRITER must emit. The plugin does not currently write
    the sidecar, but if/when read-status writeback lands it must use this so
    every client agrees on the shape.'''
    return {KEY_VALUE: bool(value), KEY_UPDATED_AT: format_iso(stamp)}


# ---------------------------------------------------------------------------
# Self-check: `python3 sidecar_merge.py` (also runnable under unittest)
# ---------------------------------------------------------------------------

if __name__ == '__main__':  # pragma: no cover
    import unittest

    SEP = parse_iso('2026-09-15T05:00:00Z')
    NOV = parse_iso('2026-11-02T18:31:12.482Z')
    OCT = parse_iso('2026-10-01T00:00:00Z')

    class ParseTests(unittest.TestCase):
        def test_bare_bool_takes_file_stamp(self):
            e = parse_sidecar({'updated_at': '2026-09-15T05:00:00Z',
                               'books': {'A': True}})['a']
            self.assertEqual((e.value, e.stamp, e.dated), (True, SEP, False))

        def test_object_form_takes_own_stamp(self):
            e = parse_sidecar({'updated_at': '2026-09-15T05:00:00Z',
                               'books': {'A': {'value': False,
                                               'updated_at': '2026-11-02T18:31:12.482Z'}}})['a']
            self.assertEqual((e.value, e.stamp, e.dated), (False, NOV, True))

        def test_object_without_stamp_falls_back_to_file_stamp(self):
            e = parse_sidecar({'updated_at': '2026-09-15T05:00:00Z',
                               'books': {'A': {'value': True}}})['a']
            self.assertEqual((e.stamp, e.dated), (SEP, False))

        def test_mixed_forms_coexist(self):
            got = parse_sidecar({'updated_at': '2026-09-15T05:00:00Z', 'books': {
                'legacy': True,
                'modern': {'value': False, 'updated_at': '2026-11-02T18:31:12.482Z'}}})
            self.assertEqual(sorted(got), ['legacy', 'modern'])
            self.assertFalse(got['legacy'].dated)
            self.assertTrue(got['modern'].dated)

        def test_garbage_entries_are_dropped(self):
            got = parse_sidecar({'books': {'a': 1, 'b': None, 'c': 'yes',
                                           'd': {'updated_at': '2026-01-01T00:00:00Z'}}})
            self.assertEqual(got, {})

        def test_no_file_stamp_means_undated(self):
            self.assertIsNone(parse_sidecar({'books': {'a': True}})['a'].stamp)

        def test_iso_variants(self):
            self.assertEqual(parse_iso('2026-09-15T05:00:00Z'), SEP)
            self.assertEqual(parse_iso('2026-09-15T05:00:00.000Z'), SEP)
            self.assertEqual(parse_iso('2026-09-15T07:00:00+02:00'), SEP)
            self.assertEqual(parse_iso('2026-09-15T05:00:00'), SEP)
            self.assertIsNone(parse_iso('not a date'))
            self.assertIsNone(parse_iso(None))

        def test_format_roundtrip(self):
            self.assertEqual(format_iso(NOV), '2026-11-02T18:31:12.482Z')

    class DecideTests(unittest.TestCase):
        def test_fresh_book_applies(self):
            self.assertEqual(decide(Entry(True, NOV, True), None, None, None, None), APPLY)

        def test_already_agrees_records_only(self):
            self.assertEqual(decide(Entry(True, NOV, True), True, None, None, None), RECORD)

        def test_september_false_cannot_beat_applied_november_read(self):
            # The headline scenario: we already applied the November "read";
            # a September "false" that is still sitting in the file must lose.
            self.assertEqual(
                decide(Entry(False, SEP, True), True, NOV, NOV, NOV), SKIP)

        def test_november_read_beats_applied_september_false(self):
            self.assertEqual(
                decide(Entry(True, NOV, True), False, SEP, SEP, SEP), APPLY)

        def test_tie_with_applied_stamp_skips(self):
            self.assertEqual(
                decide(Entry(False, NOV, True), True, NOV, NOV, NOV), SKIP)

        def test_desktop_edit_after_apply_wins(self):
            # applied in Sept (lm was Sept), user edited the flag in calibre in
            # Nov; an October sidecar entry must not revert it.
            self.assertEqual(
                decide(Entry(True, OCT, True), False, SEP, SEP, NOV), SKIP)

        def test_entry_newer_than_desktop_edit_applies(self):
            self.assertEqual(
                decide(Entry(True, NOV, True), False, SEP, SEP, OCT), APPLY)

        def test_never_applied_and_desktop_newer_skips(self):
            self.assertEqual(
                decide(Entry(True, SEP, True), False, None, None, NOV), SKIP)

        def test_our_own_write_does_not_block_the_next_entry(self):
            # We applied the Sept entry; set_field bumped last_modified to Oct
            # and we recorded that. A November entry must still get through.
            self.assertEqual(
                decide(Entry(False, NOV, True), True, SEP, OCT, OCT), APPLY)

        def test_undated_bootstraps_once_then_never_overrides(self):
            self.assertEqual(decide(Entry(True, None), None, None, None, None), APPLY)
            self.assertEqual(decide(Entry(False, None), True, SEP, SEP, SEP), SKIP)

    class PlanTests(unittest.TestCase):
        def test_plan_splits_every_bucket(self):
            entries = parse_sidecar({'updated_at': '2026-09-15T05:00:00Z', 'books': {
                'fresh':  {'value': True,  'updated_at': '2026-11-02T18:31:12.482Z'},
                'stale':  {'value': False, 'updated_at': '2026-09-15T05:00:00Z'},
                'agrees': {'value': True,  'updated_at': '2026-11-02T18:31:12.482Z'},
                'absent': True,
            }})
            state = {
                'fresh':  (False, None, None, SEP),
                'stale':  (True,  NOV,  NOV,  NOV),
                'agrees': (True,  None, None, SEP),
            }
            updates, records, skipped, unmatched = plan(entries, state)
            self.assertEqual(updates, {'fresh': True})
            self.assertEqual(sorted(records), ['agrees', 'fresh'])
            self.assertEqual(skipped, ['stale'])
            self.assertEqual(unmatched, ['absent'])

    class BookkeepingTests(unittest.TestCase):
        def test_store_then_load_roundtrip(self):
            blob = {}
            store_applied(blob, 'ABC', NOV, OCT)
            # survives a JSON round trip, which is how calibre stores prefs
            loaded = load_applied(json.loads(json.dumps(blob)))
            self.assertEqual(loaded['abc'], (NOV, OCT))

        def test_load_tolerates_garbage(self):
            self.assertEqual(load_applied({'a': 'nope', 'b': {'stamp': 'x'}}),
                             {'b': (None, None)})

        def test_writer_object_shape(self):
            self.assertEqual(build_entry_object(True, NOV),
                             {'value': True, 'updated_at': '2026-11-02T18:31:12.482Z'})

        def test_refresh_lm_moves_on_only_over_our_own_write(self):
            blob = {}
            store_applied(blob, 'abc', SEP, OCT)
            self.assertTrue(refresh_lm(blob, 'ABC', OCT, NOV))
            self.assertEqual(load_applied(blob)['abc'], (SEP, NOV))
            # a desktop edit happened since (lm_before is not what we recorded)
            self.assertFalse(refresh_lm(blob, 'abc', OCT, NOV + datetime.timedelta(days=1)))
            self.assertEqual(load_applied(blob)['abc'], (SEP, NOV))
            self.assertFalse(refresh_lm(blob, 'missing', OCT, NOV))

        def test_refresh_lm_compares_at_millisecond_precision(self):
            blob = {}
            store_applied(blob, 'abc', SEP, OCT)
            self.assertTrue(refresh_lm(blob, 'abc', OCT.replace(microsecond=400), NOV))

    class PrecisionTests(unittest.TestCase):
        def test_sub_millisecond_lm_is_not_a_desktop_edit(self):
            # Recorded lm went through format_iso (ms); calibre reports µs.
            # A late-arriving entry older than our last write must still apply.
            recorded = parse_iso(format_iso(OCT.replace(microsecond=123456)))
            now = OCT.replace(microsecond=123999)
            self.assertEqual(
                decide(Entry(True, OCT - datetime.timedelta(hours=1), True),
                       False, SEP, recorded, now), APPLY)

    class PositionTests(unittest.TestCase):
        DOC = {'updated_at': '2026-09-15T05:00:00Z', 'positions': {
            'Read':   {'progress': 0.47395, 'updated_at': '2026-11-02T18:31:12.482Z'},
            'peek':   {'progress': 0.0023, 'updated_at': '2026-11-02T18:31:12.482Z'},
            'zero':   {'progress': 0, 'updated_at': '2026-11-02T18:31:12.482Z'},
            'old':    {'progress': 0.5},
            'bad':    {'progress': 'half'},
            'flag':   {'progress': True},
            'over':   {'progress': 1.5},
            'list':   [0.5],
        }}

        def test_threshold_and_garbage(self):
            got, below = parse_positions(self.DOC, 1)
            self.assertEqual(sorted(got), ['old', 'read'])
            self.assertEqual(below, 2)          # 'peek' and 'zero'
            self.assertEqual((got['read'].stamp, got['read'].dated), (NOV, True))
            self.assertEqual((got['old'].stamp, got['old'].dated), (SEP, False))

        def test_threshold_is_configurable(self):
            got, below = parse_positions(self.DOC, 0.1)
            self.assertIn('peek', got)
            got, below = parse_positions(self.DOC, 50)
            self.assertEqual(sorted(got), ['old'])

        def test_no_positions_section(self):
            self.assertEqual(parse_positions({'books': {}}), ({}, 0))
            self.assertEqual(parse_positions(None), ({}, 0))

        def test_percent_for_int_column(self):
            self.assertEqual(percent_for_column(0.47395, 'int'), 47)
            self.assertEqual(percent_for_column(0.995, 'int'), 100)
            self.assertEqual(percent_for_column(0.005, 'int'), 1)
            self.assertEqual(percent_for_column(1.0, 'int'), 100)

        def test_percent_for_float_columns(self):
            self.assertEqual(percent_for_column(0.47391, 'float', '{:.0%}'), 0.4739)
            self.assertEqual(percent_for_column(0.47391, 'float', '{0:.1%}'), 0.4739)
            self.assertEqual(percent_for_column(0.47395, 'float', '{:.1f}%'), 47.4)
            self.assertEqual(percent_for_column(0.47395, 'float', None), 47.4)
            with self.assertRaises(ValueError):
                percent_for_column(0.5, 'datetime')

        def test_last_read_value(self):
            self.assertEqual(last_read_value(NOV), NOV.replace(microsecond=0))
            self.assertIsNone(last_read_value(None))

        def test_target_decides_like_entry(self):
            t = Target((47, NOV.replace(microsecond=0)), NOV, True)
            self.assertEqual(decide(t, (None, None), None, None, SEP), APPLY)
            self.assertEqual(decide(t, t.value, None, None, SEP), RECORD)
            self.assertEqual(decide(t, (10, None), NOV, NOV, NOV), SKIP)

    unittest.main(verbosity=2)

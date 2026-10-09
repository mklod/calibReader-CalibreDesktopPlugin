# Last modified: 2026-10-09--1459
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
import re

# Objects in `books` use these keys. Kept as constants so the reader and any
# future writer can't drift apart.
KEY_VALUE = 'value'
KEY_UPDATED_AT = 'updated_at'

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


# --- decisions -------------------------------------------------------------

APPLY = 'apply'        # write the value into #read_status, then record
RECORD = 'record'      # entry won, but the column already agrees: record only
SKIP = 'skip'          # entry lost (stale, or a desktop edit is newer)


def decide(entry, current_value, applied_stamp, applied_last_modified,
           book_last_modified):
    '''The one rule the plugin applies, per book. Returns APPLY/RECORD/SKIP.

    Arguments
    ---------
    entry                  : Entry from the sidecar (never None).
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
            or book_last_modified > applied_last_modified      # edited since
        )
        if desktop_touched and entry.stamp <= book_last_modified:
            return SKIP

    return APPLY if current_value != entry.value else RECORD


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

    unittest.main(verbosity=2)

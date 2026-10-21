# Last modified: 2026-10-21--1618
#
# sync_core.py — one sync run, without the GUI.
#
# Reads the two sidecars the CalibReader apps keep in the library folder and
# writes what wins into calibre:
#   CalibReader_sync.json      -> the read flag (#read_status, Yes/No)
#   CalibReader_positions.json -> % read and last-read date, into columns the
#                                 user picked (plugin settings, stored in the
#                                 library's own metadata.db preferences)
# One way only: app -> calibre. The sidecars are never written.
#
# Like sidecar_merge.py this imports nothing from calibre. `db` is anything
# with calibre's Cache API (gui.current_db.new_api): field_metadata,
# all_book_ids, field_for, set_field, pref, set_pref. `python sync_core.py`
# runs the tests against an in-memory stand-in for it.
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

from __future__ import annotations

__license__   = 'GPL v3'
__copyright__ = '2026, CalibReader'

import json
import os
import re

try:
    from calibre_plugins.calibreader_sync.sidecar_merge import (
        UTC, Target, format_iso, last_read_value, load_applied, parse_iso,
        parse_positions, parse_sidecar, percent_for_column, plan, refresh_lm,
        store_applied,
    )
except ImportError:  # pragma: no cover - standalone / test import
    from sidecar_merge import (
        UTC, Target, format_iso, last_read_value, load_applied, parse_iso,
        parse_positions, parse_sidecar, percent_for_column, plan, refresh_lm,
        store_applied,
    )

SYNC_FILE = 'CalibReader_sync.json'
POSITIONS_FILE = 'CalibReader_positions.json'

READ_COLUMN = '#read_status'

# --- settings (library metadata.db preferences) ------------------------------
#
# Stored with db.set_pref(SETTINGS_KEY, {...}) so they travel with the library
# to every computer and every phone that opens it. Schema:
#   {"version": 1, "percent_column": "#read_percent" | "",
#    "last_read_column": "#last_read" | "", "progress_threshold": 1}
# "" means "don't sync". `version` is present once the user has saved the
# dialog; until then the dialog may pre-select a suggested column.
# Reserved for a later release: "read_column" (the read flag's column, which
# the apps must learn to follow before the plugin can let users change it).

SETTINGS_KEY = 'calibreader_sync'
SETTINGS_VERSION = 1
DEFAULT_THRESHOLD = 1
MAX_THRESHOLD = 50

PERCENT_TYPES = ('int', 'float')
DATE_TYPES = ('datetime',)

_LOOKUP_RE = re.compile(r'^#[a-z][a-z0-9_]*$')


def normalise_settings(raw):
    '''Whatever is stored (possibly nothing, possibly hand-edited) -> a valid
    settings dict. A malformed value falls back to "don't sync", never to a
    guess: a guessed column is a write into the user's data.'''
    raw = raw if isinstance(raw, dict) else {}
    out = {
        'version': raw.get('version') if isinstance(raw.get('version'), int) else None,
        'percent_column': '',
        'last_read_column': '',
        'progress_threshold': DEFAULT_THRESHOLD,
    }
    for key in ('percent_column', 'last_read_column'):
        value = raw.get(key)
        if isinstance(value, str) and _LOOKUP_RE.match(value):
            out[key] = value
    threshold = raw.get('progress_threshold')
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        out['progress_threshold'] = min(MAX_THRESHOLD, max(0, threshold))
    return out


def settings_to_store(percent_column, last_read_column, progress_threshold):
    return normalise_settings({
        'version': SETTINGS_VERSION,
        'percent_column': percent_column or '',
        'last_read_column': last_read_column or '',
        'progress_threshold': progress_threshold,
    })


def eligible_columns(custom_columns, kind):
    '''[(lookup, heading)] of the custom columns that can hold `kind`
    ('percent' or 'last_read'), sorted by heading.'''
    types = PERCENT_TYPES if kind == 'percent' else DATE_TYPES
    out = []
    for lookup, meta in (custom_columns or {}).items():
        if not isinstance(meta, dict) or meta.get('datatype') not in types:
            continue
        if meta.get('is_multiple'):
            continue
        out.append((lookup, meta.get('name') or lookup))
    return sorted(out, key=lambda x: (x[1].lower(), x[0]))


# Checked in order; the first pattern any eligible column matches wins. Only
# names that mean the same thing: a "date read" or "finished" column is NOT a
# last-read column, so it is never suggested for one.
_SUGGEST = {
    'percent': [r'^#read_?perc', r'perc\w*.?read|read\w*.?perc', r'progress',
                r'perc|pct|%'],
    'last_read': [r'^#last_?read', r'last.?read|read.?last', r'last.?open'],
}


def suggest_column(custom_columns, kind):
    '''The eligible column that most likely already means `kind`, or "".'''
    cols = eligible_columns(custom_columns, kind)
    for pattern in _SUGGEST[kind]:
        rx = re.compile(pattern, re.I)
        for lookup, heading in cols:
            if rx.search(lookup) or rx.search(heading):
                return lookup
    return ''


# --- a read column the user already has (v1.2.2) -----------------------------
#
# Many libraries already track read status in their own Yes/No column (#read,
# #finished, KOReader Sync's #ko_statusbool). The apps read #read_status only,
# until a later release lets the user choose a column and the apps follow that
# choice. Until then, when #read_status is missing, the plugin names the column
# it found and explains how to copy it over once with calibre's bulk edit.
# Search terms are :yes / :no, never :true / :false: on a Yes/No column
# ":true" matches every book with a value, No included (calibre manual).

_READ_FLAG_RE = re.compile(r'read|finish|done|complete|statusbool', re.I)
_NOT_READ_RE = re.compile(r'unread|not.?read|to.?read|want|tbr|wish', re.I)


def existing_read_columns(custom_columns):
    '''[(lookup, heading)] of Yes/No columns, other than #read_status, whose
    lookup name or heading looks like a read flag (not "unread" / "to read").'''
    out = []
    for lookup, meta in (custom_columns or {}).items():
        if lookup == READ_COLUMN or not isinstance(meta, dict) or meta.get('datatype') != 'bool':
            continue
        heading = meta.get('name') or lookup
        names = lookup + ' ' + heading
        if _READ_FLAG_RE.search(names) and not _NOT_READ_RE.search(names):
            out.append((lookup, heading))
    return sorted(out, key=lambda x: (x[0] != '#read', x[1].lower(), x[0]))


def read_column_copy_hint(custom_columns):
    '''Plain-English steps to copy an existing read column into #read_status
    once, or "" when the library has no such column.'''
    cols = existing_read_columns(custom_columns)
    if not cols:
        return ''
    lookup, heading = cols[0]
    also = ''
    if len(cols) > 1:
        also = ' (also: {})'.format(', '.join('{} ({})'.format(h, l) for l, h in cols[1:]))
    return (
        'This library already has a Yes/No column that looks like a read flag: {heading} '
        '({lookup}){also}. CalibReader uses {col} for now; choosing your own column is coming '
        'in the next update. To copy yours over once:\n'
        '1. Preferences > Add your own columns: lookup name read_status, type Yes/No. '
        'Restart calibre.\n'
        '2. Search for {lookup}:yes and select all the books. Right-click Edit metadata and '
        'choose Edit metadata in bulk. On the Custom metadata tab set Read Status to Yes, '
        'tick Apply changes, then OK.\n'
        '3. Do the same with {lookup}:no and No.'
    ).format(heading=heading, lookup=lookup, also=also, col=READ_COLUMN)


# --- one run ------------------------------------------------------------------

class SyncResult(object):
    '''What a run did, for the summary dialog and the tests.'''

    def __init__(self):
        self.found_sync_file = False
        self.found_positions_file = False
        self.flags_updated = 0
        self.flags_skipped = 0
        self.progress_updated = 0
        self.progress_skipped = 0
        self.below_threshold = 0
        self.unmatched = set()
        self.progress_columns = []     # the % read / last read columns usable this run
        self.columns_written = []      # e.g. ['#read_status', '#read_percent']
        self.notes = []                # plain-English problems worth showing

    @property
    def found_any_file(self):
        return self.found_sync_file or self.found_positions_file

    def summary(self):
        lines = []
        if self.found_sync_file:
            lines.append('Read status: {} book(s) updated.'.format(self.flags_updated))
        if self.found_positions_file and self.progress_columns:
            lines.append('Reading progress: {} book(s) updated.'.format(self.progress_updated))
        skipped = self.flags_skipped + self.progress_skipped
        if skipped:
            lines.append('{} entr(y/ies) were already applied, or older than a change '
                         'made in calibre: left alone.'.format(skipped))
        if self.unmatched:
            lines.append('{} book(s) in the files are not in this library.'.format(len(self.unmatched)))
        lines.extend(self.notes)
        return '\n\n'.join(lines) if lines else 'Nothing to sync.'


def _load_json(path):
    '''(data, error). (None, None) when the file doesn't exist. A cloud client
    may leave a half-written file behind; that is an error for this run only.'''
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f), None
    except Exception as exc:
        return None, '{} could not be read ({}). Try again once your cloud app has ' \
                     'finished syncing.'.format(os.path.basename(path), exc)


def _lm(db, book_id):
    return parse_iso(db.field_for('last_modified', book_id))


def _column_check(custom_columns, lookup, types, what):
    '''(usable, note) for a configured column.'''
    meta = custom_columns.get(lookup)
    if meta is None:
        return False, ('The {} column {} does not exist yet. If you just created it, '
                       'restart calibre.'.format(what, lookup))
    if meta.get('datatype') not in types:
        return False, ('The {} column {} has the wrong type ({}); it was not '
                       'written.'.format(what, lookup, meta.get('datatype')))
    return True, None


def _norm_date(value):
    dt = parse_iso(value)
    return None if dt is None else dt.astimezone(UTC).replace(microsecond=0)


def run_sync(db, library_path, settings, flag_blob, pos_blob):
    '''One sync. Returns (SyncResult, new_flag_blob, new_pos_blob); the blobs
    are the plugin's per-book bookkeeping (see sidecar_merge), to be stored by
    the caller. Nothing is written to calibre unless a rule says it may.'''
    settings = normalise_settings(settings)
    flag_blob = dict(flag_blob or {})
    pos_blob = dict(pos_blob or {})
    res = SyncResult()

    flags_data, err = _load_json(os.path.join(library_path, SYNC_FILE))
    res.found_sync_file = flags_data is not None or err is not None
    if err:
        res.notes.append(err)
    pos_data, err = _load_json(os.path.join(library_path, POSITIONS_FILE))
    res.found_positions_file = pos_data is not None or err is not None
    if err:
        res.notes.append(err)

    custom = db.field_metadata.custom_field_metadata()

    # Which columns may be written this run.
    flag_col = None
    if flags_data is not None:
        meta = custom.get(READ_COLUMN)
        if meta is None:
            res.notes.append(
                'Read status was not synced: this library has no {0} column. Create it in '
                'calibre: Preferences > Add your own columns, lookup name read_status, type '
                'Yes/No, then restart calibre.'.format(READ_COLUMN))
            hint = read_column_copy_hint(custom)
            if hint:
                res.notes.append(hint)
        elif meta.get('datatype') != 'bool':
            res.notes.append(
                'Read status was not synced: {} is a {} column, and it needs to be '
                'Yes/No.'.format(READ_COLUMN, meta.get('datatype')))
        else:
            flag_col = READ_COLUMN

    pct_col = last_col = None
    if pos_data is not None:
        if settings['percent_column']:
            ok, note = _column_check(custom, settings['percent_column'], PERCENT_TYPES, '% read')
            pct_col = settings['percent_column'] if ok else None
            if note:
                res.notes.append(note)
        if settings['last_read_column']:
            ok, note = _column_check(custom, settings['last_read_column'], DATE_TYPES, 'last read')
            last_col = settings['last_read_column'] if ok else None
            if note:
                res.notes.append(note)
    res.progress_columns = [c for c in (pct_col, last_col) if c]

    if flag_col is None and not res.progress_columns:
        return res, flag_blob, pos_blob

    uuid_to_id = {}
    for book_id in db.all_book_ids():
        uuid = db.field_for('uuid', book_id)
        if uuid:
            uuid_to_id[uuid.lower()] = book_id

    lm_before = {}

    # Read flags: exactly as in 1.1.
    flag_updates, flag_records = {}, {}
    if flag_col:
        entries = parse_sidecar(flags_data)
        applied = load_applied(flag_blob)
        state = {}
        for uuid in entries:
            book_id = uuid_to_id.get(uuid)
            if book_id is None:
                continue
            value = db.field_for(flag_col, book_id)
            lm_before[uuid] = _lm(db, book_id)
            stamp, lm = applied.get(uuid, (None, None))
            state[uuid] = (None if value is None else bool(value), stamp, lm, lm_before[uuid])
        flag_updates, flag_records, skipped, unmatched = plan(entries, state)
        res.flags_skipped = len(skipped)
        res.unmatched.update(unmatched)

    # Reading progress: % read and last read, from one position entry.
    pos_updates, pos_records = {}, {}
    if res.progress_columns:
        positions, res.below_threshold = parse_positions(pos_data, settings['progress_threshold'])
        pct_meta = custom.get(pct_col) or {}
        number_format = (pct_meta.get('display') or {}).get('number_format')
        applied = load_applied(pos_blob)
        targets, state = {}, {}
        for uuid, pos in positions.items():
            book_id = uuid_to_id.get(uuid)
            if book_id is None:
                res.unmatched.add(uuid)
                continue
            want_pct = (percent_for_column(pos.progress, pct_meta['datatype'], number_format)
                        if pct_col else None)
            want_last = last_read_value(pos.stamp) if last_col else None
            if want_pct is None and want_last is None:
                continue
            # Compare only the parts this entry can write; a None part is never written.
            have_pct = db.field_for(pct_col, book_id) if want_pct is not None else None
            have_last = _norm_date(db.field_for(last_col, book_id)) if want_last is not None else None
            targets[uuid] = Target((want_pct, want_last), pos.stamp, pos.dated)
            lm_before.setdefault(uuid, _lm(db, book_id))
            stamp, lm = applied.get(uuid, (None, None))
            if (pos.stamp is not None and stamp is None and lm is None
                    and have_pct is None and have_last is None):
                # Never written by us and the columns are empty: there is no
                # desktop edit to protect. (Undated entries bootstrap through
                # decide's rule 1 instead.) Without this, any later change to
                # the book (often our own read-flag write) would keep an older
                # position out for good, e.g. a book finished in September
                # never gets its 100 % when the column is added in October.
                lm = lm_before[uuid]
            state[uuid] = ((have_pct, have_last), stamp, lm, lm_before[uuid])
        pos_updates, pos_records, skipped, _ = plan(targets, state)
        res.progress_skipped = len(skipped)

    # Writes. Every set_field bumps the books' last_modified.
    if flag_updates:
        db.set_field(flag_col, {uuid_to_id[u]: v for u, v in flag_updates.items()})
        res.columns_written.append(flag_col)
    pct_map = {uuid_to_id[u]: v[0] for u, v in pos_updates.items() if v[0] is not None}
    last_map = {uuid_to_id[u]: v[1] for u, v in pos_updates.items() if v[1] is not None}
    if pct_map:
        db.set_field(pct_col, pct_map)
        res.columns_written.append(pct_col)
    if last_map:
        db.set_field(last_col, last_map)
        res.columns_written.append(last_col)
    res.flags_updated = len(flag_updates)
    res.progress_updated = len(pos_updates)

    # Bookkeeping, with each book's last_modified as it stands after ALL writes.
    written = set(flag_updates) | set(pos_updates)
    lm_after = {u: _lm(db, uuid_to_id[u]) for u in set(flag_records) | set(pos_records) | written}
    for uuid, entry in flag_records.items():
        store_applied(flag_blob, uuid, entry.stamp, lm_after[uuid])
    for uuid, entry in pos_records.items():
        store_applied(pos_blob, uuid, entry.stamp, lm_after[uuid])
    # Our write for one kind of entry must not look like a desktop edit to the other.
    for uuid in written:
        if uuid not in flag_records:
            refresh_lm(flag_blob, uuid, lm_before.get(uuid), lm_after[uuid])
        if uuid not in pos_records:
            refresh_lm(pos_blob, uuid, lm_before.get(uuid), lm_after[uuid])
    return res, flag_blob, pos_blob


# ---------------------------------------------------------------------------
# Self-check: `python sync_core.py`
# ---------------------------------------------------------------------------

if __name__ == '__main__':  # pragma: no cover
    import datetime
    import shutil
    import tempfile
    import unittest

    class FakeFieldMetadata(object):
        def __init__(self, custom):
            self.custom = custom

        def custom_field_metadata(self):
            return dict(self.custom)

    class FakeDB(object):
        '''Just enough of calibre's Cache: set_field bumps last_modified to a
        clock that moves one second per write, with microseconds, as calibre's
        does.'''

        def __init__(self, custom, books):
            self.field_metadata = FakeFieldMetadata(custom)
            self.values = {}               # (field, book_id) -> value
            self.clock = datetime.datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=UTC)
            self.writes = []
            for book_id, uuid in books.items():
                self.values[('uuid', book_id)] = uuid
                self.values[('last_modified', book_id)] = self.clock
            self.prefs = {}

        def all_book_ids(self):
            return {b for (f, b) in self.values if f == 'uuid'}

        def field_for(self, name, book_id, default_value=None):
            return self.values.get((name, book_id), default_value)

        def tick(self, seconds=1):
            self.clock += datetime.timedelta(seconds=seconds, microseconds=777)
            return self.clock

        def set_field(self, name, mapping):
            now = self.tick()
            for book_id, value in mapping.items():
                self.values[(name, book_id)] = value
                self.values[('last_modified', book_id)] = now
            self.writes.append((name, dict(mapping)))
            return set(mapping)

        def desktop_edit(self, name, book_id, value):
            self.set_field(name, {book_id: value})

        def pref(self, name, default=None):
            return self.prefs.get(name, default)

        def set_pref(self, name, value):
            self.prefs[name] = value

    COLS = {
        '#read_status': {'datatype': 'bool', 'name': 'Read Status'},
        '#read_percent': {'datatype': 'int', 'name': 'Read %', 'display': {'number_format': '{0:d}%'}},
        '#last_read': {'datatype': 'datetime', 'name': 'Last Read'},
        '#date_read': {'datatype': 'datetime', 'name': 'Date Read'},
        '#ko_progfloat': {'datatype': 'float', 'name': 'KO progress', 'display': {'number_format': '{:.0%}'}},
        '#genre': {'datatype': 'text', 'name': 'Genre', 'is_multiple': True},
    }
    SETTINGS = {'version': 1, 'percent_column': '#read_percent',
                'last_read_column': '#last_read', 'progress_threshold': 1}

    def stamp(day, hour=12):
        return '2026-10-{:02d}T{:02d}:00:00.250Z'.format(day, hour)

    class Base(unittest.TestCase):
        def setUp(self):
            self.dir = tempfile.mkdtemp()
            self.db = FakeDB(COLS, {1: 'uuid-a', 2: 'uuid-b', 3: 'uuid-c'})
            self.flag_blob, self.pos_blob = {}, {}

        def tearDown(self):
            shutil.rmtree(self.dir)

        def write(self, name, doc):
            with open(os.path.join(self.dir, name), 'w', encoding='utf-8') as f:
                json.dump(doc, f)

        def positions(self, **entries):
            self.write(POSITIONS_FILE, {'schema': 1, 'updated_at': stamp(1), 'positions': entries})

        def flags(self, **entries):
            self.write(SYNC_FILE, {'schema': 1, 'updated_at': stamp(1), 'books': entries})

        def sync(self, settings=SETTINGS):
            res, self.flag_blob, self.pos_blob = run_sync(
                self.db, self.dir, settings, self.flag_blob, self.pos_blob)
            return res

        def v(self, name, book_id):
            return self.db.field_for(name, book_id)

    class ProgressTests(Base):
        def test_first_run_writes_percent_and_last_read_above_threshold(self):
            self.positions(**{'UUID-A': {'progress': 0.47395, 'updated_at': stamp(5)},
                              'uuid-b': {'progress': 0.004, 'updated_at': stamp(5)}})
            res = self.sync()
            self.assertEqual(self.v('#read_percent', 1), 47)
            self.assertEqual(self.v('#last_read', 1),
                             datetime.datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC))
            self.assertIsNone(self.v('#read_percent', 2))       # a peek, under 1%
            self.assertEqual((res.progress_updated, res.below_threshold), (1, 1))

        def test_second_run_is_a_no_op(self):
            self.positions(**{'uuid-a': {'progress': 0.5, 'updated_at': stamp(5)}})
            self.sync()
            n = len(self.db.writes)
            res = self.sync()
            self.assertEqual(len(self.db.writes), n)
            self.assertEqual(res.progress_updated, 0)

        def test_newer_position_applies(self):
            self.positions(**{'uuid-a': {'progress': 0.5, 'updated_at': stamp(5)}})
            self.sync()
            self.db.tick(3600 * 24 * 3)
            self.positions(**{'uuid-a': {'progress': 0.62, 'updated_at': '2026-10-09T09:00:00Z'}})
            self.sync()
            self.assertEqual(self.v('#read_percent', 1), 62)

        def test_desktop_edit_beats_an_older_position(self):
            self.positions(**{'uuid-a': {'progress': 0.5, 'updated_at': stamp(1, 13)}})
            self.sync()                                   # clock: 2026-10-01 12:00:01
            self.db.tick(3600 * 48)                       # Oct 3: user types 90 in calibre
            self.db.desktop_edit('#read_percent', 1, 90)
            self.positions(**{'uuid-a': {'progress': 0.55, 'updated_at': stamp(2)}})
            res = self.sync()
            self.assertEqual(self.v('#read_percent', 1), 90)
            self.assertEqual(res.progress_skipped, 1)

        def test_empty_column_takes_an_older_position(self):
            # Finished on the phone Oct 1 13:00; the read flag synced at once
            # (bumping last_modified); the % column is added days later.
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(1, 13)}})
            self.sync({})
            self.db.tick(3600 * 24 * 10)
            self.db.desktop_edit('#genre', 1, 'Fiction')   # any later calibre edit
            self.positions(**{'uuid-a': {'progress': 1.0, 'updated_at': stamp(1, 13)}})
            res = self.sync()
            self.assertEqual(self.v('#read_percent', 1), 100)
            self.assertEqual(res.progress_updated, 1)

        def test_value_from_another_tool_beats_an_older_position(self):
            # Kobo Utilities (say) wrote 80 on Oct 5; our position is from Oct 2.
            self.db.tick(3600 * 24 * 4)
            self.db.desktop_edit('#read_percent', 1, 80)
            self.positions(**{'uuid-a': {'progress': 0.3, 'updated_at': stamp(2)}})
            res = self.sync()
            self.assertEqual(self.v('#read_percent', 1), 80)
            self.assertEqual(res.progress_skipped, 1)

        def test_own_progress_write_does_not_block_a_late_read_flag(self):
            # Run 1 applies a flag; run 2 writes only % (bumping last_modified);
            # then a flag entry stamped BEFORE run 2 arrives late via the cloud.
            # It must still apply: run 2 was our write, not a desktop edit.
            self.flags(**{'uuid-a': {'value': False, 'updated_at': stamp(1, 13)}})
            self.assertEqual(self.sync().flags_updated, 1)
            self.db.tick(3600 * 24 * 5)                   # Oct 6
            self.positions(**{'uuid-a': {'progress': 0.3, 'updated_at': stamp(6, 10)}})
            self.sync()                                   # writes % at Oct 6 12:00
            self.db.tick(3600)
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(6, 11)}})
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), True)
            self.assertEqual(res.flags_updated, 1)

        def test_threshold_setting(self):
            self.positions(**{'uuid-a': {'progress': 0.03, 'updated_at': stamp(5)}})
            self.sync(dict(SETTINGS, progress_threshold=5))
            self.assertIsNone(self.v('#read_percent', 1))
            self.sync(dict(SETTINGS, progress_threshold=2))
            self.assertEqual(self.v('#read_percent', 1), 3)

        def test_fraction_float_column(self):
            self.positions(**{'uuid-a': {'progress': 0.47391, 'updated_at': stamp(5)}})
            self.sync(dict(SETTINGS, percent_column='#ko_progfloat', last_read_column=''))
            self.assertEqual(self.v('#ko_progfloat', 1), 0.4739)
            self.assertIsNone(self.v('#last_read', 1))

        def test_undated_position_writes_percent_but_no_date(self):
            self.write(POSITIONS_FILE, {'positions': {'uuid-a': {'progress': 0.4}}})
            self.sync()
            self.assertEqual(self.v('#read_percent', 1), 40)
            self.assertIsNone(self.v('#last_read', 1))

        def test_nothing_configured_writes_nothing(self):
            self.positions(**{'uuid-a': {'progress': 0.5, 'updated_at': stamp(5)}})
            res = self.sync({})
            self.assertEqual(self.db.writes, [])
            self.assertEqual(res.summary(), 'Nothing to sync.')

        def test_missing_and_wrong_type_columns_are_reported_not_written(self):
            self.positions(**{'uuid-a': {'progress': 0.5, 'updated_at': stamp(5)}})
            res = self.sync(dict(SETTINGS, percent_column='#nope', last_read_column='#read_percent'))
            self.assertEqual(self.db.writes, [])
            self.assertEqual(len(res.notes), 2)
            self.assertIn('restart calibre', res.notes[0])
            self.assertIn('wrong type', res.notes[1])

        def test_half_written_positions_file_does_not_stop_read_flags(self):
            self.flags(**{'uuid-b': {'value': True, 'updated_at': stamp(5)}})
            with open(os.path.join(self.dir, POSITIONS_FILE), 'w') as f:
                f.write('{"positions": {"uuid-a": {"progr')
            res = self.sync()
            self.assertIs(self.v('#read_status', 2), True)
            self.assertTrue(any('could not be read' in n for n in res.notes))

        def test_unknown_books_are_counted_once(self):
            self.flags(**{'uuid-z': True})
            self.positions(**{'uuid-z': {'progress': 0.5, 'updated_at': stamp(5)}})
            res = self.sync()
            self.assertEqual(res.unmatched, {'uuid-z'})

    class ReadFlagTests(Base):
        def test_missing_read_column_still_syncs_progress(self):
            custom = {k: v for k, v in COLS.items() if k != '#read_status'}
            self.db.field_metadata = FakeFieldMetadata(custom)
            self.flags(**{'uuid-a': True})
            self.positions(**{'uuid-a': {'progress': 0.5, 'updated_at': stamp(5)}})
            res = self.sync()
            self.assertEqual(self.v('#read_percent', 1), 50)
            self.assertIn('no #read_status column', res.notes[0])

        def test_wrong_type_read_column_is_refused(self):
            custom = dict(COLS, **{'#read_status': {'datatype': 'text', 'name': 'Read Status'}})
            self.db.field_metadata = FakeFieldMetadata(custom)
            self.flags(**{'uuid-a': True})
            res = self.sync()
            self.assertIsNone(self.v('#read_status', 1))
            self.assertIn('needs to be Yes/No', res.notes[0])

        def test_flags_alone_behave_as_before(self):
            self.flags(**{'uuid-a': True, 'uuid-b': {'value': False, 'updated_at': stamp(5)}})
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), True)
            self.assertIs(self.v('#read_status', 2), False)
            self.assertEqual(res.flags_updated, 2)
            self.assertEqual(self.sync().flags_updated, 0)

        def test_missing_read_column_explains_copying_an_existing_one(self):
            custom = {k: v for k, v in COLS.items() if k != '#read_status'}
            custom['#read'] = {'datatype': 'bool', 'name': 'Read'}
            custom['#finished'] = {'datatype': 'bool', 'name': 'Finished'}
            self.db.field_metadata = FakeFieldMetadata(custom)
            self.flags(**{'uuid-a': True})
            res = self.sync()
            self.assertIsNone(self.v('#read', 1))            # never written
            self.assertEqual(len(res.notes), 2)
            hint = res.notes[1]
            self.assertIn('Read (#read)', hint)
            self.assertIn('also: Finished (#finished)', hint)
            self.assertIn('#read:yes', hint)
            self.assertIn('#read:no', hint)
            self.assertNotIn(':true', hint)
            self.assertIn('next update', hint)

        def test_no_hint_without_a_likely_column(self):
            custom = {k: v for k, v in COLS.items() if k != '#read_status'}
            custom['#unread'] = {'datatype': 'bool', 'name': 'Unread'}
            custom['#to_read'] = {'datatype': 'bool', 'name': 'To read'}
            custom['#read_text'] = {'datatype': 'text', 'name': 'Read'}
            self.db.field_metadata = FakeFieldMetadata(custom)
            self.flags(**{'uuid-a': True})
            res = self.sync()
            self.assertEqual(len(res.notes), 1)
            self.assertEqual(read_column_copy_hint(custom), '')

        def test_existing_read_columns(self):
            cols = {'#read_status': {'datatype': 'bool', 'name': 'Read Status'},
                    '#ko_statusbool': {'datatype': 'bool', 'name': 'KOReader status'},
                    '#done': {'datatype': 'bool', 'name': 'Done'},
                    '#read': {'datatype': 'bool', 'name': 'Have read'},
                    '#tbr': {'datatype': 'bool', 'name': 'TBR'},
                    '#owned': {'datatype': 'bool', 'name': 'Owned'}}
            self.assertEqual([c for c, _ in existing_read_columns(cols)],
                             ['#read', '#done', '#ko_statusbool'])

    class SettingsTests(unittest.TestCase):
        def test_normalise(self):
            self.assertEqual(normalise_settings(None)['progress_threshold'], 1)
            s = normalise_settings({'percent_column': 'read_percent', 'last_read_column': '#Bad',
                                    'progress_threshold': 500})
            self.assertEqual((s['percent_column'], s['last_read_column'], s['progress_threshold']),
                             ('', '', 50))
            self.assertIsNone(s['version'])
            self.assertEqual(settings_to_store('#read_percent', None, 2),
                             {'version': 1, 'percent_column': '#read_percent',
                              'last_read_column': '', 'progress_threshold': 2})

        def test_eligible_and_suggested(self):
            self.assertEqual([c for c, _ in eligible_columns(COLS, 'percent')],
                             ['#ko_progfloat', '#read_percent'])
            self.assertEqual(suggest_column(COLS, 'percent'), '#read_percent')
            # A "Date Read" column means finished, not last read: never suggested.
            self.assertEqual(suggest_column(COLS, 'last_read'), '#last_read')
            no_last = {k: v for k, v in COLS.items() if k != '#last_read'}
            self.assertEqual(suggest_column(no_last, 'last_read'), '')

        def test_suggests_existing_conventions(self):
            kobo = {'#percent_read': {'datatype': 'int', 'name': 'Percent Read'},
                    '#last_read_ts': {'datatype': 'datetime', 'name': 'Last Read Timestamp'}}
            self.assertEqual(suggest_column(kobo, 'percent'), '#percent_read')
            self.assertEqual(suggest_column(kobo, 'last_read'), '#last_read_ts')
            ko = {'#ko_progint': {'datatype': 'int', 'name': 'KOReader Progress'}}
            self.assertEqual(suggest_column(ko, 'percent'), '#ko_progint')

    unittest.main(verbosity=2)

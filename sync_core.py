# Last modified: 2026-10-22--2048
#
# sync_core.py — one sync run, without the GUI.
#
# Reads the two sidecars the CalibReader apps keep in the library folder and
# writes what wins into calibre:
#   CalibReader_sync.json      -> the read flag, into a Yes/No column the user
#                                 picked (1.3.0; #read_status by default)
#   CalibReader_positions.json -> % read and last-read date, into columns the
#                                 user picked
# The choices are plugin settings, stored in the library's own metadata.db
# preferences. From the app to calibre, plus two writes into
# CalibReader_sync.json (update_sync_file): when a calibre edit of the read flag
# beats a phone entry, calibre's value (1.2.4), so the app follows it; and on
# each entry the plugin applies, `applied_value` + `applied_at` (1.3.0), so the
# app can tell a later calibre edit from the plugin's own write.
# CalibReader_positions.json is never written.
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
        APPLY, KEY_APPLIED_AT, KEY_APPLIED_VALUE, KEY_UPDATED_AT, KEY_VALUE, NOT_SEEN, RECORD,
        UTC, Target, ceil_ms, decide_flag, format_iso, last_read_value, load_applied,
        load_flag_records, load_pending_stamps, parse_iso, parse_positions, parse_sidecar,
        percent_for_column, plan, refresh_lm, store_applied, store_flag_record, writeback_for,
    )
except ImportError:  # pragma: no cover - standalone / test import
    from sidecar_merge import (
        APPLY, KEY_APPLIED_AT, KEY_APPLIED_VALUE, KEY_UPDATED_AT, KEY_VALUE, NOT_SEEN, RECORD,
        UTC, Target, ceil_ms, decide_flag, format_iso, last_read_value, load_applied,
        load_flag_records, load_pending_stamps, parse_iso, parse_positions, parse_sidecar,
        percent_for_column, plan, refresh_lm, store_applied, store_flag_record, writeback_for,
    )
import datetime as _dt
import time as _time
import uuid as _uuid

SYNC_FILE = 'CalibReader_sync.json'
POSITIONS_FILE = 'CalibReader_positions.json'

READ_COLUMN = '#read_status'        # the default read column (the only one before 1.3.0)

# --- settings (library metadata.db preferences) ------------------------------
#
# Stored with db.set_pref(SETTINGS_KEY, {...}) so they travel with the library
# to every computer and every phone that opens it. Schema:
#   {"version": 1, "read_column": "#read_status",
#    "percent_column": "#read_percent" | "", "last_read_column": "#last_read" | "",
#    "progress_threshold": 1, "auto_sync": true}
# "" means "don't sync". `version` is present once the user has saved the
# dialog; until then the dialog may pre-select a suggested column.
# "read_column" (1.3.0): the read flag's Yes/No column, a lookup name with '#'.
# Missing or malformed means #read_status. If the column is missing from the
# library or is not Yes/No, the sync uses #read_status instead and says so; the
# apps (1.0.1 and later) read the same key and fall back the same way.

SETTINGS_KEY = 'calibreader_sync'
SETTINGS_VERSION = 1
DEFAULT_THRESHOLD = 1
MAX_THRESHOLD = 50

PERCENT_TYPES = ('int', 'float')
DATE_TYPES = ('datetime',)
READ_TYPES = ('bool',)
_KIND_TYPES = {'percent': PERCENT_TYPES, 'last_read': DATE_TYPES, 'read': READ_TYPES}

_LOOKUP_RE = re.compile(r'^#[a-z][a-z0-9_]*$')


def normalise_settings(raw):
    '''Whatever is stored (possibly nothing, possibly hand-edited) -> a valid
    settings dict. A malformed value falls back to "don't sync", never to a
    guess: a guessed column is a write into the user's data.'''
    raw = raw if isinstance(raw, dict) else {}
    out = {
        'version': raw.get('version') if isinstance(raw.get('version'), int) else None,
        'read_column': READ_COLUMN,     # 1.3.0
        'percent_column': '',
        'last_read_column': '',
        'progress_threshold': DEFAULT_THRESHOLD,
        'auto_sync': True,              # 1.2.5: on unless the user turned it off
    }
    if isinstance(raw.get('auto_sync'), bool):
        out['auto_sync'] = raw['auto_sync']
    for key in ('read_column', 'percent_column', 'last_read_column'):
        value = raw.get(key)
        if isinstance(value, str) and _LOOKUP_RE.match(value):
            out[key] = value
    threshold = raw.get('progress_threshold')
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        out['progress_threshold'] = min(MAX_THRESHOLD, max(0, threshold))
    return out


def settings_to_store(percent_column, last_read_column, progress_threshold, auto_sync=True,
                      read_column=READ_COLUMN):
    return normalise_settings({
        'version': SETTINGS_VERSION,
        'read_column': read_column or READ_COLUMN,
        'percent_column': percent_column or '',
        'last_read_column': last_read_column or '',
        'progress_threshold': progress_threshold,
        'auto_sync': bool(auto_sync),
    })


def eligible_columns(custom_columns, kind):
    '''[(lookup, heading)] of the custom columns that can hold `kind`
    ('percent', 'last_read' or 'read'), sorted by heading.'''
    types = _KIND_TYPES[kind]
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


# --- the read column (1.3.0) ---------------------------------------------------
#
# Many libraries already track read status in their own Yes/No column (#read,
# #finished, KOReader Sync's #ko_statusbool). From 1.3.0 the user picks the read
# column in Settings ("read_column"), and the apps follow the same choice. Until
# the user has chosen, Settings offers #read_status if the library has it, else
# the existing column that looks most like a read flag. Nothing is ever picked
# without the user clicking OK in Settings.

_READ_FLAG_RE = re.compile(r'read|finish|done|complete|statusbool', re.I)
_NOT_READ_RE = re.compile(r'unread|not.?read|to.?read|want|tbr|wish', re.I)
# Most likely first: "#read", a heading "Read" / "Have read", then finished, done...
_READ_RANK = [re.compile(p, re.I) for p in (
    r'^#read$', r'^read$|have.?read', r'finish', r'done', r'complete', r'read', r'statusbool')]


def _read_rank(lookup, heading):
    for rank, rx in enumerate(_READ_RANK):
        if rx.search(lookup) or rx.search(heading):
            return rank
    return len(_READ_RANK)


def existing_read_columns(custom_columns, exclude=READ_COLUMN):
    '''[(lookup, heading)] of Yes/No columns, other than `exclude` (one lookup name
    or several), whose lookup name or heading looks like a read flag (not "unread"
    / "to read"), the most likely first.'''
    excluded = (exclude,) if isinstance(exclude, str) else tuple(exclude or ())
    out = []
    for lookup, meta in (custom_columns or {}).items():
        if lookup in excluded or not isinstance(meta, dict) or meta.get('datatype') != 'bool':
            continue
        heading = meta.get('name') or lookup
        names = lookup + ' ' + heading
        if _READ_FLAG_RE.search(names) and not _NOT_READ_RE.search(names):
            out.append((lookup, heading))
    return sorted(out, key=lambda x: (_read_rank(*x), x[1].lower(), x[0]))


def suggest_read_column(custom_columns):
    '''The read column Settings offers before the user has chosen one: #read_status
    if the library has it as Yes/No, else the likeliest existing read column, else
    #read_status (to be created).'''
    meta = (custom_columns or {}).get(READ_COLUMN)
    if isinstance(meta, dict) and meta.get('datatype') in READ_TYPES:
        return READ_COLUMN
    cols = existing_read_columns(custom_columns)
    return cols[0][0] if cols else READ_COLUMN


def read_column_hint(custom_columns, exclude=READ_COLUMN, where='in Settings'):
    '''One short line naming the library's own read column(s), or "".'''
    cols = existing_read_columns(custom_columns, exclude)
    if not cols:
        return ''
    lookup, heading = cols[0]
    also = ''
    if len(cols) > 1:
        also = ' (also: {})'.format(', '.join('{} ({})'.format(h, l) for l, h in cols[1:]))
    return ('This library already has a Yes/No column that looks like a read flag: {} ({}){}. '
            'To use it, pick it as the read status column {}.').format(heading, lookup, also, where)


def _a_type(datatype):
    datatype = str(datatype)
    return '{} {} column'.format('an' if datatype[:1] in 'aeiou' else 'a', datatype)


def _column_problem(custom_columns, lookup):
    '''Why `lookup` can't hold the read flag, or None when it can.'''
    meta = custom_columns.get(lookup)
    if not isinstance(meta, dict):
        return 'does not exist (restart calibre if you just created it)'
    if meta.get('datatype') not in READ_TYPES:
        return 'is {}, not Yes/No'.format(_a_type(meta.get('datatype')))
    return None


def resolve_read_column(custom_columns, chosen):
    '''(column to write, [notes]). The chosen column when it is a Yes/No column of
    this library; otherwise #read_status, as the apps do, with a note saying so;
    None (with a note) when there is no usable column at all.'''
    custom_columns = custom_columns or {}
    notes = []
    if chosen and chosen != READ_COLUMN:
        why = _column_problem(custom_columns, chosen)
        if why is None:
            return chosen, notes
        fallback = _column_problem(custom_columns, READ_COLUMN)
        if fallback is None:
            notes.append('Read status went into {} instead: the column chosen in Settings, {}, '
                         '{}.'.format(READ_COLUMN, chosen, why))
            return READ_COLUMN, notes
        notes.append('Read status was not synced: the column chosen in Settings, {}, {}; and '
                     '{}, used instead, {}. Pick a Yes/No column in Settings.'.format(
                         chosen, why, READ_COLUMN, fallback))
        return None, notes
    meta = custom_columns.get(READ_COLUMN)
    if not isinstance(meta, dict):
        notes.append('Read status was not synced: this library has no {} column. In Settings '
                     '(the arrow next to Sync Read Status), pick the Yes/No column you use for '
                     'read status, or create one there.'.format(READ_COLUMN))
        hint = read_column_hint(custom_columns)
        if hint:
            notes.append(hint)
        return None, notes
    if meta.get('datatype') not in READ_TYPES:
        notes.append('Read status was not synced: {} is {}, and it needs to be Yes/No. Pick a '
                     'Yes/No column in Settings.'.format(READ_COLUMN, _a_type(meta.get('datatype'))))
        return None, notes
    return READ_COLUMN, notes


# The read flag's per-book memory (sidecar_merge's records) is kept PER COLUMN
# (1.3.0): what the plugin last saw or applied in one column says nothing about
# another, so switching columns starts the new one from a clean slate and leaves
# the old one's memory for a switch back. In the plugin's prefs, #read_status
# keeps the key 1.2.x used ('applied'); other columns go under 'applied_cols'.

def flag_blobs_from_prefs(applied, applied_cols):
    '''Plugin prefs -> {column: {uuid: record}} for run_sync.'''
    blobs = {}
    if isinstance(applied_cols, dict):
        for column, blob in applied_cols.items():
            if isinstance(column, str) and column != READ_COLUMN and isinstance(blob, dict):
                blobs[column] = blob
    blobs[READ_COLUMN] = applied if isinstance(applied, dict) else {}
    return blobs


def flag_blobs_to_prefs(blobs):
    '''{column: {uuid: record}} -> (applied, applied_cols) for the plugin prefs.'''
    blobs = dict(blobs or {})
    return blobs.pop(READ_COLUMN, {}), blobs


# --- one run ------------------------------------------------------------------

class SyncResult(object):
    '''What a run did, for the summary dialog and the tests.'''

    def __init__(self):
        self.found_sync_file = False
        self.found_positions_file = False
        self.flags_updated = 0
        self.flags_skipped = 0
        self.flags_sent_back = 0       # 1.2.4: calibre edits written back for the app
        self.flags_stamped = 0         # 1.3.0: applied entries stamped applied_value / applied_at
        self.read_column = None        # 1.3.0: the column read status went into this run
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

    def status_line(self):
        '''(1.2.5) One short line for calibre's status bar after an AUTOMATIC run,
        or "" when nothing changed and nothing needs attention (a quiet run).'''
        parts = []
        if self.flags_updated:
            column = (' ({})'.format(self.read_column)
                      if self.read_column and self.read_column != READ_COLUMN else '')
            parts.append('{} read status{}'.format(self.flags_updated, column))
        if self.progress_updated:
            parts.append('{} reading progress'.format(self.progress_updated))
        if self.flags_sent_back:
            parts.append('{} passed back to the app'.format(self.flags_sent_back))
        line = ('updated ' + ', '.join(parts) + '.') if parts else ''
        if self.notes:
            line = (line + ' ' if line else '') + 'Something needs attention: click Sync Read Status for details.'
        return line

    def summary(self):
        lines = []
        if self.found_sync_file:
            column = ' ({})'.format(self.read_column) if self.read_column else ''
            lines.append('Read status{}: {} book(s) updated.'.format(column, self.flags_updated))
        if self.found_positions_file and self.progress_columns:
            lines.append('Reading progress: {} book(s) updated.'.format(self.progress_updated))
        skipped = self.flags_skipped + self.progress_skipped
        if skipped:
            lines.append('{} entr(y/ies) were already applied, or older than a change '
                         'made in calibre: left alone.'.format(skipped))
        if self.flags_sent_back:
            lines.append('{} read status change(s) made in calibre were passed back to the app.'.format(
                self.flags_sent_back))
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


def update_sync_file(library_path, writebacks=None, applied=None):
    '''Write into CalibReader_sync.json, following the apps' sidecar protocol
    (docs/PARITY.md, 2026-10-13): re-read the file right before writing and write
    only if it parses cleanly; touch only these books' entries; never remove a key;
    never touch the file-level updated_at (legacy bare entries inherit it); write a
    temp file and rename it in, with the backup swap when rename-over is refused.

    writebacks (1.2.4) = {uuid: (value, stamp)}: calibre's newer read values. The
        entry is replaced, only when ours is strictly newer.
    applied (1.3.0) = {uuid: (value, entry_stamp, applied_at)}: entries this plugin
        wrote into calibre. `applied_value` and `applied_at` are added to the entry,
        all its other keys kept, only while the file still holds that same entry
        (same value and stamp) as an object; a legacy bare bool is left alone.

    Returns ({uuid: stamp written back}, {uuid: applied_at stamped}, failed, note);
    `failed` is True when the file could not be read or written this time.'''
    path = os.path.join(library_path, SYNC_FILE)
    writebacks, applied = writebacks or {}, applied or {}
    retry = ('Read status set in calibre could not be passed back to the app this time '
             '({}); it will be tried again next sync.')

    def failure(why):
        return {}, {}, True, (retry.format(why) if writebacks else None)

    if not (writebacks or applied) or not os.path.exists(path):
        return {}, {}, False, None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return failure('the file is being written, or damaged')
    books = data.get('books') if isinstance(data, dict) else None
    if not isinstance(books, dict):
        return failure('the file has no books table')
    current = parse_sidecar(data)
    keys = {k.lower(): k for k in books if isinstance(k, str)}
    written, stamped, dirty = {}, {}, False
    for uuid, (value, stamp) in writebacks.items():
        have = current.get(uuid)
        if have is not None and have.stamp is not None and have.stamp >= stamp:
            continue                        # someone wrote a newer entry meanwhile
        books[keys.get(uuid, uuid)] = {KEY_VALUE: bool(value), KEY_UPDATED_AT: format_iso(stamp)}
        written[uuid] = stamp
        dirty = True
    for uuid, (value, entry_stamp, at) in applied.items():
        have, raw = current.get(uuid), books.get(keys.get(uuid))
        if (uuid in written or have is None or not isinstance(raw, dict) or have.value != bool(value)
                or format_iso(have.stamp) != format_iso(entry_stamp)):
            continue                        # replaced or removed meanwhile, or a bare bool
        want = {KEY_APPLIED_VALUE: bool(value), KEY_APPLIED_AT: format_iso(at)}
        if any(raw.get(k) != v for k, v in want.items()):
            raw.update(want)
            dirty = True
        stamped[uuid] = at
    if not dirty:
        return written, stamped, False, None
    tmp = '{}.crtmp.{}'.format(path, _uuid.uuid4().hex)
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        try:
            os.replace(tmp, path)
        except OSError:
            bak = '{}.crbak.{}.{}-1'.format(path, int(_time.time()), _uuid.uuid4().hex)
            os.rename(path, bak)
            os.rename(tmp, path)
            os.remove(bak)
    except OSError as exc:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return failure(exc)
    return written, stamped, False, None


def write_back_flags(library_path, writebacks):
    '''(1.2.4) update_sync_file with write-backs only: ({uuid: stamp written}, note).'''
    written, _, _, note = update_sync_file(library_path, writebacks)
    return written, note


def run_sync(db, library_path, settings, flag_blobs, pos_blob):
    '''One sync. Returns (SyncResult, new_flag_blobs, new_pos_blob); these are the
    plugin's per-book bookkeeping (see sidecar_merge), to be stored by the caller.
    `flag_blobs` = {read column: {uuid: record}} (flag_blobs_from_prefs); only the
    blob of the column used this run is read or changed. Nothing is written to
    calibre unless a rule says it may.'''
    settings = normalise_settings(settings)
    flag_blobs = {c: dict(b) for c, b in (flag_blobs or {}).items() if isinstance(b, dict)}
    flag_blob = {}
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
        flag_col, notes = resolve_read_column(custom, settings['read_column'])
        res.notes.extend(notes)
        res.read_column = flag_col
        if flag_col:
            flag_blob = flag_blobs.get(flag_col, {})

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
        return res, flag_blobs, pos_blob

    uuid_to_id = {}
    for book_id in db.all_book_ids():
        uuid = db.field_for('uuid', book_id)
        if uuid:
            uuid_to_id[uuid.lower()] = book_id

    lm_before = {}

    # Read flags (1.2.3): a calibre-side edit of the flag is a change of its VALUE
    # since the last run (sidecar_merge.decide_flag), so every book's value is
    # remembered, not only the books with a phone entry.
    flag_updates, flag_records, flag_state, writebacks, pending = {}, {}, {}, {}, {}
    now = _dt.datetime.now(UTC)
    if flag_col:
        entries = parse_sidecar(flags_data)
        records = load_flag_records(flag_blob)
        pending = {u: p for u, p in load_pending_stamps(flag_blob).items() if u in uuid_to_id}
        skipped = []
        for uuid, book_id in uuid_to_id.items():
            value = db.field_for(flag_col, book_id)
            value = None if value is None else bool(value)
            stamp, lm, seen, dt = records.get(uuid, (None, None, NOT_SEEN, None))
            book_lm = _lm(db, book_id)
            entry = entries.get(uuid)
            if entry is not None:
                lm_before[uuid] = book_lm
                verdict, dt = decide_flag(entry, value, stamp, lm, book_lm, seen, dt, now)
                wb = writeback_for(entry, verdict, value, dt)
                if wb is not None:
                    writebacks[uuid] = wb
                if verdict == APPLY:
                    flag_updates[uuid] = entry.value
                if verdict in (APPLY, RECORD):
                    flag_records[uuid] = entry
                else:
                    skipped.append(uuid)
            elif seen is not NOT_SEEN and value != seen and book_lm is not None:
                mark = book_lm if book_lm <= now else now
                dt = mark if dt is None or mark > dt else dt
            flag_state[uuid] = (stamp, lm, dt)
        res.flags_skipped = len(skipped)
        res.unmatched.update(u for u in entries if u not in uuid_to_id)

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
    # 1.3.0: each entry applied above gets applied_value + applied_at in the sync
    # file: the book's last_modified after ALL our writes, rounded up to the ms, so
    # only a later calibre edit can be newer. Stamps an earlier run could not write
    # (`pending`) are tried again; they stay pending only while the file fails.
    stamps = dict(pending)
    for uuid in flag_updates:
        entry = flag_records[uuid]
        stamps[uuid] = (entry.value, entry.stamp, ceil_ms(lm_after.get(uuid) or _dt.datetime.now(UTC)))
    sent, stamped, failed, note = update_sync_file(library_path, writebacks, stamps)
    res.flags_sent_back, res.flags_stamped = len(sent), len(stamped)
    keep = stamps if failed else {}
    if note:
        res.notes.append(note)
    for uuid, (stamp, lm, dt) in flag_state.items():
        if uuid in flag_records:
            stamp, lm = flag_records[uuid].stamp, lm_after[uuid]
        elif uuid in sent:
            stamp = sent[uuid]              # our own entry: never re-decided as a phone change
        value = db.field_for(flag_col, uuid_to_id[uuid])
        store_flag_record(flag_blob, uuid, stamp, lm, None if value is None else bool(value), dt,
                          keep.get(uuid))
    for uuid, entry in pos_records.items():
        store_applied(pos_blob, uuid, entry.stamp, lm_after[uuid])
    # Our write for one kind of entry must not look like a desktop edit to the other.
    for uuid in written:
        if uuid not in flag_records:
            refresh_lm(flag_blob, uuid, lm_before.get(uuid), lm_after[uuid])
        if uuid not in pos_records:
            refresh_lm(pos_blob, uuid, lm_before.get(uuid), lm_after[uuid])
    if flag_col:
        flag_blobs[flag_col] = flag_blob
    return res, flag_blobs, pos_blob


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
            self.flag_blobs, self.pos_blob = {}, {}

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
            res, self.flag_blobs, self.pos_blob = run_sync(
                self.db, self.dir, settings, self.flag_blobs, self.pos_blob)
            return res

        def read_file(self):
            with open(os.path.join(self.dir, SYNC_FILE), encoding='utf-8') as f:
                return json.load(f)

        def file_bytes(self):
            with open(os.path.join(self.dir, SYNC_FILE), 'rb') as f:
                return f.read()

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

        def test_missing_read_column_points_to_an_existing_one_in_settings(self):
            custom = {k: v for k, v in COLS.items() if k != '#read_status'}
            custom['#read'] = {'datatype': 'bool', 'name': 'Read'}
            custom['#finished'] = {'datatype': 'bool', 'name': 'Finished'}
            self.db.field_metadata = FakeFieldMetadata(custom)
            self.flags(**{'uuid-a': True})
            res = self.sync()
            self.assertIsNone(self.v('#read', 1))            # never written unless chosen
            self.assertEqual(len(res.notes), 2)
            self.assertIn('pick the Yes/No column you use', res.notes[0])
            hint = res.notes[1]
            self.assertIn('Read (#read)', hint)
            self.assertIn('also: Finished (#finished)', hint)
            self.assertIn('pick it as the read status column in Settings', hint)
            self.assertNotIn('#read:yes', hint)              # the 1.2.2 copy steps are gone

        def test_no_hint_without_a_likely_column(self):
            custom = {k: v for k, v in COLS.items() if k != '#read_status'}
            custom['#unread'] = {'datatype': 'bool', 'name': 'Unread'}
            custom['#to_read'] = {'datatype': 'bool', 'name': 'To read'}
            custom['#read_text'] = {'datatype': 'text', 'name': 'Read'}
            self.db.field_metadata = FakeFieldMetadata(custom)
            self.flags(**{'uuid-a': True})
            res = self.sync()
            self.assertEqual(len(res.notes), 1)
            self.assertEqual(read_column_hint(custom), '')

        def test_existing_read_columns(self):
            cols = {'#read_status': {'datatype': 'bool', 'name': 'Read Status'},
                    '#ko_statusbool': {'datatype': 'bool', 'name': 'KOReader status'},
                    '#done': {'datatype': 'bool', 'name': 'Done'},
                    '#read': {'datatype': 'bool', 'name': 'Have read'},
                    '#tbr': {'datatype': 'bool', 'name': 'TBR'},
                    '#owned': {'datatype': 'bool', 'name': 'Owned'}}
            self.assertEqual([c for c, _ in existing_read_columns(cols)],
                             ['#read', '#done', '#ko_statusbool'])

    class ReadFlagByValueTests(Base):
        '''1.2.3: an unrelated calibre edit no longer drops a phone toggle; a real
        calibre edit of the flag still wins over older phone entries.'''

        def test_rating_edit_after_toggle_does_not_drop_it(self):
            self.flags(**{'uuid-a': {'value': False, 'updated_at': stamp(2)}})
            self.sync()                                        # first contact: values recorded
            self.assertIs(self.v('#read_status', 1), False)
            self.db.tick(3600 * 24 * 2)                        # Oct 3
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(4)}})   # phone toggle, Oct 4
            self.db.clock = datetime.datetime(2026, 10, 5, 9, 0, 0, 1, tzinfo=UTC)
            self.db.desktop_edit('rating', 1, 8)               # unrelated calibre edit, Oct 5
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), True)     # 1.2.2 skipped this
            self.assertEqual(res.flags_updated, 1)

        def test_calibre_flag_edit_beats_an_older_toggle(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            self.sync()
            self.flags(**{'uuid-a': {'value': False, 'updated_at': stamp(4)}})  # phone, Oct 4
            self.db.clock = datetime.datetime(2026, 10, 5, 9, 0, 0, 1, tzinfo=UTC)
            self.db.desktop_edit('#read_status', 1, True)      # calibre, Oct 5 (value unchanged: True)
            self.db.desktop_edit('#read_status', 1, None)      # ... then cleared in calibre
            res = self.sync()
            self.assertIsNone(self.v('#read_status', 1))       # calibre's later edit stands
            self.assertEqual(res.flags_updated, 0)
            self.assertEqual(self.sync().flags_updated, 0)     # and keeps standing

        def test_newer_toggle_beats_an_earlier_calibre_edit(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            self.sync()
            self.db.clock = datetime.datetime(2026, 10, 3, 9, 0, 0, 1, tzinfo=UTC)
            self.db.desktop_edit('#read_status', 1, False)     # calibre, Oct 3
            self.sync()                                        # sees the edit, remembers when
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(6)}})   # phone, Oct 6
            self.db.clock = datetime.datetime(2026, 10, 7, 9, 0, 0, 1, tzinfo=UTC)
            self.db.desktop_edit('rating', 1, 6)               # unrelated, Oct 7
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), True)
            self.assertEqual(res.flags_updated, 1)

        def test_calibre_edit_seen_between_runs_without_an_entry(self):
            self.flags(**{'uuid-b': {'value': True, 'updated_at': stamp(2)}})
            self.sync()                                        # records book a's value (None) too
            self.db.clock = datetime.datetime(2026, 10, 3, 9, 0, 0, 1, tzinfo=UTC)
            self.db.desktop_edit('#read_status', 1, True)      # calibre marks a read, Oct 3
            self.sync()                                        # no entry for a: remembers the edit
            self.flags(**{'uuid-a': {'value': False, 'updated_at': stamp(2, 18)}})  # older phone entry
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), True)
            self.assertEqual(res.flags_updated, 0)

        def test_upgrade_from_122_records_keeps_the_old_rule_once(self):
            self.flag_blobs = flag_blobs_from_prefs(
                {'uuid-a': {'stamp': stamp(1), 'lm': '2026-10-01T12:00:00.123Z'}}, None)
            self.db.values[('#read_status', 1)] = True
            self.flags(**{'uuid-a': {'value': False, 'updated_at': stamp(4)}})
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), False)
            self.assertIn('val', self.flag_blobs['#read_status']['uuid-a'])

    class WriteBackTests(Base):
        '''1.2.4: a calibre edit made after the plugin applied a phone entry, before the
        phone synced again, is passed back through the sync file so both sides agree.'''

        def test_calibre_edit_after_apply_is_written_back_once(self):
            self.flags(**{'uuid-a': {'value': False, 'updated_at': stamp(2)}, 'uuid-b': True})
            self.sync()                                        # applies No to a, Yes to b
            self.assertIs(self.v('#read_status', 1), False)
            self.db.clock = datetime.datetime(2026, 10, 5, 9, 0, 0, 1, tzinfo=UTC)
            self.db.desktop_edit('#read_status', 1, True)      # the user ticks it in calibre
            edited_at = format_iso(self.v('last_modified', 1))   # stamped with the edit's time
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), True)     # calibre's later edit stands
            self.assertEqual(res.flags_sent_back, 1)
            doc = self.read_file()
            # replaced whole: the earlier apply's applied_* stamps go with the old entry
            self.assertEqual(doc['books']['uuid-a'], {'value': True, 'updated_at': edited_at})
            self.assertIs(doc['books']['uuid-b'], True)        # other keys untouched (bare bool kept)
            self.assertEqual(doc['updated_at'], stamp(1))      # file-level stamp untouched
            before = self.file_bytes()
            res = self.sync()                                  # stable: nothing more to do
            self.assertEqual((res.flags_updated, res.flags_sent_back), (0, 0))
            self.assertEqual(self.file_bytes(), before)
            self.assertFalse([f for f in os.listdir(self.dir) if '.crtmp.' in f or '.crbak.' in f])

        def test_no_write_back_when_calibre_cleared_the_flag(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            self.sync()
            self.db.clock = datetime.datetime(2026, 10, 5, 9, 0, 0, 1, tzinfo=UTC)
            self.db.desktop_edit('#read_status', 1, None)
            res = self.sync()
            self.assertEqual(res.flags_sent_back, 0)
            self.assertEqual(self.read_file()['books']['uuid-a']['value'], True)

        def test_no_write_back_for_an_unrelated_edit(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            self.sync()
            self.db.clock = datetime.datetime(2026, 10, 5, 9, 0, 0, 1, tzinfo=UTC)
            self.db.desktop_edit('rating', 1, 8)
            res = self.sync()
            self.assertEqual(res.flags_sent_back, 0)

        def test_newer_entry_in_the_file_is_not_overwritten(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(9)}})
            written, note = write_back_flags(self.dir, {'uuid-a': (False, parse_iso(stamp(5)))})
            self.assertEqual((written, note), ({}, None))
            self.assertEqual(self.read_file()['books']['uuid-a']['value'], True)

        def test_damaged_file_is_left_alone(self):
            with open(os.path.join(self.dir, SYNC_FILE), 'w', encoding='utf-8') as f:
                f.write('{"schema": 1, "books": {"uuid-a": tr')
            written, note = write_back_flags(self.dir, {'uuid-a': (False, parse_iso(stamp(5)))})
            self.assertEqual(written, {})
            self.assertIn('tried again next sync', note)
            with open(os.path.join(self.dir, SYNC_FILE), encoding='utf-8') as f:
                self.assertTrue(f.read().endswith('tr'))

    class ReadColumnTests(Base):
        '''1.3.0: the user picks the read column; the memory is kept per column.'''

        CUSTOM = dict(COLS, **{'#read': {'datatype': 'bool', 'name': 'Have read'},
                               '#finished': {'datatype': 'datetime', 'name': 'Finished'}})

        def setUp(self):
            Base.setUp(self)
            self.db.field_metadata = FakeFieldMetadata(self.CUSTOM)

        def test_chosen_column_gets_the_read_flag(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}, 'uuid-b': False})
            res = self.sync(dict(SETTINGS, read_column='#read'))
            self.assertIs(self.v('#read', 1), True)
            self.assertIs(self.v('#read', 2), False)
            self.assertIsNone(self.v('#read_status', 1))     # the default column is left alone
            self.assertEqual((res.read_column, res.notes), ('#read', []))
            self.assertIn('Read status (#read): 2 book(s) updated.', res.summary())
            self.assertEqual(res.status_line(), 'updated 2 read status (#read).')
            self.assertEqual(sorted(self.flag_blobs), ['#read'])

        def test_non_bool_choice_falls_back_to_read_status_with_a_note(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            res = self.sync(dict(SETTINGS, read_column='#finished'))
            self.assertIs(self.v('#read_status', 1), True)
            self.assertIsNone(self.v('#finished', 1))         # a Date column is never written
            self.assertEqual(res.read_column, '#read_status')
            self.assertEqual(len(res.notes), 1)
            self.assertIn('went into #read_status instead', res.notes[0])
            self.assertIn('#finished, is a datetime column, not Yes/No', res.notes[0])

        def test_missing_choice_falls_back_and_says_restart(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            res = self.sync(dict(SETTINGS, read_column='#done'))
            self.assertIs(self.v('#read_status', 1), True)
            self.assertIn('#done, does not exist (restart calibre', res.notes[0])

        def test_no_usable_column_at_all_writes_nothing(self):
            custom = {k: v for k, v in self.CUSTOM.items() if k != '#read_status'}
            self.db.field_metadata = FakeFieldMetadata(custom)
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            res = self.sync(dict(SETTINGS, read_column='#genre'))
            self.assertEqual([w for w in self.db.writes if w[0] in ('#genre', '#read_status')], [])
            self.assertIsNone(res.read_column)
            self.assertIn('was not synced', res.notes[0])
            self.assertIn('#read_status, used instead, does not exist', res.notes[0])

        def test_changing_the_read_column_does_not_carry_the_memory_over(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            self.assertEqual(self.sync().flags_updated, 1)    # into #read_status
            # The user picks #read (empty). With one shared memory the entry would be
            # "already applied" and #read would never get it.
            res = self.sync(dict(SETTINGS, read_column='#read'))
            self.assertIs(self.v('#read', 1), True)
            self.assertEqual(res.flags_updated, 1)
            self.assertEqual(sorted(self.flag_blobs), ['#read', '#read_status'])
            # ... and #read's memory does not leak back: a calibre edit of #read_status
            # made meanwhile is still seen as one when the user switches back.
            self.db.clock = datetime.datetime(2026, 10, 5, 9, 0, 0, 1, tzinfo=UTC)
            self.db.desktop_edit('#read_status', 1, False)
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(4)}})   # older than the edit
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), False)   # calibre's later edit stands
            self.assertEqual((res.flags_updated, res.flags_sent_back), (0, 1))

        def test_prefs_mapping_keeps_read_status_where_122_had_it(self):
            blobs = flag_blobs_from_prefs({'u': {'stamp': None}}, {'#read': {'v': {}}, '#read_status': {'x': 1},
                                                                  'bad': 3})
            self.assertEqual(blobs, {'#read_status': {'u': {'stamp': None}}, '#read': {'v': {}}})
            self.assertEqual(flag_blobs_to_prefs(blobs), ({'u': {'stamp': None}}, {'#read': {'v': {}}}))
            self.assertEqual(flag_blobs_from_prefs(None, None), {'#read_status': {}})

        def test_suggestions(self):
            self.assertEqual(suggest_read_column(self.CUSTOM), '#read_status')   # it exists
            no_rs = {k: v for k, v in self.CUSTOM.items() if k != '#read_status'}
            self.assertEqual(suggest_read_column(no_rs), '#read')
            self.assertEqual(suggest_read_column({}), '#read_status')
            self.assertEqual([c for c, _ in eligible_columns(self.CUSTOM, 'read')], ['#read', '#read_status'])
            cols = {'#fin': {'datatype': 'bool', 'name': 'Finished'},
                    '#hr': {'datatype': 'bool', 'name': 'Have read'},
                    '#is_done': {'datatype': 'bool', 'name': 'Done'}}
            self.assertEqual([c for c, _ in existing_read_columns(cols)], ['#hr', '#fin', '#is_done'])
            hint = read_column_hint(dict(cols, **self.CUSTOM), exclude=('#hr', '#read_status'), where='above')
            self.assertIn('Have read (#read) (also: Finished (#fin), Done (#is_done))', hint)
            self.assertTrue(hint.endswith('pick it as the read status column above.'))
            self.assertNotIn('#hr', hint)

    class AppliedStampTests(Base):
        '''1.3.0 (bug 4): each entry the plugin applies gets applied_value + applied_at.'''

        AT_RE = r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$'

        def test_applied_entry_is_stamped_once_and_nothing_else_changes(self):
            self.write(SYNC_FILE, {'schema': 1, 'updated_at': stamp(1), 'books': {
                'UUID-A': {'value': True, 'updated_at': stamp(2), 'deviceId': 'phone-1'},
                'uuid-b': False,                                          # legacy bare bool
                'uuid-c': {'value': True, 'updated_at': stamp(2)},        # calibre already agrees
                'uuid-z': {'value': True, 'updated_at': stamp(2)}}})      # not in this library
            self.db.values[('#read_status', 3)] = True
            self.positions(**{'uuid-a': {'progress': 0.5, 'updated_at': stamp(2)}})
            res = self.sync()
            self.assertEqual((res.flags_updated, res.flags_stamped), (2, 1))
            doc = self.read_file()
            a = doc['books']['UUID-A']
            self.assertEqual(sorted(a), ['applied_at', 'applied_value', 'deviceId', 'updated_at', 'value'])
            self.assertEqual((a['value'], a['updated_at'], a['deviceId'], a['applied_value']),
                             (True, stamp(2), 'phone-1', True))
            self.assertRegex(a['applied_at'], self.AT_RE)
            # not earlier than the last_modified ALL our writes left (flag, then % and date)
            lm = self.v('last_modified', 1)
            self.assertGreaterEqual(parse_iso(a['applied_at']), lm)
            self.assertLess(parse_iso(a['applied_at']) - lm, datetime.timedelta(milliseconds=1))
            self.assertIs(doc['books']['uuid-b'], False)                 # bare bool kept as it was
            self.assertEqual(doc['books']['uuid-c'], {'value': True, 'updated_at': stamp(2)})
            self.assertEqual(doc['books']['uuid-z'], {'value': True, 'updated_at': stamp(2)})
            self.assertEqual(doc['updated_at'], stamp(1))
            before = self.file_bytes()
            res = self.sync()                                            # nothing new: no write
            self.assertEqual((res.flags_updated, res.flags_stamped), (0, 0))
            self.assertEqual(self.file_bytes(), before)
            self.assertFalse([f for f in os.listdir(self.dir) if '.crtmp.' in f or '.crbak.' in f])

        def test_a_newer_phone_entry_is_stamped_afresh(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            self.sync()
            first = self.read_file()['books']['uuid-a']['applied_at']
            self.db.tick(3600 * 24 * 3)
            self.flags(**{'uuid-a': {'value': False, 'updated_at': stamp(6)}})   # the app rewrote it
            self.sync()
            a = self.read_file()['books']['uuid-a']
            self.assertEqual((a['value'], a['applied_value']), (False, False))
            self.assertGreater(parse_iso(a['applied_at']), parse_iso(first))

        def test_entry_replaced_during_the_run_is_not_stamped(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)},
                          'uuid-b': {'value': True, 'updated_at': stamp(2)}})
            replaced = {'uuid-a': {'value': True, 'updated_at': stamp(3)},     # same value, newer
                        'uuid-b': {'value': False, 'updated_at': stamp(2)}}    # other value
            real_set_field = self.db.set_field

            def set_field(name, mapping):                    # the phone writes while we sync
                self.flags(**replaced)
                return real_set_field(name, mapping)
            self.db.set_field = set_field
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), True)
            self.assertEqual((res.flags_updated, res.flags_stamped), (2, 0))
            self.assertEqual(self.read_file()['books'], replaced)

        def test_a_stamp_the_file_could_not_take_is_written_next_run(self):
            entry = {'value': True, 'updated_at': stamp(2)}
            self.flags(**{'uuid-a': entry})
            real_set_field = self.db.set_field

            def set_field(name, mapping):                    # the cloud app is mid-write
                with open(os.path.join(self.dir, SYNC_FILE), 'w') as f:
                    f.write('{"schema": 1, "books": {"uuid-a": {"val')
                return real_set_field(name, mapping)
            self.db.set_field = set_field
            res = self.sync()
            self.db.set_field = real_set_field
            self.assertEqual((res.flags_updated, res.flags_stamped, res.notes), (1, 0, []))
            pending = load_pending_stamps(self.flag_blobs['#read_status'])
            self.assertEqual(list(pending), ['uuid-a'])
            self.flags(**{'uuid-a': entry})                  # the cloud app finished writing
            res = self.sync()
            self.assertEqual((res.flags_updated, res.flags_stamped), (0, 1))
            a = self.read_file()['books']['uuid-a']
            self.assertEqual(parse_iso(a['applied_at']), pending['uuid-a'][2])
            self.assertEqual(load_pending_stamps(self.flag_blobs['#read_status']), {})
            before = self.file_bytes()
            self.sync()
            self.assertEqual(self.file_bytes(), before)

        def test_parser_reads_a_stamped_file_as_before(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}, 'uuid-b': True})
            before = parse_sidecar(self.read_file())
            self.sync()
            self.assertIn('applied_at', self.read_file()['books']['uuid-a'])
            self.assertEqual(parse_sidecar(self.read_file()), before)

    class SettingsTests(unittest.TestCase):
        def test_read_column_round_trip(self):
            db = FakeDB(COLS, {})
            db.set_pref(SETTINGS_KEY, json.loads(json.dumps(
                settings_to_store('', '', 1, True, read_column='#read'))))
            self.assertEqual(normalise_settings(db.pref(SETTINGS_KEY))['read_column'], '#read')
            self.assertEqual(settings_to_store('', '', 1, read_column=None)['read_column'], '#read_status')
            for bad in (None, 5, 'read', '#Read', '#read status', ''):
                self.assertEqual(normalise_settings({'read_column': bad})['read_column'], '#read_status')
            self.assertEqual(normalise_settings(None)['read_column'], '#read_status')

        def test_normalise(self):
            self.assertEqual(normalise_settings(None)['progress_threshold'], 1)
            s = normalise_settings({'percent_column': 'read_percent', 'last_read_column': '#Bad',
                                    'progress_threshold': 500})
            self.assertEqual((s['percent_column'], s['last_read_column'], s['progress_threshold']),
                             ('', '', 50))
            self.assertIsNone(s['version'])
            self.assertEqual(settings_to_store('#read_percent', None, 2),
                             {'version': 1, 'read_column': '#read_status', 'percent_column': '#read_percent',
                              'last_read_column': '', 'progress_threshold': 2, 'auto_sync': True})
            self.assertIs(normalise_settings(None)['auto_sync'], True)
            self.assertIs(normalise_settings({'auto_sync': False})['auto_sync'], False)
            self.assertIs(normalise_settings({'auto_sync': 'no'})['auto_sync'], True)

        def test_status_line(self):
            r = SyncResult()
            self.assertEqual(r.status_line(), '')                      # a quiet run says nothing
            r.flags_updated, r.progress_updated = 2, 1
            self.assertEqual(r.status_line(), 'updated 2 read status, 1 reading progress.')
            r.notes.append('x')
            self.assertIn('needs attention', r.status_line())

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

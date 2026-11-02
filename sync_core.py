# Last modified: 2026-11-02--1430
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

import hashlib
import json
import os
import re

try:
    from calibre_plugins.calibreader_sync.sidecar_merge import (
        APPLY, RETIRE, KEY_APPLIED_AT, KEY_APPLIED_VALUE, KEY_UPDATED_AT, KEY_VALUE, NOT_SEEN, RECORD,
        UTC, Target, ceil_ms, decide_flag, format_iso, last_read_value, load_applied,
        load_applied_values, load_flag_records, load_pending_stamps, parse_iso, parse_positions, parse_sidecar,
        percent_for_column, plan, refresh_lm, store_applied, store_flag_record, writeback_for,
    )
except ImportError:  # pragma: no cover - standalone / test import
    from sidecar_merge import (
        APPLY, RETIRE, KEY_APPLIED_AT, KEY_APPLIED_VALUE, KEY_UPDATED_AT, KEY_VALUE, NOT_SEEN, RECORD,
        UTC, Target, ceil_ms, decide_flag, format_iso, last_read_value, load_applied,
        load_applied_values, load_flag_records, load_pending_stamps, parse_iso, parse_positions, parse_sidecar,
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
# Missing or malformed means #read_status (the 1.2.x behaviour). 1.3.1 (review M1):
# when the user HAS chosen a column and it is missing from the library (deleted,
# renamed, or created and calibre not restarted yet) or is not Yes/No, read status
# is not written at all, never into #read_status, and the summary says why; % read
# and last read still sync. normalise_settings marks a choice with
# "read_column_set" (never stored). The apps (1.0.1 and later) read the same key;
# their read-only fallback to #read_status writes nothing into calibre.

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
    # 1.3.1: did the user choose the read column? Survives normalising twice.
    chosen = raw.get('read_column_set')
    out['read_column_set'] = (chosen if isinstance(chosen, bool)
                              else isinstance(raw.get('read_column'), str)
                              and bool(_LOOKUP_RE.match(raw['read_column'])))
    threshold = raw.get('progress_threshold')
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        out['progress_threshold'] = min(MAX_THRESHOLD, max(0, threshold))
    return out


def settings_to_store(percent_column, last_read_column, progress_threshold, auto_sync=True,
                      read_column=READ_COLUMN):
    '''The settings dict to store. read_column=None (1.3.1) leaves "read_column" out:
    the user has not chosen a read column, and none is saved on their behalf.'''
    out = normalise_settings({
        'version': SETTINGS_VERSION,
        'read_column': read_column or READ_COLUMN,
        'percent_column': percent_column or '',
        'last_read_column': last_read_column or '',
        'progress_threshold': progress_threshold,
        'auto_sync': bool(auto_sync),
    })
    del out['read_column_set']                  # derived, never stored
    if read_column is None:
        del out['read_column']
    return out


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
# column in Settings ("read_column"), and the apps follow the same choice.
# 1.3.1 (review H1): a column is named as a likely read column only when its lookup
# name or its whole heading says exactly that (#read, #have_read, #is_read,
# #finished, #ko_statusbool; "Read", "Have read", "Is read", "Read status",
# "Finished"). Matching parts of words named columns meaning the opposite (#abandoned
# via "done", "Did not finish", #incomplete, #proofread, #ready...). And it is only
# ever NAMED (a hint in Settings and in the summary): Settings never pre-selects it,
# and saves a read column only when the user picked one.

_EXPLICIT_READ = [(re.compile(lookup), re.compile(heading) if heading else None) for lookup, heading in (
    (r'^#read$', r'^read$'),                    # most likely first
    (r'^#have_?read$', r'^have read$'),
    (r'^#is_?read$', r'^is read$'),
    (r'^#read_?status$', r'^read status$'),
    (r'^#finished$', r'^finished$'),
    (r'^#ko_statusbool$', None),                # KOReader Sync's read flag
)]


def _read_rank(lookup, heading):
    '''Position in _EXPLICIT_READ of the first pattern the column matches, or None.'''
    lookup = (lookup or '').lower()
    heading = ' '.join((heading or '').lower().replace('_', ' ').split())
    for rank, (lookup_rx, heading_rx) in enumerate(_EXPLICIT_READ):
        if lookup_rx.match(lookup) or (heading_rx is not None and heading_rx.match(heading)):
            return rank
    return None


def existing_read_columns(custom_columns, exclude=READ_COLUMN):
    '''[(lookup, heading)] of Yes/No columns, other than `exclude` (one lookup name
    or several), whose lookup name or whole heading says "read" explicitly (see
    _EXPLICIT_READ), the most likely first.'''
    excluded = (exclude,) if isinstance(exclude, str) else tuple(exclude or ())
    out = []
    for lookup, meta in (custom_columns or {}).items():
        if lookup in excluded or not isinstance(meta, dict) or meta.get('datatype') != 'bool':
            continue
        heading = meta.get('name') or lookup
        rank = _read_rank(lookup, heading)
        if rank is not None:
            out.append((rank, heading.lower(), lookup, heading))
    return [(lookup, heading) for _, _, lookup, heading in sorted(out)]


def suggest_read_column(custom_columns):
    '''The read column to NAME before the user has chosen one (1.3.1: Settings shows it
    as a hint and never pre-selects it): #read_status if the library has it as Yes/No,
    else the likeliest explicit read column, else #read_status (to be created).'''
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
        return ('is not in this library (deleted or renamed? If you just created it, restart '
                'calibre)')
    if meta.get('datatype') not in READ_TYPES:
        return 'is {}, not Yes/No'.format(_a_type(meta.get('datatype')))
    return None


def resolve_read_column(custom_columns, chosen, explicit=True):
    '''(column to write, [notes]). With a choice (`explicit`): that column when it is
    a Yes/No column of this library, otherwise None with a note (1.3.1, review M1:
    never another column, so a deleted, renamed or not-yet-created choice can't send
    read status into the #read_status the user moved away from). Without one:
    #read_status, or None with a note when it is missing or not Yes/No.'''
    custom_columns = custom_columns or {}
    notes = []
    if explicit and chosen:
        why = _column_problem(custom_columns, chosen)
        if why is None:
            return chosen, notes
        notes.append('Read status was not synced: the column chosen in Settings, {}, {}. It was '
                     'not written into any other column, and % read and last read still sync. '
                     'Pick a Yes/No column in Settings (the arrow next to Sync Read '
                     'Status).'.format(chosen, why))
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


# --- the change log (1.3.1, review M4) ------------------------------------------
#
# calibre has no undo for set_field, so every value the plugin writes into a
# library is recorded, one JSON object per line: when, book id, title, uuid, column,
# old value, new value, and the stamp of the app entry that won. One file per
# library (library_key) in the plugin's own folder of calibre's config directory,
# never in the library; the oldest lines go once it passes CHANGE_LOG_MAX_LINES.

CHANGE_LOG_DIR = 'calibreader_sync_changes'     # under calibre's config_dir/plugins
CHANGE_LOG_MAX_LINES = 10000


def library_key(db, library_path):
    '''A per-library key for the plugin's own files and memory: calibre's library_id
    plus a hash of the library's folder. A copied library keeps the same library_id
    (it lives in metadata.db), so the folder tells copies apart.'''
    lid = re.sub(r'[^A-Za-z0-9_-]', '', str(getattr(db, 'library_id', '') or ''))[:64] or 'library'
    folder = os.path.normcase(os.path.realpath(library_path or ''))
    return '{}-{}'.format(lid, hashlib.sha1(folder.encode('utf-8')).hexdigest()[:10])


def change_log_path(config_dir, db, library_path):
    return os.path.join(config_dir, 'plugins', CHANGE_LOG_DIR, library_key(db, library_path) + '.log')


def _json_value(value):
    if isinstance(value, _dt.datetime):
        return format_iso(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def append_change_log(path, changes, at, max_lines=CHANGE_LOG_MAX_LINES):
    '''Append `changes` (SyncResult.changes) to the log at `path`, each line stamped
    `at`; keep only the newest max_lines lines.'''
    if not changes:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [json.dumps(dict([('at', at)] + list(c.items())), ensure_ascii=False) + '\n'
             for c in changes]
    existing = []
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            existing = f.readlines()
    if len(existing) + len(lines) <= max_lines:
        with open(path, 'a', encoding='utf-8', newline='\n') as f:
            f.writelines(lines)
        return
    keep = (existing + lines)[-max_lines:]
    tmp = '{}.tmp.{}'.format(path, _uuid.uuid4().hex)
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.writelines(keep)
    os.replace(tmp, path)


def confirm_note(confirm):
    '''(1.3.1) The summary line for a first sync waiting for the user's OK.'''
    change = (' and change {} filled cell(s)'.format(confirm['change']) if confirm['change'] else '')
    return ('Read status is waiting for your OK: this is the first sync into {} ({}), and it would '
            'fill {} empty cell(s){} with the app\'s read marks. Click Sync Read Status to review '
            'and confirm it. Nothing has been written into that column yet.').format(
                confirm['heading'], confirm['column'], confirm['fill'], change)


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
        self.changes = []              # 1.3.1: every value written, old -> new (append_change_log)
        self.confirm = None            # 1.3.1: {column, heading, fill, change} of a first sync
                                       # into a chosen column waiting for the user's OK

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


def update_sync_file(library_path, writebacks=None, applied=None, agreeing=None):
    '''Write into CalibReader_sync.json, following the apps' sidecar protocol
    (docs/PARITY.md, 2026-10-13): re-read the file right before writing and write
    only if it parses cleanly; touch only these books' entries; never remove a key;
    never touch the file-level updated_at (legacy bare entries inherit it); write a
    temp file and rename it in, with the backup swap when rename-over is refused;
    don't write at all when nothing changes.

    writebacks (1.2.4) = {uuid: (value, stamp[, applied_at])}: calibre's newer read
        values. The entry is replaced, only when ours is strictly newer; with an
        applied_at (1.3.0) it carries applied_value + applied_at as well.
    applied (1.3.0) = {uuid: (value, entry_stamp, applied_at)}: entries this plugin
        wrote into calibre. They get `applied_value` and `applied_at`, all their
        other keys kept, only while the file still holds that same entry (same value
        and stamp) as an object; a legacy bare bool is left alone.
    agreeing (1.3.0) = the same shape, for entries calibre already agrees with (the
        plugin wrote nothing): stamped the same way, once. An entry that already
        carries applied_value == its value (and an applied_at) is left alone.

    Returns ({uuid: stamp written back}, {uuid: applied_at stamped}, failed, note);
    `failed` is True when the file could not be read or written this time.'''
    path = os.path.join(library_path, SYNC_FILE)
    writebacks, applied, agreeing = writebacks or {}, applied or {}, agreeing or {}
    retry = ('Read status set in calibre could not be passed back to the app this time '
             '({}); it will be tried again next sync.')

    def failure(why):
        return {}, {}, True, (retry.format(why) if writebacks else None)

    if not (writebacks or applied or agreeing) or not os.path.exists(path):
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
    for uuid, wb in writebacks.items():
        value, stamp = wb[0], wb[1]
        have = current.get(uuid)
        if have is not None and have.stamp is not None and have.stamp >= stamp:
            continue                        # someone wrote a newer entry meanwhile
        entry = {KEY_VALUE: bool(value), KEY_UPDATED_AT: format_iso(stamp)}
        if len(wb) > 2 and wb[2] is not None:          # calibre holds this value: stamp it now
            entry.update({KEY_APPLIED_VALUE: bool(value), KEY_APPLIED_AT: format_iso(wb[2])})
        books[keys.get(uuid, uuid)] = entry
        written[uuid] = stamp
        dirty = True

    def same_entry(uuid, value, entry_stamp):
        '''The raw object entry, if the file still holds the entry we decided on.'''
        have, raw = current.get(uuid), books.get(keys.get(uuid))
        if (uuid in written or have is None or not isinstance(raw, dict) or have.value != bool(value)
                or format_iso(have.stamp) != format_iso(entry_stamp)):
            return None                     # replaced or removed meanwhile, or a bare bool
        return raw

    for uuid, (value, entry_stamp, at) in applied.items():
        raw = same_entry(uuid, value, entry_stamp)
        if raw is None:
            continue
        want = {KEY_APPLIED_VALUE: bool(value), KEY_APPLIED_AT: format_iso(at)}
        if any(raw.get(k) != v for k, v in want.items()):
            raw.update(want)
            dirty = True
        stamped[uuid] = at
    for uuid, (value, entry_stamp, at) in agreeing.items():
        raw = None if uuid in applied else same_entry(uuid, value, entry_stamp)
        if raw is None or (raw.get(KEY_APPLIED_VALUE) is bool(value)
                           and parse_iso(raw.get(KEY_APPLIED_AT)) is not None):
            continue                        # stamped once already: left alone
        raw.update({KEY_APPLIED_VALUE: bool(value), KEY_APPLIED_AT: format_iso(at)})
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


def run_sync(db, library_path, settings, flag_blobs, pos_blob, approved=()):
    '''One sync. Returns (SyncResult, new_flag_blobs, new_pos_blob); these are the
    plugin's per-book bookkeeping (see sidecar_merge), to be stored by the caller.
    `flag_blobs` = {read column: {uuid: record}} (flag_blobs_from_prefs); only the
    blob of the column used this run is read or changed. Nothing is written to
    calibre unless a rule says it may.

    1.3.1 (review H1/M4): the FIRST sync into a read column the user chose (no memory
    for it yet) writes nothing into it, unless the column is in `approved`: it sets
    res.confirm to the counts (empty cells it would fill, filled ones it would change)
    for the user's OK, and everything else syncs as usual. The #read_status default of
    a library where no read column was ever chosen works as in 1.2.x.'''
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
        flag_col, notes = resolve_read_column(custom, settings['read_column'],
                                              settings['read_column_set'])
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
    retired = {}                            # 1.3.1 (M3): first-contact conflicts, calibre kept
    now = _dt.datetime.now(UTC)
    if flag_col:
        entries = parse_sidecar(flags_data)
        records = load_flag_records(flag_blob)
        applied_values = load_applied_values(flag_blob)
        pending = {u: p for u, p in load_pending_stamps(flag_blob).items() if u in uuid_to_id}
        skipped = []
        for uuid, book_id in uuid_to_id.items():
            value = db.field_for(flag_col, book_id)
            value = None if value is None else bool(value)
            stamp, lm, seen, dt = records.get(uuid, (None, None, NOT_SEEN, None))
            av = applied_values.get(uuid)
            book_lm = _lm(db, book_id)
            entry = entries.get(uuid)
            if (entry is not None and av is None and not entry.dated and entry.stamp is not None
                    and stamp is not None and entry.stamp <= stamp):
                av = entry.value        # a 1.3.0 record of this same bare entry (M2)
            if entry is not None:
                lm_before[uuid] = book_lm
                verdict, dt = decide_flag(entry, value, stamp, lm, book_lm, seen, dt, now,
                                          applied_value=av)
                wb = writeback_for(entry, verdict, value, dt)
                if wb is not None:
                    writebacks[uuid] = wb
                if verdict == APPLY:
                    flag_updates[uuid] = entry.value
                if verdict in (APPLY, RECORD):
                    flag_records[uuid] = entry
                else:
                    skipped.append(uuid)
                    if verdict == RETIRE:
                        retired[uuid] = entry
            elif seen is not NOT_SEEN and value != seen and book_lm is not None:
                mark = book_lm if book_lm <= now else now
                dt = mark if dt is None or mark > dt else dt
            flag_state[uuid] = (stamp, lm, dt, av)
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

    # 1.3.1: the first sync into a chosen read column waits for the user's OK.
    gated = False
    if (flag_col and flag_updates and settings['read_column_set'] and not flag_blobs.get(flag_col)
            and flag_col not in (approved or ())):
        fill = sum(1 for u in flag_updates if db.field_for(flag_col, uuid_to_id[u]) is None)
        res.confirm = {'column': flag_col, 'heading': (custom.get(flag_col) or {}).get('name') or flag_col,
                       'fill': fill, 'change': len(flag_updates) - fill}
        res.notes.append(confirm_note(res.confirm))
        flag_updates, flag_records, flag_state, writebacks, pending, retired = {}, {}, {}, {}, {}, {}
        res.flags_skipped = 0
        gated = True

    # Writes. Every set_field bumps the books' last_modified. 1.3.1 (M4): each value
    # written is recorded with the value it replaced (res.changes).
    def write(column, mapping, stamps):
        for book_id, uuid in sorted((uuid_to_id[u], u) for u in mapping):
            res.changes.append({'book_id': book_id, 'title': _json_value(db.field_for('title', book_id)),
                                'uuid': uuid, 'column': column,
                                'old': _json_value(db.field_for(column, book_id)),
                                'new': _json_value(mapping[uuid]), 'entry': format_iso(stamps.get(uuid))})
        db.set_field(column, {uuid_to_id[u]: v for u, v in mapping.items()})
        res.columns_written.append(column)

    if flag_updates:
        write(flag_col, flag_updates, {u: flag_records[u].stamp for u in flag_updates})
    pos_stamps = {u: t.stamp for u, t in pos_records.items()}
    pct_map = {u: v[0] for u, v in pos_updates.items() if v[0] is not None}
    last_map = {u: v[1] for u, v in pos_updates.items() if v[1] is not None}
    if pct_map:
        write(pct_col, pct_map, pos_stamps)
    if last_map:
        write(last_col, last_map, pos_stamps)
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
    # Entries calibre already agrees with (the plugin wrote nothing), and the 1.2.4
    # write-backs (calibre's own value), are stamped too, with the book's current
    # last_modified rounded up to the ms; update_sync_file stamps each entry once.
    agreeing, wb_stamped = {}, {}
    if flag_col and not gated:
        def current_lm(uuid):
            return ceil_ms(lm_after.get(uuid) or _lm(db, uuid_to_id[uuid]) or _dt.datetime.now(UTC))
        for uuid, entry in entries.items():
            if uuid not in uuid_to_id or uuid in stamps or uuid in writebacks:
                continue
            value = db.field_for(flag_col, uuid_to_id[uuid])
            if value is not None and bool(value) == entry.value:
                agreeing[uuid] = (entry.value, entry.stamp, current_lm(uuid))
        wb_stamped = {u: (v, s, current_lm(u)) for u, (v, s) in writebacks.items()}
    sent, stamped, failed, note = update_sync_file(library_path, wb_stamped, stamps, agreeing)
    res.flags_sent_back, res.flags_stamped = len(sent), len(stamped)
    keep = stamps if failed else {}
    if note:
        res.notes.append(note)
    for uuid, (stamp, lm, dt, av) in flag_state.items():
        if uuid in flag_records:
            stamp, lm, av = flag_records[uuid].stamp, lm_after[uuid], flag_records[uuid].value
        elif uuid in sent:
            stamp = sent[uuid]              # our own entry: never re-decided as a phone change
            av = writebacks[uuid][0]
        elif uuid in retired:
            stamp, lm, av = retired[uuid].stamp, lm_before[uuid], retired[uuid].value
        value = db.field_for(flag_col, uuid_to_id[uuid])
        store_flag_record(flag_blob, uuid, stamp, lm, None if value is None else bool(value), dt,
                          keep.get(uuid), applied_value=av)
    for uuid, entry in pos_records.items():
        store_applied(pos_blob, uuid, entry.stamp, lm_after[uuid])
    # Our write for one kind of entry must not look like a desktop edit to the other.
    for uuid in written:
        if uuid not in flag_records:
            refresh_lm(flag_blob, uuid, lm_before.get(uuid), lm_after[uuid])
        if uuid not in pos_records:
            refresh_lm(pos_blob, uuid, lm_before.get(uuid), lm_after[uuid])
    if flag_col and not gated:
        flag_blobs[flag_col] = flag_blob
    return res, flag_blobs, pos_blob


# The plugin's memory, per library (1.3.1, review L1). Up to 1.3.0 one memory served
# every library, so a copy of a library (same uuids) saw the other copy's values as
# calibre edits and wrote them back into its own sync file, and the memory grew with
# every library ever synced. Now: prefs['libraries'][library_key] = {'applied',
# 'applied_cols', 'applied_pos', 'approved', 'used'}, for the MAX_LIBRARIES most
# recently synced libraries. The 1.3.0 / 1.2.x top-level memory moves, once, to the
# first library that syncs with CalibReader files in it.

MAX_LIBRARIES = 8
_STATE_KEYS = ('applied', 'applied_cols', 'applied_pos', 'approved')


def _load_library_state(prefs, key):
    '''(state, from_legacy) for one library.'''
    libraries = prefs.get('libraries')
    state = libraries.get(key) if isinstance(libraries, dict) else None
    if isinstance(state, dict):
        return state, False
    if any(k in prefs for k in _STATE_KEYS):
        return {k: prefs.get(k) for k in _STATE_KEYS}, True
    return {}, False


def _store_library_state(prefs, key, state, now, from_legacy):
    libraries = dict(prefs.get('libraries') or {})
    libraries[key] = dict(state, used=format_iso(now))
    if len(libraries) > MAX_LIBRARIES:
        oldest = sorted((k for k in libraries if k != key),
                        key=lambda k: ((libraries[k] or {}).get('used') or '', k))
        for k in oldest[:len(libraries) - MAX_LIBRARIES]:
            del libraries[k]
    prefs['libraries'] = libraries              # assignment is what commits a JSONConfig
    if from_legacy:
        for k in _STATE_KEYS:
            if k in prefs:
                del prefs[k]


def sync_library(db, library_path, prefs, log_dir=None, approve=None, now=None):
    '''(1.3.1) One run as the plugin's GUI makes it (the button and the automatic sync,
    ui.py): the library's settings, this library's memory from `prefs` (the plugin's
    JSONConfig, or any dict), run_sync, the memory stored back, and the change log
    written into `log_dir` (change_log_path). `approve` = a read column the user just
    OK'd for its first sync (res.confirm). Returns the SyncResult.'''
    now = now or _dt.datetime.now(UTC)
    key = library_key(db, library_path)
    state, from_legacy = _load_library_state(prefs, key)
    settings = normalise_settings(db.pref(SETTINGS_KEY, None))
    approved = set(c for c in (state.get('approved') or ()) if isinstance(c, str))
    if approve:
        approved.add(approve)
    res, flag_blobs, pos_blob = run_sync(
        db, library_path, settings,
        flag_blobs_from_prefs(state.get('applied'), state.get('applied_cols')),
        state.get('applied_pos'), approved=approved)
    if res.found_any_file:
        # Stored even when nothing was written: a RECORD verdict must persist so a
        # retired entry is never reconsidered.
        applied, applied_cols = flag_blobs_to_prefs(flag_blobs)
        _store_library_state(prefs, key, {
            'applied': applied, 'applied_cols': applied_cols, 'applied_pos': pos_blob,
            'approved': sorted(c for c in approved if not flag_blobs.get(c)),   # an OK is used up
        }, now, from_legacy)
    if log_dir and res.changes:
        try:
            append_change_log(os.path.join(log_dir, key + '.log'), res.changes, format_iso(now))
        except OSError as exc:
            res.notes.append('The change log could not be written ({}).'.format(exc))
    return res


# ---------------------------------------------------------------------------
# Self-check: `python sync_core.py`
# ---------------------------------------------------------------------------

if __name__ == '__main__':  # pragma: no cover
    import datetime
    import shutil
    import tempfile
    import unittest

    # calibre 9.x returns last_modified (and date columns) in the computer's local time,
    # e.g. 2026-11-02 14:15:15.960620-08:00 after set_field; the fake does the same.
    LOCAL = datetime.timezone(datetime.timedelta(hours=-8))

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
            self.library_id = 'lib-0001'      # a copied library keeps it (it is in metadata.db)

        def all_book_ids(self):
            return {b for (f, b) in self.values if f == 'uuid'}

        def field_for(self, name, book_id, default_value=None):
            value = self.values.get((name, book_id), default_value)
            if isinstance(value, datetime.datetime) and value.tzinfo is not None:
                value = value.astimezone(LOCAL)    # real calibre hands dates back in local time
            return value

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

        def sync(self, settings=SETTINGS, approved=None):
            kwargs = {} if approved is None else {'approved': approved}
            res, self.flag_blobs, self.pos_blob = run_sync(
                self.db, self.dir, settings, self.flag_blobs, self.pos_blob, **kwargs)
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
                             ['#read', '#ko_statusbool'])          # 1.3.1: "Done" is not explicit

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
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(4)}})   # phone, Oct 4
            self.db.clock = datetime.datetime(2026, 10, 5, 9, 0, 0, 1, tzinfo=UTC)
            self.db.desktop_edit('#read_status', 1, False)     # calibre, Oct 5
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), False)    # calibre's later edit stands
            self.assertEqual((res.flags_updated, res.flags_sent_back), (0, 1))
            self.assertEqual(self.sync().flags_updated, 0)     # and keeps standing

        def test_cleared_cell_takes_a_newer_mark_but_not_the_applied_one(self):
            # 1.3.0 ruling: an empty cell takes the app's mark whatever its age.
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            self.sync()
            self.db.clock = datetime.datetime(2026, 10, 5, 9, 0, 0, 1, tzinfo=UTC)
            self.db.desktop_edit('#read_status', 1, None)      # cleared in calibre, Oct 5
            res = self.sync()                                  # the Oct 2 entry was applied already
            self.assertIsNone(self.v('#read_status', 1))
            self.assertEqual((res.flags_updated, res.flags_sent_back), (0, 0))
            self.flags(**{'uuid-a': {'value': False, 'updated_at': stamp(4)}})  # app, Oct 4: older than the clear
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), False)
            self.assertEqual(res.flags_updated, 1)
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(4, 13)}})  # a later app toggle
            self.assertEqual(self.sync().flags_updated, 1)     # not held back by the old clear
            self.assertIs(self.v('#read_status', 1), True)

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

    class EmptyCellTests(Base):
        '''1.3.0, user ruling 2026-10-22: an EMPTY read cell takes the app's mark whatever
        its age; a FILLED cell keeps its value under the usual rules.'''

        def test_old_app_entry_fills_an_empty_cell(self):
            self.db.tick(3600 * 24 * 9)                        # Oct 10: the books were edited in calibre
            self.db.desktop_edit('rating', 1, 8)
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})   # app, Oct 2
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), True)
            self.assertEqual(res.flags_updated, 1)

        def test_old_app_entry_does_not_overwrite_a_filled_cell(self):
            self.db.tick(3600 * 24 * 9)
            self.db.desktop_edit('#read_status', 1, False)     # calibre, Oct 10
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})   # app, Oct 2
            res = self.sync()
            self.assertIs(self.v('#read_status', 1), False)
            # 1.3.1 (M3): first contact, so nothing proves the cell is newer than the entry:
            # calibre's value is kept and nothing goes back to the phone
            self.assertEqual((res.flags_updated, res.flags_sent_back), (0, 0))
            self.assertEqual(self.read_file()['books']['uuid-a']['value'], True)

        def test_switching_to_a_column_with_empty_and_filled_cells(self):
            self.db.field_metadata = FakeFieldMetadata(
                dict(COLS, **{'#read': {'datatype': 'bool', 'name': 'Have read'}}))
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)},
                          'uuid-b': {'value': True, 'updated_at': stamp(2)},
                          'uuid-c': {'value': False, 'updated_at': stamp(2)}})
            self.sync()                                        # into #read_status, Oct 1 run
            before = self.read_file()['books']
            self.db.tick(3600 * 24 * 9)                        # Oct 10: the user's own #read
            self.db.set_field('#read', {2: False, 3: False})   # a empty, b filled differently, c agrees
            self.db.desktop_edit('rating', 1, 8)               # a edited too: newer than its app entry
            res = self.sync(dict(SETTINGS, read_column='#read'), approved={'#read'})
            self.assertIs(self.v('#read', 1), True)            # empty: filled from the old entry
            self.assertIs(self.v('#read', 2), False)           # filled: kept
            self.assertIs(self.v('#read', 3), False)           # filled, agrees
            self.assertEqual((res.flags_updated, res.flags_sent_back), (1, 0))
            books = self.read_file()['books']
            self.assertEqual(books['uuid-a']['applied_value'], True)    # stamped again: filled from empty
            self.assertGreater(parse_iso(books['uuid-a']['applied_at']),
                               parse_iso(before['uuid-a']['applied_at']))
            self.assertGreaterEqual(parse_iso(books['uuid-a']['applied_at']), self.v('last_modified', 1))
            self.assertEqual(books['uuid-b'], before['uuid-b'])         # 1.3.1 (M3): kept, not pushed back
            self.assertEqual(books['uuid-c'], before['uuid-c'])         # agreeing cell: entry untouched
            self.assertEqual(self.sync(dict(SETTINGS, read_column='#read')).flags_updated, 0)

        def test_filled_from_empty_is_stamped(self):
            self.db.tick(3600 * 24 * 9)
            self.db.desktop_edit('rating', 1, 8)               # book edited after the app's mark
            self.flags(**{'uuid-a': {'value': False, 'updated_at': stamp(2)}})
            res = self.sync()
            self.assertEqual((res.flags_updated, res.flags_stamped), (1, 1))
            a = self.read_file()['books']['uuid-a']
            self.assertEqual((a['value'], a['applied_value']), (False, False))
            self.assertGreaterEqual(parse_iso(a['applied_at']), self.v('last_modified', 1))

    class BareBoolTests(Base):
        '''M2 (review): a bare-bool entry (Android) carries only the FILE's updated_at, which
        both apps move on every upload. An entry applied once must stay applied once.'''

        def bare(self, day, **books):
            self.write(SYNC_FILE, {'schema': 1, 'updated_at': stamp(day), 'books': books})

        def test_bare_bool_does_not_refill_a_cleared_cell_when_the_file_stamp_moves(self):
            self.bare(1, **{'uuid-a': True})
            self.assertEqual(self.sync().flags_updated, 1)
            self.db.desktop_edit('#read_status', 1, None)          # the user clears it in calibre
            self.sync()
            self.bare(5, **{'uuid-a': True, 'uuid-b': True})       # the phone marks another book
            res = self.sync()
            self.assertIsNone(self.v('#read_status', 1))
            self.assertIs(self.v('#read_status', 2), True)
            self.assertEqual(res.flags_updated, 1)

        def test_bare_bool_does_not_overwrite_a_calibre_edit_when_the_file_stamp_moves(self):
            self.bare(1, **{'uuid-a': True})
            self.sync()
            self.db.tick(3600 * 24 * 2)
            self.db.desktop_edit('#read_status', 1, False)         # unticked in calibre
            self.bare(5, **{'uuid-a': True, 'uuid-b': True})       # the phone re-uploads its state
            self.sync()
            self.bare(7, **{'uuid-a': True, 'uuid-b': True, 'uuid-c': False})
            self.sync()
            self.assertIs(self.v('#read_status', 1), False)

        def test_a_new_bare_mark_still_applies(self):
            self.bare(1, **{'uuid-a': True})
            self.sync()
            self.db.desktop_edit('#read_status', 1, None)
            self.sync()
            self.bare(6, **{'uuid-a': False})                      # marked unread on the phone
            self.assertEqual(self.sync().flags_updated, 1)
            self.assertIs(self.v('#read_status', 1), False)

    class ColumnSwitchTests(Base):
        '''M3 (review): switching columns is a first contact for every book. calibre keeps
        no per-field edit time, so an unrelated edit (a rating) newer than a phone toggle is
        no evidence that the new column's value is newer: it must not push calibre's old
        value back over the phone's mark.'''

        def setUp(self):
            Base.setUp(self)
            self.db.field_metadata = FakeFieldMetadata(
                dict(COLS, **{'#finished': {'datatype': 'bool', 'name': 'Finished'}}))
            self.db.values[('#finished', 1)] = False                   # old value, Oct 1

        def switch_after_rating_edit(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(3)}})   # phone: read, Oct 3
            self.db.clock = datetime.datetime(2026, 10, 3, 13, 0, 0, 1, tzinfo=UTC)
            self.assertEqual(self.sync().flags_updated, 1)              # into #read_status
            self.db.clock = datetime.datetime(2026, 10, 5, 9, 0, 0, 1, tzinfo=UTC)
            self.db.desktop_edit('rating', 1, 8)                       # unrelated, Oct 5
            return self.sync(dict(SETTINGS, read_column='#finished'))   # the user switches

        def test_switch_does_not_write_back_an_unrelated_edit(self):
            res = self.switch_after_rating_edit()
            entry = self.read_file()['books']['uuid-a']
            self.assertEqual((entry['value'], entry['updated_at']), (True, stamp(3)))
            self.assertEqual(res.flags_sent_back, 0)
            self.assertIs(self.v('#finished', 1), False)               # rule (a): calibre's value kept
            res = self.sync(dict(SETTINGS, read_column='#finished'))   # and the next run agrees
            self.assertEqual((res.flags_updated, res.flags_sent_back), (0, 0))
            self.assertEqual(self.read_file()['books']['uuid-a']['value'], True)

        def test_a_later_phone_mark_lands_in_the_new_column(self):
            self.switch_after_rating_edit()
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(4)}})   # made Oct 4, synced late
            res = self.sync(dict(SETTINGS, read_column='#finished'))
            self.assertIs(self.v('#finished', 1), True)
            self.assertEqual(res.flags_updated, 1)

    class ChangeLogTests(Base):
        '''M4 (review): every value the plugin writes into the library is recorded, old ->
        new, so a wrong pick can be undone by hand (calibre has no undo for set_field).'''

        AT = '2026-11-02T22:00:00.000Z'

        def log_lines(self, path):
            with open(path, encoding='utf-8') as f:
                return [json.loads(line) for line in f]

        def test_every_cell_write_is_logged_with_its_old_value(self):
            self.db.values[('#read_status', 2)] = False
            self.db.values[('title', 1)] = 'Dracula'
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(9)},
                          'uuid-b': {'value': True, 'updated_at': stamp(9)}})
            res = self.sync()
            path = os.path.join(self.dir, 'logs', 'library.log')
            append_change_log(path, res.changes, self.AT)
            got = sorted((c['book_id'], c['column'], c['old'], c['new']) for c in self.log_lines(path))
            self.assertEqual(got, [(1, '#read_status', None, True), (2, '#read_status', False, True)])
            first = [c for c in self.log_lines(path) if c['book_id'] == 1][0]
            self.assertEqual((first['at'], first['title'], first['uuid'], first['entry']),
                             (self.AT, 'Dracula', 'uuid-a', stamp(9)))

        def test_progress_writes_are_logged_too(self):
            self.db.values[('#read_percent', 1)] = 20
            self.positions(**{'uuid-a': {'progress': 0.5, 'updated_at': stamp(9)}})
            res = self.sync()
            got = sorted((c['column'], c['old'], c['new']) for c in res.changes)
            self.assertEqual(got, [('#last_read', None, '2026-10-09T12:00:00.000Z'), ('#read_percent', 20, 50)])

        def test_nothing_written_nothing_logged(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(9)}})
            self.sync()
            self.assertEqual(self.sync().changes, [])

        def test_the_log_is_capped(self):
            path = os.path.join(self.dir, 'library.log')
            for run in range(4):
                append_change_log(path, [{'book_id': run * 10 + i, 'column': '#c', 'old': None, 'new': True}
                                         for i in range(10)], self.AT, max_lines=25)
            got = [c['book_id'] for c in self.log_lines(path)]
            self.assertEqual(got, list(range(15, 20)) + list(range(20, 40)))

    class FirstSyncConfirmationTests(Base):
        '''H1/M4 (review): the first sync into a newly chosen read column waits for the
        user's OK, which shows how many cells it would fill, before writing any of them.'''

        def setUp(self):
            Base.setUp(self)
            self.db.field_metadata = FakeFieldMetadata(
                dict(COLS, **{'#read': {'datatype': 'bool', 'name': 'Have read'}}))
            self.db.values[('#read', 2)] = False
            self.flags(**{u: {'value': True, 'updated_at': stamp(9)} for u in ('uuid-a', 'uuid-b', 'uuid-c')})
            self.positions(**{'uuid-a': {'progress': 0.5, 'updated_at': stamp(9)}})

        def test_first_sync_into_a_chosen_column_waits_for_an_ok_with_counts(self):
            before = self.file_bytes()
            res = self.sync(dict(SETTINGS, read_column='#read'))
            self.assertEqual(res.confirm, {'column': '#read', 'heading': 'Have read', 'fill': 2, 'change': 1})
            self.assertEqual([w for w in self.db.writes if w[0] == '#read'], [])
            self.assertEqual(self.v('#read_percent', 1), 50)              # everything else syncs
            self.assertEqual(res.flags_updated, 0)
            self.assertNotIn('#read', self.flag_blobs)                    # nothing remembered either
            self.assertEqual(self.file_bytes(), before)                   # and no stamps
            self.assertTrue(any('2 empty' in n and '#read' in n for n in res.notes), res.notes)
            self.assertIn('needs attention', res.status_line())
            res = self.sync(dict(SETTINGS, read_column='#read'))          # still waiting
            self.assertIsNotNone(res.confirm)
            res = self.sync(dict(SETTINGS, read_column='#read'), approved={'#read'})
            self.assertIsNone(res.confirm)
            self.assertEqual(res.flags_updated, 3)
            self.assertEqual([self.v('#read', b) for b in (1, 2, 3)], [True, True, True])
            self.flags(**{'uuid-a': {'value': False, 'updated_at': stamp(10)}})
            res = self.sync(dict(SETTINGS, read_column='#read'))          # later runs need no OK
            self.assertIsNone(res.confirm)
            self.assertIs(self.v('#read', 1), False)

        def test_default_column_without_a_choice_needs_no_ok(self):
            res = self.sync()                                             # 1.2.x: #read_status, never chosen
            self.assertIsNone(res.confirm)
            self.assertEqual(res.flags_updated, 3)

        def test_a_chosen_read_status_without_memory_waits_too(self):
            res = self.sync(dict(SETTINGS, read_column='#read_status'))
            self.assertEqual(res.confirm['fill'], 3)

        def test_no_ok_needed_when_nothing_would_change(self):
            self.db.values.update({('#read', b): True for b in (1, 2, 3)})
            res = self.sync(dict(SETTINGS, read_column='#read'))
            self.assertIsNone(res.confirm)
            self.assertIn('#read', self.flag_blobs)

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
            # replaced whole (the earlier apply's stamps go with the old entry) and, since
            # calibre holds this value, stamped in the same write (1.3.0)
            self.assertEqual(doc['books']['uuid-a'], {
                'value': True, 'updated_at': edited_at, 'applied_value': True,
                'applied_at': format_iso(ceil_ms(self.v('last_modified', 1)))})
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
            res = self.sync(dict(SETTINGS, read_column='#read'), approved={'#read'})
            self.assertIs(self.v('#read', 1), True)
            self.assertIs(self.v('#read', 2), False)
            self.assertIsNone(self.v('#read_status', 1))     # the default column is left alone
            self.assertEqual((res.read_column, res.notes), ('#read', []))
            self.assertIn('Read status (#read): 2 book(s) updated.', res.summary())
            self.assertEqual(res.status_line(), 'updated 2 read status (#read).')
            self.assertEqual(sorted(self.flag_blobs), ['#read'])

        def test_non_bool_choice_writes_nothing(self):
            # 1.3.1 (M1): no fallback into #read_status
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            res = self.sync(dict(SETTINGS, read_column='#finished'))
            self.assertIsNone(self.v('#read_status', 1))
            self.assertIsNone(self.v('#finished', 1))         # a Date column is never written
            self.assertIsNone(res.read_column)
            self.assertEqual(len(res.notes), 1)
            self.assertIn('#finished, is a datetime column, not Yes/No', res.notes[0])
            self.assertIn('not written into any other column', res.notes[0])

        def test_missing_choice_writes_nothing_and_says_restart(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            res = self.sync(dict(SETTINGS, read_column='#done'))
            self.assertIsNone(self.v('#read_status', 1))
            self.assertIn('#done, is not in this library (deleted or renamed? If you just created '
                          'it, restart calibre)', res.notes[0])

        def test_no_usable_column_at_all_writes_nothing(self):
            custom = {k: v for k, v in self.CUSTOM.items() if k != '#read_status'}
            self.db.field_metadata = FakeFieldMetadata(custom)
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            res = self.sync(dict(SETTINGS, read_column='#genre'))
            self.assertEqual([w for w in self.db.writes if w[0] in ('#genre', '#read_status')], [])
            self.assertIsNone(res.read_column)
            self.assertIn('was not synced', res.notes[0])

        def test_changing_the_read_column_does_not_carry_the_memory_over(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            self.assertEqual(self.sync().flags_updated, 1)    # into #read_status
            # The user picks #read (empty). With one shared memory the entry would be
            # "already applied" and #read would never get it.
            res = self.sync(dict(SETTINGS, read_column='#read'), approved={'#read'})
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

        def test_explicit_choice_missing_writes_nothing(self):
            # M1 (review): deleted, renamed, or created and calibre not restarted yet
            self.db.values[('#read_status', 1)] = False
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(9)}})
            res = self.sync(dict(SETTINGS, read_column='#gone'))
            self.assertEqual(self.db.writes, [])
            self.assertIsNone(res.read_column)
            self.assertEqual(len(res.notes), 1)
            self.assertIn('#gone', res.notes[0])
            self.assertNotIn('instead', res.notes[0])
            self.assertIs(self.v('#read_status', 1), False)

        def test_explicit_choice_unusable_still_syncs_progress(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(9)}})
            self.positions(**{'uuid-a': {'progress': 0.5, 'updated_at': stamp(9)}})
            for choice in ('#gone', '#finished'):           # missing; a Date column
                res = self.sync(dict(SETTINGS, read_column=choice))
                self.assertIsNone(res.read_column, choice)
                self.assertIsNone(self.v('#read_status', 1), choice)
                self.assertIsNone(self.v('#finished', 1), choice)
            self.assertEqual(self.v('#read_percent', 1), 50)
            self.assertEqual(self.flag_blobs.get('#read_status', {}), {})   # no memory taken for it either

        def test_no_choice_saved_keeps_the_read_status_default(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(9)}})
            settings = settings_to_store('#read_percent', '#last_read', 1, True, read_column=None)
            res = self.sync(normalise_settings(settings))   # normalised twice, as ui.py does
            self.assertEqual(res.read_column, '#read_status')
            self.assertIs(self.v('#read_status', 1), True)

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
            self.assertEqual([c for c, _ in existing_read_columns(cols)], ['#hr', '#fin'])
            hint = read_column_hint(dict(cols, **self.CUSTOM), exclude=('#hr', '#read_status'), where='above')
            self.assertIn('Have read (#read) (also: Finished (#fin))', hint)
            self.assertTrue(hint.endswith('pick it as the read status column above.'))
            self.assertNotIn('#hr', hint)

    class ReadColumnSuggestionTests(unittest.TestCase):
        '''H1 (review 2026-11-02): only an explicit read column is ever named, never one
        that merely contains "read", "done", "finish" or "complete" (or means the opposite),
        and Settings saves a read column only when the user picked one.'''

        OPPOSITES = {'#abandoned': 'Abandoned', '#dnf': 'Did not finish', '#unfinished': 'Unfinished',
                     '#incomplete': 'Incomplete', '#proofread': 'Proofread', '#ready': 'Ready',
                     '#already_owned': 'Already owned', '#readaloud': 'Read aloud', '#thread': 'Thread',
                     '#not_read': 'Not read', '#done_reading_soon': 'Done reading soon'}

        def test_suggestion_never_offers_an_opposite_column(self):
            for lookup, heading in self.OPPOSITES.items():
                cols = {lookup: {'datatype': 'bool', 'name': heading}}
                self.assertEqual(suggest_read_column(cols), '#read_status', lookup)
                self.assertEqual(existing_read_columns(cols), [], lookup)
                self.assertEqual(read_column_hint(cols), '', lookup)

        def test_only_explicit_read_columns_are_named(self):
            for lookup, heading in (('#read', 'Have read'), ('#have_read', 'Have read'),
                                    ('#isread', 'Is read'), ('#finished', 'Finished'),
                                    ('#ko_statusbool', 'KOReader status'), ('#x1', 'Read'),
                                    ('#x2', 'Have Read'), ('#x3', 'read status')):
                cols = {lookup: {'datatype': 'bool', 'name': heading}}
                self.assertEqual(suggest_read_column(cols), lookup, lookup)
            # a heading that only contains one of the words is not explicit
            self.assertEqual(suggest_read_column({'#x': {'datatype': 'bool', 'name': 'Read in 2024'}}),
                             '#read_status')

        def test_untouched_read_choice_is_not_stored(self):
            self.assertNotIn('read_column', settings_to_store('#read_percent', '', 1, True, read_column=None))
            self.assertEqual(settings_to_store('', '', 1, True, read_column='#read')['read_column'], '#read')

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
            self.assertEqual((res.flags_updated, res.flags_stamped), (2, 2))   # a applied, c agreeing
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
            c = doc['books']['uuid-c']                                   # calibre already agreed
            self.assertEqual((c['value'], c['updated_at'], c['applied_value']), (True, stamp(2), True))
            self.assertEqual(parse_iso(c['applied_at']), ceil_ms(self.v('last_modified', 3)))
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

        def test_agreeing_entry_is_stamped_once(self):
            self.db.values[('#read_status', 1)] = True               # calibre already says Yes
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}, 'uuid-b': True})
            self.db.values[('#read_status', 2)] = True
            res = self.sync()
            self.assertEqual((res.flags_updated, res.flags_stamped), (0, 1))
            doc = self.read_file()
            a = doc['books']['uuid-a']
            self.assertEqual((a['value'], a['updated_at'], a['applied_value']), (True, stamp(2), True))
            self.assertEqual(parse_iso(a['applied_at']), ceil_ms(self.v('last_modified', 1)))
            self.assertIs(doc['books']['uuid-b'], True)              # bare bool untouched
            before = self.file_bytes()
            self.db.tick(3600)
            self.db.desktop_edit('rating', 1, 6)                     # unrelated edit: no re-stamp
            self.assertEqual(self.sync().flags_stamped, 0)
            self.assertEqual(self.file_bytes(), before)

        def test_already_retired_agreeing_entries_are_stamped_once(self):
            # Upgrade from 1.2.5: entries retired long ago, calibre agrees, no stamps yet.
            self.write(SYNC_FILE, {'schema': 1, 'updated_at': stamp(1), 'books': {
                'uuid-a': {'value': False, 'updated_at': stamp(2)}}})
            self.db.values[('#read_status', 1)] = False
            self.flag_blobs = flag_blobs_from_prefs({'uuid-a': {'stamp': stamp(2), 'lm': None, 'val': False,
                                                                'dt': None}}, None)
            self.db.tick(3600 * 24 * 5)
            self.db.desktop_edit('rating', 1, 6)
            res = self.sync()
            self.assertEqual((res.flags_updated, res.flags_skipped, res.flags_stamped), (0, 1, 1))
            self.assertEqual(self.read_file()['books']['uuid-a']['applied_value'], False)
            before = self.file_bytes()
            self.sync()
            self.assertEqual(self.file_bytes(), before)

        def test_agreeing_entry_with_a_stale_stamp_is_stamped_again(self):
            self.db.values[('#read_status', 1)] = True
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(4),
                                     'applied_value': False, 'applied_at': stamp(2)}})   # left from an older value
            res = self.sync()
            self.assertEqual((res.flags_updated, res.flags_stamped), (0, 1))
            a = self.read_file()['books']['uuid-a']
            self.assertEqual((a['value'], a['updated_at'], a['applied_value']), (True, stamp(4), True))
            self.assertEqual(parse_iso(a['applied_at']), ceil_ms(self.v('last_modified', 1)))
            before = self.file_bytes()
            self.assertEqual(self.sync().flags_stamped, 0)
            self.assertEqual(self.file_bytes(), before)

        def test_empty_cell_is_filled_and_stamped_disagreeing_cell_is_not_stamped(self):
            self.db.values[('#read_status', 2)] = False              # filled, disagrees, newer than the entry
            self.db.tick(3600 * 24 * 5)
            self.db.desktop_edit('#read_status', 2, True)
            self.db.desktop_edit('#read_status', 2, False)
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)},
                          'uuid-b': {'value': True, 'updated_at': stamp(2)}})
            res = self.sync()
            books = self.read_file()['books']
            self.assertIs(self.v('#read_status', 1), True)
            self.assertEqual(books['uuid-a']['applied_value'], True)             # filled from empty
            self.assertIs(self.v('#read_status', 2), False)                      # calibre's value kept
            self.assertEqual(books['uuid-b'], {'value': True, 'updated_at': stamp(2)})   # (M3) not pushed back
            self.assertEqual((res.flags_updated, res.flags_sent_back), (1, 0))

        def test_parser_reads_a_stamped_file_as_before(self):
            self.flags(**{'uuid-a': {'value': True, 'updated_at': stamp(2)}, 'uuid-b': True})
            before = parse_sidecar(self.read_file())
            self.sync()
            self.assertIn('applied_at', self.read_file()['books']['uuid-a'])
            self.assertEqual(parse_sidecar(self.read_file()), before)

    class PerLibraryMemoryTests(unittest.TestCase):
        '''L1 (review): the read-flag memory is kept per library, and bounded.'''

        def setUp(self):
            self.dirs = [tempfile.mkdtemp(), tempfile.mkdtemp()]
            self.dbs = [FakeDB(COLS, {1: 'uuid-a', 2: 'uuid-b'}) for _ in self.dirs]
            self.prefs = {}

        def tearDown(self):
            for d in self.dirs:
                shutil.rmtree(d)

        def flags(self, i, **books):
            with open(os.path.join(self.dirs[i], SYNC_FILE), 'w', encoding='utf-8') as f:
                json.dump({'schema': 1, 'updated_at': stamp(1), 'books': books}, f)

        def sync_lib(self, i):
            return sync_library(self.dbs[i], self.dirs[i], self.prefs)

        def test_two_copies_of_a_library_do_not_share_memory(self):
            # same uuids AND the same library_id (a copied metadata.db), different values
            self.dbs[0].values[('#read_status', 1)] = True
            self.dbs[1].values[('#read_status', 1)] = False
            for i in (0, 1):
                self.flags(i, **{'uuid-a': {'value': True, 'updated_at': '2026-09-20T12:00:00.000Z'}})
            sent = []
            for i in (0, 1, 0, 1, 0, 1):
                sent.append(self.sync_lib(i).flags_sent_back)
            self.assertEqual(sent, [0] * 6)
            self.assertEqual(len(self.prefs['libraries']), 2)

        def test_memory_is_kept_for_a_bounded_number_of_libraries(self):
            self.flags(0, **{'uuid-a': True})
            for n in range(MAX_LIBRARIES + 3):
                self.dbs[0].library_id = 'lib-{}'.format(n)
                sync_library(self.dbs[0], self.dirs[0], self.prefs,
                             now=datetime.datetime(2026, 11, 1, tzinfo=UTC) + datetime.timedelta(hours=n))
            self.assertEqual(len(self.prefs['libraries']), MAX_LIBRARIES)
            kept = sorted(k.split('-')[1] for k in self.prefs['libraries'])
            self.assertEqual(kept, sorted(str(n) for n in range(3, MAX_LIBRARIES + 3)))

        def test_the_12x_memory_moves_to_the_first_library_that_syncs(self):
            self.dbs[0].values[('#read_status', 1)] = True
            self.prefs.update({'applied': {'uuid-a': {'stamp': stamp(2), 'lm': None, 'val': True, 'dt': None}},
                               'applied_cols': {}, 'applied_pos': {}})
            self.flags(0, **{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            res = self.sync_lib(0)
            self.assertEqual((res.flags_updated, res.flags_skipped), (0, 1))      # the 1.2.x record was used
            self.assertNotIn('applied', self.prefs)
            (state,) = self.prefs['libraries'].values()
            self.assertIn('uuid-a', state['applied'])
            self.flags(1, **{'uuid-a': {'value': True, 'updated_at': stamp(2)}})
            self.assertEqual(self.sync_lib(1).flags_updated, 1)                        # a second library starts fresh

    class SettingsTests(unittest.TestCase):
        def test_read_column_round_trip(self):
            db = FakeDB(COLS, {})
            db.set_pref(SETTINGS_KEY, json.loads(json.dumps(
                settings_to_store('', '', 1, True, read_column='#read'))))
            self.assertEqual(normalise_settings(db.pref(SETTINGS_KEY))['read_column'], '#read')
            self.assertNotIn('read_column', settings_to_store('', '', 1, read_column=None))   # 1.3.1
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

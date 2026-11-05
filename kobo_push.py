# Last modified: 2026-11-05--0413
#
# kobo_push.py — CalibReader's reading place to the Kobo, through Calibre-Web
# (step 2 of the Kobo sync, plugin 1.5.0, tester preview).
#
# The CalibReader apps write each book's place into CalibReader_positions.json.
# When the setting is on (Settings -> Kobo, off by default; the CLI's --push), a
# run sends a book's place to Calibre-Web exactly as a Kobo does:
#     PUT <Calibre-Web>/kobo/<token>/v1/library/<book uuid>/state
# Calibre-Web stores it (server-time stamp) and hands it to the Kobo as
# ChangedReadingState at the Kobo's next sync. Whether the Kobo then jumps
# there is the open question the 1.5.0 tester run answers.
#
#   * app.db is only READ (sqlite mode=ro): the chosen user's Kobo rows and that
#     user's Kobo token. Calibre-Web itself writes the place. The token is never
#     printed, logged or kept: every note and log line is scrubbed of it.
#   * Forward-only: a book is sent only when the app's entry is strictly newer
#     than the Kobo's bookmark AND ahead of the Kobo's place in reading order
#     (chapter, then textAnchor; the app's progress against the Kobo's % when
#     the Kobo's place is % only). No Kobo place yet: sent.
#   * Precision: with the book's KEPUB in the library, the KoboSpan the app's
#     sentence is in (kobo_position.kobo_location); without, the chapter (Source)
#     and the two percentages.
#   * Echo guard: what was sent is remembered per book (`memory`, the plugin's
#     prefs or the CLI's state file, never the shared sidecar); step 1's import
#     skips a Kobo bookmark equal to it (kobo_import.is_echo).
#   * A failing Calibre-Web (down, wrong address, token refused, an answer that
#     is not Success) stops the run's sending with one note; nothing is retried
#     and no follow-up run is asked for.
#
# See docs/handoffs/104-kobo-sync/DESIGN.md, "Step 2: CalibReader -> Kobo".
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

from __future__ import annotations

__license__   = 'GPL v3'
__copyright__ = '2026, CalibReader'

import datetime as _dt
import http.client
import json
import os
import sqlite3
import urllib.error
import urllib.request

try:
    from calibre_plugins.calibreader_sync.kobo_import import (
        FUTURE_SLACK, KOBO_DEVICE_ID, MAX_BOOKS, POSITIONS_FILE, _connect_ro, _is_newer, _load_positions,
        _parse_iso, import_positions, kobo_stamp, pick_user, served_library,
    )
    from calibre_plugins.calibreader_sync.kobo_position import (
        BOOK_PERCENT, CHAPTER_PERCENT, CHAPTER_START, SPAN, Book, kobo_location, map_bookmark,
    )
except ImportError:  # pragma: no cover - standalone / test import
    from kobo_import import (
        FUTURE_SLACK, KOBO_DEVICE_ID, MAX_BOOKS, POSITIONS_FILE, _connect_ro, _is_newer, _load_positions,
        _parse_iso, import_positions, kobo_stamp, pick_user, served_library,
    )
    from kobo_position import (
        BOOK_PERCENT, CHAPTER_PERCENT, CHAPTER_START, SPAN, Book, kobo_location, map_bookmark,
    )

VERSION = '1.5.0'
DEFAULT_CALIBRE_WEB = 'http://127.0.0.1:8083'
DEFAULT_STATE_FILE = os.path.join('~', '.calibreader_sync', 'kobo_push_state.json')
TIMEOUT = 10                   # seconds per request


def push_enabled(entry):
    '''The Settings switch: on only when explicitly True (off by default).'''
    return isinstance(entry, dict) and entry.get('push') is True


def calibre_web_url(entry):
    url = ((entry or {}).get('cw_url') or '').strip().rstrip('/')
    return url or DEFAULT_CALIBRE_WEB


class KoboPushResult(object):
    def __init__(self):
        self.user = None
        self.sent = []             # uuids whose place Calibre-Web accepted
        self.skipped = {}          # uuid -> why not sent
        self.precisions = {}       # uuid -> kobo_position precision used
        self.notes = []            # things that need attention
        self.lines = []            # what was sent, with the precision (the run's result)
        self.more = False          # L1: books left for the next run (never set by a failure)

    def status_line(self):
        if self.sent:
            return 'Kobo: {} reading place{} sent'.format(len(self.sent), '' if len(self.sent) == 1 else 's')
        return ''


# Books whose push Calibre-Web refused for the book itself (it does not have it,
# a malformed body), per calibre session: (library, app.db, uuid) -> the app
# entry's stamp. Not tried again until the app's place changes (N1).
_FAILED = {}


def forget_failures():
    _FAILED.clear()


def _failure_key(library_path, app_db, uuid):
    return (os.path.realpath(library_path), os.path.realpath(app_db), uuid.lower())


def is_app_entry(entry):
    '''An entry written by a CalibReader device (not step 1's Kobo entries).'''
    if not isinstance(entry, dict):
        return False
    device = entry.get('deviceId')
    if not isinstance(device, str) or not device:
        return False
    return device != KOBO_DEVICE_ID and not device.lower().startswith('kobo-')


def _has_place(row):
    return bool((row.get('location_source') or '').strip() or (row.get('location_value') or '').strip()
                or row.get('progress_percent') is not None or row.get('content_source_progress_percent') is not None)


def read_kobo_states(app_db, user=None):
    '''-> (rows, user name, token, notes). rows: every Kobo reading state of the
    chosen user (Calibre-Web creates one, with an empty bookmark, for each book
    it syncs to the Kobo), app.db column names + book_id + the sidecar-format
    stamp. token: that user's Kobo token (never printed), or None.'''
    if not app_db or not os.path.isfile(app_db):
        return [], None, None, ['Calibre-Web app.db not found: {}'.format(app_db)]
    try:
        con = _connect_ro(app_db)
    except sqlite3.Error as exc:
        return [], None, None, ['Could not open Calibre-Web app.db read-only ({}).'.format(exc)]
    try:
        cols = {r[1] for r in con.execute('PRAGMA table_info(user)')}
        users = con.execute('SELECT id, name, {} FROM user'.format('role' if 'role' in cols else '0')).fetchall()
        raw = con.execute('''SELECT rs.user_id, rs.book_id, b.last_modified, b.location_source, b.location_type,
                                    b.location_value, b.progress_percent, b.content_source_progress_percent
                             FROM kobo_reading_state rs LEFT JOIN kobo_bookmark b ON b.kobo_reading_state_id = rs.id
                          ''').fetchall()
        uid, notes = pick_user(users, [r[0] for r in raw], user)
        token = None
        if uid is not None:
            try:
                row = con.execute('SELECT auth_token FROM remote_auth_token WHERE user_id=? AND token_type=1 '
                                  'ORDER BY id LIMIT 1', (uid,)).fetchone()
                token = row[0] if row and row[0] else None
            except sqlite3.Error:
                token = None
    except sqlite3.Error as exc:
        return [], None, None, ['This does not look like a Calibre-Web app.db ({}).'.format(exc)]
    finally:
        con.close()
    if uid is None:
        return [], None, None, notes
    names = {i: n for i, n, _ in users}
    rows = {}
    for (u, book_id, lm, src, ltype, val, pct, cpct) in raw:
        if u != uid:
            continue
        row = {'book_id': book_id, 'updated_at': kobo_stamp(lm), 'location_source': src, 'location_type': ltype,
               'location_value': val, 'progress_percent': pct, 'content_source_progress_percent': cpct}
        prev = rows.get(book_id)
        if prev is None or _is_newer(row['updated_at'], prev['updated_at']):
            rows[book_id] = row
    return sorted(rows.values(), key=lambda r: r['book_id']), names.get(uid), token, notes


def compare(entry, kobo_place, row):
    '''The app's place against the Kobo's: 'ahead', 'same', 'behind' or 'unknown'.'''
    if kobo_place is None:
        return 'ahead'                      # no Kobo place yet
    mine = (entry['chapter'], entry['textAnchor'])
    precision = kobo_place['precision']
    if precision == BOOK_PERCENT:           # the Kobo's place is % only: the app's progress
        try:
            app_pct, kobo_pct = 100.0 * float(entry.get('progress')), float(row.get('progress_percent'))
        except (TypeError, ValueError):
            return 'unknown'
        return 'ahead' if app_pct > kobo_pct else ('same' if app_pct == kobo_pct else 'behind')
    if precision == CHAPTER_START:          # Source only: the Kobo could be anywhere in that chapter
        if mine[0] == kobo_place['chapter']:
            return 'unknown'
        return 'ahead' if mine[0] > kobo_place['chapter'] else 'behind'
    theirs = (kobo_place['chapter'], kobo_place['textAnchor'])
    return 'ahead' if mine > theirs else ('same' if mine == theirs else 'behind')


def state_body(uuid, loc, now_iso):
    '''The PUT body, shaped like a Kobo's (FINDINGS). StatusInfo and Statistics
    are null: Calibre-Web then leaves the read status and the reading
    statistics alone; only the bookmark changes.'''
    return {'ReadingStates': [{
        'EntitlementId': uuid,
        'Created': now_iso,
        'LastModified': now_iso,
        'PriorityTimestamp': now_iso,
        'StatusInfo': None,
        'Statistics': None,
        'CurrentBookmark': {
            'LastModified': now_iso,
            'ProgressPercent': loc['progressPercent'],
            'ContentSourceProgressPercent': loc['contentSourceProgressPercent'],
            'Location': {'Value': loc['value'], 'Type': loc['type'], 'Source': loc['source']},
        },
    }]}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                         # a redirect is an answer, never followed


def _send(url, body, timeout):
    '''One PUT. -> (HTTP status, body bytes). Never through a system proxy (the
    token is in the path), never following a redirect. Network errors raise.'''
    req = urllib.request.Request(url, data=body, method='PUT', headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'User-Agent': 'CalibReaderSync/' + VERSION})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b''
        return exc.code, raw


def scrub(text, token):
    text = str(text)
    return text.replace(token, '<token>') if token else text


def put_state(base_url, token, uuid, loc, user_name=None, timeout=TIMEOUT):
    '''-> (ok, book_specific, note). The note never holds the token.'''
    url = '{}/kobo/{}/v1/library/{}/state'.format(base_url, token, uuid)
    body = json.dumps(state_body(uuid, loc, _kobo_now())).encode('utf-8')
    try:
        status, raw = _send(url, body, timeout)
    except ValueError:
        return False, False, ('The Calibre-Web address "{}" in Settings is not a web address (like {}); '
                              'nothing sent to the Kobo.'.format(base_url, DEFAULT_CALIBRE_WEB))
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, 'reason', None) or exc
        return False, False, scrub('Calibre-Web at {} did not answer ({}); CalibReader\'s places were not sent '
                                   'to the Kobo. They go with the next sync.'.format(base_url, reason), token)
    if status == 401:
        return False, False, ('Calibre-Web at {} did not accept the Kobo token of {} (is Kobo sync on for that '
                              'user?); nothing sent to the Kobo.'.format(base_url, user_name or 'this user'))
    if 300 <= status < 400:
        return False, False, ('Calibre-Web at {} answered with a redirect (HTTP {}); nothing sent to the Kobo. '
                              'Check the address in Settings.'.format(base_url, status))
    if status == 400:
        return False, True, ('Calibre-Web at {} did not accept the place for book {} (HTTP 400); nothing more '
                             'sent this run.'.format(base_url, uuid))
    if status != 200:
        return False, False, ('Calibre-Web at {} answered HTTP {}; nothing more sent to the Kobo this run.'
                              .format(base_url, status))
    try:
        answer = json.loads(raw.decode('utf-8') or '{}')
        result = answer['UpdateResults'][0]['CurrentBookmarkResult']['Result']
        ok = answer.get('RequestResult') == 'Success' and result == 'Success'
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        ok = False
    if not ok:
        return False, True, ('Calibre-Web at {} does not have book {} (it answered without storing the place): '
                             'is it serving this calibre library? Nothing more sent this run.'.format(base_url, uuid))
    return True, False, None


def _kobo_now():
    return _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _precision_lines(precisions):
    span = sum(1 for p in precisions.values() if p == SPAN)
    pct = len(precisions) - span
    lines = []
    if span:
        lines.append('Kobo: CalibReader\'s place sent for {} book{} to the sentence (KoboSpan, from the KEPUB).'
                     .format(span, '' if span == 1 else 's'))
    if pct:
        lines.append('Kobo: CalibReader\'s place sent for {} book{} by chapter % (no KEPUB in the library; '
                     'the Kobo gets the percentages only).'.format(pct, '' if pct == 1 else 's'))
    return lines


def push_positions(library_path, app_db, lookup, base_url=DEFAULT_CALIBRE_WEB, memory=None, user=None, log=None,
                   max_books=MAX_BOOKS, now=None, timeout=TIMEOUT):
    '''One push run. `lookup(book_id)` -> (uuid, EPUB path, KEPUB path) or None,
    as for the import. `memory`: {uuid (lower case): what was last sent}, updated
    in place for each book Calibre-Web accepted (the caller saves it).'''
    res = KoboPushResult()
    memory = {} if memory is None else memory
    base_url = (base_url or DEFAULT_CALIBRE_WEB).strip().rstrip('/')
    if not app_db or not os.path.isfile(app_db):
        res.notes.append('Calibre-Web app.db not found: {}'.format(app_db))
        return res
    library_state, served, note = served_library(app_db, library_path)
    if library_state == 'other':
        res.notes.append(note)
        return res
    rows, res.user, token, notes = read_kobo_states(app_db, user)
    res.notes.extend(notes)
    if res.user is None:
        return res
    data, err = _load_positions(os.path.join(library_path, POSITIONS_FILE))
    if err:
        res.notes.append(err)
        return res
    on_kobo = {}
    for row in rows:
        info = lookup(row['book_id'])
        if info and info[0]:
            on_kobo[info[0].lower()] = (row, info)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    future = unproven = attempted = 0
    to_send = []
    for key in sorted(data['positions']):
        entry = data['positions'][key]
        if not is_app_entry(entry):
            continue
        uuid = key.lower()
        stamp = _parse_iso(entry.get('updated_at'))
        if stamp is None:
            res.skipped[uuid] = 'no usable updated_at'
            continue
        if stamp - now > FUTURE_SLACK:
            future += 1
            continue
        if isinstance(entry.get('chapter'), bool) or not isinstance(entry.get('chapter'), int) \
                or isinstance(entry.get('textAnchor'), bool) or not isinstance(entry.get('textAnchor'), int):
            res.skipped[uuid] = 'no textAnchor (an app before 1.0.1): no sentence to send'
            continue
        if uuid not in on_kobo:
            res.skipped[uuid] = 'not on the Kobo (Calibre-Web has not synced it to {}\'s Kobo)'.format(res.user)
            continue
        row, (book_uuid, epub_path, kepub_path) = on_kobo[uuid]
        sent = memory.get(uuid)
        if isinstance(sent, dict) and sent.get('app_updated_at') == entry['updated_at']:
            res.skipped[uuid] = 'already sent'
            continue
        fkey = _failure_key(library_path, app_db, uuid)
        if _FAILED.get(fkey) == entry['updated_at']:
            res.skipped[uuid] = 'Calibre-Web refused it before; tried again when the place changes'
            continue
        has_place = _has_place(row)
        if has_place and not _is_newer(entry['updated_at'], row['updated_at']):
            res.skipped[uuid] = 'the Kobo\'s place is as new or newer'
            continue
        if library_state != 'same' and not (row.get('location_source') or '').strip():
            # Book ids unproven: only the Kobo's own Source in this book (checked by
            # map_bookmark below, M4) shows Calibre-Web's book is this one.
            unproven += 1
            res.skipped[uuid] = 'Calibre-Web\'s library is not visible here and the Kobo has no place in this book'
            continue
        if not epub_path or not os.path.isfile(epub_path):
            res.skipped[uuid] = 'no EPUB (CalibReader reads the EPUB)'
            continue
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
            kobo_place = map_bookmark(epub, row, kepub) if has_place else None
            if has_place and kobo_place is None:
                # Its Source is in neither the EPUB nor the KEPUB (M4): another book?
                res.skipped[uuid] = 'the Kobo\'s place does not match this book'
                continue
            if not (0 <= entry['chapter'] < len(epub.chapters)):
                res.skipped[uuid] = 'the app\'s chapter is not in this book'
                continue
            order = compare(entry, kobo_place, row)
            if order != 'ahead':
                res.skipped[uuid] = {'same': 'the same place as the Kobo', 'behind': 'behind the Kobo (forward only)',
                                     'unknown': 'not clearly ahead of the Kobo'}[order]
                continue
            loc = kobo_location(epub, entry['chapter'], entry['textAnchor'], kepub,
                                kobo_source=row.get('location_source'))
        except Exception as exc:
            res.skipped[uuid] = 'could not read the book ({})'.format(exc)
            continue
        finally:
            for b in (epub, kepub):
                if b is not None:
                    b.close()
        if loc is None:
            res.skipped[uuid] = 'the app\'s place is not in this book'
            continue
        to_send.append((uuid, book_uuid, entry, loc, fkey))
    if future:
        res.notes.append('{} CalibReader place(s) are stamped more than 10 minutes in the future: an iPhone or iPad '
                         'clock looks wrong. Not sent to the Kobo.'.format(future))
    if unproven:
        res.notes.append('Calibre-Web\'s library folder ({}) is not visible on this computer, so CalibReader '
                         'cannot confirm it is this library: only books the Kobo has already opened are sent '
                         '({} held back).'.format(served or 'unknown', unproven))
    if not to_send:
        return res
    if not token:
        res.notes.append('Calibre-Web has no Kobo sync token for {}: set up Kobo sync for that user in '
                         'Calibre-Web. Nothing sent to the Kobo.'.format(res.user))
        return res
    for uuid, book_uuid, entry, loc, fkey in to_send:
        ok, book_specific, note = put_state(base_url, token, book_uuid, loc, res.user, timeout)
        if not ok:
            res.notes.append(scrub(note, token))
            if book_specific:
                _FAILED[fkey] = entry['updated_at']
            res.more = False                # a failing run never asks for another
            break
        memory[uuid] = {
            'type': loc['type'], 'value': loc['value'], 'source': loc['source'],
            'progressPercent': loc['progressPercent'],
            'contentSourceProgressPercent': loc['contentSourceProgressPercent'],
            'precision': loc['precision'], 'chapter': loc['chapter'], 'textAnchor': loc['anchor'],
            'app_updated_at': entry['updated_at'], 'pushed_at': _kobo_now(),
        }
        res.sent.append(book_uuid)
        res.precisions[book_uuid] = loc['precision']
        if log:
            log(scrub('Kobo push {} -> {} {} in {} ({}; app chapter {} textAnchor {}, lands at {})'.format(
                book_uuid, loc['type'] or 'Source', loc['value'] or '', loc['source'], loc['precision'],
                entry['chapter'], entry['textAnchor'], loc['anchor']), token))
    res.lines = _precision_lines(res.precisions)
    return res


def sync_kobo(library_path, app_db, lookup, user=None, push=False, base_url=DEFAULT_CALIBRE_WEB, memory=None,
              log=None, max_books=MAX_BOOKS, now=None, timeout=TIMEOUT):
    '''One Kobo run: step 1's import (with the echo guard), then, only when
    `push` is True, this module's push. -> (import result, push result or None).'''
    memory = {} if memory is None else memory
    kres = import_positions(library_path, app_db, lookup, user=user, log=log, max_books=max_books, now=now,
                            pushed=memory)
    if not push:
        return kres, None
    pres = push_positions(library_path, app_db, lookup, base_url=base_url, memory=memory, user=user, log=log,
                          max_books=max_books, now=now, timeout=timeout)
    pres.notes = [n for n in pres.notes if n not in kres.notes]
    return kres, pres


class StateFile(object):
    '''The CLI's memory of what was sent (the plugin keeps it in calibre's prefs):
    {library folder (real path): {uuid: record}} in one JSON file on this computer.'''

    def __init__(self, path, library_path):
        self.path = os.path.expanduser(path)
        self.key = os.path.realpath(library_path)
        self.table = {}
        try:
            with open(self.path, 'rb') as f:
                table = json.loads(f.read().decode('utf-8'))
            if isinstance(table, dict):
                self.table = table
        except (OSError, ValueError):
            pass
        mine = self.table.get(self.key)
        self.memory = dict(mine) if isinstance(mine, dict) else {}

    def save(self):
        self.table[self.key] = self.memory
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(self.table, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, self.path)


def load_state(path, library_path):
    return StateFile(path, library_path)

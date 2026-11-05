# Last modified: 2026-11-05--0516
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
#   * Only places the app wrote AFTER sending was turned on (`since`, review
#     round 1 B3): ticking the box never sends old places.
#   * Forward-only, twice. First in CalibReader's terms: the app's entry is
#     strictly newer than the Kobo's bookmark AND ahead of the Kobo's place
#     (chapter, then textAnchor; the app's progress when the Kobo's place is %
#     only). Then, just before each PUT, in the KOBO'S OWN terms (round 1 B1/B2):
#     span against span by kobo.P.S (same file) or spine order (other file);
#     anything else by the % the Kobo will get against its own %, plus a margin.
#     What cannot be compared is not sent. No Kobo place yet: sent.
#   * Just before each PUT the Kobo's row is read again; if the Kobo synced
#     meanwhile, that book waits for the next run (S3).
#   * Precision: with the book's KEPUB (listed by calibre, or beside the EPUB
#     when Calibre-Web added it while calibre ran: S2), the KoboSpan the app's
#     sentence is in (kobo_position.kobo_location); otherwise Source and the two
#     percentages.
#   * Echo guard: what was sent is remembered per book (`memory`: the plugin's
#     prefs or the CLI's state file, never the shared sidecar); step 1's import
#     skips our own push coming back, also in the Kobo's own terms (S1).
#   * Every request has a hard wall-clock limit (name lookup included) and a run
#     starts no PUT after its budget (S4); in calibre the run is on a worker
#     thread (ui.py). A failing Calibre-Web stops the run's sending with one
#     note; nothing is retried and no follow-up run is asked for.
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
import re
import sqlite3
import threading
import time
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
TIMEOUT = 10                   # seconds, wall clock, per request (name lookup included)
RUN_BUDGET = 20                # seconds: no PUT starts later than this into a run; the rest go next run
BACKOFF = 300                  # seconds (ui.py): after Calibre-Web failed, automatic runs do not send
PERCENT_MARGIN = 0.5           # % points: a %-place must beat the Kobo's own % by more than this
_KOBO_SPAN = re.compile(r'^\s*kobo\.(\d+)\.(\d+)\s*$')


def push_enabled(entry):
    '''The Settings switch: on only when explicitly True (off by default).'''
    return isinstance(entry, dict) and entry.get('push') is True


def clean_address(url):
    '''-> (Calibre-Web address, True when a pasted Kobo sync link was cut). Calibre-Web's
    Kobo page shows only http://host:8083/kobo/<token>: everything from /kobo/ on is
    dropped (N7), so the token is never used as part of the address or stored.'''
    url = (url or '').strip()
    i = url.lower().find('/kobo/')
    if i < 0 and url.lower().endswith('/kobo'):
        i = len(url) - len('/kobo')
    cut = i >= 0
    if cut:
        url = url[:i]
    return url.rstrip('/'), cut


def calibre_web_url(entry):
    url, _ = clean_address((entry or {}).get('cw_url'))
    return url or DEFAULT_CALIBRE_WEB


def now_iso():
    t = _dt.datetime.now(_dt.timezone.utc)
    return t.strftime('%Y-%m-%dT%H:%M:%S.') + '{:03d}Z'.format(t.microsecond // 1000)


def kepub_beside(epub_path):
    '''The KEPUB Calibre-Web's kepubify saved next to the EPUB (same base name), even
    when calibre, which was running at the time, does not list it yet (S2).'''
    if not epub_path:
        return None
    base = os.path.splitext(epub_path)[0]
    for ext in ('.kepub', '.kepub.epub'):
        if os.path.isfile(base + ext):
            return base + ext
    return None


def with_kepub_beside(lookup):
    '''`lookup` -> (uuid, EPUB, KEPUB), with the KEPUB found on disk when not listed.'''
    def wrapped(book_id):
        info = lookup(book_id)
        if not info:
            return info
        uuid, epub, kepub = info
        if (not kepub or not os.path.isfile(kepub)) and epub:
            kepub = kepub_beside(epub) or kepub
        return uuid, epub, kepub
    wrapped.kepub_beside = True
    return wrapped


class KoboPushResult(object):
    def __init__(self):
        self.user = None
        self.sent = []             # uuids whose place Calibre-Web accepted
        self.skipped = {}          # uuid -> why not sent
        self.precisions = {}       # uuid -> kobo_position precision used
        self.notes = []            # things that need attention
        self.lines = []            # what was sent, with the precision (the run's result)
        self.more = False          # L1: books left for the next run (never set by a failure)
        self.failure = None        # 'network' | 'timeout' | 'server' | 'address' | 'book' | None

    def status_line(self):
        if self.sent:
            return '{} reading place{} sent to the Kobo.'.format(len(self.sent), '' if len(self.sent) == 1 else 's')
        return ''


# Books whose push Calibre-Web refused for the book itself (it does not have it,
# a malformed body), per calibre session: (library, app.db, uuid) -> the app
# entry's stamp. Not tried again until the app's place changes (N1).
_FAILED = {}
# Books mapped and NOT sent (behind / same place / not ahead in the Kobo's terms /
# the Kobo's place not in the book / unreadable), per calibre session: key ->
# (app stamp, Kobo stamp, why). Not parsed again until either place changes (N1).
_SETTLED = {}


def forget_failures():
    _FAILED.clear()
    _SETTLED.clear()


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


_BOOKMARK_SQL = '''SELECT rs.user_id, rs.book_id, b.last_modified, b.location_source, b.location_type,
                          b.location_value, b.progress_percent, b.content_source_progress_percent, {read}
                   FROM kobo_reading_state rs LEFT JOIN kobo_bookmark b ON b.kobo_reading_state_id = rs.id {join}'''


def _bookmark_query(con, where=''):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'book_read_link' in tables:
        sql = _BOOKMARK_SQL.format(read='rl.read_status', join='LEFT JOIN book_read_link rl ON '
                                   'rl.book_id = rs.book_id AND rl.user_id = rs.user_id')
    else:
        sql = _BOOKMARK_SQL.format(read='NULL', join='')
    return sql + where


def _row(raw):
    (u, book_id, lm, src, ltype, val, pct, cpct, read_status) = raw
    return {'user_id': u, 'book_id': book_id, 'updated_at': kobo_stamp(lm), 'location_source': src,
            'location_type': ltype, 'location_value': val, 'progress_percent': pct,
            'content_source_progress_percent': cpct, 'read_status': read_status}


def read_kobo_states(app_db, user=None):
    '''-> (rows, user name, token, notes). rows: every Kobo reading state of the
    chosen user (Calibre-Web creates one, with an empty bookmark, for each book
    it syncs to the Kobo), app.db column names + user_id, book_id, the
    sidecar-format stamp and Calibre-Web's read status. token: that user's Kobo
    token (never printed), or None.'''
    if not app_db or not os.path.isfile(app_db):
        return [], None, None, ['Calibre-Web app.db not found: {}'.format(app_db)]
    try:
        con = _connect_ro(app_db)
    except sqlite3.Error as exc:
        return [], None, None, ['Could not open Calibre-Web app.db read-only ({}).'.format(exc)]
    try:
        cols = {r[1] for r in con.execute('PRAGMA table_info(user)')}
        users = con.execute('SELECT id, name, {} FROM user'.format('role' if 'role' in cols else '0')).fetchall()
        raw = con.execute(_bookmark_query(con)).fetchall()
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
    for r in raw:
        if r[0] != uid:
            continue
        row = _row(r)
        prev = rows.get(row['book_id'])
        if prev is None or _is_newer(row['updated_at'], prev['updated_at']):
            rows[row['book_id']] = row
    return sorted(rows.values(), key=lambda r: r['book_id']), names.get(uid), token, notes


def read_kobo_row(app_db, user_id, book_id):
    '''That book's Kobo row as it is NOW (S3: re-read just before the PUT), or None.'''
    try:
        con = _connect_ro(app_db)
        try:
            raws = con.execute(_bookmark_query(con, ' WHERE rs.user_id = ? AND rs.book_id = ?'),
                               (user_id, book_id)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    rows = [_row(r) for r in raws]
    best = None
    for row in rows:
        if best is None or _is_newer(row['updated_at'], best['updated_at']):
            best = row
    return best


def compare(entry, kobo_place, row):
    '''The app's place against the Kobo's, in CalibReader's terms: 'ahead', 'same',
    'behind' or 'unknown'. (ahead_in_kobo_terms then checks what is SENT.)'''
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


def span_key(value):
    '''"kobo.12.3" -> (12, 3): spans are numbered in document order within a file.'''
    m = _KOBO_SPAN.match(value or '')
    return (int(m.group(1)), int(m.group(2))) if m else None


def ahead_in_kobo_terms(loc, row, book):
    '''The last check before a PUT (review round 1 B1/B2): is the bookmark we are
    about to send strictly ahead of the Kobo's place AS THE KOBO SEES IT?
    -> (ok, why not). `book`: the Book whose chapter list loc['source'] names
    (the KEPUB for a span, else the EPUB; closed is fine).
      * no Kobo place: yes;
      * our KoboSpan against the Kobo's KoboSpan: same file -> kobo.P.S later;
        another file -> later in the spine; a Source not in the book -> no;
      * anything else: the % the Kobo will get above its own % + PERCENT_MARGIN;
        no Kobo % to compare with -> no.'''
    if not _has_place(row):
        return True, None
    theirs = span_key(row.get('location_value')) \
        if (row.get('location_type') or '').strip().lower() == 'kobospan' else None
    ours = span_key(loc.get('value')) if loc.get('type') == 'KoboSpan' else None
    if ours is not None and theirs is not None:
        src = (row.get('location_source') or '').strip()
        their_ch = book.chapter_for_source(src) if src else None
        our_ch = book.chapter_for_source(loc['source'])
        if their_ch is None or our_ch is None:
            return False, 'the Kobo\'s place cannot be compared with this book in the Kobo\'s terms'
        if our_ch != their_ch:
            return (True, None) if our_ch > their_ch else (False, 'behind the Kobo\'s own chapter')
        if ours > theirs:
            return True, None
        return False, ('the Kobo\'s own sentence (not ahead)' if ours == theirs
                       else 'before the Kobo\'s own sentence (not ahead in the Kobo\'s terms)')
    try:
        kobo_pct = float(row.get('progress_percent'))
    except (TypeError, ValueError):
        return False, 'the Kobo\'s place has no % to compare with (not sent)'
    if float(loc['progressPercent']) > kobo_pct + PERCENT_MARGIN:
        return True, None
    return False, 'not ahead of the Kobo\'s own % ({:.1f} %, sending {:.1f} %)'.format(
        kobo_pct, float(loc['progressPercent']))


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


class _NoAnswer(Exception):
    pass


def _send_within(url, body, timeout):
    '''_send with a hard wall-clock limit (S4): the socket timeout does not cover the
    name lookup, so the request runs on a helper thread and is abandoned (its
    answer ignored) after `timeout` seconds.'''
    out = {}

    def run():
        try:
            out['ok'] = _send(url, body, timeout)
        except BaseException as exc:          # handed to the caller below
            out['error'] = exc
    t = threading.Thread(target=run, name='CalibReader Sync: Calibre-Web request', daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise _NoAnswer()
    if 'error' in out:
        raise out['error']
    return out['ok']


def scrub(text, token):
    text = str(text)
    return text.replace(token, '<token>') if token else text


def put_state(base_url, token, uuid, loc, user_name=None, timeout=TIMEOUT):
    '''-> (ok, failure kind or None, note). The note never holds the token.
    Kinds: 'network' (no answer), 'timeout' (no answer in time: the PUT may still
    land), 'address', 'server' (refused), 'book' (refused for this book).'''
    url = '{}/kobo/{}/v1/library/{}/state'.format(base_url, token, uuid)
    body = json.dumps(state_body(uuid, loc, _kobo_now())).encode('utf-8')
    try:
        status, raw = _send_within(url, body, timeout)
    except _NoAnswer:
        return False, 'timeout', ('Calibre-Web at {} did not answer within {:g} s; CalibReader\'s places were not '
                                  'sent to the Kobo. They go with the next sync.'.format(base_url, timeout))
    except ValueError:
        return False, 'address', ('The Calibre-Web address "{}" in Settings is not a web address (like {}); '
                                  'nothing sent to the Kobo.'.format(base_url, DEFAULT_CALIBRE_WEB))
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, 'reason', None) or exc
        return False, 'network', scrub('Calibre-Web at {} did not answer ({}); CalibReader\'s places were not '
                                       'sent to the Kobo. They go with the next sync.'.format(base_url, reason), token)
    if status == 401:
        return False, 'server', ('Calibre-Web at {} did not accept the Kobo token of {} (is Kobo sync on for that '
                                 'user?); nothing sent to the Kobo.'.format(base_url, user_name or 'this user'))
    if 300 <= status < 400:
        return False, 'server', ('Calibre-Web at {} answered with a redirect (HTTP {}); nothing sent to the Kobo. '
                                 'Check the address in Settings.'.format(base_url, status))
    if status == 400:
        return False, 'book', ('Calibre-Web at {} did not accept the place for book {} (HTTP 400); nothing more '
                               'sent this run.'.format(base_url, uuid))
    if status != 200:
        return False, 'server', ('Calibre-Web at {} answered HTTP {}; nothing more sent to the Kobo this run.'
                                 .format(base_url, status))
    try:
        answer = json.loads(raw.decode('utf-8') or '{}')
        result = answer['UpdateResults'][0]['CurrentBookmarkResult']['Result']
        ok = answer.get('RequestResult') == 'Success' and result == 'Success'
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        ok = False
    if not ok:
        return False, 'book', ('Calibre-Web at {} does not have book {} (it answered without storing the place): '
                               'is it serving this calibre library? Nothing more sent this run.'.format(base_url, uuid))
    return True, None, None


def _kobo_now():
    return _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _precision_lines(precisions, had_kepub):
    span = [u for u, p in precisions.items() if p == SPAN]
    pct_kepub = [u for u, p in precisions.items() if p != SPAN and u in had_kepub]
    pct_plain = [u for u, p in precisions.items() if p != SPAN and u not in had_kepub]

    def books(n):
        return '{} book{}'.format(n, '' if n == 1 else 's')
    lines = []
    if span:
        lines.append('Kobo: CalibReader\'s place sent for {} to the sentence (KoboSpan, from the KEPUB).'
                     .format(books(len(span))))
    if pct_kepub:
        lines.append('Kobo: CalibReader\'s place sent for {} by chapter % (no KoboSpan at that place in the '
                     'KEPUB; the Kobo gets the percentages only).'.format(books(len(pct_kepub))))
    if pct_plain:
        lines.append('Kobo: CalibReader\'s place sent for {} by chapter % (no KEPUB in the library; '
                     'the Kobo gets the percentages only).'.format(books(len(pct_plain))))
    return lines


def push_positions(library_path, app_db, lookup, base_url=DEFAULT_CALIBRE_WEB, memory=None, user=None, log=None,
                   max_books=MAX_BOOKS, now=None, timeout=TIMEOUT, since=None, budget=RUN_BUDGET):
    '''One push run. `lookup(book_id)` -> (uuid, EPUB path, KEPUB path) or None,
    as for the import. `since`: when sending was turned on (ISO, UTC); only app
    places stamped after it are sent, and without it nothing is. `memory`:
    {uuid (lower case): what was last sent}, updated in place (the caller saves
    it). `budget`: seconds after which no further PUT starts (the rest: `more`).'''
    res = KoboPushResult()
    memory = {} if memory is None else memory
    if not getattr(lookup, 'kepub_beside', False):
        lookup = with_kepub_beside(lookup)
    base_url, cut = clean_address(base_url or DEFAULT_CALIBRE_WEB)
    base_url = base_url or DEFAULT_CALIBRE_WEB
    if cut:
        res.notes.append('The Calibre-Web address in Settings ended in a Kobo sync link (/kobo/…); only {} is '
                         'used. Please remove the rest there: it holds a token.'.format(base_url))
    since_t = _parse_iso(since) if since else None
    if since_t is None:
        res.notes.append('Sending to the Kobo has no start time yet; nothing sent.')
        return res
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
    future = unproven = attempted = no_token = 0
    to_send = []
    had_kepub = set()
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
        if not stamp > since_t:
            res.skipped[uuid] = 'read before sending to the Kobo was turned on'
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
        settled = (entry['updated_at'], row['updated_at'])
        if _SETTLED.get(fkey, (None, None))[:2] == settled:
            res.skipped[uuid] = _SETTLED[fkey][2]
            continue
        if not has_place and row.get('read_status') == 1:
            # N2: Calibre-Web says Finished (the Kobo marked it, or a reset) and keeps no place.
            res.skipped[uuid] = 'marked Finished in Calibre-Web, with no Kobo place: not sent'
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
        has_kepub = bool(kepub_path and os.path.isfile(kepub_path))
        if (row.get('location_type') or '').strip().lower() == 'kobospan' and not has_kepub:
            # S2: the Kobo reads a KEPUB the library does not show: never answer its span by %.
            res.skipped[uuid] = ('the Kobo reads this book\'s KEPUB, which calibre does not list and is not beside '
                                 'the EPUB: restart calibre after Calibre-Web converts a book')
            continue
        if not token:
            no_token += 1                   # S5: nothing is mapped without a token
            continue
        if attempted >= max_books:
            res.more = True
            continue
        attempted += 1
        epub = kepub = None
        loc = None
        try:
            epub = Book(epub_path)
            if has_kepub:
                try:
                    kepub = Book(kepub_path)
                except Exception:          # a damaged KEPUB only costs precision
                    kepub = None
            kobo_place = map_bookmark(epub, row, kepub) if has_place else None
            why = None
            if has_place and kobo_place is None:
                # Its Source is in neither the EPUB nor the KEPUB (M4): another book?
                why = 'the Kobo\'s place does not match this book'
            elif not (0 <= entry['chapter'] < len(epub.chapters)):
                why = 'the app\'s chapter is not in this book'
            else:
                order = compare(entry, kobo_place, row)
                if order != 'ahead':
                    why = {'same': 'the same place as the Kobo', 'behind': 'behind the Kobo (forward only)',
                           'unknown': 'not clearly ahead of the Kobo'}[order]
            if why is None:
                loc = kobo_location(epub, entry['chapter'], entry['textAnchor'], kepub,
                                    kobo_source=row.get('location_source'))
                if loc is None:
                    why = 'the app\'s place is not in this book'
            if why is not None:
                res.skipped[uuid] = why
                _SETTLED[fkey] = settled + (why,)
                continue
        except Exception as exc:
            res.skipped[uuid] = 'could not read the book ({})'.format(exc)
            _SETTLED[fkey] = settled + (res.skipped[uuid],)
            continue
        finally:
            for b in (epub, kepub):
                if b is not None:
                    b.close()
        if kepub is not None:
            had_kepub.add(book_uuid)
        # The Book whose chapter list the bookmark's Source names (closed is fine for that).
        to_send.append((uuid, book_uuid, entry, loc, fkey, row, kepub if loc['type'] == 'KoboSpan' else epub,
                        settled))
    if future:
        res.notes.append('{} CalibReader place(s) are stamped more than 10 minutes in the future: an iPhone or iPad '
                         'clock looks wrong. Not sent to the Kobo.'.format(future))
    if unproven:
        res.notes.append('Calibre-Web\'s library folder ({}) is not visible on this computer, so CalibReader '
                         'cannot confirm it is this library: only books the Kobo has already opened are sent '
                         '({} held back).'.format(served or 'unknown', unproven))
    if no_token:
        res.notes.append('Calibre-Web has no Kobo sync token for {}: set up Kobo sync for that user in '
                         'Calibre-Web. Nothing sent to the Kobo.'.format(res.user))
        res.more = False
        return res
    started = time.monotonic()
    for uuid, book_uuid, entry, loc, fkey, row, book, settled in to_send:
        if time.monotonic() - started >= budget:
            res.more = True                 # S4: the rest go with the next run
            break
        # S3: the Kobo's row as it is now; a Kobo sync since the run began wins this run.
        fresh = read_kobo_row(app_db, row['user_id'], row['book_id'])
        if fresh is None or fresh['updated_at'] != row['updated_at']:
            res.skipped[uuid] = 'the Kobo synced during this run; looked at again next run'
            continue
        ok, why = ahead_in_kobo_terms(loc, fresh, book)          # B1/B2: the last check before the PUT
        if not ok:
            res.skipped[uuid] = why
            _SETTLED[fkey] = settled + (why,)
            continue
        loc = dict(loc)
        if loc['type'] == 'KoboSpan' and span_key(fresh.get('location_value')) is not None:
            # Ruling (round 1): a span ahead of the Kobo's never comes with a % below the Kobo's own.
            try:
                loc['progressPercent'] = max(float(loc['progressPercent']), float(fresh.get('progress_percent')))
            except (TypeError, ValueError):
                pass
        previous = memory.get(uuid)
        memory[uuid] = {                    # before the PUT: an import meanwhile knows it is ours
            'type': loc['type'], 'value': loc['value'], 'source': loc['source'],
            'progressPercent': loc['progressPercent'],
            'contentSourceProgressPercent': loc['contentSourceProgressPercent'],
            'precision': loc['precision'], 'chapter': loc['chapter'], 'textAnchor': loc['anchor'],
            'app_chapter': entry['chapter'], 'app_textAnchor': entry['textAnchor'],
            'app_updated_at': entry['updated_at'], 'pushed_at': _kobo_now(),
        }
        ok, kind, note = put_state(base_url, token, book_uuid, loc, res.user, timeout)
        if not ok:
            res.notes.append(scrub(note, token))
            res.failure = kind
            if kind != 'timeout':           # a PUT that timed out may still land: keep it as ours
                if previous is None:
                    memory.pop(uuid, None)
                else:
                    memory[uuid] = previous
            if kind == 'book':
                _FAILED[fkey] = entry['updated_at']
            res.more = False                # a failing run never asks for another
            break
        res.sent.append(book_uuid)
        res.precisions[book_uuid] = loc['precision']
        if log:
            log(scrub('Kobo push {} -> {} {} in {} ({}; app chapter {} textAnchor {}, lands at {}; {:.2f} %)'.format(
                book_uuid, loc['type'] or 'Source', loc['value'] or '', loc['source'], loc['precision'],
                entry['chapter'], entry['textAnchor'], loc['anchor'], float(loc['progressPercent'])), token))
    res.lines = _precision_lines(res.precisions, had_kepub)
    return res


def sync_kobo(library_path, app_db, lookup, user=None, push=False, base_url=DEFAULT_CALIBRE_WEB, memory=None,
              log=None, max_books=MAX_BOOKS, now=None, timeout=TIMEOUT, since=None, budget=RUN_BUDGET):
    '''One Kobo run: step 1's import (with the echo guard), then, only when
    `push` is True, this module's push (places stamped after `since` only).
    -> (import result, push result or None). The lookup finds a KEPUB beside
    the EPUB too (S2).'''
    memory = {} if memory is None else memory
    lookup = with_kepub_beside(lookup)
    kres = import_positions(library_path, app_db, lookup, user=user, log=log, max_books=max_books, now=now,
                            pushed=memory)
    if not push:
        return kres, None
    pres = push_positions(library_path, app_db, lookup, base_url=base_url, memory=memory, user=user, log=log,
                          max_books=max_books, now=now, timeout=timeout, since=since, budget=budget)
    pres.notes = [n for n in pres.notes if n not in kres.notes]
    return kres, pres


class StateFile(object):
    '''The CLI's memory (the plugin keeps the same in calibre's prefs): per library
    folder (real path), what was sent per book, and 'push_since', when sending was
    turned on for this library (its first --push run). One JSON file on this
    computer: {library: {'push_since': iso, uuid: record, ...}}.'''

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
        mine = dict(mine) if isinstance(mine, dict) else {}
        since = mine.pop('push_since', None)
        self.since = since if isinstance(since, str) and since else None
        self.memory = mine

    def save(self):
        mine = dict(self.memory)
        if self.since:
            mine['push_since'] = self.since
        self.table[self.key] = mine
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

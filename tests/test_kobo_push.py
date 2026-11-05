# Last modified: 2026-11-05--0516
#
# Step 2, CalibReader -> Kobo (kobo_push): the app's place in
# CalibReader_positions.json is PUT to Calibre-Web's Kobo API, forward-only,
# never with the setting off, and step 1's import never reads our own push back
# as a Kobo move (echo guard).
#
# Calibre-Web here is a tiny local stand-in (FakeCalibreWeb) for 0.6.27's
# PUT /kobo/<token>/v1/library/<uuid>/state: it answers like HandleStateRequest
# and, like Calibre-Web, stores the bookmark in the test's app.db with a
# server-time stamp. The token is a fake one; the plugin itself only ever opens
# app.db read-only (checked by hash).
#
#   python3 tests/run_tests.py          (from the plugin folder)

import contextlib
import datetime
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import kobo_import  # noqa: E402
import kobo_position  # noqa: E402
import kobo_push  # noqa: E402

EPUB = os.path.join(HERE, 'fixtures', 'metamorphosis.epub')
KEPUB = os.path.join(HERE, 'fixtures', 'metamorphosis.kepub')
UUID = '4261f375-f734-42c2-b5f6-df7e99b16fa4'          # book 60: EPUB + KEPUB
NOKEPUB = '70b275c5-99f6-4347-8ba4-d4435c38a3ae'       # book 61: EPUB only
THIRD = '11111111-0000-0000-0000-000000000062'         # book 62: EPUB + KEPUB
CH = 'OEBPS/7446794606207039498_5200-h-{}.htm.xhtml'   # chapter n+1 of the app = h-n
TOKEN = 'f00dfeed' * 4                                 # fake, 32 hex like Calibre-Web's
TOKEN2 = 'deadbeef' * 4
PHONE = '2CC5EEC4-D75D-4592-803C-F1CA6D74D508'

SCHEMA = '''
CREATE TABLE user (id INTEGER PRIMARY KEY, name VARCHAR(64), email VARCHAR(120), role SMALLINT);
CREATE TABLE remote_auth_token (id INTEGER PRIMARY KEY, auth_token VARCHAR, user_id INTEGER,
    verified BOOLEAN, expiration DATETIME, token_type INTEGER, request_ip VARCHAR, request_user_agent VARCHAR);
CREATE TABLE settings (id INTEGER PRIMARY KEY, config_calibre_dir VARCHAR);
CREATE TABLE kobo_reading_state (id INTEGER PRIMARY KEY, user_id INTEGER, book_id INTEGER,
    last_modified DATETIME, priority_timestamp DATETIME);
CREATE TABLE kobo_bookmark (id INTEGER PRIMARY KEY, kobo_reading_state_id INTEGER, last_modified DATETIME,
    location_source VARCHAR, location_type VARCHAR, location_value VARCHAR,
    progress_percent FLOAT, content_source_progress_percent FLOAT);
'''


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def server_now():
    '''Calibre-Web's stamp: datetime.now(timezone.utc), stored naive.'''
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S.%f')


def iso_in(minutes):
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
    return t.strftime('%Y-%m-%dT%H:%M:%S.') + '{:03d}Z'.format(t.microsecond // 1000)


class FakeCalibreWeb(object):
    '''Enough of Calibre-Web 0.6.27's Kobo state endpoint (cps/kobo.py HandleStateRequest).'''

    def __init__(self, app_db, books, store=True, status=None):
        self.app_db = app_db
        self.books = dict(books)            # uuid -> calibre book id (the books Calibre-Web has)
        self.store = store
        self.status = status                # force an HTTP status (500, …)
        self.delay = 0                      # seconds before answering (a slow server)
        self.requests = []                  # (method, path, json body, user id)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj):
                body = json.dumps(obj).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):
                n = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(n)
                parts = self.path.split('/')       # '', 'kobo', token, 'v1', 'library', uuid, 'state'
                user = fake.user_for(parts[2] if len(parts) > 2 else '')
                try:
                    body = json.loads(raw.decode('utf-8'))
                except ValueError:
                    body = None
                fake.requests.append(('PUT', self.path, body, user, self.headers.get('Content-Type')))
                if fake.delay:
                    time.sleep(fake.delay)
                if fake.status:
                    return self._reply(fake.status, {'error': 'forced'})
                if user is None:
                    return self._reply(401, {})
                uuid = parts[5] if len(parts) > 5 else ''
                if uuid not in fake.books:
                    return self._reply(200, {})      # redirect_or_proxy_request without the proxy
                try:
                    state = body['ReadingStates'][0]
                    bm = state['CurrentBookmark']
                    state['Statistics'], state['StatusInfo']    # KeyError -> 400, as Calibre-Web
                    pct, cpct, loc = bm['ProgressPercent'], bm['ContentSourceProgressPercent'], bm['Location']
                except (KeyError, TypeError, IndexError):
                    return self._reply(400, {})
                if fake.store:
                    fake.save(user, fake.books[uuid], pct, cpct, loc)
                self._reply(200, {'RequestResult': 'Success', 'UpdateResults': [
                    {'EntitlementId': uuid, 'CurrentBookmarkResult': {'Result': 'Success'},
                     'LastModified': '2026-11-05T00:00:00Z', 'PriorityTimestamp': '2026-11-05T00:00:00Z'}]})

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return 'http://127.0.0.1:{}'.format(self.httpd.server_address[1])

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def user_for(self, token):
        con = sqlite3.connect(self.app_db)
        try:
            row = con.execute('SELECT user_id FROM remote_auth_token WHERE auth_token=? AND token_type=1',
                              (token,)).fetchone()
        finally:
            con.close()
        return row[0] if row else None

    def save(self, user, book_id, pct, cpct, loc):
        '''What get_or_create_reading_state + the bookmark assignment + before_flush do.'''
        now = server_now()
        con = sqlite3.connect(self.app_db)
        try:
            rs = con.execute('SELECT id FROM kobo_reading_state WHERE user_id=? AND book_id=?',
                             (user, book_id)).fetchone()
            if rs is None:
                cur = con.execute('INSERT INTO kobo_reading_state (user_id, book_id, last_modified, priority_timestamp)'
                                  ' VALUES (?,?,?,?)', (user, book_id, now, now))
                rid = cur.lastrowid
                con.execute('INSERT INTO kobo_bookmark (kobo_reading_state_id, last_modified) VALUES (?,?)', (rid, now))
            else:
                rid = rs[0]
            old = con.execute('SELECT progress_percent, content_source_progress_percent, location_value, '
                              'location_type, location_source FROM kobo_bookmark WHERE kobo_reading_state_id=?',
                              (rid,)).fetchone()
            new = (pct, cpct) + ((loc['Value'], loc['Type'], loc['Source']) if loc else tuple(old[2:]))
            if tuple(old) != new:              # SQLAlchemy onupdate fires on a changed row only
                con.execute('UPDATE kobo_bookmark SET progress_percent=?, content_source_progress_percent=?, '
                            'location_value=?, location_type=?, location_source=?, last_modified=? '
                            'WHERE kobo_reading_state_id=?', new + (now, rid))
            con.execute('UPDATE kobo_reading_state SET last_modified=?, priority_timestamp=? WHERE id=?', (now, now, rid))
            con.commit()
        finally:
            con.close()


def closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return 'http://127.0.0.1:{}'.format(port)


class PushBase(unittest.TestCase):

    def setUp(self):
        kobo_import.forget_failures()
        kobo_push.forget_failures()
        self.dir = tempfile.mkdtemp(prefix='kobo-push-')
        self.lib = os.path.join(self.dir, 'library')
        os.mkdir(self.lib)
        self.app_db = os.path.join(self.dir, 'app.db')
        con = sqlite3.connect(self.app_db)
        con.executescript(SCHEMA)
        con.execute("INSERT INTO user VALUES (1, 'admin', 'a@x', 1), (2, 'martin', 'm@x', 0), (3, 'Guest', '', 32)")
        con.execute("INSERT INTO remote_auth_token (auth_token, user_id, token_type) VALUES (?, 1, 1), (?, 2, 1)",
                    (TOKEN, TOKEN2))
        con.execute('INSERT INTO settings VALUES (1, ?)', (self.lib,))     # Calibre-Web serves THIS library
        con.commit()
        con.close()
        # Book 61 has no KEPUB: its EPUB sits in a folder of its own (round 1, S2: a KEPUB
        # beside the EPUB is found even when calibre does not list it).
        plain = os.path.join(self.dir, 'plain', 'metamorphosis.epub')
        os.makedirs(os.path.dirname(plain))
        shutil.copy(EPUB, plain)
        self.books = {60: (UUID, EPUB, KEPUB), 61: (NOKEPUB, plain, None), 62: (THIRD, EPUB, KEPUB)}
        self.cw = FakeCalibreWeb(self.app_db, {UUID: 60, NOKEPUB: 61, THIRD: 62})
        self.memory = {}

    def tearDown(self):
        self.cw.stop()
        shutil.rmtree(self.dir)

    def lookup(self, book_id):
        return self.books.get(book_id)

    # -- app.db as Calibre-Web left it ------------------------------------------
    def kobo(self, book_id, stamp='2026-10-30 21:03:48', source=None, ltype=None, value=None, pct=None, cpct=None,
             user=1):
        '''A synced book's reading state (+ bookmark; all None = synced, never read on the Kobo).'''
        con = sqlite3.connect(self.app_db)
        cur = con.execute('INSERT INTO kobo_reading_state (user_id, book_id, last_modified, priority_timestamp) '
                          'VALUES (?,?,?,?)', (user, book_id, stamp, stamp))
        con.execute('INSERT INTO kobo_bookmark (kobo_reading_state_id, last_modified, location_source, location_type,'
                    ' location_value, progress_percent, content_source_progress_percent) VALUES (?,?,?,?,?,?,?)',
                    (cur.lastrowid, stamp, source, ltype, value, pct, cpct))
        con.commit()
        con.close()

    def kobo_row(self, book_id, user=1):
        con = sqlite3.connect(self.app_db)
        try:
            return con.execute(
                'SELECT b.location_type, b.location_value, b.location_source, b.progress_percent, '
                'b.content_source_progress_percent, b.last_modified FROM kobo_bookmark b JOIN kobo_reading_state rs '
                'ON rs.id = b.kobo_reading_state_id WHERE rs.book_id=? AND rs.user_id=?', (book_id, user)).fetchone()
        finally:
            con.close()

    def kobo_moves(self, book_id, ltype, value, source, pct, cpct, user=1):
        '''The Kobo PUTs a new place (Calibre-Web stamps server time).'''
        self.cw.save(user, book_id, pct, cpct, {'Type': ltype, 'Value': value, 'Source': source})

    # -- the sidecar the app wrote ----------------------------------------------
    @property
    def positions_path(self):
        return os.path.join(self.lib, 'CalibReader_positions.json')

    def app(self, uuid, chapter, anchor, stamp='2026-10-31T10:00:00.000Z', progress=None, device=PHONE, **extra):
        data = self.read_positions() if os.path.exists(self.positions_path) else {'schema': 1, 'positions': {}}
        entry = {'chapter': chapter, 'page': 3, 'scrollY': 0, 'progress': progress if progress is not None else 0.5,
                 'updated_at': stamp, 'deviceId': device}
        if anchor is not None:
            entry['textAnchor'] = anchor
        entry.update(extra)
        data['positions'][uuid] = entry
        with open(self.positions_path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f)

    def read_positions(self):
        with open(self.positions_path, encoding='utf-8') as f:
            return json.load(f)

    def anchor_of(self, chapter, span_id, plus=0):
        epub, kepub = kobo_position.Book(EPUB), kobo_position.Book(KEPUB)
        try:
            return kobo_position.resolve_span(epub, kepub, chapter, span_id)[0] + plus
        finally:
            epub.close()
            kepub.close()

    # -- runs ---------------------------------------------------------------------
    def push(self, url=None, user='admin', **kw):
        kw.setdefault('since', SINCE)                  # round 1 (B3): sending turned on before these places
        return kobo_push.push_positions(self.lib, self.app_db, self.lookup, base_url=url or self.cw.url,
                                        memory=self.memory, user=user, **kw)

    def sync(self, push=True, url=None, user='admin', memory='mine', **kw):
        kw.setdefault('since', SINCE)
        return kobo_push.sync_kobo(self.lib, self.app_db, self.lookup, user=user, push=push,
                                   base_url=url or self.cw.url,
                                   memory=self.memory if memory == 'mine' else memory, **kw)

    def puts(self):
        return [r for r in self.cw.requests if r[0] == 'PUT']

    def metadata_db(self):
        '''A calibre metadata.db for the CLI (it looks the books up there, read-only).'''
        con = sqlite3.connect(os.path.join(self.lib, 'metadata.db'))
        con.executescript('CREATE TABLE books (id INTEGER PRIMARY KEY, uuid TEXT, path TEXT);'
                          'CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT);'
                          'CREATE TABLE library_id (id INTEGER PRIMARY KEY, uuid TEXT NOT NULL);')
        con.execute("INSERT INTO library_id VALUES (1, 'dddddddd-0000-0000-0000-000000000004')")
        for bid, (uuid, epub, kepub) in self.books.items():
            rel = 'Author/Book ({})'.format(bid)
            os.makedirs(os.path.join(self.lib, rel), exist_ok=True)
            con.execute('INSERT INTO books VALUES (?,?,?)', (bid, uuid, rel))
            for fmt, src in (('EPUB', epub), ('KEPUB', kepub)):
                if src:
                    shutil.copy(src, os.path.join(self.lib, rel, 'book.' + fmt.lower()))
                    con.execute('INSERT INTO data (book, format, name) VALUES (?,?,?)', (bid, fmt, 'book'))
        con.commit()
        con.close()


class ForwardOnlyTests(PushBase):

    def test_ahead_and_newer_is_pushed_as_the_kobo_span(self):
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        target = self.anchor_of(3, 'kobo.5.1', plus=7)          # mid-sentence in Part II
        self.app(UUID, 3, target)
        res = self.push()
        self.assertEqual(res.sent, [UUID])
        (method, path, body, user, ctype), = self.puts()
        self.assertEqual(path, '/kobo/{}/v1/library/{}/state'.format(TOKEN, UUID))
        self.assertEqual((user, ctype), (1, 'application/json'))
        state = body['ReadingStates'][0]
        self.assertEqual(state['EntitlementId'], UUID)
        self.assertIsNone(state['StatusInfo'])                  # read status and statistics untouched
        self.assertIsNone(state['Statistics'])
        bm = state['CurrentBookmark']
        self.assertEqual(bm['Location'], {'Type': 'KoboSpan', 'Value': 'kobo.5.1', 'Source': CH.format(2)})
        start = self.anchor_of(3, 'kobo.5.1')                   # the span's own place: what lands back
        self.assertAlmostEqual(bm['ProgressPercent'], 100.0 * (39125 + start) / 138516, places=3)
        self.assertAlmostEqual(bm['ContentSourceProgressPercent'], 100.0 * start / 40209, places=3)
        self.assertEqual(self.kobo_row(60)[:3], ('KoboSpan', 'kobo.5.1', CH.format(2)))   # Calibre-Web stored it
        self.assertEqual(res.precisions[UUID], kobo_position.SPAN)
        self.assertTrue(any('sentence' in line for line in res.lines), res.lines)
        self.assertEqual(self.memory[UUID]['value'], 'kobo.5.1')
        self.assertEqual(self.memory[UUID]['app_updated_at'], '2026-10-31T10:00:00.000Z')

    def test_behind_the_kobo_is_never_pushed(self):
        self.kobo(60, '2026-10-30 21:03:48', CH.format(2), 'KoboSpan', 'kobo.5.1', 40.0, 3.0)
        self.app(UUID, 2, 641)                                  # Part I: behind the Kobo's Part II
        res = self.push()
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertIn('behind', res.skipped[UUID])

    def test_behind_in_the_same_chapter_is_never_pushed(self):
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.app(UUID, 2, self.anchor_of(2, 'kobo.3.5') - 30)
        self.assertEqual((self.push().sent, self.puts()), ([], []))

    def test_the_same_place_is_not_pushed(self):
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.app(UUID, 2, self.anchor_of(2, 'kobo.3.5'))
        res = self.push()
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertIn('same place', res.skipped[UUID])

    def test_an_older_app_place_is_not_pushed_even_when_ahead(self):
        self.kobo(60, '2026-10-31 12:00:00', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.app(UUID, 4, 100, stamp='2026-10-31T11:59:59.000Z')
        self.assertEqual((self.push().sent, self.puts()), ([], []))

    def test_no_kobo_place_yet_is_pushed(self):
        self.kobo(60, '2026-10-30 21:03:48')                    # synced, never opened on the Kobo
        self.app(UUID, 3, 500)
        self.assertEqual(self.push().sent, [UUID])
        self.assertEqual(len(self.puts()), 1)

    def test_a_percent_only_kobo_place_compares_progress(self):
        self.kobo(60, '2026-10-30 21:03:48', None, None, None, 50.0, None)
        self.app(UUID, 4, 100, progress=0.45)
        self.assertEqual(self.push().sent, [])
        self.app(UUID, 4, 100, progress=0.55, stamp='2026-10-31T10:05:00.000Z')
        self.assertEqual(self.push().sent, [UUID])

    def test_a_source_only_kobo_place_is_never_pushed_over(self):
        # Round 1 (B1/B2 ruling): what cannot be compared in the Kobo's own terms is not sent. A
        # Source with no span and no % gives nothing to compare with, even from a later chapter.
        self.kobo(60, '2026-10-30 21:03:48', CH.format(2), None, None, None, None)
        self.app(UUID, 3, 30000)                                # same chapter: the Kobo could be anywhere in it
        self.assertEqual(self.push().sent, [])
        self.app(UUID, 4, 10, stamp='2026-10-31T10:05:00.000Z')
        res = self.push()
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertIn('no %', res.skipped[UUID])

    def test_a_kobo_place_not_in_this_book_is_not_pushed_over(self):
        self.kobo(60, '2026-10-30 21:03:48', 'OEBPS/not-in-this-book.xhtml', 'KoboSpan', 'kobo.1.1', 10.0, 3.0)
        self.app(UUID, 4, 100)
        res = self.push()
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertIn(UUID, res.skipped)

    def test_kobo_entries_in_the_sidecar_are_not_the_apps(self):
        self.kobo(60, '2026-10-30 21:03:48')
        self.kobo(62, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500, device='kobo-calibre-web')
        self.app(THIRD, 3, 500, device='kobo-something-else')
        self.assertEqual((self.push().sent, self.puts()), ([], []))

    def test_an_entry_without_a_text_anchor_is_not_pushed(self):
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, None)
        res = self.push()
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertIn('textAnchor', res.skipped[UUID])

    def test_a_book_calibre_web_never_synced_is_not_pushed(self):
        self.app(THIRD, 3, 500)                                 # no kobo_reading_state row for it
        res = self.push()
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertIn('not on the Kobo', res.skipped[THIRD])

    def test_an_app_place_stamped_far_in_the_future_is_not_pushed(self):
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500, stamp=iso_in(120))
        res = self.push()
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertTrue(any('clock' in n for n in res.notes), res.notes)

    def test_without_a_kepub_source_and_percentages(self):
        self.kobo(61, '2026-10-30 21:03:48')
        self.app(NOKEPUB, 3, 20000)
        res = self.push()
        self.assertEqual(res.sent, [NOKEPUB])
        bm = self.puts()[0][2]['ReadingStates'][0]['CurrentBookmark']
        self.assertEqual(bm['Location'], {'Type': '', 'Value': '', 'Source': CH.format(2)})
        self.assertAlmostEqual(bm['ContentSourceProgressPercent'], 100.0 * 20000 / 40209, delta=0.05)
        self.assertEqual(res.precisions[NOKEPUB], kobo_position.CHAPTER_PERCENT)
        self.assertTrue(any('chapter %' in line for line in res.lines), res.lines)

    def test_several_users_need_a_choice_and_the_chosen_users_token_is_used(self):
        self.kobo(60, '2026-10-30 21:03:48', user=2)
        self.app(UUID, 3, 500)
        res = self.push(user=None)
        self.assertEqual((res.sent, self.puts()), ([], []))
        res = self.push(user='martin')
        self.assertEqual(res.sent, [UUID])
        self.assertEqual(self.puts()[0][3], 2)                  # martin's token

    def test_no_kobo_token_for_the_user_is_a_note(self):
        con = sqlite3.connect(self.app_db)
        con.execute('DELETE FROM remote_auth_token')
        con.commit(); con.close()
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        res = self.push()
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertTrue(any('token' in n.lower() for n in res.notes), res.notes)

    def test_a_run_pushes_at_most_a_chunk(self):
        for bid, uuid in ((60, UUID), (61, NOKEPUB), (62, THIRD)):
            self.kobo(bid, '2026-10-30 21:03:48')
            self.app(uuid, 3, 500)
        res = self.push(max_books=2)
        self.assertEqual((len(res.sent), res.more), (2, True))
        res = self.push(max_books=2)
        self.assertEqual((len(res.sent), res.more), (1, False))

    def test_N1_books_behind_the_kobo_are_not_mapped_again_and_again(self):
        # 30 books where the app is behind the Kobo, 25 mapped per run: the second run maps
        # the other 5 and stops asking for more; a third maps nothing until a place moves.
        for i in range(30):
            bid = 300 + i
            self.books[bid] = ('44444444-0000-0000-0000-%012d' % bid, EPUB, KEPUB)
            self.cw.books['44444444-0000-0000-0000-%012d' % bid] = bid
            self.kobo(bid, '2026-10-30 21:03:48', CH.format(2), 'KoboSpan', 'kobo.5.1', 40.0, 3.0)
            self.app('44444444-0000-0000-0000-%012d' % bid, 2, 641)
        with mock.patch('kobo_push.map_bookmark', wraps=kobo_position.map_bookmark) as m:
            res = self.push()
        self.assertEqual((m.call_count, res.more, res.sent), (25, True, []))
        with mock.patch('kobo_push.map_bookmark', wraps=kobo_position.map_bookmark) as m:
            res = self.push()
        self.assertEqual((m.call_count, res.more), (5, False))
        with mock.patch('kobo_push.map_bookmark', wraps=kobo_position.map_bookmark) as m:
            res = self.push()
        self.assertEqual((m.call_count, res.more), (0, False))
        self.assertEqual(len([u for u, why in res.skipped.items() if 'behind' in why]), 30)
        # the app reads past the Kobo in one of them: that one is mapped again, and sent
        self.app('44444444-0000-0000-0000-000000000300', 4, 10, stamp='2026-10-31T11:00:00.000Z')
        with mock.patch('kobo_push.map_bookmark', wraps=kobo_position.map_bookmark) as m:
            res = self.push()
        self.assertEqual((m.call_count, res.sent), (1, ['44444444-0000-0000-0000-000000000300']))

    def test_app_db_is_never_written_by_the_plugin(self):
        self.cw.store = False
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        before = sha(self.app_db)
        self.assertEqual(self.push().sent, [UUID])
        self.assertEqual(sha(self.app_db), before)


class LibraryGuardTests(PushBase):

    def _served(self, path):
        con = sqlite3.connect(self.app_db)
        con.execute('UPDATE settings SET config_calibre_dir=?', (path,))
        con.commit(); con.close()

    def _metadata(self, folder, library_uuid):
        os.makedirs(folder, exist_ok=True)
        con = sqlite3.connect(os.path.join(folder, 'metadata.db'))
        con.execute('CREATE TABLE library_id (id INTEGER PRIMARY KEY, uuid TEXT NOT NULL)')
        con.execute('INSERT INTO library_id VALUES (1, ?)', (library_uuid,))
        con.commit(); con.close()

    def test_a_library_not_visible_here_needs_the_kobos_own_source(self):
        self._served('/books')                                  # Docker: not visible on this computer
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.kobo(62, '2026-10-30 21:03:48')                    # never opened on the Kobo: no proof
        self.app(UUID, 3, 500)
        self.app(THIRD, 3, 500)
        res = self.push()
        self.assertEqual(res.sent, [UUID])
        self.assertEqual(len(self.puts()), 1)
        self.assertTrue(any('not visible' in n for n in res.notes), res.notes)

    def test_another_library_gets_nothing(self):
        other = os.path.join(self.dir, 'other')
        self._metadata(other, 'aaaaaaaa-0000-0000-0000-000000000001')
        self._metadata(self.lib, 'bbbbbbbb-0000-0000-0000-000000000002')
        self._served(other)
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        res = self.push()
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertTrue(any('another calibre library' in n for n in res.notes), res.notes)

    def test_the_same_library_under_another_path_is_verified(self):
        twin = os.path.join(self.dir, 'twin')
        self._metadata(twin, 'cccccccc-0000-0000-0000-000000000003')
        self._metadata(self.lib, 'cccccccc-0000-0000-0000-000000000003')
        self._served(twin)
        self.kobo(62, '2026-10-30 21:03:48')
        self.app(THIRD, 3, 500)
        self.assertEqual(self.push().sent, [THIRD])


class FailureTests(PushBase):

    def three_ahead(self):
        for bid, uuid in ((60, UUID), (61, NOKEPUB), (62, THIRD)):
            self.kobo(bid, '2026-10-30 21:03:48')
            self.app(uuid, 3, 500)

    def test_unreachable_calibre_web_is_one_note_and_no_loop(self):
        self.three_ahead()
        url = closed_port()
        with mock.patch('kobo_push._send', wraps=kobo_push._send) as send:
            res = self.push(url=url)
        self.assertEqual(send.call_count, 1)                    # the first failure stops the run
        self.assertEqual(res.sent, [])
        self.assertFalse(res.more)
        about = [n for n in res.notes if url in n]
        self.assertEqual(len(about), 1, res.notes)
        self.assertEqual(self.memory, {})

    def test_an_error_answer_is_one_note_and_no_loop(self):
        self.three_ahead()
        self.cw.status = 500
        res = self.push()
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual((res.sent, res.more), ([], False))
        self.assertEqual(len([n for n in res.notes if '500' in n]), 1, res.notes)

    def test_a_book_calibre_web_does_not_have_is_not_tried_again(self):
        self.cw.books.pop(UUID)                                 # Calibre-Web answers {} for it
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        res = self.push()
        self.assertEqual((res.sent, len(self.puts())), ([], 1))
        self.assertTrue(res.notes)
        res = self.push()
        self.assertEqual(len(self.puts()), 1)                   # remembered until the app place changes
        self.app(UUID, 3, 900, stamp='2026-10-31T11:00:00.000Z')
        self.push()
        self.assertEqual(len(self.puts()), 2)

    def test_the_import_still_runs_when_calibre_web_is_down(self):
        self.kobo(62, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)   # Kobo newer
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        kres, pres = self.sync(url=closed_port())
        self.assertEqual(kres.written, [THIRD])
        self.assertEqual(pres.sent, [])
        self.assertTrue(pres.notes)


class ConsentTests(PushBase):

    def test_the_setting_is_off_by_default(self):
        self.assertFalse(kobo_push.push_enabled({'app_db': self.app_db, 'user': 'admin'}))
        self.assertFalse(kobo_push.push_enabled({'app_db': self.app_db, 'push': 'yes'}))
        self.assertFalse(kobo_push.push_enabled(None))
        self.assertTrue(kobo_push.push_enabled({'app_db': self.app_db, 'push': True}))
        self.assertEqual(kobo_push.calibre_web_url({'app_db': self.app_db}), 'http://127.0.0.1:8083')
        self.assertEqual(kobo_push.calibre_web_url({'cw_url': ' http://mini.local:8083/ '}), 'http://mini.local:8083')

    def test_consent_off_means_no_network_at_all(self):
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)

        def no_network(*a, **k):
            raise AssertionError('network used with the setting off')
        with mock.patch('socket.socket.connect', side_effect=no_network), \
                mock.patch('kobo_push._send', side_effect=no_network):
            kres, pres = self.sync(push=False)
        self.assertIsNone(pres)
        self.assertEqual(self.puts(), [])

    def test_the_cli_pushes_only_with_push(self):
        self.metadata_db()
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        state = os.path.join(self.dir, 'state.json')
        base = ['--library', self.lib, '--app-db', self.app_db, '--user', 'admin', '--calibre-web', self.cw.url,
                '--state', state, '--since', SINCE]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(kobo_import.main(base), 0)
        self.assertEqual(self.puts(), [])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(kobo_import.main(base + ['--push']), 0)
        self.assertEqual(len(self.puts()), 1)
        with open(state, encoding='utf-8') as f:
            saved = json.load(f)
        (lib_key, table), = saved.items()
        self.assertEqual(lib_key, os.path.realpath(self.lib))
        self.assertTrue(table[UUID]['value'].startswith('kobo.'), table)
        self.assertIn('sent', out.getvalue())


class TokenTests(PushBase):

    def outputs(self, res):
        return ' '.join(res.notes + res.lines + list(res.skipped.values()) + [json.dumps(self.memory)])

    def test_the_token_is_never_in_notes_logs_state_or_cli_output(self):
        self.metadata_db()
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        logged = []
        # 1. Calibre-Web refuses the token (401)
        con = sqlite3.connect(self.app_db)
        con.execute('UPDATE remote_auth_token SET token_type=0 WHERE user_id=1')   # CW no longer knows it as Kobo's
        con.execute("INSERT INTO remote_auth_token (auth_token, user_id, token_type) VALUES (?, 1, 1)", (TOKEN + 'x',))
        con.commit(); con.close()
        self.cw.user_for = lambda token: None                   # every token refused
        res = self.push(log=logged.append)
        self.assertTrue(res.notes)
        # 2. unreachable
        res2 = self.push(url=closed_port(), log=logged.append)
        # 3. an exception whose text carries the whole URL (token included)
        boom = urllib.error.URLError('cannot reach {}/kobo/{}/v1/library/{}/state'.format(self.cw.url, TOKEN + 'x', UUID))
        with mock.patch('kobo_push._send', side_effect=boom):
            res3 = self.push(log=logged.append)
        # 4. the CLI, success path, every line it prints
        del self.cw.user_for                                    # Calibre-Web knows the token again
        state = os.path.join(self.dir, 'state.json')
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
            kobo_import.main(['--library', self.lib, '--app-db', self.app_db, '--user', 'admin', '--push',
                              '--calibre-web', self.cw.url, '--state', state, '--since', SINCE])
        with open(state, encoding='utf-8') as f:
            state_text = f.read()
        everything = ' '.join([self.outputs(res), self.outputs(res2), self.outputs(res3), ' '.join(logged),
                               out.getvalue(), err.getvalue(), state_text])
        for secret in (TOKEN, TOKEN + 'x', TOKEN2):
            self.assertNotIn(secret, everything)
        self.assertTrue(res3.notes)


class EchoGuardTests(PushBase):
    '''Rule 3: after a push Calibre-Web stamps the bookmark with server time, so
    step 1's import would read our own push back as a newer Kobo place.'''

    def setUp(self):
        PushBase.setUp(self)
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.target = self.anchor_of(3, 'kobo.5.1', plus=7)     # mid-sentence: the round trip lands 7 earlier
        self.app(UUID, 3, self.target)
        kres, pres = self.sync()
        self.assertEqual((kres.written, pres.sent), ([], [UUID]))
        self.stored = self.kobo_row(60)
        self.assertEqual(self.stored[:3], ('KoboSpan', 'kobo.5.1', CH.format(2)))

    def test_the_import_after_a_push_writes_nothing(self):
        before = sha(self.positions_path)
        kres, pres = self.sync()
        self.assertEqual((kres.written, pres.sent), ([], []))
        self.assertEqual(sha(self.positions_path), before)
        self.assertIn('echo', kres.skipped[UUID])
        self.assertEqual(self.puts().__len__(), 1)              # and the same app place is not sent again

    def test_without_the_guard_the_import_would_take_it_back_a_few_characters_behind(self):
        res = kobo_import.import_positions(self.lib, self.app_db, self.lookup, user='admin')
        self.assertEqual(res.written, [UUID])
        e = self.read_positions()['positions'][UUID]
        self.assertEqual(e['deviceId'], kobo_import.KOBO_DEVICE_ID)
        self.assertEqual(e['chapter'], 3)
        self.assertEqual(e['textAnchor'], self.target - 7)      # behind the app: the app would ask "go there?"

    def test_the_kobo_echoing_the_same_span_with_its_own_percentages_is_still_ours(self):
        self.kobo_moves(60, 'KoboSpan', 'kobo.5.1', CH.format(2), 41.0, 0.0)
        before = sha(self.positions_path)
        kres, _ = self.sync(push=False)
        self.assertEqual(kres.written, [])
        self.assertEqual(sha(self.positions_path), before)

    def test_a_real_kobo_move_after_the_push_is_imported(self):
        self.kobo_moves(60, 'KoboSpan', 'kobo.20.1', CH.format(2), 45.0, 30.0)
        kres, pres = self.sync()
        self.assertEqual(kres.written, [UUID])
        e = self.read_positions()['positions'][UUID]
        self.assertEqual((e['deviceId'], e['kobo']['value']), (kobo_import.KOBO_DEVICE_ID, 'kobo.20.1'))
        self.assertEqual(pres.sent, [])                         # the sidecar now holds the Kobo's place

    def test_an_app_clock_ahead_does_not_make_us_push_the_same_place_again(self):
        self.app(UUID, 3, self.target, stamp=iso_in(5))         # phone clock 5 min ahead of the server
        self.memory[UUID]['app_updated_at'] = self.read_positions()['positions'][UUID]['updated_at']
        _, pres = self.sync()
        self.assertEqual((pres.sent, len(self.puts())), ([], 1))

    def test_the_app_reading_on_is_pushed_again(self):
        later = self.anchor_of(3, 'kobo.30.1', plus=3)
        self.app(UUID, 3, later, stamp=iso_in(1))
        kres, pres = self.sync()
        self.assertEqual((kres.written, pres.sent), ([], [UUID]))
        self.assertEqual(self.kobo_row(60)[1], 'kobo.30.1')

    def test_a_no_kepub_push_round_trips_too(self):
        self.kobo(61, '2026-10-30 21:03:48')
        self.app(NOKEPUB, 3, 20000)
        _, pres = self.sync()
        self.assertEqual(pres.sent, [NOKEPUB])
        before = sha(self.positions_path)
        kres, _ = self.sync()
        self.assertEqual(kres.written, [])
        self.assertEqual(sha(self.positions_path), before)
        # a Kobo move by % only (Calibre-Web keeps our Location next to it) is NOT our echo
        con = sqlite3.connect(self.app_db)
        con.execute("UPDATE kobo_bookmark SET progress_percent=progress_percent+5, content_source_progress_percent=90, "
                    "last_modified=? WHERE kobo_reading_state_id=(SELECT id FROM kobo_reading_state WHERE book_id=61)",
                    (server_now(),))
        con.commit(); con.close()
        kres, _ = self.sync(push=False)
        self.assertEqual(kres.written, [NOKEPUB])



# -- review round 1 (REVIEW-STEP2.md) -------------------------------------------------------

SINCE = '2026-10-01T00:00:00.000Z'      # sending turned on before every app stamp of these tests


def pspan(sid):
    return tuple(int(x) for x in sid.split('.')[1:])


def edited_epub(folder):
    '''The library's EPUB no longer matches the KEPUB Calibre-Web served (edited after kepubify
    ran: curly quotes straightened in Part II), so many of the Kobo's spans resolve only as
    span~ (REVIEW-STEP2 B2, probe_span_backward).'''
    path = os.path.join(folder, 'edited', 'metamorphosis.epub')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(EPUB) as zin, zipfile.ZipFile(path, 'w') as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == CH.format(2):
                s = data.decode('utf-8')
                s = s.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
                data = s.encode('utf-8')
            zout.writestr(item, data)
    return path


class ReviewB1Tests(PushBase):
    '''B1: what is SENT must be ahead in the Kobo's own terms: a push the Kobo gets as % only
    must carry a % strictly above the Kobo's own (plus a margin).'''

    def test_B1_a_chapter_percent_push_below_the_kobos_own_percent_is_not_sent(self):
        # probe_percent_backward A1: the Kobo (plain EPUB) at 47 % of the book, 50 % of Part II;
        # the app at 60 % of Part II's text: ahead by the plugin's measure, but 45.66 % is sent.
        self.kobo(61, '2026-10-30 21:03:48', CH.format(2), '', '', 47.0, 50.0)
        self.app(NOKEPUB, 3, int(40209 * 0.60))
        res = self.push(since=SINCE)
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertIn('Kobo\'s own %', res.skipped[NOKEPUB])

    def test_B1_a_percent_only_kobo_is_compared_with_the_percent_sent(self):
        # A2: Kobo at 40 % only; the app's own progress says 41 %, the text fraction sent is 39.5 %.
        self.kobo(60, '2026-10-30 21:03:48', None, None, None, 40.0, None)
        self.app(UUID, 3, int(0.395 * 138516) - 39125, progress=0.41)
        res = self.push(since=SINCE)
        self.assertEqual((res.sent, self.puts()), ([], []))

    def test_B1_a_percent_push_must_clear_the_margin(self):
        self.kobo(60, '2026-10-30 21:03:48', None, None, None, 40.0, None)
        self.app(UUID, 3, int(0.403 * 138516) - 39125, progress=0.5)            # 40.3 %: inside the margin
        self.assertEqual(self.push(since=SINCE).sent, [])
        self.app(UUID, 3, int(0.45 * 138516) - 39125, progress=0.5, stamp='2026-10-31T10:05:00.000Z')
        res = self.push(since=SINCE)
        self.assertEqual(res.sent, [UUID])
        bm = self.puts()[0][2]['ReadingStates'][0]['CurrentBookmark']
        self.assertGreater(bm['ProgressPercent'], 40.5)


class ReviewB2Tests(PushBase):
    '''B2 (and N1): a span push is ahead only when its kobo.P.S comes after the Kobo's own span
    (same file) or its file comes later in the spine; never the Kobo's own span again.'''

    def test_B2_never_a_span_before_the_kobos_own_span(self):
        edited = edited_epub(self.dir)
        self.books[60] = (UUID, edited, KEPUB)
        e, k = kobo_position.Book(edited), kobo_position.Book(KEPUB)
        try:
            approx = []
            for sid in [s for s in k.text(3).ids if kobo_position._SPAN_ID.match(s)]:
                row = {'location_type': 'KoboSpan', 'location_value': sid, 'location_source': CH.format(2),
                       'progress_percent': 30.0, 'content_source_progress_percent': 10.0}
                place = kobo_position.map_bookmark(e, row, k)
                if place is not None and place['precision'] == kobo_position.SPAN_APPROX:
                    approx.append((sid, place['textAnchor']))
        finally:
            e.close()
            k.close()
        self.assertGreater(len(approx), 20)
        self.cw.store = False                    # the Kobo row is set by the test each time
        self.kobo(60, '2026-10-30 21:03:48', CH.format(2), 'KoboSpan', approx[0][0], 30.0, 10.0)
        backwards = []
        for i, (sid, anchor) in enumerate(approx):
            con = sqlite3.connect(self.app_db)
            con.execute("UPDATE kobo_bookmark SET location_value=?, last_modified='2026-10-30 21:03:48'", (sid,))
            con.commit(); con.close()
            kobo_push.forget_failures()
            self.memory.clear()
            self.app(UUID, 3, anchor + 1, stamp='2026-10-31T10:{:02d}:{:02d}.000Z'.format(i // 60, i % 60))
            before = len(self.puts())
            self.push(since=SINCE)
            for put in self.puts()[before:]:
                sent = put[2]['ReadingStates'][0]['CurrentBookmark']['Location']['Value']
                if sent and pspan(sent) <= pspan(sid):
                    backwards.append((sid, sent))
        self.assertEqual(backwards, [])

    def test_B2_the_worst_probe_case_sends_nothing(self):
        # probe_span_backward: Kobo at kobo.19.2 (span~), the app 1 character past the plugin's
        # estimate of it -> kobo.18.6 was sent, 591 characters back.
        edited = edited_epub(self.dir)
        self.books[60] = (UUID, edited, KEPUB)
        e, k = kobo_position.Book(edited), kobo_position.Book(KEPUB)
        try:
            row = {'location_type': 'KoboSpan', 'location_value': 'kobo.19.2', 'location_source': CH.format(2),
                   'progress_percent': 30.0, 'content_source_progress_percent': 10.0}
            anchor = kobo_position.map_bookmark(e, row, k)['textAnchor']
        finally:
            e.close()
            k.close()
        self.kobo(60, '2026-10-30 21:03:48', CH.format(2), 'KoboSpan', 'kobo.19.2', 30.0, 10.0)
        self.app(UUID, 3, anchor + 1)
        res = self.push(since=SINCE)
        self.assertEqual((res.sent, self.puts()), ([], []))

    def test_N1_the_kobos_own_span_is_not_sent_again(self):
        self.kobo(60, '2026-10-30 21:03:48', CH.format(2), 'KoboSpan', 'kobo.5.1', 30.0, 7.0)
        self.app(UUID, 3, self.anchor_of(3, 'kobo.5.1', plus=7))     # a few characters into the Kobo's sentence
        res = self.push(since=SINCE)
        self.assertEqual((res.sent, self.puts()), ([], []))

    def test_a_span_push_never_carries_a_lower_percent_than_the_kobos_own(self):
        # Ruling (round 1): two signals must not disagree. kobo.5.1 -> kobo.30.1 is forward,
        # but its text fraction (~48 %) is below the Kobo's own 60 %: the Kobo's % is kept.
        self.kobo(60, '2026-10-30 21:03:48', CH.format(2), 'KoboSpan', 'kobo.5.1', 60.0, 7.0)
        self.app(UUID, 3, self.anchor_of(3, 'kobo.30.1', plus=3))
        res = self.push(since=SINCE)
        self.assertEqual(res.sent, [UUID])
        bm = self.puts()[0][2]['ReadingStates'][0]['CurrentBookmark']
        self.assertEqual(bm['Location']['Value'], 'kobo.30.1')
        self.assertGreaterEqual(bm['ProgressPercent'], 60.0)


class ReviewB3Tests(PushBase):
    '''B3: only places read after sending was turned on are sent; an empty Kobo bookmark is not
    proof the Kobo has not read the book offline.'''

    def test_B3_old_app_places_are_not_sent_after_ticking(self):
        for bid, uuid in ((60, UUID), (61, NOKEPUB), (62, THIRD)):
            self.kobo(bid, '2026-11-04 21:00:00')                          # synced yesterday, bookmark empty
            self.app(uuid, 3, 500, stamp='2025-03-01T10:00:00.000Z')       # read in the app long ago
        res = self.push(since='2026-11-05T08:00:00.000Z')
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertTrue(all('before sending' in res.skipped[u] for u in (UUID, NOKEPUB, THIRD)), res.skipped)
        # reading on after the box was ticked: that one is sent
        self.app(UUID, 3, 900, stamp='2026-11-05T08:00:01.000Z')
        self.assertEqual(self.push(since='2026-11-05T08:00:00.000Z').sent, [UUID])

    def test_B3_no_start_time_sends_nothing(self):
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        res = self.push(since=None)
        self.assertEqual((res.sent, self.puts()), ([], []))

    def test_B3_the_cli_starts_sending_at_its_first_push_run(self):
        self.metadata_db()
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)                                             # read before the first --push
        state = os.path.join(self.dir, 'state.json')
        args = ['--library', self.lib, '--app-db', self.app_db, '--user', 'admin', '--push',
                '--calibre-web', self.cw.url, '--state', state]
        with contextlib.redirect_stdout(io.StringIO()):
            kobo_import.main(args)
        self.assertEqual(self.puts(), [])
        with open(state, encoding='utf-8') as f:
            since = json.load(f)[os.path.realpath(self.lib)]['push_since']
        self.app(UUID, 3, 900, stamp=iso_in(1))                            # read after it
        with contextlib.redirect_stdout(io.StringIO()):
            kobo_import.main(args)
        self.assertEqual(len(self.puts()), 1)
        with open(state, encoding='utf-8') as f:
            self.assertEqual(json.load(f)[os.path.realpath(self.lib)]['push_since'], since)


class ReviewS1Tests(PushBase):
    '''S1: our push reported back by the Kobo in its own terms is still our echo.'''

    def pushed_span(self):
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.app_anchor = self.anchor_of(3, 'kobo.5.1', plus=7)
        self.app(UUID, 3, self.app_anchor)
        kres, pres = self.sync(since=SINCE)
        self.assertEqual(pres.sent, [UUID])

    def assert_no_kobo_entry(self):
        before = open(self.positions_path, 'rb').read()
        kres, _ = self.sync(push=False)
        self.assertEqual(kres.written, [])
        self.assertEqual(open(self.positions_path, 'rb').read(), before)

    def test_S1_the_kobo_reports_the_top_of_its_page(self):
        self.pushed_span()
        self.kobo_moves(60, 'KoboSpan', 'kobo.4.7', CH.format(2), 34.0, 7.0)     # the sentence before ours
        self.assert_no_kobo_entry()

    def test_S1_the_kobo_spells_source_its_own_way(self):
        self.pushed_span()
        self.kobo_moves(60, 'KoboSpan', 'kobo.5.1', '/' + CH.format(2), 34.0, 7.0)
        self.assert_no_kobo_entry()

    def test_S1_a_percent_push_landing_slightly_behind_is_ours(self):
        self.kobo(61, '2026-10-30 21:03:48')
        self.app(NOKEPUB, 3, 20000)
        _, pres = self.sync(since=SINCE)
        self.assertEqual(pres.sent, [NOKEPUB])
        sent = self.memory[NOKEPUB]['progressPercent']
        # the Kobo lands by its own measure a little before our % and reports its own place
        # (re-review R1-S1: further on than our % + 0.5 is a real move, not ours)
        self.kobo_moves(61, '', '', CH.format(2), sent - 1.0, 45.0)
        self.assert_no_kobo_entry()

    def test_S1_a_real_move_outside_the_window_is_imported(self):
        self.pushed_span()
        self.kobo_moves(60, 'KoboSpan', 'kobo.3.1', CH.format(1), 10.0, 2.0)     # back in Part I: a real move
        kres, _ = self.sync(push=False)
        self.assertEqual(kres.written, [UUID])


class ReviewS2Tests(PushBase):
    '''S2: a KEPUB Calibre-Web added while calibre was open (calibre's lookup does not list it).'''

    def setUp(self):
        PushBase.setUp(self)
        folder = os.path.join(self.dir, 'book60')
        os.makedirs(folder)
        shutil.copy(EPUB, os.path.join(folder, 'Kafka - The Metamorphosis.epub'))
        shutil.copy(KEPUB, os.path.join(folder, 'Kafka - The Metamorphosis.kepub'))
        self.books[60] = (UUID, os.path.join(folder, 'Kafka - The Metamorphosis.epub'), None)   # not listed

    def test_S2_the_kepub_beside_the_epub_gives_a_span_push(self):
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.app(UUID, 3, self.anchor_of(3, 'kobo.5.1', plus=7))
        _, pres = self.sync(since=SINCE)
        self.assertEqual(pres.precisions.get(UUID), kobo_position.SPAN, pres.skipped)

    def test_S2_the_import_uses_it_too(self):
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        kres, _ = self.sync(push=False)
        self.assertEqual(self.read_positions()['positions'][UUID]['kobo']['precision'], kobo_position.SPAN)

    def test_S2_a_kobo_span_with_no_kepub_anywhere_is_not_answered_by_percent(self):
        os.remove(os.path.join(self.dir, 'book60', 'Kafka - The Metamorphosis.kepub'))
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.app(UUID, 4, 100)
        res = self.push(since=SINCE)
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertIn('KEPUB', res.skipped[UUID])


class ReviewS3Tests(PushBase):

    def test_S3_a_kobo_sync_during_the_run_is_not_overwritten(self):
        # probe_race: the Kobo syncs Part III while the run maps; the run must not PUT Part II.
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.app(UUID, 3, self.anchor_of(3, 'kobo.5.1', plus=7))
        real = kobo_push.kobo_location

        def kobo_syncs_meanwhile(*a, **kw):
            self.kobo_moves(60, 'KoboSpan', 'kobo.7.4', CH.format(3), 70.0, 12.0)
            return real(*a, **kw)
        with mock.patch('kobo_push.kobo_location', side_effect=kobo_syncs_meanwhile):
            res = self.push(since=SINCE)
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertEqual(self.kobo_row(60)[1], 'kobo.7.4')
        self.assertIn('synced', res.skipped[UUID])


class ReviewS4Tests(PushBase):
    '''S4: the run's sending has a wall-clock budget that covers name lookup too.'''

    def test_S4_a_hanging_name_lookup_is_cut_by_the_budget(self):
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        real = socket.getaddrinfo

        def slow(host, *a, **kw):
            if host == 'slow-name.invalid':
                time.sleep(4)
            return real(host, *a, **kw)
        t = time.monotonic()
        with mock.patch('socket.getaddrinfo', side_effect=slow):
            res = self.push(url='http://slow-name.invalid:8083', since=SINCE, budget=1.0, timeout=1.0)
        self.assertLess(time.monotonic() - t, 2.5)
        self.assertEqual((res.sent, res.more), ([], False))
        self.assertEqual(len([n for n in res.notes if 'slow-name.invalid' in n]), 1, res.notes)

    def test_S4_a_slow_server_leaves_the_rest_for_the_next_run(self):
        self.cw.delay = 0.6
        for bid, uuid in ((60, UUID), (61, NOKEPUB), (62, THIRD)):
            self.kobo(bid, '2026-10-30 21:03:48')
            self.app(uuid, 3, 500)
        res = self.push(since=SINCE, budget=1.0)
        self.assertEqual((len(res.sent), res.more, res.notes), (2, True, []))
        res = self.push(since=SINCE, budget=1.0)
        self.assertEqual((len(res.sent), res.more), (1, False))


class ReviewS5Tests(PushBase):

    def test_S5_no_token_maps_nothing_and_never_asks_for_more(self):
        # probe_no_token_loop: 30 books to send, the token deleted in Calibre-Web.
        con = sqlite3.connect(self.app_db)
        con.execute('DELETE FROM remote_auth_token')
        con.commit(); con.close()
        for i in range(30):
            bid = 500 + i
            uuid = '55555555-0000-0000-0000-%012d' % bid
            self.books[bid] = (uuid, EPUB, KEPUB)
            self.kobo(bid, '2026-10-30 21:03:48')
            self.app(uuid, 3, 500)
        for run in (1, 2):
            with mock.patch('kobo_push.Book', wraps=kobo_position.Book) as m:
                res = self.push(since=SINCE)
            self.assertEqual((m.call_count, res.more), (0, False), run)
            self.assertEqual(len([n for n in res.notes if 'token' in n.lower()]), 1, res.notes)


class ReviewNitTests(PushBase):

    def test_N2_a_book_finished_on_the_kobo_with_an_empty_bookmark_is_not_sent(self):
        con = sqlite3.connect(self.app_db)
        con.execute('CREATE TABLE IF NOT EXISTS book_read_link (id INTEGER PRIMARY KEY, book_id INTEGER, '
                    'user_id INTEGER, read_status INTEGER)')
        con.execute('INSERT INTO book_read_link (book_id, user_id, read_status) VALUES (60, 1, 1)')
        con.commit(); con.close()
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        res = self.push(since=SINCE)
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertIn('Finished', res.skipped[UUID])

    def test_N4_a_kepub_book_pushed_by_percent_does_not_say_no_kepub(self):
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 0, 3)                                               # the cover wrapper: no span there
        res = self.push(since=SINCE)
        self.assertEqual(res.precisions.get(UUID), kobo_position.CHAPTER_PERCENT)
        self.assertFalse([line for line in res.lines if 'no KEPUB' in line], res.lines)

    def test_N7_a_pasted_kobo_sync_link_is_cut_to_the_address(self):
        self.assertEqual(kobo_push.calibre_web_url({'cw_url': 'http://mini.local:8083/kobo/' + 'ab' * 16}),
                         'http://mini.local:8083')
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        res = self.push(url=self.cw.url + '/kobo/' + TOKEN2, since=SINCE)
        self.assertEqual(res.sent, [UUID])
        self.assertTrue(self.puts()[0][1].startswith('/kobo/{}/v1/'.format(TOKEN)))
        self.assertTrue(any('/kobo/' in n for n in res.notes), res.notes)
        self.assertNotIn(TOKEN2, ' '.join(res.notes))



# -- re-review round 1 (REVIEW-STEP2.md "Re-review round 1") ----------------------------------

class ReReviewS1Tests(PushBase):
    '''R1-S1: an echo window swallows only the Kobo's report OF OUR PUSH (its place still
    matches what we sent); a Kobo move beyond it imports exactly as in 1.4.0, and once the app
    or the Kobo has moved on, the record no longer hides anything.'''

    def pushed_percent(self):
        # the Kobo (plain EPUB) at 44 % of the book; the app at 60 % of Part II's text (45.66 %)
        self.kobo(61, '2026-10-30 21:03:48', CH.format(2), '', '', 44.0, 30.0)
        self.app(NOKEPUB, 3, int(40209 * 0.60))
        _, pres = self.sync()
        self.assertEqual(pres.sent, [NOKEPUB])
        return self.memory[NOKEPUB]['progressPercent']

    def test_R1S1_a_forward_kobo_move_after_a_percent_push_is_imported(self):
        sent = self.pushed_percent()
        self.assertAlmostEqual(sent, 45.66, delta=0.05)
        self.kobo_moves(61, '', '', CH.format(2), 47.5, 55.0)            # read on, on the Kobo
        kres, _ = self.sync(push=False)
        self.assertEqual(kres.written, [NOKEPUB])
        self.assertEqual(self.read_positions()['positions'][NOKEPUB]['deviceId'], kobo_import.KOBO_DEVICE_ID)

    def test_R1S1_the_kobo_at_our_percent_within_rounding_is_still_ours(self):
        sent = self.pushed_percent()
        self.kobo_moves(61, '', '', CH.format(2), round(sent + 0.4, 1), 61.0)
        before = open(self.positions_path, 'rb').read()
        kres, _ = self.sync(push=False)
        self.assertEqual(kres.written, [])
        self.assertEqual(open(self.positions_path, 'rb').read(), before)

    def test_R1S1_a_kobo_span_after_ours_is_a_real_move(self):
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.app(UUID, 3, self.anchor_of(3, 'kobo.5.1'))                 # exactly on kobo.5.1's start
        _, pres = self.sync()
        self.assertEqual(pres.sent, [UUID])
        self.kobo_moves(60, 'KoboSpan', 'kobo.5.2', CH.format(2), 30.0, 7.5)
        kres, _ = self.sync(push=False)
        self.assertEqual(kres.written, [UUID])

    def test_R1S1_a_kobo_paging_back_after_the_app_moved_on_shows_the_card_again(self):
        # re-review probe: span push in Part II; the app reads on into Part III (Calibre-Web
        # unreachable, so not sent); then the Kobo pages back about a page in Part II.
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        anchor = self.anchor_of(3, 'kobo.8.1', plus=5)
        self.app(UUID, 3, anchor)
        _, pres = self.sync()
        self.assertEqual(pres.sent, [UUID])
        self.app(UUID, 4, 9000, stamp=iso_in(0))
        _, pres = self.sync(url=closed_port())
        self.assertEqual(pres.sent, [])
        time.sleep(0.01)
        e, k = kobo_position.Book(EPUB), kobo_position.Book(KEPUB)
        try:
            _, spans = kobo_position.kepub_spans(e, k, 3)
        finally:
            e.close()
            k.close()
        sid = max((a, s) for a, s in spans if a <= anchor - 1200)[1]
        self.kobo_moves(60, 'KoboSpan', sid, CH.format(2), 30.0, 20.0)
        kres, _ = self.sync(push=False)
        self.assertEqual(kres.written, [UUID])

    def test_R1S1_after_a_real_kobo_move_a_page_back_is_imported(self):
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.app(UUID, 3, self.anchor_of(3, 'kobo.5.1', plus=7))
        _, pres = self.sync()
        self.assertEqual(pres.sent, [UUID])
        self.kobo_moves(60, 'KoboSpan', 'kobo.20.1', CH.format(2), 45.0, 30.0)   # the Kobo reads on
        kres, _ = self.sync(push=False)
        self.assertEqual(kres.written, [UUID])
        time.sleep(0.01)
        self.kobo_moves(60, 'KoboSpan', 'kobo.4.7', CH.format(2), 30.0, 7.0)     # then pages way back
        kres, _ = self.sync(push=False)
        self.assertEqual(kres.written, [UUID])
        self.assertEqual(self.read_positions()['positions'][UUID]['kobo']['value'], 'kobo.4.7')


class ReReviewS2Tests(PushBase):
    '''R1-S2: what is about to be sent is persisted BEFORE the PUT (unconfirmed), then confirmed
    or dropped; a calibre that closes mid-send never sees its own push come back as a card.'''

    def test_R1S2_the_record_is_persisted_before_the_put_and_confirmed_after(self):
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        saved = []

        def persist(memory):
            saved.append(json.loads(json.dumps(memory)))
        at_put = []
        real = kobo_push._send

        def send(*a, **kw):
            at_put.append(json.loads(json.dumps(saved[-1])) if saved else None)
            return real(*a, **kw)
        with mock.patch('kobo_push._send', side_effect=send):
            res = self.push(persist=persist)
        self.assertEqual(res.sent, [UUID])
        self.assertIs(at_put[0][UUID]['confirmed'], False)               # on disk before the request
        self.assertIs(saved[-1][UUID]['confirmed'], True)

    def test_R1S2_a_failed_put_is_dropped_from_what_was_persisted(self):
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        saved = []
        res = self.push(url=closed_port(), persist=lambda m: saved.append(dict(m)))
        self.assertEqual(res.sent, [])
        self.assertNotIn(UUID, saved[-1])

    def test_R1S2_calibre_closing_mid_send_does_not_bring_our_push_back(self):
        # re-review probe: the PUT lands, calibre closes before the GUI thread saves anything.
        self.kobo(60, '2026-10-30 21:03:48', CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.app(UUID, 3, self.anchor_of(3, 'kobo.5.1', plus=7))
        journal = os.path.join(self.dir, 'journal.json')
        state = kobo_push.load_state(journal, self.lib)
        worker_memory = state.memory
        res = kobo_push.push_positions(self.lib, self.app_db, self.lookup, base_url=self.cw.url, memory=worker_memory,
                                       user='admin', since=SINCE, persist=lambda m: state.save())
        self.assertEqual(res.sent, [UUID])
        del worker_memory, state                                          # calibre gone
        after_restart = kobo_push.load_state(journal, self.lib).memory
        before = open(self.positions_path, 'rb').read()
        kres = kobo_import.import_positions(self.lib, self.app_db, self.lookup, user='admin', pushed=after_restart)
        self.assertEqual(kres.written, [])
        self.assertEqual(open(self.positions_path, 'rb').read(), before)

    def test_R1S2_an_unconfirmed_record_whose_put_never_landed_is_sent_again(self):
        # calibre died after persisting, before the PUT: Calibre-Web still has the old place.
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        self.memory[UUID] = {'type': 'KoboSpan', 'value': 'kobo.5.9', 'source': CH.format(2), 'progressPercent': 30.0,
                             'contentSourceProgressPercent': 5.0, 'precision': 'span', 'chapter': 3,
                             'textAnchor': 480, 'app_chapter': 3, 'app_textAnchor': 500,
                             'app_updated_at': '2026-10-31T10:00:00.000Z', 'pushed_at': '2026-10-31T10:00:01Z',
                             'confirmed': False}
        res = self.push()
        self.assertEqual(res.sent, [UUID])
        self.assertIs(self.memory[UUID]['confirmed'], True)

    def test_R1S2_the_state_file_keeps_other_libraries(self):
        path = os.path.join(self.dir, 'journal.json')
        a, b = kobo_push.load_state(path, self.lib), kobo_push.load_state(path, self.dir)
        a.memory['x'] = {'value': 'a'}
        a.save()
        b.memory['y'] = {'value': 'b'}
        b.save()                                                          # must not drop library a
        self.assertEqual(kobo_push.load_state(path, self.lib).memory, {'x': {'value': 'a'}})


class ReReviewS3Tests(PushBase):
    '''R1-S3: the consent is checked before EVERY PUT of a running batch.'''

    def test_R1S3_unticking_stops_the_batch_at_once(self):
        for bid, uuid in ((60, UUID), (61, NOKEPUB), (62, THIRD)):
            self.kobo(bid, '2026-10-30 21:03:48')
            self.app(uuid, 3, 500)
        answers = iter([True])
        res = self.push(still_on=lambda: next(answers, False))           # unticked after the first PUT
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual((len(res.sent), res.more), (1, False))
        self.assertTrue(any('turned off' in n for n in res.notes), res.notes)

    def test_R1S3_off_before_the_first_put_sends_nothing(self):
        self.kobo(60, '2026-10-30 21:03:48')
        self.app(UUID, 3, 500)
        res = self.push(still_on=lambda: False)
        self.assertEqual((res.sent, self.puts()), ([], []))


class ReReviewNitTests(PushBase):

    def test_R1N1_a_kobo_at_100_percent_is_not_sent_a_place(self):
        # an old Part I span with 100 % (a Finished PUT kept the old Location): never raise to 100 %
        self.kobo(60, '2026-10-31 09:00:00', CH.format(1), 'KoboSpan', 'kobo.3.5', 100.0, 100.0)
        self.app(UUID, 3, self.anchor_of(3, 'kobo.5.1', plus=7), stamp='2026-10-31T10:00:00.000Z')
        res = self.push()
        self.assertEqual((res.sent, self.puts()), ([], []))
        self.assertIn('finished', res.skipped[UUID])

    def test_R1N1_the_chapter_percent_is_not_below_the_kobos_own_in_the_same_chapter(self):
        self.kobo(60, '2026-10-30 21:03:48', CH.format(2), 'KoboSpan', 'kobo.5.1', 30.0, 9.0)
        self.app(UUID, 3, self.anchor_of(3, 'kobo.6.1', plus=3))
        res = self.push()
        self.assertEqual(res.sent, [UUID])
        bm = self.puts()[0][2]['ReadingStates'][0]['CurrentBookmark']
        self.assertGreaterEqual(bm['ContentSourceProgressPercent'], 9.0)


if __name__ == '__main__':
    unittest.main()

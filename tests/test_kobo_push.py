# Last modified: 2026-11-05--0413
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
import unittest
import urllib.error
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
        self.books = {60: (UUID, EPUB, KEPUB), 61: (NOKEPUB, EPUB, None), 62: (THIRD, EPUB, KEPUB)}
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
        return kobo_push.push_positions(self.lib, self.app_db, self.lookup, base_url=url or self.cw.url,
                                        memory=self.memory, user=user, **kw)

    def sync(self, push=True, url=None, user='admin', memory='mine', **kw):
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

    def test_a_source_only_kobo_place_needs_a_later_chapter(self):
        self.kobo(60, '2026-10-30 21:03:48', CH.format(2), None, None, None, None)
        self.app(UUID, 3, 30000)                                # same chapter: the Kobo could be anywhere in it
        self.assertEqual(self.push().sent, [])
        self.app(UUID, 4, 10, stamp='2026-10-31T10:05:00.000Z')
        self.assertEqual(self.push().sent, [UUID])

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
                '--state', state]
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
                              '--calibre-web', self.cw.url, '--state', state])
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


if __name__ == '__main__':
    unittest.main()

# Last modified: 2026-11-05--0415
#
# Calibre-Web app.db (kobo_bookmark) -> CalibReader_positions.json
# (kobo_import.import_positions). app.db here is a throwaway sqlite file with
# Calibre-Web 0.6.27's own column names (cps/ub.py); the book is the SAFE
# library's Metamorphosis fixture.
#
#   python3 -m unittest discover -s tests -v      (from the plugin folder)

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import stat
import datetime
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import kobo_import  # noqa: E402
from kobo_import import KOBO_DEVICE_ID, import_positions, kobo_stamp, read_kobo_bookmarks  # noqa: E402

EPUB = os.path.join(HERE, 'fixtures', 'metamorphosis.epub')
KEPUB = os.path.join(HERE, 'fixtures', 'metamorphosis.kepub')
UUID = '4261f375-f734-42c2-b5f6-df7e99b16fa4'
OTHER = '70b275c5-99f6-4347-8ba4-d4435c38a3ae'
CH1 = 'OEBPS/7446794606207039498_5200-h-1.htm.xhtml'

SCHEMA = '''
CREATE TABLE user (id INTEGER PRIMARY KEY, name VARCHAR(64), email VARCHAR(120));
CREATE TABLE kobo_reading_state (id INTEGER PRIMARY KEY, user_id INTEGER, book_id INTEGER,
    last_modified DATETIME, priority_timestamp DATETIME);
CREATE TABLE kobo_bookmark (id INTEGER PRIMARY KEY, kobo_reading_state_id INTEGER, last_modified DATETIME,
    location_source VARCHAR, location_type VARCHAR, location_value VARCHAR,
    progress_percent FLOAT, content_source_progress_percent FLOAT);
'''


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


class ImportTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kobo-import-')
        self.lib = os.path.join(self.dir, 'library')
        os.mkdir(self.lib)
        self.app_db = os.path.join(self.dir, 'app.db')
        con = sqlite3.connect(self.app_db)
        con.executescript(SCHEMA)
        con.execute("INSERT INTO user VALUES (1, 'admin', 'a@x'), (2, 'martin', 'm@x')")
        con.commit()
        con.close()
        self.books = {60: (UUID, EPUB, KEPUB), 69: (OTHER, None, None)}
        self.next_id = 1

    def tearDown(self):
        shutil.rmtree(self.dir)

    def lookup(self, book_id):
        return self.books.get(book_id)

    def add(self, user_id, book_id, stamp, source=None, ltype=None, value=None, pct=None, cpct=None):
        con = sqlite3.connect(self.app_db)
        i = self.next_id
        self.next_id += 1
        con.execute('INSERT INTO kobo_reading_state VALUES (?,?,?,?,?)', (i, user_id, book_id, stamp, stamp))
        con.execute('INSERT INTO kobo_bookmark VALUES (?,?,?,?,?,?,?,?)',
                    (i, i, stamp, source, ltype, value, pct, cpct))
        con.commit()
        con.close()

    def positions_path(self):
        return os.path.join(self.lib, 'CalibReader_positions.json')

    def write_positions(self, table, **extra):
        data = {'schema': 1, 'updated_at': '2026-10-01T00:00:00Z', 'positions': table}
        data.update(extra)
        with open(self.positions_path(), 'w') as f:
            json.dump(data, f)

    def read_positions(self):
        with open(self.positions_path()) as f:
            return json.load(f)

    def run_import(self, user='__default__'):
        # Two Calibre-Web users exist (admin, martin): name one unless a test asks for none.
        return import_positions(self.lib, self.app_db, self.lookup,
                                user=('admin' if user == '__default__' else user))

    # -- stamps ------------------------------------------------------------------
    def test_stamp_is_utc_iso_with_milliseconds(self):
        self.assertEqual(kobo_stamp('2026-10-30 21:03:48.356000'), '2026-10-30T21:03:48.356Z')
        self.assertEqual(kobo_stamp('2026-10-30 21:03:48'), '2026-10-30T21:03:48.000Z')
        self.assertIsNone(kobo_stamp(None))
        self.assertIsNone(kobo_stamp('garbage'))

    # -- the write ------------------------------------------------------------------
    def test_new_entry_is_written_and_everything_else_kept(self):
        mine = {'chapter': 4, 'page': 2, 'scrollY': 0, 'progress': 0.3,
                'updated_at': '2026-10-02T10:00:00.000Z', 'deviceId': 'PHONE', 'textAnchor': 77, 'future': [1]}
        self.write_positions({OTHER: mine, 'weird': None}, extra_top={'keep': True})
        self.add(1, 60, '2026-10-30 21:03:48.356000', CH1, 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        res = self.run_import()
        self.assertEqual(res.written, [UUID])
        data = self.read_positions()
        self.assertEqual(data['positions'][OTHER], mine)
        self.assertIsNone(data['positions']['weird'])
        self.assertEqual(data['extra_top'], {'keep': True})
        e = data['positions'][UUID]
        self.assertEqual((e['chapter'], e['page'], e['scrollY']), (2, 0, 0))
        self.assertEqual(e['updated_at'], '2026-10-30T21:03:48.356Z')
        self.assertEqual(e['deviceId'], KOBO_DEVICE_ID)
        self.assertAlmostEqual(e['progress'], 0.12)
        self.assertIsInstance(e['textAnchor'], int)
        self.assertEqual(e['kobo']['precision'], 'span')
        self.assertEqual(e['kobo']['value'], 'kobo.3.5')

    def test_creates_the_file_when_missing(self):
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        res = self.run_import()
        self.assertEqual(res.written, [UUID])
        self.assertEqual(self.read_positions()['schema'], 1)

    def test_a_newer_app_position_wins(self):
        newer = {'chapter': 5, 'page': 0, 'scrollY': 0, 'progress': 0.99,
                 'updated_at': '2026-10-30T21:03:49.000Z', 'deviceId': 'PHONE'}
        self.write_positions({UUID: newer})
        before = sha(self.positions_path())
        self.add(1, 60, '2026-10-30 21:03:48.356000', CH1, 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        res = self.run_import()
        self.assertEqual(res.written, [])
        self.assertEqual(sha(self.positions_path()), before)    # not even rewritten

    def test_an_older_app_position_is_replaced(self):
        self.write_positions({UUID: {'chapter': 1, 'page': 0, 'scrollY': 0, 'progress': 0.01,
                                     'updated_at': '2026-10-29T08:00:00Z', 'deviceId': 'PHONE'}})
        self.add(1, 60, '2026-10-30 21:03:48.356000', CH1, 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.assertEqual(self.run_import().written, [UUID])
        self.assertEqual(self.read_positions()['positions'][UUID]['deviceId'], KOBO_DEVICE_ID)

    def test_matching_is_case_insensitive_and_keeps_the_files_key(self):
        self.write_positions({UUID.upper(): {'chapter': 1, 'progress': 0.01, 'updated_at': '2026-10-29T08:00:00Z'}})
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        self.run_import()
        table = self.read_positions()['positions']
        self.assertEqual(list(table), [UUID.upper()])
        self.assertEqual(table[UUID.upper()]['deviceId'], KOBO_DEVICE_ID)

    def test_second_run_is_a_no_op(self):
        self.add(1, 60, '2026-10-30 21:03:48.356000', CH1, 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.run_import()
        before = sha(self.positions_path())
        self.assertEqual(self.run_import().written, [])
        self.assertEqual(sha(self.positions_path()), before)

    def test_empty_bookmarks_from_the_first_sync_are_skipped(self):
        self.add(1, 60, '2026-10-30 21:03:48', None, None, None, None, None)
        res = self.run_import()
        self.assertEqual(res.written, [])
        self.assertFalse(os.path.exists(self.positions_path()))

    def test_a_book_without_an_epub_is_skipped(self):
        self.add(1, 69, '2026-10-30 21:03:48', CH1, None, None, 40.0, 5.0)
        res = self.run_import()
        self.assertEqual(res.written, [])
        self.assertIn(OTHER, res.skipped)

    def test_a_book_not_in_this_library_is_skipped(self):
        self.add(1, 999, '2026-10-30 21:03:48', CH1, None, None, 40.0, 5.0)
        self.assertEqual(self.run_import().written, [])

    def test_several_users_need_a_choice(self):
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        self.add(2, 60, '2026-10-30 22:00:00', CH1, None, None, 15.0, 50.0)
        res = self.run_import(user=None)
        self.assertEqual(res.written, [])
        self.assertTrue(any('martin' in n and 'admin' in n for n in res.notes), res.notes)
        res = self.run_import(user='Martin')
        self.assertEqual(res.written, [UUID])
        self.assertAlmostEqual(self.read_positions()['positions'][UUID]['progress'], 0.15)

    def test_newest_bookmark_per_book(self):
        rows, user, _ = read_kobo_bookmarks(self.app_db, 'admin')
        self.assertEqual(rows, [])
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        rows, user, notes = read_kobo_bookmarks(self.app_db, 'admin')
        self.assertEqual((len(rows), user), (1, 'admin'))

    def test_app_db_is_never_written(self):
        self.add(1, 60, '2026-10-30 21:03:48', CH1, 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        before = sha(self.app_db)
        self.run_import()
        self.run_import()
        self.assertEqual(sha(self.app_db), before)

    def test_a_damaged_positions_file_is_never_overwritten(self):
        with open(self.positions_path(), 'w') as f:
            f.write('{"schema": 1, "positions": {')
        before = sha(self.positions_path())
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        res = self.run_import()
        self.assertEqual(res.written, [])
        self.assertEqual(sha(self.positions_path()), before)
        self.assertTrue(res.notes)

    def test_missing_app_db_is_a_note_not_a_crash(self):
        res = import_positions(self.lib, os.path.join(self.dir, 'nope.db'), self.lookup, user='admin')
        self.assertEqual(res.written, [])
        self.assertTrue(res.notes)

    # -- the right library ------------------------------------------------------
    def _settings(self, calibre_dir):
        con = sqlite3.connect(self.app_db)
        con.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, config_calibre_dir VARCHAR)')
        con.execute('DELETE FROM settings')
        con.execute('INSERT INTO settings VALUES (1, ?)', (calibre_dir,))
        con.commit()
        con.close()

    def _metadata(self, folder, library_uuid):
        os.makedirs(folder, exist_ok=True)
        con = sqlite3.connect(os.path.join(folder, 'metadata.db'))
        con.execute('CREATE TABLE library_id (id INTEGER PRIMARY KEY, uuid TEXT NOT NULL)')
        con.execute('INSERT INTO library_id VALUES (1, ?)', (library_uuid,))
        con.commit()
        con.close()

    def test_an_app_db_serving_another_library_is_refused(self):
        other = os.path.join(self.dir, 'other-library')
        self._metadata(other, 'aaaaaaaa-0000-0000-0000-000000000001')
        self._metadata(self.lib, 'bbbbbbbb-0000-0000-0000-000000000002')
        self._settings(other)
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        res = self.run_import()
        self.assertEqual(res.written, [])
        self.assertTrue(any('another calibre library' in n for n in res.notes), res.notes)

    def test_the_same_library_under_another_path_is_accepted(self):
        twin = os.path.join(self.dir, 'mounted-elsewhere')
        self._metadata(twin, 'cccccccc-0000-0000-0000-000000000003')
        self._metadata(self.lib, 'cccccccc-0000-0000-0000-000000000003')
        self._settings(twin)
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        self.assertEqual(self.run_import().written, [UUID])

    def test_a_library_path_not_visible_here_is_accepted(self):
        self._settings('/books')          # Docker: the container's path
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        self.assertEqual(self.run_import().written, [UUID])

    # -- review round (REVIEW-STEP1.md) ---------------------------------------------
    @unittest.skipIf(os.name == 'nt', 'POSIX file modes (Windows has none: 0o666 / read-only only)')
    def test_H1_the_files_mode_is_kept(self):
        self.write_positions({OTHER: {'chapter': 1, 'progress': 0.1, 'updated_at': '2026-10-01T00:00:00Z'}})
        os.chmod(self.positions_path(), 0o644)
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        self.assertEqual(self.run_import().written, [UUID])
        self.assertEqual(stat.S_IMODE(os.stat(self.positions_path()).st_mode), 0o644)
        os.chmod(self.positions_path(), 0o664)
        self.add(1, 60, '2026-10-30 21:05:00', CH1, None, None, 13.0, 55.0)
        self.run_import()
        self.assertEqual(stat.S_IMODE(os.stat(self.positions_path()).st_mode), 0o664)

    @unittest.skipIf(os.name == 'nt', 'POSIX file modes (Windows has none: 0o666 / read-only only)')
    def test_H1_a_new_file_follows_the_umask_not_0600(self):
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        old = os.umask(0o022)
        try:
            self.run_import()
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(self.positions_path()).st_mode), 0o644)

    def test_L3_a_failed_replace_falls_back_to_rename(self):
        self.write_positions({})
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        real = os.replace
        calls = []
        def flaky(a, b):
            calls.append(1)
            if len(calls) == 1:
                raise OSError('busy')
            return real(a, b)
        with mock.patch('kobo_import.os.replace', side_effect=flaky):
            res = self.run_import()
        self.assertEqual(res.written, [UUID])
        self.assertIn(UUID, self.read_positions()['positions'])
        self.assertFalse([f for f in os.listdir(self.lib) if f.startswith('.CalibReader') or 'crtmp' in f or 'crbak' in f])

    def test_M1_an_app_upload_during_mapping_is_kept(self):
        self.write_positions({})
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        import kobo_import as ki
        real = ki.map_bookmark
        def mapping_while_the_app_writes(*a, **k):
            # The phone uploads while the plugin maps: another book's place.
            self.write_positions({OTHER: {'chapter': 7, 'progress': 0.7, 'updated_at': '2026-10-30T21:04:00.000Z',
                                          'deviceId': 'PHONE'}})
            return real(*a, **k)
        with mock.patch('kobo_import.map_bookmark', side_effect=mapping_while_the_app_writes):
            self.assertEqual(self.run_import().written, [UUID])
        table = self.read_positions()['positions']
        self.assertEqual(table[OTHER]['deviceId'], 'PHONE')
        self.assertIn(UUID, table)

    def test_M1_a_newer_app_place_for_the_same_book_during_mapping_wins(self):
        self.write_positions({})
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        import kobo_import as ki
        real = ki.map_bookmark
        def app_reads_on(*a, **k):
            self.write_positions({UUID: {'chapter': 4, 'progress': 0.9, 'updated_at': '2026-10-30T21:05:00.000Z',
                                         'deviceId': 'PHONE'}})
            return real(*a, **k)
        with mock.patch('kobo_import.map_bookmark', side_effect=app_reads_on):
            self.assertEqual(self.run_import().written, [])
        self.assertEqual(self.read_positions()['positions'][UUID]['deviceId'], 'PHONE')

    def test_M2_tz_suffixed_stamps_are_accepted(self):
        self.assertEqual(kobo_stamp('2026-10-30 21:03:48.356000+00:00'), '2026-10-30T21:03:48.356Z')
        self.assertEqual(kobo_stamp('2026-10-30T23:03:48+02:00'), '2026-10-30T21:03:48.000Z')

    def test_M2_a_stamp_far_in_the_future_is_skipped(self):
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
        self.add(1, 60, future, CH1, None, None, 12.0, 50.0)
        res = self.run_import()
        self.assertEqual(res.written, [])
        self.assertTrue(any('clock' in n for n in res.notes), res.notes)
        soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=3)).strftime('%Y-%m-%d %H:%M:%S')
        con = sqlite3.connect(self.app_db)
        con.execute('UPDATE kobo_bookmark SET last_modified=?', (soon,))
        con.commit(); con.close()
        self.assertEqual(self.run_import().written, [UUID])   # a few minutes of skew is fine

    def test_M4_a_source_not_in_the_book_is_skipped_not_mapped_by_percent(self):
        self.add(1, 60, '2026-10-30 21:03:48', 'OEBPS/not-in-this-book.xhtml', 'KoboSpan', 'kobo.3.5', 60.0, 10.0)
        res = self.run_import()
        self.assertEqual(res.written, [])
        self.assertIn(UUID, res.skipped)

    def test_M4_most_sources_unresolved_refuses_the_run(self):
        self.books[61] = ('11111111-0000-0000-0000-000000000061', EPUB, None)
        self.books[62] = ('11111111-0000-0000-0000-000000000062', EPUB, None)
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        self.add(1, 61, '2026-10-30 21:03:48', 'OEBPS/Text/chapter-005.xhtml', 'KoboSpan', 'kobo.1.1', 40.0, 10.0)
        self.add(1, 62, '2026-10-30 21:03:48', 'OEBPS/Text/chapter-009.xhtml', 'KoboSpan', 'kobo.1.1', 70.0, 10.0)
        res = self.run_import()
        self.assertEqual(res.written, [])
        self.assertTrue(any('another calibre library' in n or 'do not match' in n for n in res.notes), res.notes)

    def test_M5_percent_only_0_or_100_is_status_not_a_place(self):
        self.add(1, 60, '2026-10-30 21:03:48', None, None, None, 100.0, None)
        self.assertEqual(self.run_import().written, [])
        self.add(1, 60, '2026-10-30 21:04:48', None, None, None, 0.0, None)
        self.assertEqual(self.run_import().written, [])

    def test_M5_an_unchanged_location_under_a_fresh_stamp_is_not_remapped(self):
        self.add(1, 60, '2026-10-30 21:03:48', CH1, 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.assertEqual(self.run_import().written, [UUID])
        # A later PUT without a Location (Finished, reset…): CW keeps the old one.
        con = sqlite3.connect(self.app_db)
        con.execute("UPDATE kobo_bookmark SET last_modified='2026-10-31 09:00:00', progress_percent=100.0")
        con.commit(); con.close()
        res = self.run_import()
        self.assertEqual(res.written, [])
        self.assertEqual(self.read_positions()['positions'][UUID]['updated_at'], '2026-10-30T21:03:48.000Z')

    def test_M5_a_location_far_from_the_percentage_is_distrusted(self):
        # kobo.3.5 is ~3 % into Part I (chapter 2), the book ~12 %: a 90 % claim does not fit.
        self.add(1, 60, '2026-10-30 21:03:48', CH1, 'KoboSpan', 'kobo.3.5', 90.0, 3.0)
        res = self.run_import()
        self.assertEqual(res.written, [])
        self.assertIn(UUID, res.skipped)

    def test_L4_several_calibre_web_users_need_an_explicit_choice(self):
        # Only admin has places, but martin exists too: never pick silently.
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        res = self.run_import(user=None)
        self.assertEqual(res.written, [])
        self.assertTrue(any('choose' in n.lower() for n in res.notes), res.notes)
        self.assertEqual(self.run_import(user='admin').written, [UUID])

    def test_L4_the_guest_account_does_not_count(self):
        con = sqlite3.connect(self.app_db)
        con.execute("DELETE FROM user WHERE name='martin'")
        con.execute("INSERT INTO user VALUES (3, 'Guest', '')")
        con.commit(); con.close()
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        self.assertEqual(self.run_import(user=None).written, [UUID])

    def test_L1_a_run_maps_at_most_a_chunk_and_says_there_is_more(self):
        self.books[61] = ('11111111-0000-0000-0000-000000000061', EPUB, None)
        self.add(1, 60, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        self.add(1, 61, '2026-10-30 21:03:49', CH1, None, None, 12.0, 60.0)
        res = import_positions(self.lib, self.app_db, self.lookup, user='admin', max_books=1)
        self.assertEqual((len(res.written), res.more), (1, True))
        res = import_positions(self.lib, self.app_db, self.lookup, user='admin', max_books=1)
        self.assertEqual((len(res.written), res.more), (1, False))

    # -- re-check N1: a refused / failing run must not loop ----------------------------
    def _many_unresolved(self, n=40):
        for i in range(n):
            bid = 100 + i
            self.books[bid] = ('22222222-0000-0000-0000-%012d' % bid, EPUB, None)
            self.add(1, bid, '2026-10-30 21:03:48', 'OEBPS/Text/not-here-%d.xhtml' % i, 'KoboSpan', 'kobo.1.1', 40.0, 10.0)

    def test_N1_three_refused_runs_never_ask_for_more(self):
        kobo_import.forget_failures()
        self._many_unresolved()
        for _ in range(3):
            res = self.run_import()
            self.assertEqual(res.written, [])
            self.assertFalse(res.more, res.notes)

    def test_N1_failed_books_are_not_mapped_again_until_their_stamp_changes(self):
        kobo_import.forget_failures()
        self._many_unresolved()
        self.run_import()
        import kobo_import as ki
        with mock.patch('kobo_import.map_bookmark', wraps=ki.map_bookmark) as m:
            res = self.run_import()
        self.assertEqual(m.call_count, 0)
        self.assertFalse(res.more)
        # The Kobo moves one of them: that one is tried again.
        con = sqlite3.connect(self.app_db)
        con.execute("UPDATE kobo_bookmark SET last_modified='2026-10-31 08:00:00' WHERE id=1")
        con.commit(); con.close()
        with mock.patch('kobo_import.map_bookmark', wraps=ki.map_bookmark) as m:
            self.run_import()
        self.assertEqual(m.call_count, 1)

    def test_N1_a_chunk_of_good_books_still_continues(self):
        kobo_import.forget_failures()
        for i in range(3):
            bid = 200 + i
            self.books[bid] = ('33333333-0000-0000-0000-%012d' % bid, EPUB, None)
            self.add(1, bid, '2026-10-30 21:03:48', CH1, None, None, 12.0, 50.0)
        res = import_positions(self.lib, self.app_db, self.lookup, user='admin', max_books=2)
        self.assertEqual((len(res.written), res.more), (2, True))
        res = import_positions(self.lib, self.app_db, self.lookup, user='admin', max_books=2)
        self.assertEqual((len(res.written), res.more), (1, False))


if __name__ == '__main__':
    unittest.main()

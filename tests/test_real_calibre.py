# Last modified: 2026-11-05--0516
#
# test_real_calibre.py — CalibReader Sync against REAL calibre (LibraryDatabase, set_field,
# metadata.db, JSONConfig, the Settings widget), not the in-memory FakeDB of sync_core.py.
#
# Run it with tests/run_calibre.sh (calibre-debug + an isolated, throwaway calibre config
# directory + TZ=America/Los_Angeles, so last_modified comes back at -08:00 as on a user's
# Mac). Under plain python the real-calibre classes are skipped; the runner's guard tests
# run everywhere.
#
# Every library here is a THROWAWAY copy of the SAFE library's metadata.db (the template
# is CR_SAFE_LIBRARY, default ~/builds/plugin13/safe, itself a copy). A template on the NAS
# is refused, and the template is never opened by calibre, only copied.
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

import datetime
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

try:
    import calibre  # noqa: F401
    from calibre.constants import config_dir
    HAVE_CALIBRE = True
except ImportError:
    HAVE_CALIBRE = False

TEMPLATE = os.path.expanduser(os.environ.get('CR_SAFE_LIBRARY', '~/builds/plugin13/safe'))
UTC = datetime.timezone.utc


class RunnerGuardTests(unittest.TestCase):
    '''The runner itself: a run that executes nothing is VOID, never green.'''

    def runner(self):
        spec = importlib.util.spec_from_file_location('cr_run_tests', os.path.join(TESTS, 'run_tests.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_run_that_executes_nothing_is_void(self):
        r = self.runner()
        code, rows = r.run([('empty', unittest.TestSuite())], stream=open(os.devnull, 'w'))
        self.assertNotEqual(code, 0)

    def test_a_run_where_everything_is_skipped_is_void(self):
        class Skipped(unittest.TestCase):
            @unittest.skip('x')
            def test_x(self):
                pass
        r = self.runner()
        suite = unittest.TestLoader().loadTestsFromTestCase(Skipped)
        code, rows = r.run([('skipped', suite)], stream=open(os.devnull, 'w'))
        self.assertNotEqual(code, 0)

    def test_in_file_tests_are_found(self):
        r = self.runner()
        suite = r.in_file_suite(os.path.join(REPO, 'sidecar_merge.py'))
        self.assertGreater(suite.countTestCases(), 30)


# --------------------------------------------------------------------------- real calibre

def _check_environment():
    if not HAVE_CALIBRE:
        return
    env = os.environ.get('CALIBRE_CONFIG_DIRECTORY')
    if not env or os.path.realpath(env) != os.path.realpath(config_dir):
        raise RuntimeError('Refusing to run against the real calibre config ({}): use '
                           'tests/run_calibre.sh, which sets an isolated '
                           'CALIBRE_CONFIG_DIRECTORY.'.format(config_dir))
    real = os.path.realpath(TEMPLATE)
    if '/nas/' in real or real.startswith('/Volumes/') or not os.path.exists(os.path.join(real, 'metadata.db')):
        raise RuntimeError('CR_SAFE_LIBRARY must be a local throwaway copy of the SAFE library, '
                           'not {}'.format(real))


class Library(object):
    '''A throwaway calibre library: the template's metadata.db in a temp dir.'''

    def __init__(self):
        _check_environment()
        self.path = tempfile.mkdtemp(prefix='cr-real-')
        shutil.copy2(os.path.join(TEMPLATE, 'metadata.db'), self.path)
        self.open()

    def open(self):
        from calibre.db.legacy import LibraryDatabase
        self.legacy = LibraryDatabase(self.path)
        self.api = self.legacy.new_api

    def reopen(self):
        self.legacy.close()
        self.open()

    def close(self):
        try:
            self.legacy.close()
        finally:
            shutil.rmtree(self.path, ignore_errors=True)

    def add_column(self, label, name, datatype='bool'):
        self.legacy.create_custom_column(label, name, datatype, False)
        self.reopen()

    def delete_column(self, label):
        self.legacy.delete_custom_column(label)
        self.reopen()

    def books(self, n=3):
        '''[(book_id, uuid)] of the first n books.'''
        ids = sorted(self.api.all_book_ids())[:n]
        return [(i, self.api.field_for('uuid', i).lower()) for i in ids]

    def write_sync(self, books, file_stamp='2026-10-01T12:00:00.000Z'):
        with open(os.path.join(self.path, 'CalibReader_sync.json'), 'w', encoding='utf-8') as f:
            json.dump({'schema': 1, 'updated_at': file_stamp, 'books': books}, f)

    def read_sync(self):
        with open(os.path.join(self.path, 'CalibReader_sync.json'), encoding='utf-8') as f:
            return json.load(f)

    def db_last_modified(self, book_id):
        '''books.last_modified as the TEXT the apps read from metadata.db.'''
        con = sqlite3.connect('file:{}?mode=ro'.format(os.path.join(self.path, 'metadata.db')), uri=True)
        try:
            return con.execute('select last_modified from books where id=?', (book_id,)).fetchone()[0]
        finally:
            con.close()

    def set_settings(self, settings):
        from sync_core import SETTINGS_KEY
        self.api.set_pref(SETTINGS_KEY, settings)


def iso(day, hour=12):
    return '2026-10-{:02d}T{:02d}:00:00.250Z'.format(day, hour)


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealCalibreBase(unittest.TestCase):
    def setUp(self):
        self.lib = Library()
        import ui
        for key in list(ui.PREFS):          # the plugin's JSONConfig, in the throwaway config dir
            del ui.PREFS[key]

    def tearDown(self):
        self.lib.close()

    def v(self, column, book_id):
        return self.lib.api.field_for(column, book_id)

    def action(self):
        '''A stand-in for the toolbar action: the plugin's own methods on a plain object
        (kept for the whole test: the Kobo push memory, worker and back-off live on it).'''
        if getattr(self, '_action', None) is not None:
            return self._action
        import ui

        class Model(object):
            refreshed = 0

            def refresh(self):
                Model.refreshed += 1

        class View(object):
            def model(self):
                return Model()

        class StatusBar(object):
            messages = []

            def show_message(self, text, ms=0):
                StatusBar.messages.append(text)

        class Gui(object):
            library_path = self.lib.path
            library_view = View()
            current_db = self.lib.legacy
            status_bar = StatusBar()

        class Kick(object):
            starts = 0

            def start(self, ms):
                Kick.starts += 1

        class Action(object):
            gui = Gui()
            _kick = Kick()
        for name, member in vars(ui.CalibReaderSyncAction).items():
            if callable(member) and name.startswith('_') and not name.startswith('__'):
                setattr(Action, name, member)               # the real Kobo step and its helpers
        self.kicks = Kick
        self.status_messages = StatusBar.messages
        self._action = Action()
        return self._action

    def run_ui(self, approve=None, manual=False):
        '''The main sync path: ui.CalibReaderSyncAction._run, as the button and the
        automatic sync call it, with the plugin's real JSONConfig.'''
        import ui
        from sync_core import SETTINGS_KEY, normalise_settings
        settings = normalise_settings(self.lib.api.pref(SETTINGS_KEY, None))
        kwargs = {}
        if approve is not None:
            kwargs['approve'] = approve
        if manual:
            kwargs['manual'] = True
        return ui.CalibReaderSyncAction._run(self.action(), self.lib.api, settings, **kwargs)

    def wait_push(self, timeout=20):
        '''1.5.0 round 1 (S4): the Kobo push runs on a worker thread and reports back on the
        GUI thread. Process Qt events until it has (or there was none).'''
        app = qt_app()
        action = self.action()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if not getattr(action, '_push_pending', False):
                break
            time.sleep(0.02)
        app.processEvents()
        return getattr(action, '_last_push', None)


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealKoboRunTests(RealCalibreBase):
    '''1.5.0: the Kobo step of a real run (ui._run -> _run_kobo -> kobo_push.sync_kobo), with
    calibre's own lookup (format_abspath EPUB/KEPUB), the plugin's real JSONConfig and a
    stand-in Calibre-Web on 127.0.0.1 (tests/test_kobo_push.FakeCalibreWeb).'''

    def setUp(self):
        RealCalibreBase.setUp(self)
        if TESTS not in sys.path:
            sys.path.insert(0, TESTS)
        import test_kobo_push as tkp
        self.tkp = tkp
        (self.book, self.uuid), = self.lib.books(1)
        fixtures = os.path.join(TESTS, 'fixtures')
        self.lib.api.add_format(self.book, 'EPUB', os.path.join(fixtures, 'metamorphosis.epub'))
        self.lib.api.add_format(self.book, 'KEPUB', os.path.join(fixtures, 'metamorphosis.kepub'))
        self.app_db = os.path.join(self.lib.path, '..', os.path.basename(self.lib.path) + '-app.db')
        con = sqlite3.connect(self.app_db)
        con.executescript(tkp.SCHEMA)
        con.execute("INSERT INTO user VALUES (1, 'admin', 'a@x', 1)")
        con.execute('INSERT INTO remote_auth_token (auth_token, user_id, token_type) VALUES (?, 1, 1)', (tkp.TOKEN,))
        con.execute('INSERT INTO settings VALUES (1, ?)', (self.lib.path,))
        cur = con.execute("INSERT INTO kobo_reading_state (user_id, book_id, last_modified, priority_timestamp) "
                          "VALUES (1, ?, '2026-10-30 21:03:48', '2026-10-30 21:03:48')", (self.book,))
        con.execute("INSERT INTO kobo_bookmark (kobo_reading_state_id, last_modified, location_source, location_type, "
                    "location_value, progress_percent, content_source_progress_percent) "
                    "VALUES (?, '2026-10-30 21:03:48', ?, 'KoboSpan', 'kobo.3.5', 12.0, 3.0)",
                    (cur.lastrowid, tkp.CH.format(1)))
        con.commit()
        con.close()
        self.cw = tkp.FakeCalibreWeb(self.app_db, {self.uuid: self.book})
        with open(os.path.join(self.lib.path, 'CalibReader_positions.json'), 'w', encoding='utf-8') as f:
            json.dump({'schema': 1, 'positions': {self.uuid: {
                'chapter': 3, 'page': 1, 'scrollY': 0, 'progress': 0.4, 'textAnchor': 2900,
                'updated_at': '2026-10-31T10:00:00.000Z', 'deviceId': tkp.PHONE}}}, f)

    def tearDown(self):
        self.cw.stop()
        try:
            os.remove(self.app_db)
        except OSError:
            pass
        RealCalibreBase.tearDown(self)

    SINCE = '2026-10-01T00:00:00.000Z'                       # the box ticked before the app's place

    def set_kobo(self, **extra):
        import ui
        entry = {'app_db': self.app_db, 'user': 'admin', 'cw_url': self.cw.url}
        entry.update(extra)
        ui.PREFS['kobo'] = {self.lib.path: entry}

    def test_nothing_is_sent_until_the_box_is_ticked(self):
        self.set_kobo()                                     # Settings saved, box never ticked
        res = self.run_ui()
        self.wait_push()
        self.assertEqual((self.cw.requests, res.kobo_pushed), ([], 0))
        self.set_kobo(push=False)
        res = self.run_ui()                                  # N9: the second run's own result
        self.wait_push()
        self.assertEqual(self.cw.requests, [])
        self.assertFalse([n for n in res.notes if 'Kobo' in n], res.notes)
        self.assertIsNone(getattr(self.action(), '_last_push', None))

    def test_ticked_it_sends_the_span_once_and_the_import_skips_the_echo(self):
        import ui
        self.set_kobo(push=True, push_since=self.SINCE)
        res = self.run_ui()
        self.assertTrue(res.kobo_push_started, res.notes)
        pres = self.wait_push()
        self.assertEqual(len(pres.sent), 1, pres.notes)
        (_, path, body, user, _), = self.cw.requests
        self.assertNotIn(self.tkp.TOKEN, ' '.join(pres.notes + pres.lines + self.status_messages))
        loc = body['ReadingStates'][0]['CurrentBookmark']['Location']
        self.assertEqual((loc['Type'], loc['Source']), ('KoboSpan', self.tkp.CH.format(2)))
        self.assertTrue(any('sentence' in line for line in pres.lines), pres.lines)
        self.assertTrue(any('sent to the Kobo' in m for m in self.status_messages), self.status_messages)
        memory = ui.PREFS['kobo_pushed'][self.lib.path]
        self.assertEqual(memory[self.uuid.lower()]['value'], loc['Value'])
        before = open(os.path.join(self.lib.path, 'CalibReader_positions.json'), 'rb').read()
        res = self.run_ui()                                  # Calibre-Web stamped our push: newer than the app's
        pres = self.wait_push()
        self.assertEqual((res.kobo_imported, pres.sent, len(self.cw.requests)), (0, [], 1), res.notes)
        self.assertEqual(open(os.path.join(self.lib.path, 'CalibReader_positions.json'), 'rb').read(), before)

    def test_calibre_web_down_is_one_note_and_no_follow_up_run(self):
        self.set_kobo(push=True, push_since=self.SINCE, cw_url=self.tkp.closed_port())
        self.run_ui()
        pres = self.wait_push()
        self.assertEqual(pres.sent, [])
        self.assertEqual(len([n for n in pres.notes if 'did not answer' in n]), 1, pres.notes)
        self.assertEqual(self.kicks.starts, 0)
        self.assertTrue(any('did not answer' in m for m in self.status_messages), self.status_messages)

    def test_S4_the_push_never_blocks_the_gui_thread(self):
        import kobo_push
        self.set_kobo(push=True, push_since=self.SINCE)
        gate = threading.Event()
        real = kobo_push._send

        def held(*a, **kw):
            gate.wait(10)
            return real(*a, **kw)
        with mock.patch('kobo_push._send', side_effect=held):
            t = time.monotonic()
            res = self.run_ui()
            self.assertLess(time.monotonic() - t, 2.0)        # the run returned while the PUT is held
            self.assertTrue(res.kobo_push_started)
            self.assertEqual(self.cw.requests, [])
            gate.set()
            pres = self.wait_push()
        self.assertEqual(len(pres.sent), 1, pres.notes)

    def test_S4_after_a_network_failure_automatic_runs_wait_the_button_does_not(self):
        import kobo_push
        self.set_kobo(push=True, push_since=self.SINCE, cw_url=self.tkp.closed_port())
        with mock.patch('kobo_push._send', wraps=kobo_push._send) as send:
            self.run_ui()
            self.wait_push()
            self.assertEqual(send.call_count, 1)
            self.run_ui()                                    # automatic: in the back-off, nothing tried
            self.wait_push()
            self.assertEqual(send.call_count, 1)
            self.run_ui(manual=True)                          # the button always tries
            self.wait_push()
            self.assertEqual(send.call_count, 2)

    def test_B3_the_first_run_after_ticking_sends_no_old_place(self):
        import ui
        self.set_kobo(push=True)                              # ticked, no start time stored yet
        self.run_ui()
        pres = self.wait_push()
        self.assertEqual(self.cw.requests, [])
        self.assertIn('push_since', ui.PREFS['kobo'][self.lib.path])
        self.assertTrue(pres is None or not pres.sent)

    # -- re-review round 1 ---------------------------------------------------------------
    def second_book(self):
        '''A second book on the Kobo with an app place (for a batch of two PUTs).'''
        (_, _), (book2, uuid2) = self.lib.books(2)
        fixtures = os.path.join(TESTS, 'fixtures')
        self.lib.api.add_format(book2, 'EPUB', os.path.join(fixtures, 'metamorphosis.epub'))
        self.lib.api.add_format(book2, 'KEPUB', os.path.join(fixtures, 'metamorphosis.kepub'))
        con = sqlite3.connect(self.app_db)
        cur = con.execute("INSERT INTO kobo_reading_state (user_id, book_id, last_modified, priority_timestamp) "
                          "VALUES (1, ?, '2026-10-30 21:03:48', '2026-10-30 21:03:48')", (book2,))
        con.execute("INSERT INTO kobo_bookmark (kobo_reading_state_id, last_modified) VALUES (?, '2026-10-30 21:03:48')",
                    (cur.lastrowid,))
        con.commit()
        con.close()
        self.cw.books[uuid2] = book2
        path = os.path.join(self.lib.path, 'CalibReader_positions.json')
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        data['positions'][uuid2] = {'chapter': 3, 'page': 1, 'scrollY': 0, 'progress': 0.4, 'textAnchor': 500,
                                    'updated_at': '2026-10-31T10:00:00.000Z', 'deviceId': self.tkp.PHONE}
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        return uuid2

    def test_R1S3_unticking_stops_a_running_batch(self):
        import kobo_push
        self.second_book()
        self.set_kobo(push=True, push_since=self.SINCE)
        gate, entered = threading.Event(), threading.Event()
        real = kobo_push._send

        def held(*a, **kw):
            entered.set()
            gate.wait(10)
            return real(*a, **kw)
        with mock.patch('kobo_push._send', side_effect=held):
            self.run_ui()
            self.assertTrue(entered.wait(10))                  # the first PUT is in flight
            self.set_kobo(push=False)                         # the tester unticks the box
            gate.set()
            pres = self.wait_push()
        self.assertEqual(len(self.cw.requests), 1)
        self.assertTrue(any('turned off' in n for n in pres.notes), pres.notes)

    def test_R1S2_a_push_in_flight_is_already_on_disk(self):
        import kobo_push
        import ui
        self.set_kobo(push=True, push_since=self.SINCE)
        gate, entered = threading.Event(), threading.Event()
        real = kobo_push._send

        def held(*a, **kw):
            entered.set()
            gate.wait(10)
            return real(*a, **kw)
        try:
            with mock.patch('kobo_push._send', side_effect=held):
                self.run_ui()
                self.assertTrue(entered.wait(10))              # the PUT is in flight
                # calibre closes now: a new session reads what the old one had on disk
                fresh = kobo_push.load_state(ui.KOBO_JOURNAL, self.lib.path).memory
                self.assertIn(self.uuid.lower(), fresh)
                self.assertIs(fresh[self.uuid.lower()]['confirmed'], False)
                gate.set()
                pres = self.wait_push()
            self.assertEqual(len(pres.sent), 1)
            confirmed = kobo_push.load_state(ui.KOBO_JOURNAL, self.lib.path).memory[self.uuid.lower()]
            self.assertIs(confirmed['confirmed'], True)
        finally:
            gate.set()
            if os.path.exists(ui.KOBO_JOURNAL):
                os.remove(ui.KOBO_JOURNAL)

    def test_R1N3_a_run_that_found_the_worker_busy_is_run_again_after_it(self):
        import kobo_push
        self.set_kobo(push=True, push_since=self.SINCE)
        gate, entered = threading.Event(), threading.Event()
        real = kobo_push._send

        def held(*a, **kw):
            entered.set()
            gate.wait(10)
            return real(*a, **kw)
        with mock.patch('kobo_push._send', side_effect=held):
            self.run_ui()
            res = self.run_ui()                                # the worker is still sending
            self.assertFalse(res.kobo_push_started)
            gate.set()
            self.wait_push()
        self.assertEqual(self.kicks.starts, 1)


class RealSyncPathTests(RealCalibreBase):

    def test_apply_stamp_and_local_time(self):
        (b1, u1), (b2, u2) = self.lib.books(2)
        self.lib.api.set_field('#read_status', {b1: None, b2: None})
        self.lib.write_sync({u1: {'value': True, 'updated_at': '2030-01-01T00:00:00.000Z'},
                             u2: False})
        res = self.run_ui()
        self.assertIs(self.v('#read_status', b1), True)
        self.assertIs(self.v('#read_status', b2), False)
        self.assertEqual(res.flags_updated, 2)
        lm = self.v('last_modified', b1)
        if os.environ.get('TZ') == 'America/Los_Angeles':
            self.assertEqual(lm.utcoffset(), datetime.timedelta(hours=-8))   # local time, as on a Mac
        from sidecar_merge import parse_iso
        entry = self.lib.read_sync()['books'][u1]
        applied_at = parse_iso(entry['applied_at'])
        stored = parse_iso(self.lib.db_last_modified(b1))
        # the app floors metadata.db's last_modified to ms and compares strictly '>'
        floor = stored.replace(microsecond=stored.microsecond // 1000 * 1000)
        self.assertFalse(floor > applied_at, (self.lib.db_last_modified(b1), entry['applied_at']))
        self.assertEqual(self.run_ui().flags_updated, 0)                 # stable


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealColumnGoneTests(RealCalibreBase):
    def test_deleted_chosen_column_never_falls_back_to_read_status(self):
        # M1: the user moved from #read_status to #read (#read_status keeps stale values),
        # then deleted #read. A new phone mark must not land in #read_status.
        (b1, u1), (b2, u2) = self.lib.books(2)
        self.lib.add_column('read', 'Read')
        self.lib.api.set_field('#read_status', {b1: False, b2: None})
        self.lib.set_settings({'version': 1, 'read_column': '#read', 'percent_column': '',
                               'last_read_column': '', 'progress_threshold': 1, 'auto_sync': True})
        self.lib.write_sync({u1: {'value': True, 'updated_at': '2030-01-01T00:00:00.000Z'}})
        self.run_ui(approve='#read')                               # the first sync's OK (1.3.1)
        self.assertIs(self.v('#read', b1), True)
        self.lib.delete_column('read')
        self.lib.write_sync({u1: {'value': True, 'updated_at': '2030-01-02T00:00:00.000Z'},
                             u2: {'value': True, 'updated_at': '2030-01-02T00:00:00.000Z'}})
        res = self.run_ui()
        self.assertIs(self.v('#read_status', b1), False)
        self.assertIsNone(self.v('#read_status', b2))
        self.assertIsNone(res.read_column)
        self.assertTrue(any('#read' in n for n in res.notes), res.notes)


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealBareBoolTests(RealCalibreBase):
    def test_cleared_cell_is_not_refilled_by_file_stamp_churn(self):
        # M2 (review probe 2): Android writes bare bools; the file stamp moves on each upload
        (b1, u1), (b2, u2) = self.lib.books(2)
        self.lib.api.set_field('#read_status', {b1: None, b2: None})
        self.lib.write_sync({u1: True}, file_stamp='2030-01-01T00:00:00.000Z')
        self.assertEqual(self.run_ui().flags_updated, 1)
        self.lib.api.set_field('#read_status', {b1: None})        # cleared in calibre
        self.run_ui()
        self.lib.write_sync({u1: True, u2: True}, file_stamp='2030-01-05T00:00:00.000Z')
        self.run_ui()
        self.assertIsNone(self.v('#read_status', b1))
        self.assertIs(self.v('#read_status', b2), True)


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealColumnSwitchTests(RealCalibreBase):
    def test_switch_after_rating_edit_does_not_flip_the_phone(self):
        # M3 (review probe 4): #finished = No (old); phone marks Read at T1, applied to
        # #read_status; a rating edit at T2 > T1; the user picks #finished.
        (b1, u1), = self.lib.books(1)
        self.lib.add_column('finished', 'Finished')
        self.lib.api.set_field('#finished', {b1: False})
        self.lib.api.set_field('#read_status', {b1: None})
        import time
        time.sleep(0.05)
        from sidecar_merge import format_iso
        t1 = format_iso(datetime.datetime.now(UTC))
        self.lib.write_sync({u1: {'value': True, 'updated_at': t1}})
        self.assertEqual(self.run_ui().flags_updated, 1)
        time.sleep(0.05)
        self.lib.api.set_field('rating', {b1: 8})
        self.lib.set_settings({'version': 1, 'read_column': '#finished', 'percent_column': '',
                               'last_read_column': '', 'progress_threshold': 1, 'auto_sync': True})
        res = self.run_ui()
        entry = self.lib.read_sync()['books'][u1]
        self.assertEqual((entry['value'], entry['updated_at']), (True, t1))
        self.assertEqual(res.flags_sent_back, 0)


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealChangeLogTests(RealCalibreBase):
    def log_lines(self):
        folder = os.path.join(config_dir, 'plugins', 'calibreader_sync_changes')
        lines = []
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            with open(os.path.join(folder, name), encoding='utf-8') as f:
                lines += [json.loads(line) for line in f]
        return lines

    def setUp(self):
        RealCalibreBase.setUp(self)
        folder = os.path.join(config_dir, 'plugins', 'calibreader_sync_changes')
        shutil.rmtree(folder, ignore_errors=True)

    def test_sync_writes_the_change_log_in_the_config_dir(self):
        (b1, u1), (b2, u2) = self.lib.books(2)
        self.lib.api.set_field('#read_status', {b1: None, b2: False})
        self.lib.write_sync({u1: {'value': True, 'updated_at': '2030-01-01T00:00:00.000Z'},
                             u2: {'value': True, 'updated_at': '2030-01-01T00:00:00.000Z'}})
        self.run_ui()
        got = sorted((c['book_id'], c['column'], c['old'], c['new']) for c in self.log_lines())
        self.assertEqual(got, [(b1, '#read_status', None, True), (b2, '#read_status', False, True)])
        self.assertEqual(self.log_lines()[0]['title'], self.v('title', self.log_lines()[0]['book_id']))

    def test_first_sync_into_a_chosen_column_waits_for_the_ok(self):
        (b1, u1), (b2, u2) = self.lib.books(2)
        self.lib.add_column('read', 'Read')
        self.lib.set_settings({'version': 1, 'read_column': '#read', 'percent_column': '',
                               'last_read_column': '', 'progress_threshold': 1, 'auto_sync': True})
        self.lib.write_sync({u1: {'value': True, 'updated_at': '2030-01-01T00:00:00.000Z'},
                             u2: {'value': False, 'updated_at': '2030-01-01T00:00:00.000Z'}})
        res = self.run_ui()
        self.assertEqual((res.confirm['column'], res.confirm['fill']), ('#read', 2))
        self.assertIsNone(self.v('#read', b1))
        self.assertEqual(self.log_lines(), [])
        res = self.run_ui(approve='#read')
        self.assertEqual(res.flags_updated, 2)
        self.assertEqual((self.v('#read', b1), self.v('#read', b2)), (True, False))
        self.assertEqual(len(self.log_lines()), 2)


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealConsentTests(RealCalibreBase):
    def test_an_empty_first_run_is_not_consent(self):
        # Re-review M4 probe on real calibre
        books = self.lib.books(3)
        self.lib.add_column('read', 'Read')
        self.lib.set_settings({'version': 1, 'read_column': '#read', 'percent_column': '',
                               'last_read_column': '', 'progress_threshold': 1, 'auto_sync': True})
        self.lib.write_sync({'00000000-0000-0000-0000-000000000000': True})
        res = self.run_ui()
        self.assertIsNone(res.confirm)
        self.lib.write_sync({u: {'value': True, 'updated_at': '2020-01-01T00:00:00.000Z'} for _, u in books})
        res = self.run_ui()
        self.assertIsNotNone(res.confirm, 'the automatic run filled the column without asking')
        self.assertEqual(res.confirm['fill'], 3)
        self.assertEqual([self.v('#read', b) for b, _ in books], [None, None, None])


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealButtonTests(RealCalibreBase):
    '''The Sync Read Status button (ui.CalibReaderSyncAction.sync_read_status) with its
    dialogs stubbed: the first-sync question, then the summary.'''

    def press(self, answer):
        import ui
        lib, asked, shown = self.lib, [], []

        class Model(object):
            def refresh(self):
                pass

        class View(object):
            def model(self):
                return Model()

        class Gui(object):
            library_path = lib.path
            library_view = View()
            current_db = lib.legacy

        class Action(object):
            gui = Gui()
            _run = ui.CalibReaderSyncAction._run
            _file_stamps = ui.CalibReaderSyncAction._file_stamps
            _confirm_first_sync = ui.CalibReaderSyncAction._confirm_first_sync
            sync_read_status = ui.CalibReaderSyncAction.sync_read_status

        saved = ui.question_dialog, ui.info_dialog, ui.error_dialog
        ui.question_dialog = lambda parent, title, msg, **kw: asked.append(msg) or answer
        ui.info_dialog = ui.error_dialog = lambda parent, title, msg, **kw: shown.append(msg)
        try:
            Action().sync_read_status()
        finally:
            ui.question_dialog, ui.info_dialog, ui.error_dialog = saved
        return asked, shown

    def setUp(self):
        RealCalibreBase.setUp(self)
        (self.b1, u1), (self.b2, u2) = self.lib.books(2)
        self.lib.add_column('read', 'Read')
        self.lib.api.set_field('#read', {self.b2: False})
        self.lib.set_settings({'version': 1, 'read_column': '#read', 'percent_column': '',
                               'last_read_column': '', 'progress_threshold': 1, 'auto_sync': True})
        self.lib.write_sync({u1: {'value': True, 'updated_at': '2030-01-01T00:00:00.000Z'},
                             u2: {'value': True, 'updated_at': '2030-01-01T00:00:00.000Z'}})

    def test_no_writes_nothing_and_says_it_is_waiting(self):
        asked, shown = self.press(False)
        self.assertEqual(len(asked), 1)
        self.assertIn('1 empty cell(s)', asked[0])
        self.assertIn('change 1 filled cell(s)', asked[0])
        self.assertIsNone(self.v('#read', self.b1))
        self.assertIn('waiting for your OK', shown[0])

    def test_yes_fills_the_column(self):
        asked, shown = self.press(True)
        self.assertEqual(len(asked), 1)
        self.assertEqual((self.v('#read', self.b1), self.v('#read', self.b2)), (True, True))
        self.assertIn('Read status (#read): 2 book(s) updated.', shown[0])
        asked, shown = self.press(True)                            # the next press asks nothing
        self.assertEqual(asked, [])


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealUpgradeTests(RealCalibreBase):
    '''Upgrade from 1.2.5: its own code ran a sync and stored its prefs; then 1.3.x runs.'''

    def load_125(self):
        path = os.path.join(TESTS, 'fixtures', 'plugin_1_2_5')
        saved = {k: sys.modules.pop(k, None) for k in ('sidecar_merge', 'sync_core')}
        sys.path.insert(0, path)
        try:
            return importlib.import_module('sync_core')
        finally:
            sys.path.remove(path)
            for k, mod in saved.items():
                if mod is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = mod

    def test_125_records_carry_over(self):
        old = self.load_125()
        (b1, u1), (b2, u2) = self.lib.books(2)
        self.lib.api.set_field('#read_status', {b1: None, b2: None})
        self.lib.set_settings({'version': 1, 'percent_column': '', 'last_read_column': '',
                               'progress_threshold': 1, 'auto_sync': True})      # 1.2.5 shape
        self.lib.write_sync({u1: {'value': True, 'updated_at': '2030-01-01T00:00:00.000Z'}})
        res, flag_blob, pos_blob = old.run_sync(self.lib.api, self.lib.path,
                                                self.lib.api.pref('calibreader_sync'), {}, {})
        self.assertEqual(res.flags_updated, 1)
        import ui
        ui.PREFS['applied'], ui.PREFS['applied_pos'] = flag_blob, pos_blob    # what 1.2.5's ui stored
        self.lib.api.set_field('#read_status', {b1: None})                   # the user clears it
        res = self.run_ui()
        self.assertIsNone(self.v('#read_status', b1))                        # not refilled by the old entry
        self.assertEqual(res.flags_updated, 0)
        # 1.3.1 (L1): the 1.2.5 memory copied under this library, on disk; the old keys stay
        from calibre.utils.config import JSONConfig
        on_disk = JSONConfig('plugins/calibreader_sync')
        self.assertIn(u1, on_disk['applied'])
        (key, state), = on_disk['libraries'].items()
        self.assertIn(u1, state['applied'])
        self.assertEqual(on_disk['legacy_done'], [key])


_APP = None


def qt_app():
    global _APP
    from qt.core import QApplication
    if QApplication.instance() is None:
        from calibre.gui2 import Application
        _APP = Application([])
    return QApplication.instance()


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealSettingsDialogTests(RealCalibreBase):
    '''The Settings widget (config.ConfigWidget) on a real library: what OK stores.'''

    def widget(self):
        qt_app()
        import config

        class Gui(object):
            current_db = self.lib.legacy
            library_path = self.lib.path
            iactions = {}
        return config.ConfigWidget(Gui())

    def stored(self):
        from sync_core import SETTINGS_KEY
        return self.lib.api.pref(SETTINGS_KEY, None)

    def pick(self, combo, lookup):
        index = combo.findData(lookup)
        self.assertGreaterEqual(index, 0, lookup)
        combo.setCurrentIndex(index)

    def test_ok_for_another_reason_never_picks_a_read_column(self):
        # H1: a 1.2.x library without #read_status but with a Yes/No "Did not finish"
        self.lib.delete_column('read_status')
        self.lib.add_column('dnf', 'Did not finish')
        self.lib.add_column('read_percent', 'Read %', 'int')
        self.lib.set_settings({'version': 1, 'percent_column': '', 'last_read_column': '',
                               'progress_threshold': 1, 'auto_sync': True})
        w = self.widget()
        self.pick(w.combos['percent'], '#read_percent')          # the reason they opened Settings
        w.save_settings()
        stored = self.stored()
        self.assertEqual(stored['percent_column'], '#read_percent')
        # not even '#read_status': a stored choice switches on the no-fallback rule and the
        # first-sync question (re-review H1 test gap)
        self.assertNotIn('read_column', stored)

    def test_an_explicit_read_column_is_not_preselected_either(self):
        self.lib.delete_column('read_status')
        self.lib.add_column('read', 'Read')
        w = self.widget()
        self.assertEqual(w.combos['read'].currentData(), '#read_status')
        self.assertIn('Read (#read)', w.read_note.text())         # named as a hint
        w.save_settings()
        self.assertNotIn('read_column', self.stored())

    def test_wording_says_android_uses_read_status_only(self):
        # L3: Android reads #read_status only; only iPhone/iPad follow read_column
        from qt.core import QLabel
        text = ' '.join(label.text() for label in self.widget().findChildren(QLabel))
        self.assertIn('iPhone and iPad', text)
        self.assertIn('Android app uses #read_status only', text)
        self.assertNotIn('The CalibReader app reads the same column', text)

    def test_kobo_push_is_off_until_the_user_ticks_it(self):
        # 1.5.0: nothing is ever sent to the Kobo unless "Also send CalibReader's place to
        # the Kobo" is ticked; the address defaults to Calibre-Web on this computer.
        import kobo_push
        from ui import PREFS
        try:
            w = self.widget()
            self.assertFalse(w.kobo_push.isChecked())
            self.assertIn('Also send CalibReader', w.kobo_push.text())
            self.assertEqual(w.kobo_url.placeholderText(), 'http://127.0.0.1:8083')
            w.kobo_db.setText(os.path.join(self.lib.path, 'app.db'))
            w.save_kobo()                                           # OK without touching the box
            entry = PREFS['kobo'][self.lib.path]
            self.assertIs(entry['push'], False)
            self.assertFalse(kobo_push.push_enabled(entry))
            self.assertNotIn('push_since', entry)
            self.assertEqual(kobo_push.calibre_web_url(entry), 'http://127.0.0.1:8083')
            w = self.widget()
            w.kobo_push.setChecked(True)
            w.save_kobo()
            entry = PREFS['kobo'][self.lib.path]
            self.assertTrue(kobo_push.push_enabled(entry))
            ticked = entry['push_since']                            # B3: when sending was turned on
            t = datetime.datetime.strptime(ticked[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=UTC)
            self.assertLess(abs((datetime.datetime.now(UTC) - t).total_seconds()), 120)
            self.assertTrue(self.widget().kobo_push.isChecked())     # reopened: still on
            self.widget().save_kobo()                               # OK again: the start time stays
            self.assertEqual(PREFS['kobo'][self.lib.path]['push_since'], ticked)
            w = self.widget()
            w.kobo_push.setChecked(False)
            w.save_kobo()
            self.assertNotIn('push_since', PREFS['kobo'][self.lib.path])   # unticking clears it
            # N7: a pasted Kobo sync link is cut to the address (its token never stored)
            w = self.widget()
            w.kobo_url.setText('http://mini.local:8083/kobo/' + 'ab' * 16)
            w.save_kobo()
            self.assertEqual(PREFS['kobo'][self.lib.path]['cw_url'], 'http://mini.local:8083')
            # N8: without a KEPUB the Kobo gets the percentages only
            from qt.core import QLabel
            text = ' '.join(label.text() for label in self.widget().findChildren(QLabel))
            self.assertIn('percentages only', text)
        finally:
            PREFS['kobo'] = {}

    def test_picking_a_read_column_saves_it(self):
        self.lib.add_column('read', 'Read')
        w = self.widget()
        self.pick(w.combos['read'], '#read')
        w.save_settings()
        self.assertEqual(self.stored()['read_column'], '#read')
        w = self.widget()                                          # reopened: still #read, kept on OK
        self.assertEqual(w.combos['read'].currentData(), '#read')
        w.save_settings()
        self.assertEqual(self.stored()['read_column'], '#read')

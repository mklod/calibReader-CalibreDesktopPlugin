# Last modified: 2026-11-05--1118
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


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealServiceTests(RealKoboRunTests):
    '''1.6.0 (step 3): calibre and the background job (kobo_service) on one library: the
    plugin stands down when the job is installed, takes the same lock (held until its push
    worker ends) and shares one echo memory with the job (calibre's journal, re-read at
    every Kobo run).'''

    def setUp(self):
        RealKoboRunTests.setUp(self)
        self.home = tempfile.mkdtemp(prefix='cr-svc-')
        self.env = mock.patch.dict(os.environ, {
            'CALIBREADER_SYNC_HOME': os.path.join(self.home, 'home'),
            'CALIBREADER_SYNC_LOGS': os.path.join(self.home, 'logs'),
            'CALIBREADER_SYNC_AGENTS': os.path.join(self.home, 'LaunchAgents'),
        })
        self.env.start()
        os.environ.pop('CALIBREADER_SYNC_LABEL', None)
        self.zip = os.path.join(config_dir, 'plugins', 'CalibReader Sync.zip')

    def tearDown(self):
        import ui
        self.env.stop()
        shutil.rmtree(self.home, ignore_errors=True)
        for path in (ui.KOBO_JOURNAL, self.zip):
            if os.path.exists(path):
                os.remove(path)
        RealKoboRunTests.tearDown(self)

    def plist_present(self):
        import kobo_service
        path = kobo_service.plist_path(kobo_service.label_for(self.lib.path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'wb').close()

    def plugin_installed(self):
        import zipfile
        os.makedirs(os.path.dirname(self.zip), exist_ok=True)
        with zipfile.ZipFile(self.zip, 'w') as z:            # S5: the job checks it is >= 1.6.0
            with open(os.path.join(REPO, '__init__.py'), 'rb') as f:
                z.writestr('__init__.py', f.read())
            z.writestr('kobo_service.py', '# the job\n')

    def heartbeat(self, seconds_ago=0):
        import kobo_service
        t = datetime.datetime.now(UTC) - datetime.timedelta(seconds=seconds_ago)
        iso = t.strftime('%Y-%m-%dT%H:%M:%S.') + '{:03d}Z'.format(t.microsecond // 1000)
        kobo_service.save_state(self.lib.path, {'last_run_at': iso, 'last_ok_at': iso, 'status': 'done',
                                                'summary': '0 places imported, 0 sent'})

    def positions(self):
        with open(os.path.join(self.lib.path, 'CalibReader_positions.json'), 'rb') as f:
            return f.read()

    def test_with_the_job_installed_automatic_runs_leave_the_kobo_to_it(self):
        self.set_kobo(push=True, push_since=self.SINCE, service=True)
        self.plist_present()
        self.heartbeat()                                     # S1: a good job run in the last 15 minutes
        res = self.run_ui()                                  # start-up / poll / follow-up kick
        self.wait_push()
        self.assertFalse(res.kobo_push_started)
        self.assertEqual(self.cw.requests, [])
        res = self.run_ui(manual=True)                       # the button still does it
        pres = self.wait_push()
        self.assertTrue(res.kobo_push_started, res.notes)
        self.assertEqual((len(pres.sent), len(self.cw.requests)), (1, 1))

    def test_S1_a_job_that_has_not_run_well_lately_leaves_the_kobo_to_calibre(self):
        # review S1: launchd could not start it, or it died: calibre's own automatic runs resume.
        self.set_kobo(push=True, push_since=self.SINCE, service=True)
        self.plist_present()
        self.heartbeat(seconds_ago=16 * 60)
        self.run_ui()
        self.assertEqual(len(self.wait_push().sent), 1)

    def test_without_the_plist_calibre_keeps_doing_it(self):
        self.set_kobo(push=True, push_since=self.SINCE, service=True)    # ticked, but not on disk
        self.run_ui()
        self.assertEqual(len(self.wait_push().sent), 1)

    def test_the_button_does_no_kobo_work_while_the_job_holds_the_lock(self):
        import kobo_service
        self.set_kobo(push=True, push_since=self.SINCE)
        lock = kobo_service.LibraryLock(self.lib.path, 'the background job')
        self.assertTrue(lock.acquire())
        try:
            res = self.run_ui(manual=True)
            self.wait_push()
        finally:
            lock.release()
        self.assertEqual(self.cw.requests, [])
        self.assertFalse(res.kobo_push_started)
        self.assertTrue(any('the background job' in n for n in res.notes), res.notes)
        res = self.run_ui(manual=True)                       # free again: it runs
        self.assertEqual(len(self.wait_push().sent), 1)

    def test_calibres_run_holds_the_lock_until_its_push_worker_is_done(self):
        import kobo_push
        import kobo_service
        self.set_kobo(push=True, push_since=self.SINCE)
        gate, entered = threading.Event(), threading.Event()
        real = kobo_push._send

        def held(*a, **kw):
            entered.set()
            gate.wait(10)
            return real(*a, **kw)
        job = kobo_service.LibraryLock(self.lib.path, 'the background job')
        try:
            with mock.patch('kobo_push._send', side_effect=held):
                self.run_ui()
                self.assertTrue(entered.wait(10))            # the PUT is in flight on the worker
                self.assertFalse(job.acquire())
                self.assertIn('calibre', job.holder())
                gate.set()
                self.wait_push()
        finally:
            gate.set()
        self.assertTrue(job.acquire())                       # released when the worker ended
        job.release()

    def test_a_push_by_the_job_is_not_a_kobo_move_for_calibre(self):
        import kobo_service
        self.plugin_installed()
        self.set_kobo(push=True, push_since=self.SINCE, service=True)    # ui.PREFS: on disk now
        out = kobo_service.run_once(self.lib.path, config_dir=config_dir)
        self.assertEqual(out.pres.sent, [self.uuid], out.notes)
        before = self.positions()
        res = self.run_ui(manual=True)                       # calibre opened afterwards
        pres = self.wait_push()
        self.assertEqual(res.kobo_imported, 0, res.notes)
        self.assertEqual(self.positions(), before)
        self.assertEqual((pres.sent, len(self.cw.requests)), ([], 1))

    def test_a_job_push_while_calibre_is_open_is_not_a_kobo_move_either(self):
        # R2-N2: calibre ran (and read the journal) BEFORE the job pushed; its next run must
        # see the job's push, not a session copy from before it.
        import kobo_service
        self.plugin_installed()
        self.set_kobo(service=True)                          # sending off: calibre's run sends nothing
        res = self.run_ui(manual=True)
        self.wait_push()
        self.assertEqual((res.kobo_imported, self.cw.requests), (0, []))
        self.set_kobo(push=True, push_since=self.SINCE, service=True)
        out = kobo_service.run_once(self.lib.path, config_dir=config_dir)
        self.assertEqual(out.pres.sent, [self.uuid], out.notes)
        before = self.positions()
        res = self.run_ui(manual=True)                       # the same calibre session
        pres = self.wait_push()
        self.assertEqual(res.kobo_imported, 0, res.notes)
        self.assertEqual(self.positions(), before)
        self.assertEqual(len(self.cw.requests), 1)

    def test_a_push_by_calibre_is_not_a_kobo_move_for_the_job(self):
        import kobo_service
        self.plugin_installed()
        self.set_kobo(push=True, push_since=self.SINCE, service=True)
        self.run_ui(manual=True)
        self.assertEqual(len(self.wait_push().sent), 1)
        before = self.positions()
        out = kobo_service.run_once(self.lib.path, config_dir=config_dir)    # calibre closed afterwards
        self.assertEqual(out.status, 'done', out.notes)
        self.assertEqual((out.kres.written, out.pres.sent, len(self.cw.requests)), ([], [], 1))
        self.assertEqual(self.positions(), before)

    def test_calibre_starting_refreshes_a_stale_copy(self):
        import kobo_service

        def stale(name):
            with open(os.path.join(REPO, name), 'rb') as f:
                return f.read() + b'\n# 1.5.9\n'
        kobo_service.install_files(stale)
        self.action()._refresh_service_copy()
        for name in kobo_service.MODULES:
            with open(os.path.join(kobo_service.service_dir(), name), 'rb') as f, \
                    open(os.path.join(REPO, name), 'rb') as g:
                self.assertEqual(f.read(), g.read(), name)


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

    def test_keep_syncing_when_calibre_is_closed_box(self):
        # 1.6.0: off by default; macOS only; OK with it ticked sets the job up (kobo_service.install
        # with this library, calibre's config folder, the app.db and the plugin's own files);
        # unticked, it is taken down. The job is never set up by anything else.
        import kobo_service
        from ui import PREFS
        app_db = os.path.join(self.lib.path, 'app.db')
        try:
            w = self.widget()
            self.assertFalse(w.kobo_service.isChecked())
            self.assertIn('Keep syncing when calibre is closed', w.kobo_service.text())
            self.assertEqual(w.kobo_service.isEnabled(), kobo_service.supported())
            with mock.patch.object(kobo_service, 'supported', return_value=True), \
                    mock.patch.object(kobo_service, 'install', return_value=(True, 'on')) as install, \
                    mock.patch.object(kobo_service, 'uninstall', return_value=(True, 'off')) as uninstall, \
                    mock.patch.object(kobo_service, 'status_text', return_value='Background job: off.'):
                w = self.widget()
                self.assertTrue(w.kobo_service.isEnabled())
                w.kobo_db.setText(app_db)
                w.save_kobo()                                   # OK without the box: nothing set up
                self.assertEqual(install.call_count, 0)
                self.assertIs(PREFS['kobo'][self.lib.path].get('service', False), False)
                w = self.widget()
                w.kobo_service.setChecked(True)
                w.save_kobo()
                self.assertIs(PREFS['kobo'][self.lib.path]['service'], True)
                (library, cfg, db, read), _ = install.call_args
                self.assertEqual((library, cfg, db), (self.lib.path, config_dir, app_db))
                self.assertTrue(read('kobo_service.py').startswith(b'# Last modified'))
                self.assertTrue(self.widget().kobo_service.isChecked())     # reopened: still on
                self.widget().save_kobo()                       # OK again: the copy is refreshed
                self.assertEqual(install.call_count, 2)
                w = self.widget()
                w.kobo_service.setChecked(False)
                w.save_kobo()                                   # unticked: taken down
                self.assertEqual(uninstall.call_args[0][0], self.lib.path)
                self.assertIs(PREFS['kobo'][self.lib.path]['service'], False)
                install.return_value = (False, 'launchctl could not load the background job (exit 5: x).')
                w = self.widget()
                w.kobo_service.setChecked(True)
                with mock.patch.object(type(w), '_show_error') as shown:
                    w.save_kobo()                               # ticked, but launchd refuses it
                self.assertIs(PREFS['kobo'][self.lib.path]['service'], False)
                self.assertIn('exit 5', shown.call_args[0][0])
                self.assertFalse(self.widget().kobo_service.isChecked())
                w = self.widget()
                w.kobo_service.setChecked(True)
                install.return_value = (True, 'on')
                w.save_kobo()
                calls = uninstall.call_count
                w = self.widget()
                w.kobo_db.setText('')                           # Kobo sync switched off: the job too
                w.save_kobo()
                self.assertEqual(uninstall.call_count, calls + 1)
                self.assertNotIn(self.lib.path, PREFS['kobo'])
        finally:
            PREFS['kobo'] = {}

    def test_S5_every_background_job_is_listed_with_a_remove_button(self):
        # review S5: jobs for renamed or removed libraries too, each with Remove (macOS).
        import kobo_service
        from qt.core import QLabel, QPushButton
        from ui import PREFS
        gone = os.path.join(os.path.dirname(self.lib.path), 'Renamed Library')
        jobs = [{'label': 'com.calibreader.sync.kobo.aaaaaaaaaaaa', 'library': self.lib.path, 'loaded': True,
                 'library_exists': True, 'plist': 'a.plist'},
                {'label': 'com.calibreader.sync.kobo.bbbbbbbbbbbb', 'library': gone, 'loaded': True,
                 'library_exists': False, 'plist': 'b.plist'}]
        try:
            PREFS['kobo'] = {gone: {'app_db': 'x', 'service': True}}
            with mock.patch.object(kobo_service, 'supported', return_value=True), \
                    mock.patch.object(kobo_service, 'installed_jobs', return_value=jobs), \
                    mock.patch.object(kobo_service, 'status_text', return_value='Background job: off.'), \
                    mock.patch.object(kobo_service, 'remove_job', return_value=(True, 'removed')) as remove:
                w = self.widget()
                text = ' '.join(label.text() for label in w.findChildren(QLabel))
                self.assertIn(gone, text)
                self.assertIn('folder not found', text)
                buttons = [b for b in w.findChildren(QPushButton) if b.text() == 'Remove']
                self.assertEqual(len(buttons), 2)
                buttons[1].click()
                remove.assert_called_once_with('com.calibreader.sync.kobo.bbbbbbbbbbbb')
                self.assertIs(PREFS['kobo'][gone]['service'], False)
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


# --------------------------------------------------------------------------- 1.7.0: Kobo Utilities

KU_CH = 'OEBPS/7446794606207039498_5200-h-{}.htm.xhtml'          # Metamorphosis: h-1 = chapter 2


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealKoboUtilitiesTests(RealCalibreBase):
    '''1.7.0 (step 4): Kobo Utilities' columns, read through calibre's db API, mapped on calibre's
    OWN kepubify of the EPUB (the KEPUB its Kobo driver sends), into CalibReader_positions.json
    by the real run (ui._run). metadata.db is never written by it.'''

    def setUp(self):
        RealCalibreBase.setUp(self)
        import ui
        for label, name, kind in (('kobo_reading_location', 'Kobo location', 'text'),
                                  ('kobo_percent_read', 'Kobo % read', 'int'),
                                  ('kobo_last_read', 'Kobo last read', 'datetime')):
            self.lib.legacy.create_custom_column(label, name, kind, False)
        self.lib.reopen()
        self.books = self.lib.books(3)
        fixtures = os.path.join(TESTS, 'fixtures')
        for book, _ in self.books[:2]:
            self.lib.api.add_format(book, 'EPUB', os.path.join(fixtures, 'metamorphosis.epub'))
        self.cache = tempfile.mkdtemp(prefix='cr-ku-cache-')
        self.cache_patch = mock.patch('kobo_ku.cache_folder', return_value=self.cache)
        self.cache_patch.start()
        if os.path.exists(ui.KU_MEMORY):
            os.remove(ui.KU_MEMORY)

    def tearDown(self):
        import ui
        self.cache_patch.stop()
        shutil.rmtree(self.cache, ignore_errors=True)
        ui.PREFS['kobo_ku'] = {}
        if os.path.exists(ui.KU_MEMORY):
            os.remove(ui.KU_MEMORY)
        RealCalibreBase.tearDown(self)

    def set_ku(self, location='#kobo_reading_location', percent='#kobo_percent_read', last_read='#kobo_last_read'):
        import kobo_ku
        import ui
        ui.PREFS['kobo_ku'] = {self.lib.path: kobo_ku.prefs_entry(location, percent, last_read)}

    def fill(self, book, location, percent=None, last_read=None):
        api = self.lib.api
        api.set_field('#kobo_reading_location', {book: location})
        api.set_field('#kobo_percent_read', {book: percent})
        api.set_field('#kobo_last_read', {book: last_read})

    def metadata_hash(self):
        import hashlib
        with open(os.path.join(self.lib.path, 'metadata.db'), 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()

    def positions_raw(self):
        path = os.path.join(self.lib.path, 'CalibReader_positions.json')
        if not os.path.exists(path):
            return None
        with open(path, 'rb') as f:
            return f.read()

    def entries(self):
        raw = self.positions_raw()
        return json.loads(raw.decode('utf-8'))['positions'] if raw else {}

    def epub_text(self, chapter):
        from kobo_position import Book
        b = Book(os.path.join(TESTS, 'fixtures', 'metamorphosis.epub'))
        try:
            return b.text(chapter).text
        finally:
            b.close()

    def test_off_until_a_location_column_is_picked(self):
        (b1, u1), = self.books[:1]
        self.fill(b1, KU_CH.format(1) + '#kobo.3.2', 4, datetime.datetime(2026, 11, 1, 10, 0, tzinfo=UTC))
        res = self.run_ui()
        self.assertEqual(getattr(res, 'kobo_ku_imported', 0), 0)
        self.assertIsNone(self.positions_raw())

    def test_columns_to_positions_with_calibres_own_kepub_and_metadata_untouched(self):
        (b1, u1), (b2, u2) = self.books[:2]
        self.fill(b1, KU_CH.format(1) + '#kobo.3.2', 4, datetime.datetime(2026, 11, 1, 10, 0, tzinfo=UTC))
        # a device-prefixed shape, and calibre's blank paragraph-start span (kobo.3.1, "\n")
        self.fill(b2, 'file:///mnt/onboard/Kafka/The Metamorphosis - Franz Kafka.kepub.epub!!' + KU_CH.format(1)
                  + '#kobo.3.1', 4, datetime.datetime(2026, 11, 1, 11, 0, tzinfo=UTC))
        self.set_ku()
        self.lib.reopen()
        before = self.metadata_hash()
        res = self.run_ui()
        self.assertEqual(res.kobo_ku_imported, 2, res.notes)
        e = self.entries()
        text = self.epub_text(2)
        for uuid in (u1, u2):
            entry = e[uuid]
            self.assertEqual((entry['deviceId'], entry['chapter'], entry['kobo']['precision']),
                             ('kobo-utilities', 2, 'span'), entry)
            self.assertTrue(text[entry['textAnchor']:].startswith('One morning, when Gregor Samsa'),
                            text[entry['textAnchor']:][:40])
            self.assertEqual(entry['kobo']['kepub'], 'calibre kepubify')
        self.assertEqual(e[u1]['updated_at'], '2026-11-01T10:00:00.000Z')
        self.assertTrue(any('Kobo Utilities' in line for line in res.kobo_ku_lines), res.kobo_ku_lines)
        raw = self.positions_raw()
        res = self.run_ui()                                  # a re-run: nothing new, nothing written
        self.assertEqual(res.kobo_ku_imported, 0)
        self.assertEqual(self.positions_raw(), raw)
        self.lib.reopen()
        self.assertEqual(self.metadata_hash(), before)       # never a write into metadata.db

    def test_a_stored_kepub_and_calibres_own_both_count_and_the_earlier_place_wins(self):
        # review step 4 S1 (supersedes "the stored KEPUB is the one the driver sends"): the KEPUB
        # format may have been added after the book was sent, so both numberings are tried and the
        # place is never after either landing
        from kobo_position import Book, resolve_span
        (b1, u1), = self.books[:1]
        stored = os.path.join(TESTS, 'fixtures', 'metamorphosis.kepub')
        self.lib.api.add_format(b1, 'KEPUB', stored)
        self.fill(b1, KU_CH.format(1) + '#kobo.10.2', 4, datetime.datetime(2026, 11, 1, 10, 0, tzinfo=UTC))
        self.set_ku()
        res = self.run_ui()
        self.assertEqual(res.kobo_ku_imported, 1, res.notes)
        entry = self.entries()[u1]
        self.assertEqual(entry['kobo']['kepub'], 'stored KEPUB + calibre kepubify')
        self.assertIn(entry['kobo']['precision'], ('span', 'span~'))
        epub, kepub = Book(os.path.join(TESTS, 'fixtures', 'metamorphosis.epub')), Book(stored)
        try:
            self.assertLessEqual(entry['textAnchor'], resolve_span(epub, kepub, 2, 'kobo.10.2')[0])
        finally:
            epub.close()
            kepub.close()
        self.assertEqual(len([n for n in os.listdir(self.cache) if n.endswith('.kepub')]), 1)   # calibre's own

    def test_the_lock_held_elsewhere_means_no_ku_work_this_run(self):
        import kobo_service
        (b1, u1), = self.books[:1]
        self.fill(b1, KU_CH.format(1) + '#kobo.3.2', 4, datetime.datetime(2026, 11, 1, 10, 0, tzinfo=UTC))
        self.set_ku()
        home = tempfile.mkdtemp(prefix='cr-ku-home-')
        try:
            with mock.patch.dict(os.environ, {'CALIBREADER_SYNC_HOME': home}):
                other = kobo_service.LibraryLock(self.lib.path, 'the background job')
                self.assertTrue(other.acquire())
                try:
                    res = self.run_ui()
                    self.assertEqual(res.kobo_ku_imported, 0)
                    self.assertTrue(any('running elsewhere' in n for n in res.notes), res.notes)
                    self.assertIsNone(self.positions_raw())
                finally:
                    other.release()
                res = self.run_ui()
                self.assertEqual(res.kobo_ku_imported, 1, res.notes)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_the_poll_sees_a_column_change(self):
        (b1, u1), = self.books[:1]
        self.set_ku()
        action = self.action()
        first = action._file_stamps()
        self.fill(b1, KU_CH.format(1) + '#kobo.3.2', 4, datetime.datetime(2026, 11, 1, 10, 0, tzinfo=UTC))
        self.assertNotEqual(action._file_stamps(), first)

    def test_a_kobo_connecting_is_remembered_and_runs_again_later(self):
        import ui
        action = self.action()

        class Timer(object):
            started = []

            def start(self, ms):
                Timer.started.append(ms)
        action._ku_device_timer = Timer()
        action._kobo_connected()
        self.assertTrue(ui.PREFS['kobo_ku_device_seen'])
        self.assertEqual(Timer.started, [ui.KU_DEVICE_DELAY_MS])

    def test_calibreaders_own_columns_never_suggest_kobo_utilities_columns(self):
        # e2e finding: on a library whose CalibReader settings were never saved, Settings pre-selected
        # #kobo_percent_read / #kobo_last_read as CalibReader's own % / last-read columns, so OK would
        # have had the plugin write the app's progress into Kobo Utilities' columns.
        qt_app()
        import config

        class Gui(object):
            current_db = self.lib.legacy
            library_path = self.lib.path
            iactions = {}
        w = config.ConfigWidget(Gui())
        self.assertNotEqual(w.combos['percent'].currentData(), '#kobo_percent_read')
        self.assertNotEqual(w.combos['last_read'].currentData(), '#kobo_last_read')
        # picked by hand they are still there to pick (only never suggested)
        self.assertGreaterEqual(w.combos['percent'].findData('#kobo_percent_read'), 0)

    def test_a_ku_column_that_calibreader_also_writes_stops_the_ku_route(self):
        # CalibReader writing the app's % into the column the KU route reads would come back as a
        # "Kobo" place: the route does not run, one note says why.
        (b1, u1), = self.books[:1]
        self.fill(b1, None, 40, datetime.datetime(2026, 11, 1, 10, 0, tzinfo=UTC))
        self.lib.set_settings({'version': 1, 'percent_column': '#kobo_percent_read', 'last_read_column': '',
                               'progress_threshold': 1, 'auto_sync': True})
        self.set_ku()
        res = self.run_ui()
        self.assertEqual(res.kobo_ku_imported, 0)
        self.assertTrue(any('#kobo_percent_read' in n and 'CalibReader' in n for n in res.notes), res.notes)
        entries = self.entries()
        self.assertFalse([e for e in entries.values() if e.get('deviceId') == 'kobo-utilities'])

    def test_the_summary_names_a_book_not_imported_and_why(self):
        (b1, u1), = self.books[:1]
        self.fill(b1, KU_CH.format(1) + '#kobo.999.1', 4, datetime.datetime(2026, 11, 1, 10, 0, tzinfo=UTC))
        self.set_ku()
        res = self.run_ui(manual=True)
        title = self.lib.api.field_for('title', b1)
        lines = [line for line in res.kobo_ku_lines if title in line]
        self.assertTrue(lines and 'kobo.999.1' in lines[0] and 'not in the KEPUB' in lines[0], res.kobo_ku_lines)

    def test_a_newer_calibre_web_place_wins(self):
        (b1, u1), = self.books[:1]
        with open(os.path.join(self.lib.path, 'CalibReader_positions.json'), 'w', encoding='utf-8') as f:
            json.dump({'schema': 1, 'positions': {u1: {
                'chapter': 3, 'page': 0, 'scrollY': 0, 'progress': 0.3, 'textAnchor': 100,
                'updated_at': '2026-11-02T10:00:00.000Z', 'deviceId': 'kobo-calibre-web',
                'kobo': {'precision': 'span'}}}}, f)
        self.fill(b1, KU_CH.format(1) + '#kobo.3.2', 4, datetime.datetime(2026, 11, 1, 10, 0, tzinfo=UTC))
        self.set_ku()
        res = self.run_ui()
        self.assertEqual(res.kobo_ku_imported, 0)
        self.assertEqual(self.entries()[u1]['deviceId'], 'kobo-calibre-web')


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealKoboUtilitiesSettingsTests(RealCalibreBase):
    '''1.7.0: Settings -> Kobo, "Kobo Utilities (USB)": the location column is a chooser, never
    pre-selected; a live preview; a column that does not read as KU locations is refused.'''

    def setUp(self):
        RealCalibreBase.setUp(self)
        for label, name, kind in (('kobo_reading_location', 'Kobo location', 'text'),
                                  ('kobo_percent_read', 'Kobo % read', 'int'),
                                  ('kobo_last_read', 'Kobo last read', 'datetime'),
                                  ('genre_note', 'Genre note', 'text'),
                                  ('read_percent', 'Read %', 'int'),
                                  ('kobo_loc_notes', 'Kobo loc notes', 'comments')):
            self.lib.legacy.create_custom_column(label, name, kind, False)
        self.lib.legacy.create_custom_column('shelves', 'Shelves', 'text', True)
        self.lib.reopen()
        books = self.lib.books(3)
        api = self.lib.api
        for (book, _), value in zip(books, ('text/titlepage.xhtml#kobo.1.1', 'OEBPS/ch08.xhtml', None)):
            api.set_field('#kobo_reading_location', {book: value})
        for (book, _), value in zip(books, ('Fantasy', 'Horror', 'text/a.xhtml')):
            api.set_field('#genre_note', {book: value})
        self.lib.set_settings({'version': 1, 'percent_column': '#read_percent', 'last_read_column': '',
                               'progress_threshold': 1, 'auto_sync': True})

    def tearDown(self):
        import ui
        ui.PREFS['kobo_ku'] = {}
        RealCalibreBase.tearDown(self)

    def widget(self):
        qt_app()
        import config

        class Gui(object):
            current_db = self.lib.legacy
            library_path = self.lib.path
            iactions = {}
        return config.ConfigWidget(Gui())

    def items(self, combo):
        return [combo.itemData(i) for i in range(combo.count())]

    def test_never_pre_selected_and_only_plausible_columns(self):
        import ui
        w = self.widget()
        self.assertEqual(w.ku_location.currentData(), '')                # "Don't use": off
        locs = self.items(w.ku_location)
        self.assertIn('#kobo_reading_location', locs)
        self.assertIn('#kobo_loc_notes', locs)                           # comments columns too
        self.assertNotIn('#shelves', locs)                               # not a multiple-value column
        self.assertNotIn('#kobo_percent_read', locs)
        self.assertEqual(w.ku_percent.currentData(), '')
        self.assertIn('#kobo_percent_read', self.items(w.ku_percent))
        # review step 4 S3: CalibReader's own % column is offered too, so a clash shows and is refused
        self.assertIn('#read_percent', self.items(w.ku_percent))
        self.assertIn('#kobo_last_read', self.items(w.ku_last_read))
        from qt.core import QLabel
        text = ' '.join(label.text() for label in w.findChildren(QLabel))
        self.assertIn('Kobo Utilities (USB)', text)
        self.assertIn('tester preview', text.lower())
        w.save_ku()
        self.assertNotIn(self.lib.path, ui.PREFS.get('kobo_ku', {}) or {})  # nothing picked: off

    def test_the_preview_and_saving(self):
        import kobo_ku
        import ui
        w = self.widget()
        w.ku_location.setCurrentIndex(w.ku_location.findData('#kobo_reading_location'))
        self.assertIn('text/titlepage.xhtml#kobo.1.1 -> span', w.ku_preview.text())
        self.assertIn('OEBPS/ch08.xhtml -> chapter', w.ku_preview.text())
        w.ku_percent.setCurrentIndex(w.ku_percent.findData('#kobo_percent_read'))
        w.ku_last_read.setCurrentIndex(w.ku_last_read.findData('#kobo_last_read'))
        w.save_ku()
        self.assertEqual(ui.PREFS['kobo_ku'][self.lib.path],
                         kobo_ku.prefs_entry('#kobo_reading_location', '#kobo_percent_read', '#kobo_last_read'))
        w = self.widget()                                                # reopened: the choice is shown
        self.assertEqual(w.ku_location.currentData(), '#kobo_reading_location')
        self.assertIn('Last Kobo Utilities store seen', w.ku_status.text())

    def test_a_column_that_is_not_ku_locations_is_refused(self):
        import ui
        w = self.widget()
        w.ku_location.setCurrentIndex(w.ku_location.findData('#genre_note'))
        self.assertIn('does not look like a Kobo Utilities location column', w.ku_preview.text())
        shown = []
        w._show_ku_error = shown.append
        w.save_ku()
        self.assertTrue(shown and '#genre_note' in shown[0], shown)       # NIT-R1-4: which column to change
        self.assertNotIn(self.lib.path, ui.PREFS.get('kobo_ku', {}) or {})


@unittest.skipUnless(HAVE_CALIBRE, 'needs calibre (run tests/run_calibre.sh)')
class RealKuReviewRound1Tests(RealCalibreBase):
    '''Review step 4 (REVIEW-STEP4.md), round 1: B1 (Settings never pre-selects CalibReader's own
    columns; a clash blocks the whole save), S3 (a saved CalibReader column that is a Kobo
    Utilities column is flagged and not written), S1 (Kobo Touch Extended: approximate), NITs 1,
    2, 10.'''

    def setUp(self):
        RealCalibreBase.setUp(self)
        import ui
        self.cache = tempfile.mkdtemp(prefix='cr-ku-cache-')
        self.cache_patch = mock.patch('kobo_ku.cache_folder', return_value=self.cache)
        self.cache_patch.start()
        if os.path.exists(ui.KU_MEMORY):
            os.remove(ui.KU_MEMORY)

    def tearDown(self):
        import ui
        self.cache_patch.stop()
        shutil.rmtree(self.cache, ignore_errors=True)
        ui.PREFS['kobo_ku'] = {}
        if os.path.exists(ui.KU_MEMORY):
            os.remove(ui.KU_MEMORY)
        RealCalibreBase.tearDown(self)

    def columns(self, *specs):
        for label, name, kind in specs:
            self.lib.legacy.create_custom_column(label, name, kind, False)
        self.lib.reopen()

    def widget(self):
        qt_app()
        import config

        class Gui(object):
            current_db = self.lib.legacy
            library_path = self.lib.path
            iactions = {}
        return config.ConfigWidget(Gui())

    def pick(self, combo, lookup):
        index = combo.findData(lookup)
        self.assertGreaterEqual(index, 0, lookup)
        combo.setCurrentIndex(index)

    def app_places(self, *uuids):
        with open(os.path.join(self.lib.path, 'CalibReader_positions.json'), 'w', encoding='utf-8') as f:
            json.dump({'schema': 1, 'updated_at': '2026-11-04T20:00:00.000Z', 'positions': {
                u: {'chapter': 3, 'page': 0, 'scrollY': 0, 'progress': 0.63, 'textAnchor': 100,
                    'updated_at': '2026-11-04T20:00:00.000Z', 'deviceId': 'iphone-x'} for u in uuids}}, f)

    def stored(self):
        from sync_core import SETTINGS_KEY
        return self.lib.api.pref(SETTINGS_KEY, None)

    # -- B1 ---------------------------------------------------------------------------------

    def test_B1_two_readers_nothing_preselected_nothing_written_into_either_readers_columns(self):
        # the reviewer's DAL/CES setup (KOBO-UTILITIES.md section 7: one KU column set per person)
        import kobo_ku
        import ui
        self.columns(('dal_location', 'DAL Reading Location', 'comments'), ('dal_percent', 'DAL % Read', 'int'),
                     ('dal_last_read', 'DAL Last Read', 'datetime'),
                     ('ces_location', 'CES Reading Location', 'comments'), ('ces_percent', 'CES % Read', 'int'),
                     ('ces_last_read', 'CES Last Read', 'datetime'))
        (b1, u1), (b2, u2) = self.lib.books(2)
        api = self.lib.api
        api.set_field('#dal_location', {b1: 'OEBPS/ch08.xhtml'})
        api.set_field('#ces_location', {b1: 'OEBPS/ch02.xhtml'})
        api.set_field('#ces_percent', {b1: 12})
        api.set_field('#ces_last_read', {b1: datetime.datetime(2026, 10, 1, 9, 0, tzinfo=UTC)})
        self.app_places(u1, u2)
        cols = ('#dal_location', '#dal_percent', '#dal_last_read', '#ces_location', '#ces_percent', '#ces_last_read')
        before = [[self.v(c, b) for c in cols] for b in (b1, b2)]
        w = self.widget()
        self.assertEqual((w.combos['percent'].currentData(), w.combos['last_read'].currentData()), ('', ''))
        self.pick(w.ku_location, '#dal_location')
        self.pick(w.ku_percent, '#dal_percent')
        self.pick(w.ku_last_read, '#dal_last_read')
        errors = []
        w._show_ku_error = errors.append
        self.assertTrue(w.validate())
        w.save_settings()
        self.assertEqual(errors, [])
        stored = self.stored()
        self.assertEqual((stored['percent_column'], stored['last_read_column']), ('', ''))
        self.assertEqual(ui.PREFS['kobo_ku'][self.lib.path],
                         kobo_ku.prefs_entry('#dal_location', '#dal_percent', '#dal_last_read'))
        self.run_ui()
        self.assertEqual([[self.v(c, b) for c in cols] for b in (b1, b2)], before)

    def test_B1_a_clash_blocks_the_whole_save_and_nothing_is_written(self):
        import ui
        self.columns(('reading_location', 'Reading location', 'text'), ('percent_read', 'Percent read', 'int'),
                     ('last_read', 'Last read', 'datetime'))
        (b1, u1), = self.lib.books(1)
        self.app_places(u1)
        w = self.widget()
        self.assertEqual((w.combos['percent'].currentData(), w.combos['last_read'].currentData()), ('', ''))
        self.pick(w.combos['percent'], '#percent_read')            # picked by hand for CalibReader...
        self.pick(w.combos['last_read'], '#last_read')
        self.pick(w.ku_location, '#reading_location')
        self.pick(w.ku_percent, '#percent_read')                   # ...and the same for Kobo Utilities
        self.pick(w.ku_last_read, '#last_read')
        errors = []
        w._show_ku_error = errors.append
        self.assertIs(getattr(w, 'validate_before_accept', False), True)
        self.assertFalse(w.validate())                              # the dialog stays open
        self.assertTrue(errors and '#percent_read' in errors[0], errors)
        w.save_settings()                                           # even if called: nothing saved
        self.assertIsNone(self.stored())
        self.assertNotIn(self.lib.path, ui.PREFS.get('kobo_ku', {}) or {})
        self.run_ui()
        self.assertEqual((self.v('#percent_read', b1), self.v('#last_read', b1)), (None, None))

    # -- S3 ---------------------------------------------------------------------------------

    def test_S3_a_saved_calibreader_column_that_ku_uses_is_flagged_and_not_written(self):
        import kobo_ku
        import ui
        self.columns(('kobo_reading_location', 'Kobo location', 'text'), ('kobo_percent_read', 'Kobo % read', 'int'),
                     ('kobo_last_read', 'Kobo last read', 'datetime'))
        (b1, u1), = self.lib.books(1)
        # saved by 1.2.0-1.6.0's suggestion, and Kobo Utilities' route uses the same % column
        self.lib.set_settings({'version': 1, 'percent_column': '#kobo_percent_read', 'last_read_column': '',
                               'progress_threshold': 1, 'auto_sync': True})
        ui.PREFS['kobo_ku'] = {self.lib.path: kobo_ku.prefs_entry('#kobo_reading_location', '#kobo_percent_read', '')}
        self.app_places(u1)
        w = self.widget()
        warning = w.ku_clash_note.text()
        self.assertIn('#kobo_percent_read', warning)
        self.assertIn('pick another', warning)
        res = self.run_ui()
        self.assertIsNone(self.v('#kobo_percent_read', b1))        # CalibReader stopped writing it
        self.assertTrue(any('#kobo_percent_read' in n and 'not written' in n for n in res.notes), res.notes)

    def test_S3_without_saved_ku_settings_the_ku_selection_is_checked(self):
        self.columns(('reading_location', 'Reading location', 'text'), ('percent_read', 'Percent read', 'int'))
        self.lib.set_settings({'version': 1, 'percent_column': '#percent_read', 'last_read_column': '',
                               'progress_threshold': 1, 'auto_sync': True})
        w = self.widget()
        self.assertNotIn('#percent_read', w.ku_clash_note.text())
        self.pick(w.ku_percent, '#percent_read')                   # offered, so the clash is visible
        self.assertIn('#percent_read', w.ku_clash_note.text())

    # -- S1 (Kobo Touch Extended) -----------------------------------------------------------

    def test_S1_with_kobo_touch_extended_installed_spans_are_approximate(self):
        import kobo_ku
        import ui
        self.columns(('kobo_reading_location', 'Kobo location', 'text'), ('kobo_last_read', 'Kobo last read', 'datetime'))
        (b1, u1), = self.lib.books(1)
        self.lib.api.add_format(b1, 'EPUB', os.path.join(TESTS, 'fixtures', 'metamorphosis.epub'))
        self.lib.api.set_field('#kobo_reading_location', {b1: KU_CH.format(1) + '#kobo.3.2'})
        self.lib.api.set_field('#kobo_last_read', {b1: datetime.datetime(2026, 11, 1, 10, 0, tzinfo=UTC)})
        ui.PREFS['kobo_ku'] = {self.lib.path: kobo_ku.prefs_entry('#kobo_reading_location', '', '#kobo_last_read')}
        with mock.patch('kobo_ku.kte_installed', return_value=True):
            res = self.run_ui()
        with open(os.path.join(self.lib.path, 'CalibReader_positions.json'), encoding='utf-8') as f:
            entry = json.load(f)['positions'][u1]
        self.assertEqual(entry['kobo']['precision'], 'span~')
        self.assertTrue(any('Kobo Touch Extended' in line for line in res.kobo_ku_lines), res.kobo_ku_lines)

    # -- NITs 1, 2, 10 ----------------------------------------------------------------------

    def test_NIT1_a_ku_catch_up_follows_after_a_longer_pause(self):
        import kobo_ku
        import ui
        self.columns(('kobo_reading_location', 'Kobo location', 'text'))
        ui.PREFS['kobo_ku'] = {self.lib.path: kobo_ku.prefs_entry('#kobo_reading_location', '', '')}
        action = self.action()
        asked = []

        class Kick(object):
            def start(self, ms):
                asked.append(ms)
        action._kick = Kick()
        more = kobo_ku.KuImportResult()
        more.more = True
        with mock.patch('kobo_ku.import_ku', return_value=more):
            self.run_ui()
        self.assertIn(ui.KU_MORE_DELAY_MS, asked)
        self.assertGreaterEqual(ui.KU_MORE_DELAY_MS, 20000)

    def test_NIT2_our_own_send_holding_the_lock_is_said_plainly(self):
        import kobo_ku
        import kobo_service
        import ui
        self.columns(('kobo_reading_location', 'Kobo location', 'text'))
        ui.PREFS['kobo_ku'] = {self.lib.path: kobo_ku.prefs_entry('#kobo_reading_location', '', '')}
        home = tempfile.mkdtemp(prefix='cr-ku-home-')
        try:
            with mock.patch.dict(os.environ, {'CALIBREADER_SYNC_HOME': home}):
                held = kobo_service.LibraryLock(self.lib.path, 'calibre')
                self.assertTrue(held.acquire())
                try:
                    class Alive(object):
                        def is_alive(self):
                            return True
                    self.action()._kobo_worker = Alive()
                    res = self.run_ui()
                finally:
                    held.release()
        finally:
            shutil.rmtree(home, ignore_errors=True)
        notes = ' '.join(res.notes)
        self.assertIn('calibre\'s own send', notes)
        self.assertNotIn('running elsewhere', notes)

    def test_NIT10_the_button_shows_the_ku_lines_in_a_library_without_calibreader_files(self):
        import kobo_ku
        import ui
        self.columns(('kobo_reading_location', 'Kobo location', 'text'))
        (b1, u1), = self.lib.books(1)
        self.lib.api.set_field('#kobo_reading_location', {b1: 'text/nowhere.xhtml#kobo.1.1'})
        ui.PREFS['kobo_ku'] = {self.lib.path: kobo_ku.prefs_entry('#kobo_reading_location', '', '')}
        action = self.action()
        action.sync_read_status = ui.CalibReaderSyncAction.sync_read_status.__get__(action)
        shown = []
        saved = ui.info_dialog, ui.error_dialog
        ui.info_dialog = ui.error_dialog = lambda parent, title, msg, **kw: shown.append(msg)
        try:
            action.sync_read_status()
        finally:
            ui.info_dialog, ui.error_dialog = saved
        self.assertTrue(shown and 'Kobo Utilities' in shown[0] and 'No CalibReader files' not in shown[0], shown)

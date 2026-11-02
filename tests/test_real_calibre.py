# Last modified: 2026-11-02--1416
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
import unittest

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

    def run_ui(self):
        '''The main sync path: ui.CalibReaderSyncAction._run, as the button and the
        automatic sync call it, with the plugin's real JSONConfig.'''
        import ui

        class Model(object):
            refreshed = 0

            def refresh(self):
                Model.refreshed += 1

        class View(object):
            def model(self):
                return Model()

        class Gui(object):
            library_path = self.lib.path
            library_view = View()
            current_db = self.lib.legacy

        class Action(object):
            gui = Gui()
        from sync_core import SETTINGS_KEY, normalise_settings
        settings = normalise_settings(self.lib.api.pref(SETTINGS_KEY, None))
        return ui.CalibReaderSyncAction._run(Action(), self.lib.api, settings)


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
        self.run_ui()
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
        self.assertIn(stored.get('read_column', '#read_status'), ('#read_status',))

    def test_an_explicit_read_column_is_not_preselected_either(self):
        self.lib.delete_column('read_status')
        self.lib.add_column('read', 'Read')
        w = self.widget()
        self.assertEqual(w.combos['read'].currentData(), '#read_status')
        self.assertIn('Read (#read)', w.read_note.text())         # named as a hint
        w.save_settings()
        self.assertNotIn('read_column', self.stored())

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

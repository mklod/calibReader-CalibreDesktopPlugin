# Last modified: 2026-11-05--0723
#
# Step 3 (plugin 1.6.0, tester preview): the Kobo sync without calibre running.
# kobo_service.run_once is what the launchd job runs (calibre-debug -e <copy>/kobo_service.py
# -- run ...): the plugin's own Kobo run, with calibre's Settings read from calibre's prefs
# file, one lock per library shared with calibre and the CLI, calibre's push journal as the
# one echo memory, N1 kept across runs, and a size-capped log without secrets. The installer
# (plist + launchctl) is tested with a stand-in launchctl; the real one runs in the Mac e2e.
#
#   python3 tests/run_tests.py          (from the plugin folder)

import contextlib
import datetime
import hashlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import kobo_import  # noqa: E402
import kobo_push  # noqa: E402
import kobo_service  # noqa: E402
import test_kobo_push as tkp  # noqa: E402

UNDER_CALIBRE = 'calibre-debug' in os.path.basename(sys.executable or '').lower()


def py(script, *args):
    '''A command running `script` with this interpreter (calibre-debug needs -e … --).'''
    if UNDER_CALIBRE:
        return [sys.executable, '-e', script, '--'] + list(args)
    return [sys.executable, script] + list(args)


def repo_reader(name):
    with open(os.path.join(REPO, name), 'rb') as f:
        return f.read()


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


class EnvMixin(object):
    '''Every path of the job in a temp folder (never ~/Library).'''

    def start_env(self, root):
        self.env = mock.patch.dict(os.environ, {
            'CALIBREADER_SYNC_HOME': os.path.join(root, 'home'),
            'CALIBREADER_SYNC_LOGS': os.path.join(root, 'logs'),
            'CALIBREADER_SYNC_AGENTS': os.path.join(root, 'LaunchAgents'),
        })
        self.env.start()
        os.environ.pop('CALIBREADER_SYNC_LABEL', None)
        self.addCleanup(self.env.stop)


class ServiceBase(EnvMixin, tkp.PushBase):
    '''The step 2 stand-ins (app.db, a stand-in Calibre-Web, the sidecar) + calibre's config
    folder as the plugin leaves it: plugins/calibreader_sync.json (PREFS) and the plugin zip.'''

    def setUp(self):
        tkp.PushBase.setUp(self)
        self.start_env(self.dir)
        self.config = os.path.join(self.dir, 'calibre-config')
        os.makedirs(os.path.join(self.config, 'plugins'))
        open(os.path.join(self.config, 'plugins', kobo_service.PLUGIN_ZIP), 'wb').close()
        self.metadata_db()                      # the job looks books up in metadata.db (read-only)
        self.set_prefs(service=True)

    def set_prefs(self, **extra):
        entry = {'app_db': self.app_db, 'user': 'admin', 'cw_url': self.cw.url}
        entry.update(extra)
        prefs = {'kobo': {self.lib: entry}, 'kobo_pushed': {}, 'libraries': {}}
        with open(kobo_service.prefs_path(self.config), 'w', encoding='utf-8', newline='\n') as f:
            json.dump(prefs, f, indent=2)        # calibre's JSONConfig writes plain JSON like this

    def run_job(self, **kw):
        return kobo_service.run_once(self.lib, config_dir=self.config, **kw)

    def journal(self):
        return kobo_push.load_state(kobo_service.journal_path(self.config), self.lib).memory

    def log_text(self):
        try:
            with open(kobo_service.log_path(), encoding='utf-8') as f:
                return f.read()
        except OSError:
            return ''

    def ahead_span(self):
        '''The Kobo at kobo.3.5 (Part I), the app mid-sentence in Part II: a span push.'''
        self.kobo(60, '2026-10-30 21:03:48', tkp.CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.app(tkp.UUID, 3, self.anchor_of(3, 'kobo.5.1', plus=7))

    def touch_app(self, uuid=tkp.THIRD, anchor=100):
        '''Another app place lands (the inputs changed: no "no change" shortcut).'''
        time.sleep(0.02)
        self.app(uuid, 2, anchor, stamp=tkp.iso_in(0))


# -- one writer per library -------------------------------------------------------------

class LockTests(EnvMixin, unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kobo-lock-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.start_env(self.dir)
        self.lib = os.path.join(self.dir, 'library')
        os.mkdir(self.lib)

    def test_one_holder_at_a_time(self):
        a = kobo_service.LibraryLock(self.lib, 'the background job')
        b = kobo_service.LibraryLock(self.lib, 'calibre')
        self.assertTrue(a.acquire())
        try:
            self.assertFalse(b.acquire())
            self.assertIn('the background job', b.holder())
        finally:
            a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_libraries_do_not_share_a_lock(self):
        other = os.path.join(self.dir, 'other')
        os.mkdir(other)
        a = kobo_service.LibraryLock(self.lib, 'x')
        b = kobo_service.LibraryLock(other, 'y')
        self.assertTrue(a.acquire())
        self.assertTrue(b.acquire())
        a.release()
        b.release()

    def test_the_lock_lives_on_this_computer_not_in_the_library(self):
        path = kobo_service.LibraryLock(self.lib, 'x').path
        self.assertTrue(path.startswith(os.environ['CALIBREADER_SYNC_HOME']), path)
        self.assertFalse(path.startswith(self.lib))

    def test_another_process_holding_it_and_dying_frees_it(self):
        script = os.path.join(self.dir, 'hold.py')
        with open(script, 'w', encoding='utf-8') as f:
            f.write('import sys, time\nsys.path.insert(0, {!r})\nimport kobo_service\n'
                    'lock = kobo_service.LibraryLock(sys.argv[1], "holder process")\n'
                    'print("held" if lock.acquire() else "busy", flush=True)\n'
                    'time.sleep(120)\n'.format(REPO))
        p = subprocess.Popen(py(script, self.lib), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, env=dict(os.environ))
        try:
            line = ''
            deadline = time.monotonic() + 60
            while line not in ('held', 'busy') and time.monotonic() < deadline:
                line = p.stdout.readline().strip()
            self.assertEqual(line, 'held')
            mine = kobo_service.LibraryLock(self.lib, 'calibre')
            self.assertFalse(mine.acquire())
            self.assertIn('holder process', mine.holder())
        finally:
            p.kill()                            # our own child, by its PID
            p.wait(30)
            p.stdout.close()
        self.assertTrue(mine.acquire())         # a dead holder never keeps it
        mine.release()

    def test_the_push_worker_can_release_what_the_gui_thread_took(self):
        lock = kobo_service.LibraryLock(self.lib, 'calibre')
        self.assertTrue(lock.acquire())
        t = threading.Thread(target=lock.release)
        t.start()
        t.join(10)
        again = kobo_service.LibraryLock(self.lib, 'the background job')
        self.assertTrue(again.acquire())
        again.release()


# -- one run of the job --------------------------------------------------------------------

class RunTests(ServiceBase):

    def test_nothing_happens_unless_the_box_is_ticked(self):
        self.set_prefs()                                         # Kobo set up, the job box never ticked
        self.kobo(60, '2026-10-30 21:03:48', tkp.CH.format(2), 'KoboSpan', 'kobo.5.1', 40.0, 3.0)
        out = self.run_job()
        self.assertEqual(out.status, 'off')
        self.assertFalse(os.path.exists(self.positions_path))
        self.assertEqual(self.puts(), [])
        self.assertIn('off in calibre', self.log_text())

    def test_nothing_happens_when_the_plugin_is_gone(self):
        os.remove(os.path.join(self.config, 'plugins', kobo_service.PLUGIN_ZIP))
        self.kobo(60, '2026-10-30 21:03:48', tkp.CH.format(2), 'KoboSpan', 'kobo.5.1', 40.0, 3.0)
        out = self.run_job()
        self.assertEqual(out.status, 'off')
        self.assertFalse(os.path.exists(self.positions_path))
        self.assertIn('no longer installed', self.log_text())

    def test_settings_caught_half_written_do_nothing(self):
        with open(kobo_service.prefs_path(self.config), 'w', encoding='utf-8') as f:
            f.write('{\n  "kobo": {\n    "')                   # calibre writing it at this instant
        self.kobo(60, '2026-10-30 21:03:48', tkp.CH.format(2), 'KoboSpan', 'kobo.5.1', 40.0, 3.0)
        out = self.run_job()
        self.assertEqual(out.status, 'error')
        self.assertFalse(os.path.exists(self.positions_path))

    def test_the_kobo_place_is_imported_with_calibre_closed(self):
        self.kobo(60, '2026-10-30 21:03:48', tkp.CH.format(2), 'KoboSpan', 'kobo.5.1', 40.0, 3.0)
        out = self.run_job()
        self.assertEqual(out.status, 'done', out.notes)
        entry = self.read_positions()['positions'][tkp.UUID]
        self.assertEqual(entry['deviceId'], kobo_import.KOBO_DEVICE_ID)
        self.assertEqual((entry['chapter'], entry['textAnchor']), (3, self.anchor_of(3, 'kobo.5.1')))
        log = self.log_text().splitlines()
        self.assertTrue(log[0].endswith('s)'), log)
        self.assertIn('[{}] 1 place imported, 0 sent'.format(kobo_service.library_key(self.lib)), log[0])
        state = kobo_service.load_state(self.lib)
        self.assertEqual(state['status'], 'done')
        self.assertIn('1 place imported', state['summary'])

    def test_push_off_in_calibre_means_no_request(self):
        self.set_prefs(service=True, push=False)
        self.ahead_span()
        with mock.patch('kobo_push._send', side_effect=AssertionError('no request with the push off')):
            out = self.run_job()
        self.assertEqual(out.status, 'done', out.notes)
        self.assertIsNone(out.pres)
        self.assertEqual(self.puts(), [])

    def test_push_ticked_without_a_start_time_sends_nothing(self):
        self.set_prefs(service=True, push=True)                  # calibre's OK sets push_since; not yet
        self.ahead_span()
        out = self.run_job()
        self.assertEqual(self.puts(), [])
        self.assertTrue(any('start time' in n for n in out.notes), out.notes)

    def test_push_on_sends_once_and_remembers_it_in_calibres_journal(self):
        self.set_prefs(service=True, push=True, push_since=tkp.SINCE)
        self.ahead_span()
        out = self.run_job()
        self.assertEqual(out.pres.sent, [tkp.UUID], out.notes)
        self.assertEqual(len(self.puts()), 1)
        record = self.journal()[tkp.UUID]
        self.assertIs(record['confirmed'], True)
        self.assertEqual(record['value'], 'kobo.5.1')
        before = open(self.positions_path, 'rb').read()
        out = self.run_job()                                     # Calibre-Web's write changed app.db
        self.assertEqual(out.status, 'done')
        self.assertEqual((out.kres.written, out.pres.sent, len(self.puts())), ([], [], 1))
        self.assertEqual(open(self.positions_path, 'rb').read(), before)

    def test_unticking_in_calibre_stops_the_jobs_next_put(self):
        self.set_prefs(service=True, push=True, push_since=tkp.SINCE)
        self.ahead_span()
        self.kobo(62, '2026-10-30 21:03:48', tkp.CH.format(1), 'KoboSpan', 'kobo.3.5', 12.0, 3.0)
        self.app(tkp.THIRD, 3, self.anchor_of(3, 'kobo.5.1', plus=7))
        real = kobo_push._send

        def first_then_untick(*a, **kw):
            self.set_prefs(service=True, push=False)             # the tester unticks in calibre
            return real(*a, **kw)
        with mock.patch('kobo_push._send', side_effect=first_then_untick):
            out = self.run_job()
        self.assertEqual(len(self.puts()), 1)
        self.assertTrue(any('turned off' in n for n in out.notes), out.notes)

    def test_a_held_lock_means_this_run_does_nothing(self):
        lock = kobo_service.LibraryLock(self.lib, 'calibre')
        self.assertTrue(lock.acquire())
        try:
            self.kobo(60, '2026-10-30 21:03:48', tkp.CH.format(2), 'KoboSpan', 'kobo.5.1', 40.0, 3.0)
            out = self.run_job()
        finally:
            lock.release()
        self.assertEqual(out.status, 'busy')
        self.assertFalse(os.path.exists(self.positions_path))
        self.assertIn('calibre (pid', self.log_text())
        out = self.run_job()                                     # the next run does the work
        self.assertEqual((out.status, out.kres.written), ('done', [tkp.UUID]))

    def test_nothing_new_is_cut_short(self):
        self.kobo(60, '2026-10-30 21:03:48', tkp.CH.format(2), 'KoboSpan', 'kobo.5.1', 40.0, 3.0)
        self.assertEqual(self.run_job().status, 'done')
        # Its own write changed the positions file after the stamps were taken (before the
        # run, so a change landing DURING a run is never missed): one more full run, which
        # finds nothing new.
        out = self.run_job()
        self.assertEqual((out.status, out.kres.written), ('done', []))
        with mock.patch('kobo_import.Book', side_effect=AssertionError('no book opened')):
            out = self.run_job()
        self.assertEqual(out.status, 'no-change')
        runs = [line for line in self.log_text().splitlines() if not line.startswith(' ')]
        self.assertEqual(len(runs), 3)                              # one line per run (+ indented details)
        self.assertTrue(runs[-1].split('] ', 1)[1].startswith('no change'), runs)
        self.touch_app()
        self.assertEqual(self.run_job().status, 'done')
        # and at least once an hour anyway
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=3700)
        self.assertEqual(self.run_job(now=later).status, 'done')

    def test_N1_a_failing_book_is_not_parsed_again_by_the_next_run(self):
        # the Kobo's Source is not in this book: M4, remembered as failed until the Kobo moves
        self.kobo(60, '2026-10-30 21:03:48', 'OEBPS/not-in-this-book.xhtml', 'KoboSpan', 'kobo.1.1', 10.0, 3.0)
        out = self.run_job()
        self.assertIn(tkp.UUID, out.kres.skipped)
        opened = []
        real = kobo_import.Book

        def counting(path):
            opened.append(path)
            return real(path)
        for state_kept in (True, False):
            kobo_import.forget_failures()                        # a new process: nothing in memory
            kobo_push.forget_failures()
            if not state_kept:
                state = kobo_service.load_state(self.lib)
                state.pop('n1', None)
                kobo_service.save_state(self.lib, state)
            self.touch_app(anchor=100 + len(opened))
            with mock.patch('kobo_import.Book', side_effect=counting):
                out = self.run_job()
            self.assertEqual(out.status, 'done')
            if state_kept:
                self.assertEqual(opened, [], 'parsed again although its Kobo stamp did not change')
        self.assertTrue(opened)                                  # without the kept memory it would be

    def test_after_calibre_web_failed_the_job_waits_before_sending_again(self):
        self.set_prefs(service=True, push=True, push_since=tkp.SINCE, cw_url=tkp.closed_port())
        self.ahead_span()
        t0 = datetime.datetime.now(datetime.timezone.utc)
        out = self.run_job(now=t0)
        self.assertEqual(out.pres.failure, 'network')
        self.assertIn('backoff_until', kobo_service.load_state(self.lib))
        self.touch_app()
        with mock.patch('kobo_push._send', side_effect=AssertionError('no request during the backoff')):
            out = self.run_job(now=t0 + datetime.timedelta(seconds=60))
        self.assertEqual(out.status, 'done')
        self.assertTrue(any('waits until' in n for n in out.notes), out.notes)
        self.touch_app(anchor=200)
        with mock.patch('kobo_push._send', wraps=kobo_push._send) as send:
            self.run_job(now=t0 + datetime.timedelta(seconds=kobo_push.BACKOFF + 5))
        self.assertEqual(send.call_count, 1)

    def test_the_token_is_never_in_the_log_state_or_journal(self):
        self.set_prefs(service=True, push=True, push_since=tkp.SINCE)
        self.ahead_span()
        self.run_job()
        self.set_prefs(service=True, push=True, push_since=tkp.SINCE, cw_url=tkp.closed_port())
        self.app(tkp.UUID, 4, 100, stamp=tkp.iso_in(0))
        self.run_job()
        texts = [self.log_text()]
        for path in (kobo_service.state_path(self.lib), kobo_service.journal_path(self.config)):
            with open(path, encoding='utf-8') as f:
                texts.append(f.read())
        for secret in (tkp.TOKEN, tkp.TOKEN2):
            self.assertNotIn(secret, ' '.join(texts))

    def test_per_book_reasons_are_logged_only_when_they_change(self):
        self.set_prefs(service=True, push=True, push_since=tkp.SINCE)
        self.kobo(60, '2026-10-30 21:03:48', tkp.CH.format(2), 'KoboSpan', 'kobo.5.1', 40.0, 3.0)
        self.app(tkp.UUID, 2, 641, stamp=tkp.iso_in(0))              # behind the Kobo: not sent
        self.run_job()
        self.assertEqual(self.log_text().count('behind the Kobo'), 1)
        self.touch_app()
        self.run_job()
        self.assertEqual(self.log_text().count('behind the Kobo'), 1)


class LogTests(EnvMixin, unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kobo-log-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.start_env(self.dir)

    def test_the_log_is_rotated_and_capped(self):
        log = kobo_service.ServiceLog()
        line = 'x' * 1000
        for _ in range(kobo_service.LOG_MAX * (kobo_service.LOG_KEEP + 3) // 1000):
            log.write([line])
        self.assertLessEqual(os.path.getsize(log.path), kobo_service.LOG_MAX)
        for i in range(1, kobo_service.LOG_KEEP + 1):
            self.assertTrue(os.path.exists('{}.{}'.format(log.path, i)))
        self.assertFalse(os.path.exists('{}.{}'.format(log.path, kobo_service.LOG_KEEP + 1)))

    def test_a_kobo_sync_link_never_reaches_the_log(self):
        log = kobo_service.ServiceLog()
        log.write(['note: http://127.0.0.1:8083/kobo/{}/v1/library/x/state failed'.format(tkp.TOKEN)])
        with open(log.path, encoding='utf-8') as f:
            text = f.read()
        self.assertNotIn(tkp.TOKEN, text)
        self.assertIn('/kobo/<token>/v1', text)


# -- set up from calibre: the copy, the plist, launchctl --------------------------------------

class FakeLaunchctl(object):
    '''launchctl as the installer calls it: bootstrap loads, bootout unloads, print reports.'''

    def __init__(self, bootstrap_code=0):
        self.calls = []
        self.loaded = set()
        self.bootstrap_code = bootstrap_code

    def __call__(self, args, timeout=15):
        self.calls.append(list(args))
        verb = args[1]
        if verb == 'bootstrap':
            if self.bootstrap_code:
                return self.bootstrap_code, 'Bootstrap failed: 5: Input/output error'
            with open(args[3], 'rb') as f:
                self.loaded.add(plistlib.load(f)['Label'])
            return 0, ''
        if verb == 'bootout':
            label = args[2].split('/')[-1]
            if label in self.loaded:
                self.loaded.discard(label)
                return 0, ''
            return 3, 'Boot-out failed: 3: No such process'
        if verb == 'print':
            return (0, 'state = not running') if args[2].split('/')[-1] in self.loaded else (113, 'Could not find')
        return 64, 'unknown'

    def verbs(self):
        return [c[1] for c in self.calls]


class InstallerTests(EnvMixin, unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kobo-install-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.start_env(self.dir)
        self.lib = os.path.join(self.dir, 'My Library')
        os.mkdir(self.lib)
        self.config = os.path.join(self.dir, 'calibre-config')
        self.app_db = os.path.join(self.dir, 'cw', 'app.db')
        self.launchctl = FakeLaunchctl()

    def install(self, **kw):
        return kobo_service.install(self.lib, self.config, self.app_db, repo_reader, run=self.launchctl,
                                    sleep=lambda s: None, **kw)

    def test_the_label_is_per_library(self):
        label = kobo_service.label_for(self.lib)
        self.assertRegex(label, r'^com\.calibreader\.sync\.kobo\.[0-9a-f]{12}$')
        other = os.path.join(self.dir, 'Other')
        os.mkdir(other)
        self.assertNotEqual(kobo_service.label_for(other), label)

    def test_the_plist(self):
        args = ['/Applications/calibre.app/Contents/MacOS/calibre-debug', '-e', 'x.py', '--', 'run']
        p = kobo_service.plist_for(self.lib, self.config, self.app_db, args=args)
        self.assertEqual(p['Label'], kobo_service.label_for(self.lib))
        self.assertEqual(p['ProgramArguments'], args)
        self.assertEqual(p['StartInterval'], 300)
        self.assertIs(p['RunAtLoad'], True)
        self.assertEqual(p['WatchPaths'], [self.app_db, self.app_db + '-wal'])
        self.assertGreaterEqual(p['ThrottleInterval'], 30)
        self.assertNotIn('KeepAlive', p)
        self.assertEqual(p['EnvironmentVariables']['CALIBRE_CONFIG_DIRECTORY'], self.config)
        self.assertEqual(p['ProcessType'], 'Background')
        plistlib.loads(plistlib.dumps(p))                        # a valid plist

    def test_program_args_use_calibre_debug_then_python3(self):
        script = os.path.join(kobo_service.service_dir(), 'kobo_service.py')
        args = kobo_service.program_args(self.lib, self.config, debug='/Apps/calibre-debug')
        self.assertEqual(args, ['/Apps/calibre-debug', '-e', script, '--', 'run', '--library', self.lib,
                                '--config-dir', self.config])
        args = kobo_service.program_args(self.lib, self.config, debug='', python='/usr/bin/python3')
        self.assertEqual(args, ['/usr/bin/python3', script, 'run', '--library', self.lib, '--config-dir', self.config])

    def test_install_copies_the_modules_writes_the_plist_and_loads_it(self):
        ok, message = self.install()
        self.assertTrue(ok, message)
        for name in kobo_service.MODULES:
            self.assertEqual(sha(os.path.join(kobo_service.service_dir(), name)), sha(os.path.join(REPO, name)))
        with open(os.path.join(kobo_service.service_dir(), 'MANIFEST.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['version'], kobo_service.VERSION)
        label = kobo_service.label_for(self.lib)
        with open(kobo_service.plist_path(label), 'rb') as f:
            plist = plistlib.load(f)
        self.assertEqual(plist['Label'], label)
        self.assertEqual(plist['ProgramArguments'][-4:], ['--library', self.lib, '--config-dir', self.config])
        bootstrap = [c for c in self.launchctl.calls if c[1] == 'bootstrap']
        self.assertEqual(bootstrap, [['launchctl', 'bootstrap', 'gui/{}'.format(kobo_service._uid()),
                                      kobo_service.plist_path(label)]])
        self.assertLess(self.launchctl.verbs().index('bootout'), self.launchctl.verbs().index('bootstrap'))
        self.assertIn(label, self.launchctl.loaded)
        self.assertTrue(kobo_service.installed_here(self.lib, {'service': True}))
        self.assertFalse(kobo_service.installed_here(self.lib, {'service': False}))

    def test_ok_again_with_nothing_changed_does_not_reload_the_job(self):
        self.install()
        self.launchctl.calls = []
        ok, _ = self.install()
        self.assertTrue(ok)
        self.assertNotIn('bootstrap', self.launchctl.verbs())

    def test_a_new_app_db_path_reloads_the_job(self):
        self.install()
        self.app_db = os.path.join(self.dir, 'cw2', 'app.db')
        self.launchctl.calls = []
        self.install()
        self.assertEqual([v for v in self.launchctl.verbs() if v != 'print'], ['bootout', 'bootstrap'])

    def test_a_failed_bootstrap_leaves_nothing_behind(self):
        self.launchctl.bootstrap_code = 5
        ok, message = self.install()
        self.assertFalse(ok)
        self.assertIn('exit 5', message)
        self.assertFalse(os.path.exists(kobo_service.plist_path(kobo_service.label_for(self.lib))))

    def test_untick_unloads_and_removes_it(self):
        self.install()
        label = kobo_service.label_for(self.lib)
        ok, message = kobo_service.uninstall(self.lib, run=self.launchctl)
        self.assertTrue(ok, message)
        self.assertNotIn(label, self.launchctl.loaded)
        self.assertFalse(os.path.exists(kobo_service.plist_path(label)))
        self.assertIn(['launchctl', 'bootout', 'gui/{}/{}'.format(kobo_service._uid(), label)],
                      self.launchctl.calls)
        ok, _ = kobo_service.uninstall(self.lib, run=self.launchctl)       # already off: still fine
        self.assertTrue(ok)

    def test_a_plugin_update_refreshes_only_what_changed(self):
        self.assertEqual(sorted(kobo_service.install_files(repo_reader)), sorted(kobo_service.MODULES + ('MANIFEST.json',)))
        self.assertEqual(kobo_service.install_files(repo_reader), [])

        def newer(name):
            data = repo_reader(name)
            return data + b'\n# 1.6.1\n' if name == 'kobo_push.py' else data
        self.assertEqual(sorted(kobo_service.install_files(newer)), ['MANIFEST.json', 'kobo_push.py'])
        with open(os.path.join(kobo_service.service_dir(), 'kobo_push.py'), 'rb') as f:
            self.assertTrue(f.read().endswith(b'# 1.6.1\n'))

    def test_the_status_line(self):
        self.assertIn('off', kobo_service.status_text(self.lib, run=self.launchctl))
        self.install()
        self.assertIn('Not run yet', kobo_service.status_text(self.lib, run=self.launchctl))
        kobo_service.save_state(self.lib, {'last_run_at': kobo_service.now_iso(), 'status': 'done',
                                           'summary': '1 place imported, 0 sent', 'note': ''})
        text = kobo_service.status_text(self.lib, run=self.launchctl)
        self.assertIn('on (launchd', text)
        self.assertIn('Last run', text)
        self.assertIn('1 place imported, 0 sent', text)
        self.launchctl.loaded.clear()                            # launchd lost it
        self.assertIn('not loaded', kobo_service.status_text(self.lib, run=self.launchctl))


# -- the job's own entry point, from the copy ----------------------------------------------------

class EntryPointTests(ServiceBase):

    def setUp(self):
        ServiceBase.setUp(self)
        kobo_service.install_files(repo_reader)
        self.script = os.path.join(kobo_service.service_dir(), 'kobo_service.py')

    def test_the_copy_loads_its_own_modules_never_the_plugin_package(self):
        env = dict(os.environ)
        marker = os.path.join(self.dir, 'plugin-package-imported')
        if not UNDER_CALIBRE:
            # a stand-in for the installed plugin zip that calibre-debug would offer
            pkg = os.path.join(self.dir, 'decoy', 'calibre_plugins', 'calibreader_sync')
            os.makedirs(pkg)
            open(os.path.join(self.dir, 'decoy', 'calibre_plugins', '__init__.py'), 'w').close()
            with open(os.path.join(pkg, '__init__.py'), 'w', encoding='utf-8') as f:
                f.write('open({!r}, "w").close()\n'.format(marker))
            for name in ('kobo_import.py', 'kobo_push.py', 'kobo_position.py'):
                with open(os.path.join(pkg, name), 'w', encoding='utf-8') as f:
                    f.write('open({!r}, "w").close()\n'.format(marker))
            env['PYTHONPATH'] = os.path.join(self.dir, 'decoy')
        p = subprocess.run(py(self.script, 'version'), capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(kobo_service.VERSION, p.stdout)
        loaded = [line.split(' ', 1)[1] for line in p.stdout.splitlines() if line.startswith('kobo_')]
        self.assertEqual(len(loaded), 3, p.stdout)
        for path in loaded:
            self.assertEqual(os.path.normcase(os.path.dirname(path)), os.path.normcase(kobo_service.service_dir()))
        self.assertFalse(os.path.exists(marker))

    def test_the_copy_runs_the_job(self):
        self.kobo(60, '2026-10-30 21:03:48', tkp.CH.format(2), 'KoboSpan', 'kobo.5.1', 40.0, 3.0)
        p = subprocess.run(py(self.script, 'run', '--library', self.lib, '--config-dir', self.config),
                           capture_output=True, text=True, env=dict(os.environ), timeout=180)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('done: 1 place imported', p.stdout)
        self.assertEqual(self.read_positions()['positions'][tkp.UUID]['deviceId'], kobo_import.KOBO_DEVICE_ID)

    def test_a_busy_run_exits_75(self):
        lock = kobo_service.LibraryLock(self.lib, 'calibre')
        self.assertTrue(lock.acquire())
        try:
            p = subprocess.run(py(self.script, 'run', '--library', self.lib, '--config-dir', self.config),
                               capture_output=True, text=True, env=dict(os.environ), timeout=180)
        finally:
            lock.release()
        self.assertEqual(p.returncode, kobo_service.BUSY, p.stdout + p.stderr)


# -- R2-N2: one echo memory, and the CLI takes the lock too ---------------------------------------

class SharedMemoryTests(ServiceBase):

    def plugin_lookup(self, book_id):
        return kobo_import.metadata_lookup(self.lib)(book_id)

    def test_a_push_by_the_job_is_not_a_kobo_move_for_calibre(self):
        self.set_prefs(service=True, push=True, push_since=tkp.SINCE)
        self.ahead_span()
        self.assertEqual(self.run_job().pres.sent, [tkp.UUID])
        before = open(self.positions_path, 'rb').read()
        # calibre's next Kobo run: the journal re-read from disk (ui._kobo_journal)
        memory = kobo_push.load_state(kobo_service.journal_path(self.config), self.lib).memory
        kres = kobo_import.import_positions(self.lib, self.app_db, self.plugin_lookup, user='admin', pushed=memory)
        self.assertEqual(kres.written, [])
        self.assertEqual(open(self.positions_path, 'rb').read(), before)

    def test_a_push_by_calibre_is_not_a_kobo_move_for_the_job(self):
        self.ahead_span()
        journal = kobo_push.load_state(kobo_service.journal_path(self.config), self.lib)
        pres = kobo_push.push_positions(self.lib, self.app_db, self.plugin_lookup, base_url=self.cw.url,
                                        memory=journal.memory, user='admin', since=tkp.SINCE,
                                        persist=lambda m: journal.save())
        self.assertEqual(pres.sent, [tkp.UUID])
        self.set_prefs(service=True, push=True, push_since=tkp.SINCE)
        before = open(self.positions_path, 'rb').read()
        out = self.run_job()
        self.assertEqual((out.kres.written, out.pres.sent, len(self.puts())), ([], [], 1))
        self.assertEqual(open(self.positions_path, 'rb').read(), before)

    def test_a_save_keeps_another_writers_start_time(self):
        path = kobo_service.journal_path(self.config)
        cli = kobo_push.load_state(path, self.lib)
        cli.since = '2026-11-01T00:00:00.000Z'
        cli.save()
        plugin = kobo_push.load_state(path, self.lib)            # calibre's journal object: no own start time
        plugin.since = None
        plugin.memory['x'] = {'value': 'kobo.1.1'}
        plugin.save()
        with open(path, encoding='utf-8') as f:
            part = json.load(f)[os.path.realpath(self.lib)]
        self.assertEqual(part['push_since'], '2026-11-01T00:00:00.000Z')
        self.assertIn('x', part)
        self.assertTrue(kobo_push.load_state(path, self.lib).had_part)
        self.assertFalse(kobo_push.load_state(path, os.path.join(self.dir, 'elsewhere')).had_part)

    def test_the_cli_shares_calibres_journal_by_default(self):
        with mock.patch.dict(os.environ, {'CALIBRE_CONFIG_DIRECTORY': self.config}):
            self.assertEqual(kobo_service.default_state_file(), kobo_service.journal_path(self.config))
            self.ahead_span()
            with contextlib.redirect_stdout(io.StringIO()):
                kobo_import.main(['--library', self.lib, '--app-db', self.app_db, '--user', 'admin', '--push',
                                  '--calibre-web', self.cw.url, '--since', tkp.SINCE])
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual(self.journal()[tkp.UUID]['value'], 'kobo.5.1')

    def test_the_cli_takes_the_lock(self):
        self.ahead_span()
        lock = kobo_service.LibraryLock(self.lib, 'the background job')
        self.assertTrue(lock.acquire())
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = kobo_import.main(['--library', self.lib, '--app-db', self.app_db, '--user', 'admin',
                                         '--push', '--calibre-web', self.cw.url, '--since', tkp.SINCE,
                                         '--state', os.path.join(self.dir, 'state.json')])
        finally:
            lock.release()
        self.assertEqual(code, kobo_service.BUSY)
        self.assertIn('the background job', out.getvalue())
        self.assertEqual(self.puts(), [])
        self.assertFalse(os.path.exists(self.positions_path) and
                         kobo_import.KOBO_DEVICE_ID in open(self.positions_path, encoding='utf-8').read())


if __name__ == '__main__':
    unittest.main()

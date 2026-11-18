# Last modified: 2026-11-05--0854
#
# kobo_service.py — the Kobo sync without calibre running (plugin 1.6.0, tester preview).
#
# A background job (macOS: a launchd user LaunchAgent, one per library) runs exactly
# the plugin's Kobo work: step 1's import (Kobo -> CalibReader_positions.json) and,
# only when the user ticked it in calibre, step 2's push (CalibReader -> Kobo through
# Calibre-Web). It runs with calibre's own Python:
#     calibre-debug -e <support>/service/kobo_service.py -- run --library <folder> --config-dir <calibre config>
# from a COPY of the Kobo modules (the plugin zip may move or change), and it never
# needs the calibre GUI. This module is also the plugin's side of it:
#   * LibraryLock: ONE writer per library. The job, every Kobo run of the plugin and
#     the CLI take the same flock'd lock file, never waiting (busy = skip, say so).
#     FileLock (waits, briefly): the push journal and the job's state file, so two
#     libraries' writers never lose each other's records (review S4).
#   * install / uninstall: Settings -> Kobo "Keep syncing when calibre is closed"
#     copies the modules, creates the log and support folders (launchd does not: B1),
#     writes ~/Library/LaunchAgents/<label>.plist and loads it with `launchctl
#     bootstrap gui/<uid>` (untick: bootout + remove). Refused for a calibre that runs
#     from a disk image or an App Translocation copy (S1), or without calibre-debug (N1).
#   * installed_here: calibre stands down only for a HEALTHY job: a good run in the
#     last 15 minutes (S1). status_text: launchd's view, the last run, the error kept
#     until a run succeeds (S2). installed_jobs / remove_job: every job on this Mac (S5).
#
# The job reads calibre's own plugin prefs file (the same per-library Settings: app.db,
# Calibre-Web user and address, push on/off and since when, `service`), before the run
# and again before every PUT; it never writes it. It runs only for calibre's installed
# CalibReader Sync >= 1.6.0 (S5). It reads and writes calibre's push journal
# (plugins/calibreader_sync_kobo_pushed.json), the one echo memory the plugin and the CLI
# share (R2-N2). A failed import or push is a failed run (S2); macOS privacy blocks get
# one message naming the folder (S3). The Kobo token stays where it was: read from app.db
# read-only by kobo_push, never logged (every log line is scrubbed of /kobo/<...> paths).
#
# Standard library only; see docs/handoffs/104-kobo-sync/DESIGN.md, "Step 3".
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

from __future__ import annotations

__license__   = 'GPL v3'
__copyright__ = '2026, CalibReader'

import datetime as _dt
import errno
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import traceback
import unicodedata
import zipfile

VERSION = '1.6.0'
MIN_PLUGIN = (1, 6, 0)         # S5: the job runs only for calibre's CalibReader Sync at least this new
LABEL_PREFIX = 'com.calibreader.sync.kobo'
INTERVAL = 300                 # seconds between runs (launchd StartInterval)
THROTTLE = 60                  # seconds: launchd starts the job at most this often (WatchPaths included)
HEALTHY = 15 * 60              # S1: calibre stands down only after a good job run this recent
STARTING = 120                 # S1: a plist this young with no run yet is "starting"
SERVICE_MAX_BOOKS = 100        # books mapped per run (no GUI to keep responsive)
FULL_RUN_AGE = 3600            # seconds: an unchanged library still gets a full run this often
LOG_MAX = 256 * 1024           # bytes before kobo.log is rotated
LOG_KEEP = 3                   # rotated logs kept (kobo.log.1 .. .3)
MODULES = ('kobo_service.py', 'kobo_import.py', 'kobo_push.py', 'kobo_position.py')
PLUGIN_ZIP = 'CalibReader Sync.zip'
PREFS_FILE = 'calibreader_sync.json'                   # calibre's JSONConfig('plugins/calibreader_sync')
JOURNAL_FILE = 'calibreader_sync_kobo_pushed.json'     # ui.KOBO_JOURNAL
BUSY = 75                      # exit code of a run that found the lock taken (EX_TEMPFAIL)
_WIN_LOCK_AT = 1 << 30         # Windows: the locked byte, far past the holder text
_open = open                   # S3 tests stand in a privacy block here


# -- where things live ------------------------------------------------------------

def supported():
    '''The background job can be set up here (macOS only for now).'''
    return sys.platform == 'darwin'


def support_dir():
    '''This computer's CalibReaderSync folder (the copy, locks, the job's state).'''
    env = os.environ.get('CALIBREADER_SYNC_HOME')        # tests and the e2e only
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/CalibReaderSync')
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
        return os.path.join(base, 'CalibReaderSync')
    base = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
    return os.path.join(base, 'CalibReaderSync')


def log_dir():
    env = os.environ.get('CALIBREADER_SYNC_LOGS')        # tests and the e2e only
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Logs/CalibReaderSync')
    return os.path.join(support_dir(), 'logs')


def log_path():
    return os.path.join(log_dir(), 'kobo.log')


def service_dir():
    return os.path.join(support_dir(), 'service')


def agents_dir():
    env = os.environ.get('CALIBREADER_SYNC_AGENTS')      # tests only (never a real LaunchAgents folder)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.expanduser('~/Library/LaunchAgents')


def ensure_folders():
    '''B1: launchd opens the plist's StandardOutPath as the user when it starts the job and
    does not create a missing folder; install() and every run create both folders.'''
    for folder in (log_dir(), support_dir()):
        os.makedirs(folder, exist_ok=True)


def _nfc(path):
    return unicodedata.normalize('NFC', path) if isinstance(path, str) else path


def library_key(library):
    # N8: the same library whether a path comes composed (calibre) or decomposed (a file API).
    real = os.path.normcase(_nfc(os.path.realpath(library)))
    return hashlib.sha1(real.encode('utf-8')).hexdigest()[:12]


def label_for(library):
    return os.environ.get('CALIBREADER_SYNC_LABEL') or '{}.{}'.format(LABEL_PREFIX, library_key(library))


def plist_path(label):
    return os.path.join(agents_dir(), label + '.plist')


def lock_path(library):
    return os.path.join(support_dir(), 'locks', 'kobo-{}.lock'.format(library_key(library)))


def state_path(library):
    return os.path.join(support_dir(), 'state', 'kobo-{}.json'.format(library_key(library)))


def calibre_config_dir():
    '''calibre's config folder: CALIBRE_CONFIG_DIRECTORY, calibre's own answer when this
    runs inside calibre, else the platform default.'''
    env = os.environ.get('CALIBRE_CONFIG_DIRECTORY')
    if env:
        return os.path.abspath(os.path.expanduser(env))
    try:
        from calibre.constants import config_dir
        return config_dir
    except Exception:
        pass
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Preferences/calibre')
    if os.name == 'nt':
        return os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~\\AppData\\Roaming'), 'calibre')
    return os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'calibre')


def prefs_path(config_dir):
    return os.path.join(config_dir, 'plugins', PREFS_FILE)


def journal_path(config_dir):
    return os.path.join(config_dir, 'plugins', JOURNAL_FILE)


def default_state_file():
    '''The CLI's push memory (R2-N2): calibre's journal when calibre's config folder is
    there, so the CLI, the job and calibre share one echo memory; else the old file.'''
    config = calibre_config_dir()
    if os.path.isdir(config):
        return journal_path(config)
    return os.path.join('~', '.calibreader_sync', 'kobo_push_state.json')


def now_iso():
    t = _dt.datetime.now(_dt.timezone.utc)
    return t.strftime('%Y-%m-%dT%H:%M:%S.') + '{:03d}Z'.format(t.microsecond // 1000)


def _parse_iso(s):
    try:
        return _dt.datetime.strptime(s, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(t):
    return t.strftime('%Y-%m-%dT%H:%M:%S.') + '{:03d}Z'.format(t.microsecond // 1000)


def _write_atomic(path, data):
    '''bytes -> path, through a temp file + os.replace (never half-written).'''
    import threading
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = '{}.{}.{}.tmp'.format(path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# -- locks ---------------------------------------------------------------------------

def _try_lock(f):
    '''One non-blocking attempt at an exclusive lock on open file `f` -> bool.'''
    try:
        if os.name == 'nt':
            import msvcrt
            f.seek(_WIN_LOCK_AT)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f):
    try:
        if os.name == 'nt':
            import msvcrt
            f.seek(_WIN_LOCK_AT)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        f.close()


def _open_lock_file(path):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    if not os.path.exists(path):
        open(path, 'ab').close()
    return open(path, 'r+b')


class FileLock(object):
    '''An exclusive lock on `path` (a .lock file next to what it guards) that WAITS, up to
    `timeout` seconds (then OSError): for short read-modify-writes of a shared file (the
    push journal, the job's state). Same primitive as LibraryLock (flock / msvcrt), so it
    holds across processes and across threads of one process.'''

    def __init__(self, path, timeout=30.0):
        self.path = path
        self.timeout = timeout
        self._f = None

    def __enter__(self):
        f = _open_lock_file(self.path)
        end = time.monotonic() + self.timeout
        while not _try_lock(f):
            if time.monotonic() > end:
                f.close()
                raise OSError(errno.ETIMEDOUT, 'timed out waiting for the lock', self.path)
            time.sleep(0.005)
        self._f = f
        return self

    def __exit__(self, *exc):
        f, self._f = self._f, None
        if f is not None:
            _unlock(f)
        return False


class LibraryLock(object):
    '''The library's Kobo lock: an flock'd file (Windows: msvcrt.locking on a byte far
    past the holder text), taken without waiting. Released by release(), or when the
    holder exits (a crash included). Another thread may release it: the plugin's push
    worker ends the run its GUI thread started.'''

    def __init__(self, library, who):
        self.path = lock_path(library)
        self.who = who
        self._f = None

    def acquire(self):
        if self._f is not None:
            return True
        f = _open_lock_file(self.path)
        if not _try_lock(f):
            f.close()
            return False
        try:
            text = '{} (pid {}) since {}\n'.format(self.who, os.getpid(), now_iso()).encode('utf-8')
            f.seek(0)
            f.write(text.ljust(200)[:200])
            f.flush()
        except OSError:
            pass
        self._f = f
        return True

    def release(self):
        f, self._f = self._f, None
        if f is not None:
            _unlock(f)

    @property
    def held(self):
        return self._f is not None

    def holder(self):
        '''Who holds it, as its holder wrote it ("calibre (pid 123) since …"), or None.'''
        try:
            with open(self.path, 'rb') as f:
                text = f.read(200).decode('utf-8', 'replace').strip()
        except OSError:
            return None
        return text or None


def busy_note(holder):
    who = holder or 'another CalibReader Sync run'
    return ('The Kobo sync of this library is running elsewhere right now ({}); this run did no Kobo work. '
            'It is picked up by the next run.'.format(who))


# -- calibre's plugin settings and plugin, read-only -------------------------------------

def read_prefs(config_dir):
    '''-> (calibre's prefs for this plugin, None) or (None, why). A file that does not
    parse: (None, why), and the run does nothing.'''
    path = prefs_path(config_dir)
    try:
        with open(path, 'rb') as f:
            data = json.loads(f.read().decode('utf-8'))
    except FileNotFoundError:
        return None, 'calibre has no CalibReader Sync settings at {}'.format(path)
    except (OSError, ValueError) as exc:
        return None, 'calibre\'s CalibReader Sync settings could not be read ({})'.format(exc)
    if not isinstance(data, dict):
        return None, 'calibre\'s CalibReader Sync settings have an unexpected layout'
    return data, None


def library_entry(prefs, library):
    '''The library's Kobo settings (PREFS['kobo'][library]), or None when Kobo sync is
    not set up for it (no app.db path). N8: a decomposed spelling of the path finds the
    composed key calibre stored.'''
    table = (prefs or {}).get('kobo') or {}
    if not isinstance(table, dict):
        return None
    entry = table.get(library)
    if entry is None:
        want = _nfc(library)
        entry = next((v for k, v in table.items() if _nfc(k) == want), None)
    return entry if isinstance(entry, dict) and entry.get('app_db') else None


def plugin_installed(config_dir):
    return os.path.isfile(os.path.join(config_dir, 'plugins', PLUGIN_ZIP))


_VERSION_RE = re.compile(rb'version\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)')


def plugin_version(config_dir):
    '''S5: the version of calibre's installed CalibReader Sync (its zip's __init__.py),
    or None when the zip cannot be read (calibre may be writing it).'''
    try:
        with zipfile.ZipFile(os.path.join(config_dir, 'plugins', PLUGIN_ZIP)) as z:
            m = _VERSION_RE.search(z.read('__init__.py'))
    except (OSError, KeyError, zipfile.BadZipFile, RuntimeError):
        return None
    return tuple(int(x) for x in m.groups()) if m else None


# -- the job's own state and log ----------------------------------------------------------

def load_state(library):
    try:
        with open(state_path(library), 'rb') as f:
            data = json.loads(f.read().decode('utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _state_lock(library):
    return FileLock(state_path(library) + '.lock', timeout=10.0)


def _write_state(library, state):
    _write_atomic(state_path(library), (json.dumps(state, indent=1, ensure_ascii=False) + '\n').encode('utf-8'))


def save_state(library, state):
    with _state_lock(library):
        _write_state(library, state)


def merge_state(library, fields):
    '''N2: a run that did not hold the library's lock updates only its own fields, re-read
    and written under the state file's lock, so a lock holder's save is never lost.'''
    with _state_lock(library):
        state = load_state(library)
        state.update(fields)
        _write_state(library, state)
        return state


_TOKENISH = (re.compile(r'(/kobo/)[0-9A-Fa-f]{16,}'), re.compile(r'(/kobo/)[^/\s\'"]{20,}'))


def scrub_line(text):
    '''No Kobo token in a log line or a status, whatever produced it (belt and braces:
    kobo_push already scrubs every note), and the home folder as ~ (N4: the tester sends
    log lines; they need not carry the user name).'''
    text = str(text)
    for pattern in _TOKENISH:
        text = pattern.sub(r'\1<token>', text)
    home = os.path.expanduser('~')
    if home and home not in ('~', os.sep):
        text = text.replace(home, '~')
    return text


class ServiceLog(object):
    '''kobo.log: appended once per run, rotated past LOG_MAX (LOG_KEEP old files).'''

    def __init__(self, path=None):
        self.path = path or log_path()

    def _rotate(self):
        for i in range(LOG_KEEP, 0, -1):
            src = self.path if i == 1 else '{}.{}'.format(self.path, i - 1)
            dst = '{}.{}'.format(self.path, i)
            if os.path.exists(src):
                if os.path.exists(dst):
                    os.remove(dst)
                os.rename(src, dst)

    def write(self, lines):
        text = ''.join(scrub_line(line).rstrip('\n') + '\n' for line in lines if line is not None)
        if not text:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            if os.path.getsize(self.path) + len(text) > LOG_MAX:
                self._rotate()
        except OSError:
            pass
        with open(self.path, 'a', encoding='utf-8', newline='\n') as f:
            f.write(text)


def _cap_launchd_log():
    '''launchd's stdout/stderr file for the job (a crash's traceback): kept small.'''
    path = os.path.join(log_dir(), 'kobo-launchd.log')
    try:
        if os.path.getsize(path) > LOG_MAX:
            os.truncate(path, 0)
    except OSError:
        pass


# -- health (S1) -------------------------------------------------------------------

def job_health(state, now=None):
    '''-> (healthy, time of the last good run or None). Healthy = a good run (done, no
    change, or found calibre at work) within HEALTHY seconds.'''
    now = now or _dt.datetime.now(_dt.timezone.utc)
    ok_at = _parse_iso((state or {}).get('last_ok_at'))
    return (ok_at is not None and 0 <= (now - ok_at).total_seconds() <= HEALTHY), ok_at


def installed_here(library, entry, now=None):
    '''The plugin's test for standing down: the job is set up for this library (Settings
    ticked, the plist there) AND healthy (S1). A job launchd cannot start, or one that
    keeps failing, gives the Kobo work back to calibre within 15 minutes (same lock). No
    launchctl call: this runs on every automatic sync.'''
    if not (isinstance(entry, dict) and entry.get('service') is True and os.path.exists(plist_path(label_for(library)))):
        return False
    return job_health(load_state(library), now)[0]


# -- macOS privacy (S3) ----------------------------------------------------------------

def _job_program():
    exe = sys.executable or ''
    return exe if 'calibre-debug' in exe else '/Applications/calibre.app/Contents/MacOS/calibre-debug'


def privacy_note(folder):
    return ('macOS did not let the background job read {}. In System Settings > Privacy & Security > '
            'Full Disk Access, turn on calibre (if it is not listed, add calibre with +; if that is not enough, '
            'also add {}). The next run picks it up; until then calibre does the Kobo work itself while it is '
            'open.'.format(scrub_line(folder), scrub_line(_job_program())))


def _probe(path):
    '''-> 'ok' | 'missing' | 'blocked': can this process read `path`? macOS privacy gives
    EPERM on open (os.path.isfile would just say False).'''
    try:
        with _open(path, 'rb') as f:
            f.read(1)
        return 'ok'
    except FileNotFoundError:
        return 'missing'
    except IsADirectoryError:
        return 'ok'
    except PermissionError:
        return 'blocked'
    except OSError as exc:
        return 'blocked' if exc.errno in (errno.EPERM, errno.EACCES) else 'missing'


def _privacy_from(text, library, app_db):
    '''sqlite's "unable to open database file" (or EPERM/EACCES text) for a file that is
    there -> the privacy note for its folder, else None.'''
    t = str(text)
    if not ('unable to open database file' in t or 'Operation not permitted' in t or 'Permission denied' in t):
        return None
    for path in (app_db, os.path.join(library, 'metadata.db')):
        if path and path in t:
            return privacy_note(os.path.dirname(path))
    return privacy_note(library)


# -- one run -----------------------------------------------------------------------

def _core():
    '''The Kobo modules: the plugin's when this runs inside calibre's plugin, else the
    ones next to this file (the job's copy; main() blocks the plugin package).'''
    try:
        from calibre_plugins.calibreader_sync import kobo_import, kobo_push
    except ImportError:
        import kobo_import
        import kobo_push
    return kobo_import, kobo_push


def _stamp(path):
    try:
        st = os.stat(path)
        return [os.path.basename(path), st.st_mtime_ns, st.st_size]
    except OSError:
        return [os.path.basename(path), None, None]


def input_stamps(library, entry, config_dir):
    app_db = entry['app_db']
    return [_stamp(p) for p in (prefs_path(config_dir), app_db, app_db + '-wal',
                                os.path.join(library, 'CalibReader_positions.json'),
                                os.path.join(library, 'metadata.db'))]


def _export_n1(kobo_import, kobo_push, library):
    '''N1 across runs: this library's "failed / echoed / refused / settled" memories.'''
    lib = os.path.realpath(library)

    def rows(table):
        return [[k[1], k[2], list(v) if isinstance(v, tuple) else v] for k, v in table.items() if k[0] == lib]
    return {'import_failed': rows(kobo_import._FAILED), 'import_echoed': rows(kobo_import._ECHOED),
            'push_failed': rows(kobo_push._FAILED), 'push_settled': rows(kobo_push._SETTLED)}


def _restore_n1(kobo_import, kobo_push, library, data):
    lib = os.path.realpath(library)
    for name, table, as_tuple in (('import_failed', kobo_import._FAILED, False),
                                  ('import_echoed', kobo_import._ECHOED, False),
                                  ('push_failed', kobo_push._FAILED, False),
                                  ('push_settled', kobo_push._SETTLED, True)):
        for row in (data or {}).get(name) or []:
            try:
                app_db, uuid, value = row
            except (TypeError, ValueError):
                continue
            table[(lib, app_db, uuid)] = tuple(value) if as_tuple and isinstance(value, list) else value


class RunOutcome(object):
    def __init__(self):
        self.status = None          # 'done' | 'no-change' | 'busy' | 'off' | 'failed'
        self.summary = ''
        self.notes = []
        self.kres = None
        self.pres = None


def _version_text(v):
    return '.'.join(str(x) for x in v)


def run_once(library, config_dir=None, now=None, hold=0, lookup=None, timeout=None):
    '''One run of the job for `library` (the folder exactly as calibre's Settings key it).
    -> RunOutcome. Writes one line (plus notes) to kobo.log and the job's state file.
    Statuses: 'done' and 'no-change' (good runs), 'busy' (calibre or the CLI had the lock:
    the job is alive), 'off' (calibre says not to run), 'failed' (S2: an import or push
    that failed, macOS privacy, a library that is gone; the error stays in the state
    until a run succeeds).'''
    started = time.monotonic()
    now = now or _dt.datetime.now(_dt.timezone.utc)
    config_dir = config_dir or calibre_config_dir()
    out = RunOutcome()
    state = load_state(library)
    log = ServiceLog()
    head = '{} [{}]'.format(_iso(now)[:19] + 'Z', library_key(library))
    lines = []

    def finish(status, summary, clean=False, held=False, problems=()):
        # Only a run that held the lock writes its whole state (N1 memories, back-off,
        # stamps); any other run updates just its own fields under the state file's lock.
        out.status, out.summary = status, summary
        fields = {'library': library, 'last_run_at': _iso(now), 'status': status,
                  'summary': scrub_line(summary), 'note': scrub_line(out.notes[0]) if out.notes else '',
                  'clean': bool(clean), 'version': VERSION}
        if status in ('done', 'no-change', 'busy'):
            fields['last_ok_at'] = _iso(now)         # S1: the heartbeat
        if status == 'failed':
            first = problems[0] if problems else (out.notes[0] if out.notes else summary)
            fields['error'] = scrub_line(first)
            fields['error_at'] = _iso(now)
        log.write(['{} {} ({:.1f} s)'.format(head, summary, time.monotonic() - started)]
                  + ['  note: ' + n for n in out.notes]
                  + [line if line.startswith(' ') else '  ' + line for line in lines])
        try:
            if held:
                state.update(fields)
                if status in ('done', 'no-change'):
                    state.pop('error', None)          # S2: kept until a run succeeds
                    state.pop('error_at', None)
                save_state(library, state)
            else:
                merged = merge_state(library, fields)
                if status in ('done', 'no-change') and 'error' in merged:
                    merged.pop('error', None)
                    merged.pop('error_at', None)
                    save_state(library, merged)
        except OSError:
            pass
        return out

    try:
        ensure_folders()                              # B1
    except OSError:
        pass
    _cap_launchd_log()
    prefs, err = read_prefs(config_dir)
    if err:
        out.notes.append(err)
        return finish('failed', 'nothing done: calibre\'s settings could not be read', problems=[err])
    entry = library_entry(prefs, library)
    if entry is None:
        return finish('off', 'nothing done: Kobo sync is not set up for this library in calibre')
    if entry.get('service') is not True:
        return finish('off', 'nothing done: "Keep syncing when calibre is closed" is off in calibre')
    if not plugin_installed(config_dir):
        return finish('off', 'nothing done: the CalibReader Sync plugin is no longer installed in calibre')
    installed = plugin_version(config_dir)
    if installed is None:
        return finish('off', 'nothing done this run: calibre\'s CalibReader Sync plugin could not be read '
                             '(calibre may be updating it)')
    if installed < MIN_PLUGIN:
        # S5: an older plugin (a rollback to 1.5.0) has no lock and keeps its own session copy
        # of the journal; this job never runs beside it.
        out.notes.append('To remove this job: install CalibReader Sync {} or later and untick the box, or run '
                         'launchctl bootout gui/{}/{} and delete {}.'.format(
                             _version_text(MIN_PLUGIN), _uid(), label_for(library), plist_path(label_for(library))))
        return finish('off', 'nothing done: calibre\'s CalibReader Sync plugin is {}, older than this job ({})'
                      .format(_version_text(installed), VERSION))
    # S3 / S5: can the job read the library and app.db at all?
    app_db = entry['app_db']
    metadata = os.path.join(library, 'metadata.db')
    probe = _probe(metadata)
    if probe == 'blocked':
        note = privacy_note(library)
        out.notes.append(note)
        return finish('failed', 'failed: macOS privacy', problems=[note])
    if probe == 'missing':
        note = ('The library folder {} is not there any more, or is not a calibre library (renamed or moved in '
                'calibre?): this job does nothing. Remove it in calibre: Settings > Kobo > Background jobs on this '
                'Mac > Remove.'.format(scrub_line(library)))
        out.notes.append(note)
        return finish('failed', 'failed: the library is gone', problems=[note])
    if _probe(app_db) == 'blocked':
        note = privacy_note(os.path.dirname(app_db))
        out.notes.append(note)
        return finish('failed', 'failed: macOS privacy', problems=[note])
    stamps = input_stamps(library, entry, config_dir)
    last_full = _parse_iso(state.get('full_run_at'))
    if (state.get('clean') and not state.get('error') and state.get('stamps') == stamps and last_full is not None
            and 0 <= (now - last_full).total_seconds() < FULL_RUN_AGE):
        return finish('no-change', 'no change', clean=True)
    lock = LibraryLock(library, 'the background job')
    try:
        got = lock.acquire()
    except OSError as exc:                            # N6: never a run without a line
        note = 'The Kobo sync failed: its lock could not be taken ({}).'.format(exc)
        out.notes.append(note)
        return finish('failed', 'failed', problems=[note])
    if not got:
        out.notes.append(busy_note(lock.holder()))
        return finish('busy', 'skipped: the library\'s Kobo sync was busy')
    try:
        kobo_import, kobo_push = _core()
        _restore_n1(kobo_import, kobo_push, library, state.get('n1'))
        journal = kobo_push.load_state(journal_path(config_dir), library)
        if not journal.had_part:
            # N3: as calibre does (ui._kobo_journal): calibre's PREFS copy fills in a library the
            # journal has no part for (1.5.0 before the journal, or a lost journal).
            for uuid, record in (((prefs.get('kobo_pushed') or {}).get(library)) or {}).items():
                journal.memory.setdefault(uuid, record)
        push = kobo_push.push_enabled(entry)
        since = entry.get('push_since') or None
        problems = []
        backoff = _parse_iso(state.get('backoff_until'))
        if push and backoff is not None and now < backoff:
            note = ('Calibre-Web failed at the last try; sending to the Kobo waits until {} UTC.'
                    .format(_iso(backoff)[11:16]))
            out.notes.append(note)
            problems.append(note)
            push = False

        def still_on():
            # The consent, again before every PUT (R1-S3, across processes): calibre's
            # Settings must still say push on, the same start time, the same app.db, job on.
            current, _ = read_prefs(config_dir)
            cur = library_entry(current, library)
            return bool(cur and kobo_push.push_enabled(cur) and cur.get('service') is True
                        and cur.get('push_since') == since and cur.get('app_db') == app_db)
        kwargs = {}
        if timeout is not None:
            kwargs['timeout'] = timeout
        kres, pres = kobo_push.sync_kobo(
            library, app_db, lookup or kobo_import.metadata_lookup(library), user=entry.get('user') or None,
            push=push, base_url=kobo_push.calibre_web_url(entry), memory=journal.memory, log=lines.append,
            max_books=SERVICE_MAX_BOOKS, since=since, persist=lambda memory: journal.save(), still_on=still_on,
            **kwargs)
        out.kres, out.pres = kres, pres
        if pres is not None and (pres.sent or pres.failure == 'timeout'):
            journal.save()
        if hold:
            time.sleep(hold)                # tests / e2e only: hold the lock a while
        state['n1'] = _export_n1(kobo_import, kobo_push, library)
        if pres is not None and pres.failure in ('network', 'timeout', 'server', 'address'):
            state['backoff_until'] = _iso(now + _dt.timedelta(seconds=kobo_push.BACKOFF))
        elif pres is not None:
            state.pop('backoff_until', None)
        # S2: what the import and the push reported is a failure, except the push's notes that
        # only inform (books held back for want of proof, sending unticked mid-batch).
        for n in kres.notes:
            problems.append(_privacy_from(n, library, app_db) or n)
        out.notes.extend(kres.notes)
        if pres is not None:
            info = getattr(pres, 'info', [])
            for n in pres.notes:
                if n not in info:
                    problems.append(_privacy_from(n, library, app_db) or n)
            out.notes.extend(n for n in pres.notes if n not in out.notes)
            lines.extend(pres.lines)
        reasons = {}
        for k, why in kres.skipped.items():
            reasons['import ' + str(k)] = why
        for k, why in (pres.skipped.items() if pres is not None else ()):
            reasons['send ' + str(k)] = why
        before = state.get('reasons') or {}
        for k in sorted(reasons):
            if before.get(k) != reasons[k]:
                lines.append('  {}: {}'.format(k.replace('import ', 'not imported ').replace('send ', 'not sent '),
                                               reasons[k]))
        state['reasons'] = reasons
        more = kres.more or (pres is not None and pres.more)
        clean = not more and not problems
        state['stamps'] = stamps
        state['full_run_at'] = _iso(now)
        summary = '{} place{} imported, {} sent{}{}'.format(
            len(kres.written), '' if len(kres.written) == 1 else 's',
            len(pres.sent) if pres is not None else 0,
            '' if pres is not None else ' (sending to the Kobo is off)',
            '; more next run' if more else '')
        if problems:
            return finish('failed', 'failed: ' + summary, clean=False, held=True, problems=problems)
        return finish('done', summary, clean=clean, held=True)
    except Exception as exc:
        note = _privacy_from(exc, library, app_db) or 'The Kobo sync failed ({}).'.format(exc)
        out.notes.append(note)
        lines.extend('  ' + line for line in traceback.format_exc().splitlines()[-6:])
        return finish('failed', 'failed', held=True, problems=[note])
    finally:
        lock.release()


# -- install / uninstall (macOS launchd) ----------------------------------------------------

def find_calibre_debug():
    '''calibre's own calibre-debug (its bundled Python), or None.'''
    names = ['calibre-debug.exe', 'calibre-debug'] if os.name == 'nt' else ['calibre-debug']
    dirs = []
    loc = getattr(sys, 'executables_location', None)        # set by calibre's macOS launcher
    if loc:
        dirs.append(loc)
    if sys.executable:
        dirs.append(os.path.dirname(sys.executable))
    if sys.platform == 'darwin':
        dirs += ['/Applications/calibre.app/Contents/MacOS',
                 os.path.expanduser('~/Applications/calibre.app/Contents/MacOS')]
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return shutil.which('calibre-debug')


def unusable_location(debug):
    '''S1: a calibre-debug launchd must not be pinned to: in a disk image (gone when it is
    ejected) or in an App Translocation copy (gone when calibre quits). -> message or None.'''
    path = debug or ''
    app = path.split('.app/')[0] + '.app' if '.app/' in path else path
    if '/AppTranslocation/' in path:
        where = 'a temporary copy that macOS made of a downloaded calibre ({})'.format(app)
    elif path.startswith('/Volumes/'):
        where = 'a disk image or another disk ({})'.format(app)
    else:
        return None
    return ('Move calibre to Applications first: calibre is running from {}, which the background job cannot use '
            'after calibre quits. Quit calibre, drag calibre into the Applications folder, open it from there and '
            'tick the box again.'.format(where))


def program_args(library, config_dir, debug):
    '''What launchd runs: calibre-debug -e <copy>/kobo_service.py -- run …. calibre's own
    Python only (review N1: a system python3 may be the developer-tools stub or too old to
    read the stamps).'''
    if not debug:
        raise ValueError('calibre-debug is needed')
    script = os.path.join(service_dir(), 'kobo_service.py')
    return [debug, '-e', script, '--', 'run', '--library', library, '--config-dir', config_dir]


def plist_for(library, config_dir, app_db, label=None, args=None):
    label = label or label_for(library)
    env = {'CALIBRE_CONFIG_DIRECTORY': config_dir}
    for name in ('CALIBREADER_SYNC_HOME', 'CALIBREADER_SYNC_LOGS', 'CALIBREADER_SYNC_LABEL'):
        if os.environ.get(name):
            env[name] = os.environ[name]
    out = os.path.join(log_dir(), 'kobo-launchd.log')
    # WatchPaths: macOS reports a file as changed when it is CLOSED after a write, and Calibre-Web
    # keeps app.db open (SQLAlchemy's pool), so app.db alone almost never fires. SQLite's rollback
    # journal (Calibre-Web's journal_mode is DELETE) is created and deleted by every write: it
    # fires at once (Mac e2e). The WAL file covers a Calibre-Web set to WAL. Best effort all the
    # same (launchd.plist(5): race-prone); StartInterval is the guarantee.
    return {
        'Label': label,
        'ProgramArguments': args or program_args(library, config_dir, find_calibre_debug()),
        'RunAtLoad': True,
        'StartInterval': INTERVAL,
        'WatchPaths': [app_db, app_db + '-journal', app_db + '-wal'],
        'ThrottleInterval': THROTTLE,
        'ProcessType': 'Background',
        'LowPriorityIO': True,
        'Nice': 10,
        'StandardOutPath': out,
        'StandardErrorPath': out,
        'EnvironmentVariables': env,
    }


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def install_files(read):
    '''Copy the Kobo modules (`read(name)` -> bytes) to service_dir(); only files that
    differ are rewritten, each atomically. -> the names written.'''
    folder = service_dir()
    written, manifest = [], {}
    for name in MODULES:
        data = read(name)
        if not data:
            raise OSError('the plugin has no {}'.format(name))
        manifest[name] = _sha(data)
        path = os.path.join(folder, name)
        try:
            with open(path, 'rb') as f:
                same = _sha(f.read()) == manifest[name]
        except OSError:
            same = False
        if not same:
            _write_atomic(path, data)
            written.append(name)
    body = (json.dumps({'version': VERSION, 'files': manifest}, indent=1, sort_keys=True) + '\n').encode('utf-8')
    mpath = os.path.join(folder, 'MANIFEST.json')
    try:
        with open(mpath, 'rb') as f:
            same = f.read() == body
    except OSError:
        same = False
    if not same:
        _write_atomic(mpath, body)
        written.append('MANIFEST.json')
    return written


def copy_present():
    return os.path.isfile(os.path.join(service_dir(), 'MANIFEST.json'))


def _uid():
    return os.getuid() if hasattr(os, 'getuid') else 0


def run_command(args, timeout=15):
    '''-> (exit code, output). Never raises for a command that fails or is missing.'''
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return p.returncode, ((p.stdout or '') + (p.stderr or '')).strip()


def launchctl_print(label, run=None):
    run = run or run_command
    return run(['launchctl', 'print', 'gui/{}/{}'.format(_uid(), label)])


def loaded(label, run=None):
    return launchctl_print(label, run)[0] == 0


_LAST_EXIT = re.compile(r'last exit code = (\d+)(?::\s*([A-Z_0-9]+))?')


def last_exit(print_text):
    '''launchd's "last exit code = 78: EX_CONFIG" -> (78, 'EX_CONFIG'), or None.'''
    m = _LAST_EXIT.search(print_text or '')
    return (int(m.group(1)), m.group(2) or '') if m else None


def install(library, config_dir, app_db, read, run=None, label=None, sleep=time.sleep, debug=None):
    '''Settings OK with the box ticked: copy the modules, create the log and support
    folders, write the plist, (re)load it. -> (ok, message). On failure nothing is left
    loaded or on disk.'''
    run = run or run_command
    label = label or label_for(library)
    debug = find_calibre_debug() if debug is None else debug
    if not debug:
        return False, ('calibre\'s own calibre-debug was not found next to calibre, so the background job cannot '
                       'run. Reinstall calibre in the Applications folder and tick the box again.')
    problem = unusable_location(debug)
    if problem:
        return False, problem
    try:
        ensure_folders()                              # B1: before launchd ever starts the job
    except OSError as exc:
        return False, 'Could not create the background job\'s folders ({}).'.format(exc)
    try:
        install_files(read)
    except OSError as exc:
        return False, 'Could not copy the background job\'s files to {} ({}).'.format(service_dir(), exc)
    path = plist_path(label)
    body = plistlib.dumps(plist_for(library, config_dir, app_db, label=label,
                                    args=program_args(library, config_dir, debug)))
    try:
        with open(path, 'rb') as f:
            unchanged = f.read() == body
    except OSError:
        unchanged = False
    if unchanged and loaded(label, run):
        return True, 'The background job is on ({}).'.format(label)
    try:
        _write_atomic(path, body)
    except OSError as exc:
        return False, 'Could not write {} ({}).'.format(path, exc)
    domain = 'gui/{}'.format(_uid())
    run(['launchctl', 'bootout', '{}/{}'.format(domain, label)])
    code, text = 1, ''
    for attempt in range(4):            # a bootout can take a moment to finish
        code, text = run(['launchctl', 'bootstrap', domain, path])
        if code == 0:
            break
        sleep(0.5)
    if code != 0:
        try:
            os.remove(path)
        except OSError:
            pass
        return False, 'launchctl could not load the background job (exit {}: {}).'.format(code, text)
    return True, 'The background job is on ({}).'.format(label)


def remove_job(label, run=None):
    '''Unload a job by its label and remove its plist (S5: any of ours, including one for
    a library calibre no longer shows). -> (ok, message).'''
    run = run or run_command
    if not label.startswith(LABEL_PREFIX + '.'):
        return False, '{} is not a CalibReader Sync job.'.format(label)
    run(['launchctl', 'bootout', 'gui/{}/{}'.format(_uid(), label)])
    path = plist_path(label)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        return False, 'Could not remove {} ({}).'.format(path, exc)
    if loaded(label, run):
        return False, 'launchctl still lists {}.'.format(label)
    return True, 'The background job is off.'


def uninstall(library, run=None, label=None):
    '''Settings OK with the box unticked: unload the job and remove its plist.
    -> (ok, message).'''
    return remove_job(label or label_for(library), run)


def installed_jobs(run=None):
    '''S5: every CalibReader Sync job on this Mac (one plist per library in LaunchAgents),
    with the library it runs for, whether launchd has it, and whether the library is still
    there. -> [{'label', 'plist', 'library', 'loaded', 'library_exists'}].'''
    jobs = []
    try:
        names = sorted(os.listdir(agents_dir()))
    except OSError:
        return jobs
    for name in names:
        if not (name.startswith(LABEL_PREFIX + '.') and name.endswith('.plist')):
            continue
        path = os.path.join(agents_dir(), name)
        try:
            with open(path, 'rb') as f:
                plist = plistlib.load(f)
            args = list(plist.get('ProgramArguments') or [])
            library = args[args.index('--library') + 1] if '--library' in args else ''
        except Exception:
            plist, library = {}, ''
        label = plist.get('Label') or name[:-len('.plist')]
        jobs.append({'label': label, 'plist': path, 'library': library, 'loaded': loaded(label, run),
                     'library_exists': bool(library) and os.path.isdir(library)})
    return jobs


def _local(t):
    return t.astimezone().strftime('%Y-%m-%d %H:%M')


def status_text(library, run=None, label=None, now=None):
    '''The Settings status line: is the job there and healthy (S1), its last run, and an
    error kept until a run succeeds (S2).'''
    label = label or label_for(library)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    path = plist_path(label)
    state = load_state(library)
    when = _parse_iso(state.get('last_run_at'))
    last = 'Last run {}: {}.'.format(_local(when), state.get('summary') or state.get('status') or '?') if when else ''
    if not os.path.exists(path):
        return ('Background job: off. ' + last).strip()
    code, text = launchctl_print(label, run)
    falls_back = 'calibre does the Kobo work itself while it is open.'
    if code != 0:
        return ('Background job isn\'t running: it is not loaded in launchd; click OK here to load it again. '
                + falls_back)
    healthy, ok_at = job_health(state, now)
    error = state.get('error')
    if healthy and not error:
        return ('Background job: on (launchd, every 5 minutes and when Calibre-Web records a Kobo sync). '
                + last).strip()
    exit_info = last_exit(text)
    if error:
        reason = 'its last run failed: {}'.format(error)
    elif exit_info and exit_info[0] not in (0, BUSY):
        reason = 'launchd could not start it or it stopped (last exit code {}{})'.format(
            exit_info[0], ': ' + exit_info[1] if exit_info[1] else '')
    elif ok_at is not None:
        reason = 'it last ran well at {}'.format(_local(ok_at))
    else:
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:
            age = STARTING
        if age < STARTING:
            return 'Background job: starting (launchd loaded it a moment ago).'
        reason = 'it has not run yet'
    if healthy:
        return 'Background job: on, but {}. {}'.format(reason, last).strip()
    return 'Background job isn\'t running: {}. {} {}'.format(reason, falls_back, last).strip()


# -- the job's command line ------------------------------------------------------------

def exit_on_sigterm():
    '''N5: launchd's SIGTERM (bootout at Settings OK, logout) ends the run through Python's
    own exit path: finally blocks run, temp files are cleaned up, the lock is released.'''
    import signal
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(143))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='CalibReader Sync: the Kobo sync without calibre running')
    sub = ap.add_subparsers(dest='cmd')
    r = sub.add_parser('run', help='one run for a library (what launchd starts)')
    r.add_argument('--library', required=True)
    r.add_argument('--config-dir', help='calibre\'s config folder (default: calibre\'s own)')
    r.add_argument('--hold-seconds', type=float, default=0, help=argparse.SUPPRESS)   # tests / e2e only
    sub.add_parser('version', help='print the version and where the Kobo modules load from')
    a = ap.parse_args(argv)
    if a.cmd == 'version':
        kobo_import, kobo_push = _core()
        print('CalibReader Sync Kobo job {}'.format(VERSION))
        for m in (sys.modules[__name__], kobo_import, kobo_push):
            print('{} {}'.format(m.__name__, os.path.abspath(m.__file__)))
        return 0
    if a.cmd != 'run':
        ap.print_help()
        return 2
    exit_on_sigterm()
    out = run_once(a.library, config_dir=a.config_dir, hold=a.hold_seconds)
    print('{}: {}'.format(out.status, scrub_line(out.summary)))
    return BUSY if out.status == 'busy' else 0


if __name__ == '__main__':
    # The job: only the copy next to this file runs, never the installed plugin zip that
    # calibre-debug loads with the user's plugins (calibre_plugins.calibreader_sync).
    _here = os.path.dirname(os.path.abspath(__file__))
    if not sys.path or sys.path[0] != _here:
        sys.path.insert(0, _here)
    sys.modules['calibre_plugins.calibreader_sync'] = None
    import kobo_service as _service
    sys.exit(_service.main())

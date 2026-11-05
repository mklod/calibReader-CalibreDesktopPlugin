# Last modified: 2026-11-05--1118
#
# CalibReader Sync — the toolbar action (GUI only).
# calibre imports this module only in its main window, through
# CalibReaderSyncPlugin.actual_plugin in __init__.py. Keeping every GUI import
# here (not in __init__.py) means calibredb, calibre-server, ebook-convert and
# the other non-GUI calibre processes no longer import Qt because this plugin
# is installed (v1.2.1; calibre's two-class plugin pattern).
#
# v1.2.5: automatic sync. The same run as the button, a few seconds after calibre
# starts and after a library opens, and when either sync file changes (polled
# every 5 minutes). Automatic runs never pop up a dialog: they put one short line
# in the status bar, and say nothing at all when nothing changed. Settings has
# the switch (on by default, stored with the library).
#
# v1.3.0: read status goes into the column picked in Settings; the plugin keeps
# its read-flag memory per column ('applied' for #read_status as before,
# 'applied_cols' for the others; sync_core.flag_blobs_from_prefs).
#
# v1.5.0 (tester preview): with Settings -> Kobo "Also send CalibReader's place to
# the Kobo" on (off by default), a run also sends the app's places to the Kobo
# through Calibre-Web (kobo_push.sync_kobo). What was sent is remembered per
# library folder in this plugin's prefs ('kobo_pushed'), so the import does not
# read our own push back as a Kobo move.
#
# v1.5.0 review round 1: the push runs on a worker thread (S4), never on calibre's
# GUI thread: the import still runs in the run itself, the push reports back
# through a Qt signal (status bar + the saved memory). Only places read after the
# box was ticked are sent (PREFS['kobo'][library]['push_since'], B3). After
# Calibre-Web failed, automatic runs do not send for BACKOFF seconds; the button
# always tries.
#
# v1.5.0 re-review round 1: what is about to be sent is written to a small journal
# file BEFORE each PUT, by the worker (KOBO_JOURNAL; R1-S2), so a calibre that
# closes mid-send still knows its own push; the consent is checked again before
# every PUT (R1-S3); a run that found the worker busy is run again after it (R1-N3).
#
# v1.6.0 (tester preview): the Kobo sync without calibre running (kobo_service.py,
# Settings -> Kobo "Keep syncing when calibre is closed", macOS). Every Kobo run here
# takes the library's lock (kobo_service.LibraryLock), shared with the background job
# and the CLI: busy = no Kobo work this run, one note; the lock is held from the import
# until the push worker ends. With the job installed for the open library AND healthy (a
# good run in the last 15 minutes: review S1), automatic runs leave the Kobo to it (the
# button still runs it); a job that cannot start or keeps failing gives it back. The push journal is read from
# disk at the start of every Kobo run (one echo memory with the job and the CLI, R2-N2);
# PREFS['kobo_pushed'] only fills in a library the journal has no part for. When calibre
# starts, a background job's copy of the Kobo modules that differs from this plugin's is
# refreshed.
#
# v1.7.0 (tester preview): Kobo Utilities (USB) -> CalibReader (kobo_ku.py). With a location
# column picked in Settings -> Kobo "Kobo Utilities (USB)" (off until then), every run (after the
# read-status sync, isolated like the Kobo step) reads Kobo Utilities' columns through the db API
# (read-only), maps the books whose values changed on the KEPUB calibre's Kobo driver sends (the
# stored KEPUB, else calibre's own kepubify, cached) and merges them into CalibReader_positions.json
# under the library's Kobo lock, deviceId "kobo-utilities". Not left to the background job (KU's
# columns change only while calibre runs). The 5-minute poll also watches a hash of those columns;
# a minute after a Kobo connects (Kobo Utilities' auto-store) a run follows.
#
# v1.3.1: one run is sync_core.sync_library (tested under real calibre in
# tests/test_real_calibre.py). The first sync into a chosen read column waits for
# the user's OK (a question with the counts, from the button; a status-bar note from
# an automatic run); every value written is logged (menu: Change log…).
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

__license__   = 'GPL v3'
__copyright__ = '2026, CalibReader'

from calibre.constants import config_dir
from calibre.gui2.actions import InterfaceAction
from calibre.gui2 import error_dialog, info_dialog, open_local_file, question_dialog
from calibre.utils.config import JSONConfig
from qt.core import QMenu, QObject, QTimer, QToolButton, pyqtSignal

import os
import threading
import time

# Pure logic — no calibre imports, so it can be unit-tested outside the GUI.
# Inside calibre the plugin is loaded as the package `calibreader_sync` (see
# plugin-import-name-calibreader_sync.txt); the bare import is the fallback
# used when the module is exercised from this folder directly.
try:
    from calibre_plugins.calibreader_sync.sync_core import (
        CHANGE_LOG_DIR, POSITIONS_FILE, SETTINGS_KEY, SYNC_FILE, change_log_path,
        normalise_settings, sync_library,
    )
except ImportError:  # pragma: no cover - standalone / test import
    from sync_core import (
        CHANGE_LOG_DIR, POSITIONS_FILE, SETTINGS_KEY, SYNC_FILE, change_log_path,
        normalise_settings, sync_library,
    )
try:
    from calibre_plugins.calibreader_sync.kobo_import import KoboImportResult, import_positions
    from calibre_plugins.calibreader_sync.kobo_push import (
        BACKOFF, KoboPushResult, calibre_web_url, load_state, now_iso, push_enabled, push_positions,
        with_kepub_beside,
    )
    from calibre_plugins.calibreader_sync import kobo_service
except ImportError:  # pragma: no cover - standalone / test import
    from kobo_import import KoboImportResult, import_positions
    from kobo_push import (
        BACKOFF, KoboPushResult, calibre_web_url, load_state, now_iso, push_enabled, push_positions,
        with_kepub_beside,
    )
    import kobo_service
try:
    from calibre_plugins.calibreader_sync import kobo_ku
except ImportError:  # pragma: no cover - standalone / test import
    import kobo_ku

# Per-book bookkeeping lives in the plugin's OWN prefs, never in the user's
# library: adding a column to someone's library to track sync state is not
# ours to do, and this file has to survive switching libraries anyway.
# 'applied' is the read flag's record for #read_status, 'applied_cols' the
# records for any other read column (1.3.0), 'applied_pos' reading progress's,
# 'confirmed' (1.3.1) the chosen read columns the user said Yes to. 1.3.1 (review L1):
# all of it per library, under 'libraries' (sync_core.sync_library).
# 1.3.1 (review M4): every value written into a library is logged, old -> new, in
# calibre's config folder: plugins/calibreader_sync_changes/<library>.log.
AUTO_DELAY_MS = 4000            # after calibre starts or a library opens
POLL_MS = 5 * 60 * 1000         # how often the sync files are checked for changes
PREFS = JSONConfig('plugins/calibreader_sync')
CHANGE_LOGS = os.path.join(config_dir, 'plugins', CHANGE_LOG_DIR)
# 1.4.0 (tester preview): Kobo reading places from Calibre-Web, per library
# folder: PREFS['kobo'] = {library path: {'app_db': path, 'user': name or ''}}.
# This computer's path, so not in the library. Off unless a path is set.
# 1.5.0: the entry also holds 'push' (True only when the user ticked "Also send
# CalibReader's place to the Kobo"; off by default) and 'cw_url' (Calibre-Web's
# address, '' = http://127.0.0.1:8083). PREFS['kobo_pushed'] = {library path:
# {uuid: what was sent}}: the echo guard's memory (never the shared sidecar).
# Review round 1 (B3): 'push_since' in the entry = when the box was ticked (UTC);
# only app places stamped after it are sent. config.save_kobo sets it when the box
# goes from off to on and drops it when unticked.
# 1.6.0: 'service' in the entry = True when "Keep syncing when calibre is closed" is
# ticked (macOS): the background job (kobo_service) reads this entry from calibre's
# prefs file at every run and before every PUT, and never writes it.
PREFS.defaults['kobo'] = {}
PREFS.defaults['kobo_pushed'] = {}
# Re-review R1-S2: the push memory's journal, written by the push worker before and
# after each PUT ({library: {uuid: record}}); PREFS['kobo_pushed'] is a copy saved
# on the GUI thread when a send is done, read only to fill in.
KOBO_JOURNAL = os.path.join(config_dir, 'plugins', 'calibreader_sync_kobo_pushed.json')
# 1.7.0: Kobo Utilities (USB). PREFS['kobo_ku'] = {library path: {'sources': [one source: the
# location / percent / last-read columns, boundReader]}} (kobo_ku.prefs_entry); what was seen per
# book in KU_MEMORY (kobo_ku.MemoryFile); PREFS['kobo_ku_device_seen'] = when a Kobo last connected
# (the staleness hint). KEPUBs regenerated with calibre's kepubify go to calibre's cache folder.
PREFS.defaults['kobo_ku'] = {}
PREFS.defaults['kobo_ku_device_seen'] = ''
KU_MEMORY = kobo_ku.memory_path(config_dir)
KU_DEVICE_DELAY_MS = 60 * 1000          # Kobo Utilities' auto-store runs when a Kobo's books are read
KU_MORE_DELAY_MS = 20 * 1000            # review step 4 NIT 1: a KU catch-up chunk every 20 s, not every 4 s


def kobo_settings(library_path):
    entry = (PREFS.get('kobo', {}) or {}).get(library_path) or {}
    return entry if entry.get('app_db') else None


def ku_source(library_path):
    '''1.7.0: the library's Kobo Utilities source (its columns), or None while no location
    column is picked.'''
    return kobo_ku.source_from_prefs((PREFS.get('kobo_ku', {}) or {}).get(library_path))


def resource_reader():
    '''name -> the bytes of one of this plugin's files: from the plugin zip inside calibre
    (get_resources, injected by calibre's plugin loader), else from this folder.'''
    getter = globals().get('get_resources')
    here = os.path.dirname(os.path.abspath(__file__))

    def read(name):
        if getter is not None:
            data = getter(name)
            if data:
                return data
        with open(os.path.join(here, name), 'rb') as f:
            return f.read()
    return read


class _PushBridge(QObject):
    '''Hands the worker's push result to the GUI thread: the bridge lives on the GUI
    thread, so a signal emitted on the worker is delivered there (queued).'''
    done = pyqtSignal(object)

    def __init__(self, callback):
        QObject.__init__(self)
        self._callback = callback
        self.done.connect(self._deliver)

    def _deliver(self, payload):
        self._callback(payload)


class CalibReaderSyncAction(InterfaceAction):
    name = 'CalibReader Sync'
    action_spec = (
        'Sync Read Status',
        None,
        'Sync read status and reading progress from the CalibReader app',
        None,
    )
    # Click = sync now; the arrow opens the menu (Sync now, Settings…).
    popup_type        = QToolButton.ToolButtonPopupMode.MenuButtonPopup
    allowed_in_toolbar = True
    allowed_in_menu   = True

    def genesis(self):
        # get_icons is injected by Calibre's plugin loader at runtime.
        # It looks for the file inside the plugin zip's images/ directory.
        icon = get_icons('images/icon.png')
        self.qaction.setIcon(icon)
        self.qaction.triggered.connect(self.sync_read_status)
        self.menu = QMenu(self.gui)
        self.menu.addAction(icon, 'Sync now', self.sync_read_status)
        self.menu.addAction('Settings…', self.show_settings)
        self.menu.addAction('Change log…', self.show_change_log)
        self.qaction.setMenu(self.menu)
        self._seen = None                       # (library, file stamps) after the last run
        self._kick = QTimer(self.gui)
        self._kick.setSingleShot(True)
        self._kick.timeout.connect(lambda: self.auto_sync('open'))
        self._poll = QTimer(self.gui)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(lambda: self.auto_sync('poll'))
        # 1.7.0: a Kobo's books read by calibre (Kobo Utilities stores its places then): run again soon.
        self._ku_device_timer = QTimer(self.gui)
        self._ku_device_timer.setSingleShot(True)
        self._ku_device_timer.timeout.connect(lambda: self.auto_sync('device'))
        try:
            from calibre.gui2.device import device_signals
            device_signals.device_metadata_available.connect(self._device_metadata_available)
        except Exception:                       # never break calibre's start over it
            pass

    # -- automatic sync (1.2.5) -----------------------------------------------

    def initialization_complete(self):
        self._kick.start(AUTO_DELAY_MS)
        self._poll.start()
        self._refresh_service_copy()

    def _refresh_service_copy(self):
        '''1.6.0: a background job set up by an older plugin runs its own copy of the Kobo
        modules; bring it in line with this plugin (files only; launchd is not touched).'''
        try:
            if kobo_service.copy_present():
                kobo_service.install_files(resource_reader())
        except Exception:                       # never break calibre's start over it
            pass

    def _device_metadata_available(self):
        try:
            device = self.gui.device_manager.connected_device
        except Exception:
            device = None
        if device is None:
            return
        name = '{} {}'.format(type(device).__name__, getattr(device, 'name', '') or '')
        if 'kobo' in name.lower():
            self._kobo_connected()

    def _kobo_connected(self):
        '''1.7.0: a Kobo is connected and calibre has read its books, which is when Kobo
        Utilities' auto-store runs: remember it (the staleness hint) and run again in a minute.'''
        PREFS['kobo_ku_device_seen'] = now_iso()
        timer = getattr(self, '_ku_device_timer', None)
        if timer is not None:
            timer.start(KU_DEVICE_DELAY_MS)

    def library_changed(self, db):
        self._seen = None
        self._kick.start(AUTO_DELAY_MS)

    def shutting_down(self):
        self._kick.stop()
        self._poll.stop()
        return True

    def _file_stamps(self):
        out = []
        paths = [(name, os.path.join(self.gui.library_path, name)) for name in (SYNC_FILE, POSITIONS_FILE)]
        kobo = kobo_settings(self.gui.library_path)
        if kobo and not kobo_service.installed_here(self.gui.library_path, kobo):
            # a Kobo sync changes app.db (or its WAL); with the background job set up, it watches
            paths += [('app.db', kobo['app_db']), ('app.db-wal', kobo['app_db'] + '-wal')]
        for name, path in paths:
            try:
                st = os.stat(path)
                out.append((name, st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((name, None, None))
        source = ku_source(self.gui.library_path)
        if source:
            # 1.7.0: Kobo Utilities writes calibre columns, not a file: a hash of them (calibre's cache)
            try:
                mark = kobo_ku.fingerprint(kobo_ku.read_columns_db(self.gui.current_db.new_api, source))
            except Exception as exc:
                mark = 'unreadable: {}'.format(exc)
            out.append(('kobo-utilities', mark, None))
        return (self.gui.library_path, tuple(out))

    def auto_sync(self, why):
        db = getattr(self.gui, 'current_db', None)
        if db is None or getattr(self.gui, 'library_path', None) is None:
            return
        api = db.new_api
        settings = normalise_settings(api.pref(SETTINGS_KEY, None))
        if not settings['auto_sync']:
            return
        stamps = self._file_stamps()
        if why == 'poll' and stamps == self._seen:
            return                              # nothing new from the app
        if (all(mtime is None for _, mtime, _ in stamps[1]) and not kobo_settings(self.gui.library_path)
                and not ku_source(self.gui.library_path)):
            self._seen = stamps                 # not a CalibReader library: stay silent
            return
        try:
            res = self._run(api, settings)
        except Exception as exc:                # never break calibre over an automatic run
            self.gui.status_bar.show_message('CalibReader Sync: automatic sync failed ({}).'.format(exc), 10000)
            return
        self._seen = self._file_stamps()        # after our own write-back, if any
        if getattr(self, '_ku_busy', False):
            self._seen = None                   # 1.7.0: the KU part waits for the lock: the next poll runs again
        if res.found_any_file:
            line = res.status_line()
            if line:
                self.gui.status_bar.show_message('CalibReader Sync: ' + line, 10000)

    def _kobo_journal(self, library):
        '''The push journal (kobo_push.StateFile) of this library, read from disk for this
        run (1.6.0, R2-N2: the background job and the CLI write the same file, under the
        same lock; no session copy survives between runs). Its .memory is the echo guard's
        memory, shared by this run's import (GUI thread) and push worker, which writes it
        to KOBO_JOURNAL before and after each PUT (R1-S2).'''
        journal = load_state(KOBO_JOURNAL, library)
        if not journal.had_part:
            # 1.5.0 before the journal (or a damaged one): PREFS' copy fills in.
            for uuid, record in ((PREFS.get('kobo_pushed', {}) or {}).get(library) or {}).items():
                journal.memory.setdefault(uuid, record)
        return journal

    def _run_kobo(self, api, manual=False):
        '''Kobo places from Calibre-Web's app.db into CalibReader_positions.json
        (1.4.0 tester preview), before the sync reads that file; then (1.5.0, only
        when the user turned it on) the app's places to the Kobo through Calibre-Web,
        on a worker thread (review round 1 S4). 1.6.0: under the library's Kobo lock;
        automatic runs leave the Kobo to the background job when it is set up here.
        -> the import result, or None when there was no Kobo work.'''
        library = self.gui.library_path
        kobo = kobo_settings(library)
        if not kobo:
            return None
        if not manual and kobo_service.installed_here(library, kobo):
            return None                         # the background job does it
        worker = getattr(self, '_kobo_worker', None)
        if worker is not None and worker.is_alive():
            self._push_status = 'busy'
            self._push_rerun = True             # R1-N3: run again when the worker is done
            return None
        lock = kobo_service.LibraryLock(library, 'calibre')
        if not lock.acquire():
            kres = KoboImportResult()
            kres.notes.append(kobo_service.busy_note(lock.holder()))
            return kres
        handed_off = False
        try:
            ids = api.all_book_ids()

            def lookup(book_id):
                if book_id not in ids:
                    return None
                return (api.field_for('uuid', book_id), api.format_abspath(book_id, 'EPUB'),
                        api.format_abspath(book_id, 'KEPUB'))
            lookup = with_kepub_beside(lookup)  # S2: a KEPUB Calibre-Web added while calibre ran
            journal = self._kobo_journal(library)
            kres = import_positions(library, kobo['app_db'], lookup, user=kobo.get('user') or None,
                                    pushed=journal.memory)
            if push_enabled(kobo):
                since = kobo.get('push_since')
                if not since:
                    # B3: ticked before 'push_since' existed (or by hand): sending starts now.
                    since = now_iso()
                    table = dict(PREFS.get('kobo', {}) or {})
                    entry = dict(table.get(library) or kobo)
                    entry['push_since'] = since
                    table[library] = entry
                    PREFS['kobo'] = table
                handed_off = self._start_kobo_push(library, kobo, lookup, journal, since, manual, lock)
            return kres
        finally:
            if not handed_off:
                lock.release()

    def _start_kobo_push(self, library, kobo, lookup, journal, since, manual, lock):
        '''Start the push on a worker thread (S4), which then owns the library's lock and
        releases it when done (1.6.0). After a Calibre-Web failure, not for BACKOFF seconds
        unless the user pressed the button. -> True when the worker took the lock.'''
        if not manual and time.monotonic() < getattr(self, '_push_backoff_until', 0):
            self._push_status = 'backoff'
            return False
        bridge = getattr(self, '_push_bridge', None)
        if bridge is None:
            bridge = self._push_bridge = _PushBridge(self._kobo_push_done)
        app_db, user, base_url = kobo['app_db'], kobo.get('user') or None, calibre_web_url(kobo)

        def still_on():
            # R1-S3: asked before every PUT; unticking (or another app.db) stops the batch.
            now = kobo_settings(library)
            return bool(push_enabled(now) and now.get('push_since') == since and now.get('app_db') == app_db)

        def work():
            try:
                pres = push_positions(library, app_db, lookup, base_url=base_url, memory=journal.memory,
                                      user=user, since=since, persist=lambda m: journal.save(), still_on=still_on)
            except Exception as exc:            # never let the worker die silently
                pres = KoboPushResult()
                pres.notes.append('Sending to the Kobo failed ({}).'.format(exc))
            finally:
                lock.release()                  # before the GUI hears of it: a follow-up run can start
            bridge.done.emit((library, pres, journal))
        self._push_pending = True
        self._push_status = 'started'
        worker = threading.Thread(target=work, name='CalibReader Sync: send to the Kobo', daemon=True)
        self._kobo_worker = worker
        worker.start()
        return True

    def _kobo_push_done(self, payload):
        '''On the GUI thread, when the worker is done: save what was sent, back off after
        a Calibre-Web failure, one status-bar line, and the next chunk if any.'''
        library, pres, journal = payload
        self._push_pending = False
        self._last_push = pres
        memory = journal.memory
        table = dict(PREFS.get('kobo_pushed', {}) or {})
        if table.get(library) != memory:
            table[library] = dict(memory)
            PREFS['kobo_pushed'] = table        # assignment commits a JSONConfig
        if pres.failure in ('network', 'timeout', 'server', 'address'):
            self._push_backoff_until = time.monotonic() + BACKOFF
        line = pres.status_line() or (('Kobo: ' + pres.notes[0]) if pres.notes else '')
        status_bar = getattr(self.gui, 'status_bar', None)
        if line and status_bar is not None:
            status_bar.show_message('CalibReader Sync: ' + line, 10000)
        rerun, self._push_rerun = getattr(self, '_push_rerun', False), False
        if (pres.more or rerun) and library == getattr(self.gui, 'library_path', None):
            self._kick.start(AUTO_DELAY_MS)

    def _run_ku(self, api, manual=False):
        '''1.7.0 (tester preview): Kobo Utilities' columns -> CalibReader_positions.json, under the
        library's Kobo lock (the background job, the CLI and calibre's Calibre-Web run take it too).
        -> kobo_ku.KuImportResult, or None while no location column is picked.'''
        library = self.gui.library_path
        self._ku_busy = False
        source = ku_source(library)
        if not source:
            return None
        res = kobo_ku.KuImportResult()
        clash = kobo_ku.own_column_clash(source, normalise_settings(api.pref(SETTINGS_KEY, None)))
        if clash:
            res.notes.append(kobo_ku.clash_note(clash))
            return res
        lock = kobo_service.LibraryLock(library, 'calibre')
        if not lock.acquire():
            res.busy = self._ku_busy = True
            worker = getattr(self, '_kobo_worker', None)
            if worker is not None and worker.is_alive():
                self._push_rerun = True         # our own push holds it: run again when it is done
                res.notes.append('Kobo Utilities: waits for calibre\'s own send to Calibre-Web to finish and runs '
                                 'right after it (no need to click again).')
            else:
                res.notes.append(kobo_service.busy_note(lock.holder()))
            return res
        try:
            try:
                values = kobo_ku.read_columns_db(api, source)
            except ValueError as exc:
                res.notes.append(str(exc))
                return res
            memory_file = kobo_ku.MemoryFile(KU_MEMORY)
            signature = kobo_ku.source_signature(source)
            memory = memory_file.load(library, signature)
            ids = api.all_book_ids()

            def lookup(book_id):
                if book_id not in ids:
                    return None
                return (api.field_for('uuid', book_id), api.format_abspath(book_id, 'EPUB'),
                        api.format_abspath(book_id, 'KEPUB'))
            cache = kobo_ku.KepubCache()
            kepub_for = kobo_ku.make_kepub_for(lambda book_id: api.formats(book_id) or (), cache)
            res = kobo_ku.import_ku(library, source, values, lookup, memory, kepub_for=kepub_for)
            if res.memory_changed:
                try:
                    memory_file.save(library, signature, memory)
                except OSError as exc:
                    res.notes.append('Kobo Utilities: what was seen could not be saved ({}); it is looked at '
                                     'again next run.'.format(exc))
            res.last_store = kobo_ku.last_store_seen(values, memory)
            if res.problems:
                uuids = {(u or '').lower(): i for i, u in api.all_field_for('uuid', ids).items()}
                for uuid in res.problems:
                    book_id = uuids.get(uuid.lower())
                    res.titles[uuid] = api.field_for('title', book_id) if book_id is not None else uuid
            return res
        finally:
            lock.release()

    def _ku_lines(self, ku):
        lines = []
        if ku.written:
            lines.append('Kobo Utilities (USB): {} reading place(s) passed to the app.'.format(len(ku.written)))
        problems = sorted(ku.problems.items(), key=lambda kv: ku.titles.get(kv[0], kv[0]))
        for uuid, why in problems[:8]:
            lines.append('Kobo Utilities (USB), not imported: {}: {}.'.format(ku.titles.get(uuid, uuid), why))
        if len(problems) > 8:
            lines.append('Kobo Utilities (USB): {} more not imported.'.format(len(problems) - 8))
        if ku.uncertain:
            lines.append('Kobo Utilities (USB): ' + ku.uncertain + '.')
        if not ku.busy:
            lines.append('Kobo Utilities (USB): ' + kobo_ku.store_line(ku.last_store))
            hint = kobo_ku.staleness_hint(ku.last_store, PREFS.get('kobo_ku_device_seen') or None)
            if hint:
                lines.append(hint)
        return lines

    def _run(self, api, settings=None, approve=None, manual=False):
        # settings: unused since 1.3.1 (sync_library reads the library's own); kept so
        # callers written for 1.3.0 still work.
        # 1.4.0 L2: the Kobo import (and 1.5.0's push) never stops the read-status sync.
        self._push_status = None
        try:
            kres, kobo_error = self._run_kobo(api, manual=manual), None
        except Exception as exc:
            kres, kobo_error = None, 'Kobo sync failed ({}); read status and progress still synced.'.format(exc)
        # L1: a run maps at most kobo_import.MAX_BOOKS books; the rest follow soon.
        # A refused or failing run never sets `more` (N1), so this never loops.
        if kres is not None and kres.more:
            self._kick.start(AUTO_DELAY_MS)
        # 1.7.0 (review step 4, S3): CalibReader's own progress column that is also one of the saved
        # Kobo Utilities columns is not written (its writes would go to the Kobo through KU's restore).
        blocked = ()
        source = ku_source(self.gui.library_path)
        if source:
            own = normalise_settings(api.pref(SETTINGS_KEY, None))
            blocked = tuple(c for c in kobo_ku.own_column_clash(source, own)
                            if c in (own['percent_column'], own['last_read_column']))
        res = sync_library(api, self.gui.library_path, PREFS, log_dir=CHANGE_LOGS, approve=approve, blocked=blocked)
        if res.columns_written:
            self.gui.library_view.model().refresh()
        # 1.7.0: Kobo Utilities (USB), after the read-status sync; a failure never stops it.
        try:
            ku, ku_error = self._run_ku(api, manual=manual), None
        except Exception as exc:
            ku, ku_error = None, 'Kobo Utilities sync failed ({}); read status and progress still synced.'.format(exc)
        if ku is not None:
            if ku.more and not (kres is not None and kres.more):
                self._kick.start(KU_MORE_DELAY_MS)  # L1 / NIT 1: the rest of the books, calibre left breathing
            res.notes.extend(ku.notes)
            res.kobo_ku_imported = len(ku.written)
            res.kobo_ku_lines = self._ku_lines(ku)
            if ku.written:
                res.found_positions_file = True
        if ku_error:
            res.notes.append(ku_error)
        if kres is not None:
            res.notes.extend(kres.notes)
            res.kobo_imported = len(kres.written)
        status = self._push_status
        if status == 'started':
            res.kobo_push_started = True
            res.kobo_push_lines = ['Kobo: CalibReader\'s places are being sent to Calibre-Web in the background; '
                                   'the status bar shows the result.']
        elif status == 'busy':
            res.kobo_push_lines = ['Kobo: still sending the last places to Calibre-Web.']
        if kobo_error:
            res.notes.append(kobo_error)
        return res

    def show_settings(self):
        self.interface_action_base_plugin.do_user_config(self.gui)

    def show_change_log(self):
        path = change_log_path(config_dir, self.gui.current_db.new_api, self.gui.library_path)
        if os.path.exists(path):
            open_local_file(path)
        else:
            info_dialog(self.gui, 'CalibReader Sync',
                        'Nothing has been written into this library yet. Every value the plugin '
                        'writes is recorded, with the value it replaced, in:\n{}'.format(path),
                        show=True)

    def _confirm_first_sync(self, confirm):
        '''(1.3.1) Ask before the first sync into a chosen read column fills it.'''
        change = (', and change {} filled cell(s) where the app\'s mark is newer'.format(confirm['change'])
                  if confirm['change'] else '')
        return question_dialog(
            self.gui, 'CalibReader Sync: first sync into {}'.format(confirm['heading']),
            '<p>This is the first sync into the read status column <b>{} ({})</b>.</p>'
            '<p>It will fill <b>{} empty cell(s)</b> with the CalibReader app\'s read marks{}. '
            'Filled cells are otherwise left as they are.</p>'
            '<p>Every value written is recorded, with the value it replaced, in the change log '
            '(the arrow next to Sync Read Status, then Change log…).</p>'
            '<p>Go ahead?</p>'.format(confirm['heading'], confirm['column'], confirm['fill'], change),
            default_yes=False)

    def sync_read_status(self):
        library_path = self.gui.library_path
        db = self.gui.current_db.new_api
        settings = normalise_settings(db.pref(SETTINGS_KEY, None))

        res = self._run(db, settings, manual=True)
        if res.confirm and self._confirm_first_sync(res.confirm):
            res = self._run(db, settings, approve=res.confirm['column'], manual=True)
        self._seen = self._file_stamps()

        if not res.found_any_file and not res.kobo_ku_lines:   # NIT 10: the KU lines are always shown
            error_dialog(
                self.gui,
                'CalibReader Sync',
                (
                    'No CalibReader files in this library\'s folder:\n{}\n\n'
                    'Looked for {} (read status) and {} (reading progress).\n\n'
                    'Make sure:\n'
                    '  1. Your cloud app has finished syncing\n'
                    '  2. This is the same library the CalibReader app opens\n'
                    '  3. You have read a book, or marked one read, in the app'
                ).format(library_path, SYNC_FILE, POSITIONS_FILE),
                show=True,
            )
            return

        message = res.summary()
        if res.found_positions_file and not (settings['percent_column']
                                             or settings['last_read_column']):
            message += ('\n\nTo copy % read and last read into calibre too, choose columns in '
                        'Settings (the arrow next to this button).')
        info_dialog(self.gui, 'CalibReader Sync', message, show=True)

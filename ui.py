# Last modified: 2026-11-02--1441
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
from qt.core import QMenu, QTimer, QToolButton

import os

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

    # -- automatic sync (1.2.5) -----------------------------------------------

    def initialization_complete(self):
        self._kick.start(AUTO_DELAY_MS)
        self._poll.start()

    def library_changed(self, db):
        self._seen = None
        self._kick.start(AUTO_DELAY_MS)

    def shutting_down(self):
        self._kick.stop()
        self._poll.stop()
        return True

    def _file_stamps(self):
        out = []
        for name in (SYNC_FILE, POSITIONS_FILE):
            try:
                st = os.stat(os.path.join(self.gui.library_path, name))
                out.append((name, st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((name, None, None))
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
        if all(mtime is None for _, mtime, _ in stamps[1]):
            self._seen = stamps                 # not a CalibReader library: stay silent
            return
        try:
            res = self._run(api, settings)
        except Exception as exc:                # never break calibre over an automatic run
            self.gui.status_bar.show_message('CalibReader Sync: automatic sync failed ({}).'.format(exc), 10000)
            return
        self._seen = self._file_stamps()        # after our own write-back, if any
        if res.found_any_file:
            line = res.status_line()
            if line:
                self.gui.status_bar.show_message('CalibReader Sync: ' + line, 10000)

    def _run(self, api, settings=None, approve=None):
        # settings: unused since 1.3.1 (sync_library reads the library's own); kept so
        # callers written for 1.3.0 still work.
        res = sync_library(api, self.gui.library_path, PREFS, log_dir=CHANGE_LOGS, approve=approve)
        if res.columns_written:
            self.gui.library_view.model().refresh()
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

        res = self._run(db, settings)
        if res.confirm and self._confirm_first_sync(res.confirm):
            res = self._run(db, settings, approve=res.confirm['column'])
        self._seen = self._file_stamps()

        if not res.found_any_file:
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

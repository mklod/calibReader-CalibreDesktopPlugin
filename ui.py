# Last modified: 2026-10-21--2257
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
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

__license__   = 'GPL v3'
__copyright__ = '2026, CalibReader'

from calibre.gui2.actions import InterfaceAction
from calibre.gui2 import error_dialog, info_dialog
from calibre.utils.config import JSONConfig
from qt.core import QMenu, QTimer, QToolButton

import os

# Pure logic — no calibre imports, so it can be unit-tested outside the GUI.
# Inside calibre the plugin is loaded as the package `calibreader_sync` (see
# plugin-import-name-calibreader_sync.txt); the bare import is the fallback
# used when the module is exercised from this folder directly.
try:
    from calibre_plugins.calibreader_sync.sync_core import (
        POSITIONS_FILE, SETTINGS_KEY, SYNC_FILE, normalise_settings, run_sync,
    )
except ImportError:  # pragma: no cover - standalone / test import
    from sync_core import (
        POSITIONS_FILE, SETTINGS_KEY, SYNC_FILE, normalise_settings, run_sync,
    )

# Per-book bookkeeping lives in the plugin's OWN prefs, never in the user's
# library: adding a column to someone's library to track sync state is not
# ours to do, and this file has to survive switching libraries anyway.
# 'applied' is the read flag's record, 'applied_pos' reading progress's.
AUTO_DELAY_MS = 4000            # after calibre starts or a library opens
POLL_MS = 5 * 60 * 1000         # how often the sync files are checked for changes
PREFS = JSONConfig('plugins/calibreader_sync')
PREFS.defaults['applied'] = {}
PREFS.defaults['applied_pos'] = {}


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

    def _run(self, api, settings):
        res, flag_blob, pos_blob = run_sync(
            api, self.gui.library_path, settings,
            PREFS.get('applied', {}), PREFS.get('applied_pos', {}))
        if res.found_any_file:
            # Assignment is what commits a JSONConfig. Stored even when nothing was
            # written: a RECORD verdict must persist so a retired entry is never
            # reconsidered.
            PREFS['applied'] = flag_blob
            PREFS['applied_pos'] = pos_blob
            if res.columns_written:
                self.gui.library_view.model().refresh()
        return res

    def show_settings(self):
        self.interface_action_base_plugin.do_user_config(self.gui)

    def sync_read_status(self):
        library_path = self.gui.library_path
        db = self.gui.current_db.new_api
        settings = normalise_settings(db.pref(SETTINGS_KEY, None))

        res = self._run(db, settings)
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

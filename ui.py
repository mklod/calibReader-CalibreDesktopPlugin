# Last modified: 2026-10-21--1601
#
# CalibReader Sync — the toolbar action (GUI only).
# calibre imports this module only in its main window, through
# CalibReaderSyncPlugin.actual_plugin in __init__.py. Keeping every GUI import
# here (not in __init__.py) means calibredb, calibre-server, ebook-convert and
# the other non-GUI calibre processes no longer import Qt because this plugin
# is installed (v1.2.1; calibre's two-class plugin pattern).
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
from qt.core import QMenu, QToolButton

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

    def show_settings(self):
        self.interface_action_base_plugin.do_user_config(self.gui)

    def sync_read_status(self):
        library_path = self.gui.library_path
        db = self.gui.current_db.new_api
        settings = normalise_settings(db.pref(SETTINGS_KEY, None))

        res, flag_blob, pos_blob = run_sync(
            db, library_path, settings,
            PREFS.get('applied', {}), PREFS.get('applied_pos', {}))

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

        # Assignment is what commits a JSONConfig. Stored even when nothing was
        # written: a RECORD verdict must persist so a retired entry is never
        # reconsidered.
        PREFS['applied'] = flag_blob
        PREFS['applied_pos'] = pos_blob

        if res.columns_written:
            self.gui.library_view.model().refresh()

        message = res.summary()
        if res.found_positions_file and not (settings['percent_column']
                                             or settings['last_read_column']):
            message += ('\n\nTo copy % read and last read into calibre too, choose columns in '
                        'Settings (the arrow next to this button).')
        info_dialog(self.gui, 'CalibReader Sync', message, show=True)

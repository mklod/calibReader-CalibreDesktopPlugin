# Last modified: 2026-10-12--1345
#
# CalibReader Sync — Calibre plugin
# Reads the sidecars the CalibReader apps keep in the Calibre library folder
# (synced by Dropbox, Google Drive, OneDrive or a network share) and writes
# them into the library:
#   CalibReader_sync.json      -> the #read_status column (Yes/No)
#   CalibReader_positions.json -> % read and last-read date, into columns the
#                                 user picks in Settings (v1.2.0)
#
# v1.1.0: the sidecar now carries PER-ENTRY timestamps
# (`books[uuid] = {"value": bool, "updated_at": "<ISO-8601 UTC>"}`; legacy bare
# bools still work and inherit the file-level `updated_at`). The plugin no
# longer applies every entry blindly and forever — it keeps a per-book record
# of what it last applied and of the book's `last_modified` at that moment, so
# a stale entry can't re-apply, and a read-flag edit made in calibre after the
# sidecar entry isn't reverted. All of that logic lives in sidecar_merge.py,
# which imports nothing from calibre and is unit-tested standalone:
#     python3 sidecar_merge.py
#
# v1.1.1: GPL v3 licence; minimum calibre raised to 9.0 (the only line it is tested on).
#
# v1.2.0: reading progress. % read and last read go into columns chosen in a
# settings dialog (config.py), saved in the library's metadata.db preferences.
# Positions under a threshold (1 % by default) are ignored. One run is
# sync_core.run_sync (no GUI; `python3 sync_core.py` runs its tests). Also:
# a wrong-type #read_status is now refused, and a missing one no longer stops
# progress from syncing.
#
# Installation:
#   1. Zip __init__.py, config.py, sync_core.py, sidecar_merge.py,
#      plugin-import-name-calibreader_sync.txt, images/icon.png and LICENSE
#      → CalibReaderSync.zip (files at the zip root, not inside a subfolder)
#   2. Calibre → Preferences → Plugins → Load plugin from file → select zip
#   3. Restart Calibre. Add "Sync Read Status" to the toolbar if desired.
#
# Read status needs this custom column in Calibre:
#   Column lookup name : read_status   (creates #read_status)
#   Column type        : Yes/No
#   Column heading     : Read Status
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

__license__   = 'GPL v3'
__copyright__ = '2026, CalibReader'

from calibre.customize import InterfaceActionBase


class CalibReaderSyncPlugin(InterfaceActionBase):
    '''
    Wrapper plugin for CalibReader Sync.
    The actual toolbar action is CalibReaderSyncAction defined below.
    '''
    name                  = 'CalibReader Sync'
    description           = ('Sync read status, % read and last-read date from the CalibReader '
                             'mobile apps')
    supported_platforms   = ['windows', 'osx', 'linux']
    author                = 'CalibReader'
    version               = (1, 2, 0)
    minimum_calibre_version = (9, 0, 0)

    #: Points Calibre at the InterfaceAction class in this same module.
    actual_plugin = 'calibre_plugins.calibreader_sync:CalibReaderSyncAction'

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre.gui2.ui import get_gui
        gui = get_gui()
        if gui is None or getattr(gui, 'current_db', None) is None:
            return ('Open calibre first',
                    'The settings belong to a library, so open them from calibre\'s main window.')
        from calibre_plugins.calibreader_sync.config import ConfigWidget
        return ConfigWidget(gui)

    def save_settings(self, config_widget):
        config_widget.save_settings()


# ---------------------------------------------------------------------------
# Actual GUI action — only imported when the GUI is running.
# ---------------------------------------------------------------------------

import importlib
import sys

from calibre.gui2.actions import InterfaceAction
from calibre.gui2 import error_dialog, info_dialog
from calibre.utils.config import JSONConfig
from qt.core import QMenu, QToolButton

# Installing a new version over an old one in a running calibre reloads ONLY
# this module (calibre.customize.zipplugin.load); the old submodules stay in
# sys.modules, and the new code then imports names the old ones lack (the
# 1.1.1 -> 1.2.0 upgrade failed with "cannot import name 'Target'"). Reload
# them from the new zip first, dependencies before their users.
for _name in ('sidecar_merge', 'sync_core', 'config'):
    _module = sys.modules.get('calibre_plugins.calibreader_sync.' + _name)
    if _module is not None:
        importlib.reload(_module)

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
        'Sync read status and reading progress from the CalibReader mobile app',
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

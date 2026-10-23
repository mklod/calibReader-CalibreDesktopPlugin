# Last modified: 2026-10-22--2048
#
# CalibReader Sync — Calibre plugin
# Reads the sidecars the CalibReader apps keep in the Calibre library folder
# (synced by Dropbox, Google Drive, OneDrive or a network share) and writes
# them into the library:
#   CalibReader_sync.json      -> read status, into a Yes/No column the user
#                                 picks in Settings (v1.3.0; #read_status by default)
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
# v1.2.1: the toolbar action moved to ui.py, so this module imports no GUI code
# and non-GUI calibre processes (calibredb, calibre-server, ebook-convert) no
# longer load Qt because the plugin is installed.
#
# v1.2.2: when #read_status is missing but the library has a Yes/No column that
# looks like a read flag (#read, #finished...), the summary and Settings name it
# and explain how to copy it into #read_status once (sync_core.read_column_copy_hint).
#
# v1.2.3: a calibre-side edit of the read flag is detected by its VALUE changing,
# not by last_modified (which moves on every metadata edit), so a rating or tag
# edit no longer drops a pending phone toggle (sidecar_merge.decide_flag).
#
# v1.2.4: when a calibre edit of the read flag beats a phone entry, calibre's
# value is written into CalibReader_sync.json (sync_core.write_back_flags, the
# apps' sidecar write protocol), so the app follows it; a future last_modified
# is capped at now.
#
# v1.2.5: automatic sync (ui.py): after calibre starts or a library opens, and
# when the app's sync files change (checked every 5 minutes); status-bar note,
# no pop-ups; a Settings switch, on by default.
#
# v1.3.0: the user picks the read status column in Settings (any Yes/No column;
# "Create column…" through calibre's dialog). Saved as "read_column" in the
# library's metadata.db preferences, which the apps (1.0.1+) follow; a missing or
# non-Yes/No choice falls back to #read_status with a note. The read-flag memory
# is kept per column. Each entry the plugin applies is stamped with
# "applied_value" + "applied_at" in CalibReader_sync.json, so the app can tell a
# later calibre edit from the plugin's own write (sync_core.update_sync_file).
#
# Installation:
#   1. Zip __init__.py, ui.py, config.py, sync_core.py, sidecar_merge.py,
#      plugin-import-name-calibreader_sync.txt, images/icon.png and LICENSE
#      → CalibReaderSync.zip (files at the zip root, not inside a subfolder)
#   2. Calibre → Preferences → Plugins → Load plugin from file → select zip
#   3. Restart Calibre. Add "Sync Read Status" to the toolbar if desired.
#
# Read status needs a Yes/No custom column in Calibre: pick yours in Settings,
# or create one there. Without a choice the plugin uses:
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
    The actual toolbar action is CalibReaderSyncAction in ui.py (GUI only).
    '''
    name                  = 'CalibReader Sync'
    description           = ('Sync read status, % read and last-read date from the CalibReader '
                             'app for iPhone and iPad')
    supported_platforms   = ['windows', 'osx', 'linux']
    author                = 'CalibReader'
    version               = (1, 3, 0)
    minimum_calibre_version = (9, 0, 0)

    #: Points Calibre at the InterfaceAction class, in ui.py so that only the GUI imports it.
    actual_plugin = 'calibre_plugins.calibreader_sync.ui:CalibReaderSyncAction'

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
# Upgrades inside a running calibre.
# ---------------------------------------------------------------------------
# Installing a new version over an old one in a running calibre reloads ONLY
# this module (calibre.customize.zipplugin.load); the old submodules stay in
# sys.modules, and new code would then import names the old ones lack (the
# 1.1.1 -> 1.2.0 upgrade failed with "cannot import name 'Target'"). Reload the
# ones already loaded from the new zip, dependencies before their users. A
# module that isn't loaded yet is left alone, so a non-GUI calibre process
# still never imports ui.py (or Qt) because of this plugin.

import importlib
import sys

for _name in ('sidecar_merge', 'sync_core', 'config', 'ui'):
    _module = sys.modules.get('calibre_plugins.calibreader_sync.' + _name)
    if _module is not None:
        importlib.reload(_module)

# Last modified: 2026-11-05--1000
#
# config.py — the plugin's settings dialog (Preferences > Plugins > CalibReader
# Sync > Customize, or the "Settings…" item in the toolbar button's menu).
#
# The user picks which calibre columns receive read status (1.3.0), % read and
# last read. Nothing is written until they click OK. A column is created only
# through calibre's own column dialog, after the user presses "Create column…"
# (the dialog is where they confirm it), and calibre must restart before it can
# be used. Settings are stored in the library's metadata.db preferences
# (sync_core.SETTINGS_KEY), so they follow the library to other computers and
# to the iPhone/iPad app, which reads the same read column (the Android app reads
# #read_status only).
#
# 1.4.0 (tester preview): the Kobo section keeps Calibre-Web's app.db path in the
# plugin's own prefs (ui.PREFS['kobo'], per library folder): it is this computer's.
# Off unless a path is set.
#
# 1.5.0 (tester preview): "Also send CalibReader's place to the Kobo", unchecked
# by default (nothing is ever sent while it is off), and Calibre-Web's address
# (default http://127.0.0.1:8083). Both in the same per-library Kobo prefs.
#
# 1.6.0 (tester preview): "Keep syncing when calibre is closed (macOS, tester preview)",
# unchecked by default. OK with it ticked sets up the background job (kobo_service.install:
# a copy of the Kobo modules, a launchd LaunchAgent for this library, loaded at once);
# unticked (or the app.db path cleared) takes it down. A status line shows launchd's view
# and the job's last run. Stored as 'service' in the same per-library Kobo prefs.
# Review round 1 (S5): every background job on this Mac is listed with a Remove button,
# also one for a library calibre no longer shows.
#
# 1.7.0 (tester preview): "Kobo Utilities (USB)": the column Kobo Utilities stores the Kobo's
# reading location in (a chooser of text / long-text columns, never pre-selected, with a live
# preview of three values; a column where fewer than half of the values read as Kobo locations is
# refused), and optionally its % read and last-read columns (never CalibReader's own columns).
# Stored in the plugin's prefs per library folder (ui.PREFS['kobo_ku'], kobo_ku.prefs_entry): a
# list of sources, one today. Off while no location column is picked.
#
# 1.3.1: no read column is pre-selected or saved unless the user picks one (H1);
# a chosen column that can't be used means read status is not synced (M1); OK
# kicks a sync (L4); the wording says Android uses #read_status only (L3).
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

from __future__ import annotations

__license__   = 'GPL v3'
__copyright__ = '2026, CalibReader'

import os

from qt.core import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QLabel, QLineEdit, QPushButton, QSpinBox, Qt, QTimer,
    QVBoxLayout, QWidget,
)

try:
    from calibre_plugins.calibreader_sync.sync_core import (
        MAX_THRESHOLD, POSITIONS_FILE, READ_COLUMN, READ_TYPES, SETTINGS_KEY, eligible_columns,
        normalise_settings, read_column_hint, settings_to_store, suggest_column,
    )
except ImportError:  # pragma: no cover - standalone / test import
    from sync_core import (
        MAX_THRESHOLD, POSITIONS_FILE, READ_COLUMN, READ_TYPES, SETTINGS_KEY, eligible_columns,
        normalise_settings, read_column_hint, settings_to_store, suggest_column,
    )
try:
    from calibre_plugins.calibreader_sync.kobo_push import DEFAULT_CALIBRE_WEB, clean_address, now_iso
    from calibre_plugins.calibreader_sync import kobo_service
except ImportError:  # pragma: no cover - standalone / test import
    from kobo_push import DEFAULT_CALIBRE_WEB, clean_address, now_iso
    import kobo_service
try:
    from calibre_plugins.calibreader_sync import kobo_ku
except ImportError:  # pragma: no cover - standalone / test import
    import kobo_ku

# What "Create column…" proposes. The user can rename them in calibre's dialog.
NEW_COLUMNS = {
    'read': dict(lookup_name=READ_COLUMN, column_heading='Read Status', datatype='bool',
                 display={'description': 'Whether you have read the book, from CalibReader (Yes/No).'}),
    'percent': dict(lookup_name='#read_percent', column_heading='Read %', datatype='int',
                    display={'number_format': '{0:d}%',
                             'description': 'How far you have read the book in CalibReader (0-100).'}),
    'last_read': dict(lookup_name='#last_read', column_heading='Last Read', datatype='datetime',
                      display={'description': 'When you last read the book in CalibReader.'}),
}


class ConfigWidget(QWidget):

    def __init__(self, gui):
        QWidget.__init__(self)
        self.gui = gui
        self.db = gui.current_db.new_api
        self.custom = self.db.field_metadata.custom_field_metadata()
        raw = self.db.pref(SETTINGS_KEY, None)
        settings = normalise_settings(raw)
        never_saved = settings['version'] is None
        read_chosen = isinstance(raw, dict) and 'read_column' in raw
        self.combos = {}
        self.new_columns = set()                # created in this dialog, usable after a restart

        v = QVBoxLayout(self)

        self.restart_label = self._text('')
        self.restart_label.setVisible(False)
        v.addWidget(self.restart_label)

        v.addWidget(self._heading('Read status'))
        v.addWidget(self._text(
            'CalibReader marks books read or unread in a Yes/No column. Pick the one you already '
            'use, or create one. The CalibReader app for iPhone and iPad (1.0.1 or later) reads '
            'the same column, and read status goes both ways. The Android app uses #read_status '
            'only: with another column picked here, its marks still reach calibre, but it does '
            'not see read status changed in calibre.'))
        grid = QGridLayout()
        # 1.3.1 (review H1): until the user has chosen, the combo shows #read_status
        # and NOTHING else is pre-selected; a likely column of their own is only named
        # in the note below. OK saves a read column only when one was chosen before or
        # the user picked one here (self.read_chosen / self.read_initial).
        self.read_chosen = read_chosen
        self.read_initial = settings['read_column'] if read_chosen else READ_COLUMN
        self._picker(grid, 0, 'read', 'Read status column:', self.read_initial)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        self.read_note = self._text('')
        v.addWidget(self.read_note)
        self.combos['read'].currentIndexChanged.connect(self._update_read_note)
        self._update_read_note()

        v.addWidget(self._line())
        v.addWidget(self._heading('Reading progress'))
        v.addWidget(self._text(
            'CalibReader can copy how far you have read each book, and when you last read it, '
            'into calibre columns you choose. It reads {} in this library\'s folder. It only '
            'goes one way, from the app to calibre.'.format(POSITIONS_FILE)))

        grid = QGridLayout()
        for row, (kind, label) in enumerate((('percent', '% read column:'),
                                             ('last_read', 'Last read column:'))):
            stored = settings['percent_column' if kind == 'percent' else 'last_read_column']
            if never_saved and not stored:
                stored = suggest_column(self.custom, kind)
            self._picker(grid, row, kind, label, stored)

        self.threshold = QSpinBox(self)
        self.threshold.setRange(0, MAX_THRESHOLD)
        self.threshold.setSuffix(' % read')
        self.threshold.setValue(int(round(settings['progress_threshold'])))
        self.threshold.setToolTip('Books under this are left alone, so opening a book to look '
                                  'at it does not count as reading it.')
        grid.addWidget(QLabel('Skip books under:', self), 2, 0)
        grid.addWidget(self.threshold, 2, 1)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)

        v.addWidget(self._line())
        v.addWidget(self._heading('Automatic sync'))
        self.auto_sync = QCheckBox('Sync automatically', self)
        self.auto_sync.setChecked(settings['auto_sync'])
        v.addWidget(self.auto_sync)
        v.addWidget(self._text(
            'When calibre starts, when you open this library, and when the app\'s files in the '
            'library folder change (checked every 5 minutes). A short note appears in the status '
            'bar; nothing pops up. The Sync Read Status button still works any time.'))

        v.addWidget(self._text(
            'These settings are saved in this library, so they follow it to other computers. '
            'Every value the plugin writes into the library is recorded, with the value it '
            'replaced, in its change log (the arrow next to Sync Read Status, then Change log…). '
            'The first sync into a read column you pick asks you first, with the number of '
            'cells it will fill.'))

        v.addWidget(self._line())
        v.addWidget(self._heading('Kobo via Calibre-Web (tester preview)'))
        v.addWidget(self._text(
            'TESTER PREVIEW: use it only with the CalibReader test build on every iPhone or '
            'iPad you read on. If your Kobo syncs with Calibre-Web, CalibReader can open your '
            'books where you stopped on the Kobo. Point this at Calibre-Web\'s app.db (it is '
            'only read, never changed). Off while the path is empty. The place goes into {} '
            'for the app. Exact to the sentence when Calibre-Web sends KEPUBs (kepubify), '
            'otherwise within a page or two.'.format(POSITIONS_FILE)))
        grid = QGridLayout()
        kobo = self._kobo_prefs().get(self.gui.library_path) or {}
        self.kobo_db = QLineEdit(kobo.get('app_db', ''), self)
        self.kobo_db.setPlaceholderText('Not set: Kobo places are not imported')
        browse = QPushButton('Choose…', self)
        browse.clicked.connect(self.choose_app_db)
        grid.addWidget(QLabel('Calibre-Web app.db:', self), 0, 0)
        grid.addWidget(self.kobo_db, 0, 1)
        grid.addWidget(browse, 0, 2)
        self.kobo_user = QLineEdit(kobo.get('user', ''), self)
        self.kobo_user.setPlaceholderText('Whose Kobo CalibReader follows (needed when Calibre-Web has several users)')
        grid.addWidget(QLabel('Calibre-Web user:', self), 1, 0)
        grid.addWidget(self.kobo_user, 1, 1)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        # 1.5.0: the other way, off unless the user ticks it.
        self.kobo_push = QCheckBox('Also send CalibReader\'s place to the Kobo (tester preview)', self)
        self.kobo_push.setChecked(kobo.get('push') is True)
        v.addWidget(self.kobo_push)
        v.addWidget(self._text(
            'When you have read further in CalibReader than on the Kobo, calibre sends that place to '
            'Calibre-Web, which hands it to the Kobo at its next sync. Only forward: never a place behind '
            'the Kobo\'s. It goes through Calibre-Web\'s own Kobo connection at the address below '
            '(app.db is still only read). Only places you read after ticking this box are sent. To the '
            'sentence when the book has a KEPUB in this library; otherwise the Kobo gets the percentages only.'))
        grid = QGridLayout()
        self.kobo_url = QLineEdit(kobo.get('cw_url', ''), self)
        self.kobo_url.setPlaceholderText(DEFAULT_CALIBRE_WEB)
        grid.addWidget(QLabel('Calibre-Web address:', self), 0, 0)
        grid.addWidget(self.kobo_url, 0, 1)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        # 1.6.0: the same Kobo work with calibre closed (a launchd job), off unless ticked.
        mac = kobo_service.supported()
        self.kobo_service = QCheckBox('Keep syncing when calibre is closed (macOS, tester preview)', self)
        self.kobo_service.setChecked(mac and kobo.get('service') is True)
        self.kobo_service.setEnabled(mac)
        v.addWidget(self.kobo_service)
        v.addWidget(self._text(
            'calibre does the Kobo work above only while it is running. Tick this and a small background job '
            'does the same with calibre closed: every 5 minutes, and soon after Calibre-Web records a Kobo sync. '
            'It uses calibre\'s own Python and these settings (untick "Also send…" and it stops sending too). '
            'Calibre-Web must keep running. Log: ~/Library/Logs/CalibReaderSync/kobo.log.'
            if mac else 'macOS only for now.'))
        self.kobo_service_status = self._text(
            kobo_service.status_text(self.gui.library_path) if mac else '')
        self.kobo_service_status.setVisible(mac)
        v.addWidget(self.kobo_service_status)
        if mac:
            self._job_rows(v)
        self._ku_group(v, settings)
        v.addWidget(self._text('Saved on this computer only, for this library.'))
        v.addStretch(1)

    # -- Kobo Utilities (USB), 1.7.0 tester preview -------------------------------------------

    def _ku_group(self, v, settings):
        v.addWidget(self._line())
        v.addWidget(self._heading('Kobo Utilities (USB) (tester preview)'))
        v.addWidget(self._text(
            'TESTER PREVIEW: use it only with the CalibReader test build on every iPhone or iPad you read on. '
            'If you connect your Kobo to calibre by USB and the Kobo Utilities plugin stores its reading '
            'positions in calibre columns ("Store current reading position", or its auto-store on connect), '
            'CalibReader can open your books where you stopped on the Kobo. Pick the column Kobo Utilities keeps '
            'the reading location in: it is only read, never changed. Off until you pick one. To the sentence '
            'when the Kobo stored its span, otherwise the chapter. The % read and last-read columns are optional; '
            'pick the last-read one for the right order between the Kobo and the app. Tested with calibre 9.6 '
            'and 9.14.'))
        # Never one of CalibReader's own columns (the plugin writes those).
        mine = {settings['read_column'], settings['percent_column'], settings['last_read_column']} - {''}
        stored = kobo_ku.source_from_prefs(self._ku_prefs().get(self.gui.library_path)) or {}
        grid = QGridLayout()
        self.ku_location = self._ku_combo(kobo_ku.LOCATION_TYPES, mine, stored.get('location'), 'Don\'t use (off)')
        self.ku_percent = self._ku_combo(kobo_ku.PERCENT_TYPES, mine, stored.get('percent'), 'None')
        self.ku_last_read = self._ku_combo(kobo_ku.DATE_TYPES, mine, stored.get('last_read'), 'None')
        for row, (label, combo) in enumerate((('Kobo Utilities location column:', self.ku_location),
                                              ('Kobo % read column:', self.ku_percent),
                                              ('Kobo last read column:', self.ku_last_read))):
            grid.addWidget(QLabel(label, self), row, 0)
            grid.addWidget(combo, row, 1)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        self.ku_preview = self._text('')
        self.ku_preview.setTextFormat(Qt.TextFormat.PlainText)
        v.addWidget(self.ku_preview)
        self.ku_status = self._text('')
        self.ku_status.setTextFormat(Qt.TextFormat.PlainText)
        v.addWidget(self.ku_status)
        self._ku_check = None
        self.ku_location.currentIndexChanged.connect(self._update_ku_preview)
        self._update_ku_preview()
        self._update_ku_status(stored)

    def _ku_prefs(self):
        prefs = self._prefs()
        return dict(prefs.get('kobo_ku', {}) or {}) if prefs is not None else {}

    def _ku_combo(self, types, exclude, selected, none_text):
        combo = QComboBox(self)
        combo.addItem(none_text, '')
        for lookup, heading in kobo_ku.eligible(self.custom, types, exclude):
            combo.addItem('{} ({})'.format(heading, lookup), lookup)
        if selected:
            index = combo.findData(selected)
            if index < 0:
                combo.addItem('{} (not usable for this here)'.format(selected), selected)
                index = combo.count() - 1
            combo.setCurrentIndex(index)
        return combo

    def _update_ku_preview(self, *args):
        '''8.2: three values of the picked column as they read, and the < 50 % refusal.'''
        lookup = self.ku_location.currentData()
        self._ku_check = None
        if not lookup:
            self.ku_preview.setText('Off: no Kobo Utilities location column picked.')
            return
        try:
            ids = sorted(self.db.all_book_ids())
            values = self.db.all_field_for(lookup, ids)
        except Exception as exc:
            self.ku_preview.setText('{} cannot be read ({}).'.format(lookup, exc))
            self._ku_check = {'ok': False, 'message': '{} cannot be read.'.format(lookup)}
            return
        filled = [values[i] for i in ids if isinstance(values.get(i), str) and values[i].strip()]
        self._ku_check = kobo_ku.column_check(filled)
        lines = [kobo_ku.describe(value) for value in filled[:3]]
        self.ku_preview.setText('\n'.join(lines + [self._ku_check['message']]))

    def _update_ku_status(self, source):
        '''8.6: when Kobo Utilities last stored positions, and a hint when that is long ago.'''
        if not source:
            self.ku_status.setText('')
            self.ku_status.setVisible(False)
            return
        try:
            values = kobo_ku.read_columns_db(self.db, source)
        except Exception as exc:
            self.ku_status.setText(str(exc))
            return
        from calibre.constants import config_dir
        memory = kobo_ku.MemoryFile(kobo_ku.memory_path(config_dir)).load(
            self.gui.library_path, kobo_ku.source_signature(source))
        last = kobo_ku.last_store_seen(values, memory)
        prefs = self._prefs()
        hint = kobo_ku.staleness_hint(last, (prefs.get('kobo_ku_device_seen') if prefs is not None else None) or None)
        self.ku_status.setText(kobo_ku.store_line(last) + ((' ' + hint) if hint else ''))

    def _show_ku_error(self, message):
        from calibre.gui2 import error_dialog
        error_dialog(self, 'CalibReader Sync: Kobo Utilities', message, show=True)

    def save_ku(self):
        PREFS = self._prefs()
        if PREFS is None:  # pragma: no cover
            return
        library = self.gui.library_path
        location = self.ku_location.currentData() or ''
        percent = self.ku_percent.currentData() or ''
        last_read = self.ku_last_read.currentData() or ''
        table = dict(PREFS.get('kobo_ku', {}) or {})
        if location:
            problem = None
            if self._ku_check is not None and not self._ku_check.get('ok'):
                problem = self._ku_check.get('message')
            mine = {self.combos[k].currentData() for k in ('read', 'percent', 'last_read')} - {'', None}
            clash = sorted(mine & {location, percent, last_read})
            if clash:
                problem = ('{} is one of CalibReader\'s own columns (the plugin writes it): pick Kobo Utilities\' '
                           'columns instead.'.format(', '.join(clash)))
            if problem:
                self._show_ku_error(problem + ' Kobo Utilities places are not imported with this column; the '
                                    'Kobo Utilities settings were not changed.')
                return
            table[library] = kobo_ku.prefs_entry(location, percent, last_read)
        else:
            table.pop(library, None)
        PREFS['kobo_ku'] = table                # assignment commits a JSONConfig

    def _job_rows(self, v):
        '''1.6.0 (review S5): every CalibReader background job on this Mac, for any library,
        including one calibre no longer shows (renamed, moved or removed), each with Remove.'''
        jobs = kobo_service.installed_jobs()
        if not jobs:
            return
        v.addWidget(self._text('Background jobs on this Mac:'))
        grid = QGridLayout()
        for row, job in enumerate(jobs):
            state = ('folder not found: renamed or moved in calibre?' if not job['library_exists']
                     else ('on' if job['loaded'] else 'not loaded'))
            label = self._text('{} ({})'.format(job['library'] or job['label'], state))
            button = QPushButton('Remove', self)
            button.setToolTip('Stop and remove this background job ({}).'.format(job['label']))
            button.clicked.connect(lambda checked=False, j=job, l=label, b=button: self._remove_job(j, l, b))
            grid.addWidget(label, row, 0)
            grid.addWidget(button, row, 1)
        grid.setColumnStretch(0, 1)
        v.addLayout(grid)

    def _remove_job(self, job, label, button):
        ok, message = kobo_service.remove_job(job['label'])
        if not ok:
            self._show_error(message)
            return
        PREFS = self._prefs()
        if PREFS is not None and job['library']:
            table = dict(PREFS.get('kobo', {}) or {})
            entry = table.get(job['library'])
            if isinstance(entry, dict) and entry.get('service') is True:
                entry = dict(entry)
                entry['service'] = False
                table[job['library']] = entry
                PREFS['kobo'] = table
        if job['library'] == self.gui.library_path:
            self.kobo_service.setChecked(False)
        label.setText('{} (removed)'.format(job['library'] or job['label']))
        button.setEnabled(False)

    # -- building blocks ------------------------------------------------------

    def _heading(self, text):
        return QLabel('<b>{}</b>'.format(text), self)

    def _text(self, text):
        label = QLabel(text, self)
        label.setWordWrap(True)
        return label

    def _line(self):
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        return line

    def _picker(self, grid, row, kind, label, selected):
        combo = self._combo(kind, selected)
        button = QPushButton('Create column…', self)
        button.setToolTip('Opens calibre\'s own dialog to add a column for this. '
                          'calibre must restart before the new column can be used.')
        button.clicked.connect(lambda checked=False, k=kind: self.create_column(k))
        grid.addWidget(QLabel(label, self), row, 0)
        grid.addWidget(combo, row, 1)
        grid.addWidget(button, row, 2)
        self.combos[kind] = combo

    def _combo(self, kind, selected):
        combo = QComboBox(self)
        if kind != 'read':                      # read status always has a column
            combo.addItem('Don\'t sync', '')
        for lookup, heading in eligible_columns(self.custom, kind):
            combo.addItem('{} ({})'.format(heading, lookup), lookup)
        if selected:
            index = combo.findData(selected)
            if index < 0:
                # Configured, but not a usable column of this library (yet): keep it
                # visible rather than silently switching the user to something else.
                meta = self.custom.get(selected)
                if isinstance(meta, dict):
                    text = '{} ({} column: it can\'t be used for this)'.format(selected, meta.get('datatype'))
                else:
                    text = ('{} (not in this library yet; restart calibre if you just created '
                            'it)'.format(selected))
                combo.addItem(text, selected)
                index = combo.count() - 1
            combo.setCurrentIndex(index)
        return combo

    def _update_read_note(self, *args):
        # 1.3.1 (review M1): a chosen column that can't be used means read status is
        # not synced at all; it never goes into #read_status instead.
        lookup = self.combos['read'].currentData()
        meta = self.custom.get(lookup)
        if isinstance(meta, dict) and meta.get('datatype') in READ_TYPES:
            text = 'Read status goes into {} ({}).'.format(meta.get('name') or lookup, lookup)
            if not self.read_chosen and lookup == READ_COLUMN:
                hint = read_column_hint(self.custom, exclude=(lookup,), where='above')
                if hint:
                    text += '\n\n' + hint
        elif isinstance(meta, dict):
            text = ('{} is not a Yes/No column, so read status is not synced. Pick a Yes/No '
                    'column.'.format(lookup))
        elif lookup in self.new_columns:
            text = ('{} can be used once calibre restarts. Until then read status is not synced '
                    '(it is not written into any other column).'.format(lookup))
        else:
            text = ('{} is not in this library. Pick another column, or create one with Create '
                    'column…. Until then read status is not synced.'.format(lookup))
            hint = read_column_hint(self.custom, exclude=(lookup, READ_COLUMN), where='above')
            if hint:
                text += '\n\n' + hint
        self.read_note.setText(text)

    # -- actions --------------------------------------------------------------

    def create_column(self, kind):
        from calibre.gui2 import error_dialog, info_dialog
        from calibre.gui2.preferences.create_custom_column import CreateNewCustomColumn

        creator = CreateNewCustomColumn(self.gui)
        if creator.must_restart():
            info_dialog(self, 'Restart calibre first',
                        'calibre has to restart before another column can be added. Restart it, '
                        'then come back here.', show=True)
            return
        spec = NEW_COLUMNS[kind]
        result, message = creator.create_column(
            spec['lookup_name'], spec['column_heading'], spec['datatype'], False,
            display=dict(spec['display']), generate_unused_lookup_name=True,
            freeze_lookup_name=False)
        if result == creator.Result.COLUMN_ADDED:
            self.new_columns.add(message)
            combo = self.combos[kind]
            combo.addItem('{} (new: restart calibre to use it)'.format(message), message)
            combo.setCurrentIndex(combo.count() - 1)
            self.restart_label.setText(
                '<b>Click OK to keep this choice, then restart calibre.</b> The new column '
                'exists only after the restart; until then the sync skips it.')
            self.restart_label.setVisible(True)
        elif result != creator.Result.CANCELED:
            error_dialog(self, 'Could not create the column', message, show=True)

    def chosen_read_column(self):
        '''The read column to save, or None: nothing is saved on the user's behalf
        (1.3.1) unless they chose a column before or picked one in this dialog.'''
        lookup = self.combos['read'].currentData()
        if self.read_chosen or lookup != self.read_initial or lookup in self.new_columns:
            return lookup
        return None

    def settings(self):
        return settings_to_store(self.combos['percent'].currentData(),
                                 self.combos['last_read'].currentData(),
                                 self.threshold.value(),
                                 self.auto_sync.isChecked(),
                                 read_column=self.chosen_read_column())

    # -- Kobo (1.4.0, tester preview) -------------------------------------------

    def _prefs(self):
        try:
            from calibre_plugins.calibreader_sync.ui import PREFS
        except ImportError:  # pragma: no cover - tests under calibre-debug load ui directly
            try:
                from ui import PREFS
            except ImportError:
                return None
        return PREFS

    def _kobo_prefs(self):
        prefs = self._prefs()
        return dict(prefs.get('kobo', {}) or {}) if prefs is not None else {}

    def choose_app_db(self):
        from calibre.gui2 import choose_files
        picked = choose_files(self, 'calibreader-sync-kobo-app-db', 'Calibre-Web app.db',
                              filters=[('Calibre-Web database', ['db'])], all_files=True, select_only_single_file=True)
        if picked:
            self.kobo_db.setText(picked[0])

    def save_kobo(self):
        PREFS = self._prefs()
        if PREFS is None:  # pragma: no cover
            return
        table = dict(PREFS.get('kobo', {}) or {})
        path = self.kobo_db.text().strip()
        if path:
            before = table.get(self.gui.library_path) or {}
            push = bool(self.kobo_push.isChecked())
            entry = {'app_db': path, 'user': self.kobo_user.text().strip(), 'push': push,
                     # N7: a pasted Kobo sync link (…/kobo/<token>) is cut to the address
                     'cw_url': clean_address(self.kobo_url.text())[0]}
            if push:
                # B3: sending starts when the box goes from off to on; OK again keeps the time.
                kept = before.get('push_since') if before.get('push') is True else None
                entry['push_since'] = kept or now_iso()
            service = bool(self.kobo_service.isChecked() and kobo_service.supported())
            entry['service'] = service
            table[self.gui.library_path] = entry
        else:
            service, entry = False, None
            before = table.pop(self.gui.library_path, None) or {}
        PREFS['kobo'] = table                   # assignment commits a JSONConfig
        self._save_service(PREFS, entry, service, before.get('service') is True)

    def _save_service(self, PREFS, entry, service, was):
        '''1.6.0: set the background job up (ticked), or take it down (unticked, or Kobo
        sync switched off). The job reads the settings just saved, so they go first.'''
        library = self.gui.library_path
        if service:
            from calibre.constants import config_dir
            try:
                from calibre_plugins.calibreader_sync.ui import resource_reader
            except ImportError:  # pragma: no cover - tests under calibre-debug load ui directly
                from ui import resource_reader
            ok, message = kobo_service.install(library, config_dir, entry['app_db'], resource_reader())
            if not ok:
                table = dict(PREFS.get('kobo', {}) or {})
                saved = dict(table.get(library) or entry)
                saved['service'] = False
                table[library] = saved
                PREFS['kobo'] = table
                self._show_error(message)
        elif was or os.path.exists(kobo_service.plist_path(kobo_service.label_for(library))):
            ok, message = kobo_service.uninstall(library)
            if not ok:
                self._show_error(message)

    def _show_error(self, message):
        from calibre.gui2 import error_dialog
        error_dialog(self, 'CalibReader Sync: background job',
                     'Keeping the Kobo in sync with calibre closed could not be {}.\n\n{}'.format(
                         'set up' if self.kobo_service.isChecked() else 'turned off', message), show=True)

    def save_settings(self):
        self.save_kobo()
        self.save_ku()
        before = normalise_settings(self.db.pref(SETTINGS_KEY, None))
        after = self.settings()
        self.db.set_pref(SETTINGS_KEY, after)
        # 1.3.1 (review L4): sync once the dialog has closed. A newly picked read column
        # that exists gets the button's run, which asks before its first sync fills it;
        # anything else an automatic run (status bar only).
        action = (getattr(self.gui, 'iactions', None) or {}).get('CalibReader Sync')
        if action is None:
            return
        column = after.get('read_column')
        usable = (self.custom.get(column) or {}).get('datatype') in READ_TYPES
        if column and usable and (not before['read_column_set'] or column != before['read_column']):
            QTimer.singleShot(0, action.sync_read_status)
        else:
            QTimer.singleShot(0, lambda: action.auto_sync('settings'))

# Last modified: 2026-10-21--2257
#
# config.py — the plugin's settings dialog (Preferences > Plugins > CalibReader
# Sync > Customize, or the "Settings…" item in the toolbar button's menu).
#
# The user picks which calibre columns receive % read and last read. Nothing
# is written until they click OK. A column is created only through calibre's
# own column dialog, after the user presses "Create column…" (the dialog is
# where they confirm it), and calibre must restart before it can be used.
# Settings are stored in the library's metadata.db preferences
# (sync_core.SETTINGS_KEY), so they follow the library to other computers.
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

from __future__ import annotations

__license__   = 'GPL v3'
__copyright__ = '2026, CalibReader'

from qt.core import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

try:
    from calibre_plugins.calibreader_sync.sync_core import (
        MAX_THRESHOLD, POSITIONS_FILE, READ_COLUMN, SETTINGS_KEY, eligible_columns,
        normalise_settings, read_column_copy_hint, settings_to_store, suggest_column,
    )
except ImportError:  # pragma: no cover - standalone / test import
    from sync_core import (
        MAX_THRESHOLD, POSITIONS_FILE, READ_COLUMN, SETTINGS_KEY, eligible_columns,
        normalise_settings, read_column_copy_hint, settings_to_store, suggest_column,
    )

# What "Create column…" proposes. The user can rename both in calibre's dialog.
NEW_COLUMNS = {
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
        settings = normalise_settings(self.db.pref(SETTINGS_KEY, None))
        never_saved = settings['version'] is None
        self.restart_note = None

        v = QVBoxLayout(self)

        v.addWidget(self._heading('Reading progress'))
        v.addWidget(self._text(
            'CalibReader can copy how far you have read each book, and when you last read it, '
            'into calibre columns you choose. It reads {} in this library\'s folder. It only '
            'goes one way, from the app to calibre.'.format(POSITIONS_FILE)))

        grid = QGridLayout()
        self.combos = {}
        for row, (kind, label) in enumerate((('percent', '% read column:'),
                                             ('last_read', 'Last read column:'))):
            stored = settings['percent_column' if kind == 'percent' else 'last_read_column']
            if never_saved and not stored:
                stored = suggest_column(self.custom, kind)
            combo = self._combo(kind, stored)
            button = QPushButton('Create column…', self)
            button.setToolTip('Opens calibre\'s own dialog to add a column for this. '
                              'calibre must restart before the new column can be used.')
            button.clicked.connect(lambda checked=False, k=kind: self.create_column(k))
            grid.addWidget(QLabel(label, self), row, 0)
            grid.addWidget(combo, row, 1)
            grid.addWidget(button, row, 2)
            self.combos[kind] = combo

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

        self.restart_label = self._text('')
        self.restart_label.setVisible(False)
        v.addWidget(self.restart_label)

        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        v.addWidget(line)

        v.addWidget(self._heading('Read status'))
        meta = self.custom.get(READ_COLUMN)
        if meta is not None and meta.get('datatype') == 'bool':
            status = 'Read status goes into {} (Yes/No). This library has it.'.format(READ_COLUMN)
        elif meta is not None:
            status = ('{} is a {} column. Read status needs a Yes/No column, so it is not '
                      'synced.'.format(READ_COLUMN, meta.get('datatype')))
        else:
            status = ('Read status goes into {0}, which this library does not have yet. Add it '
                      'in Preferences > Add your own columns: lookup name read_status, type '
                      'Yes/No. Then restart calibre.'.format(READ_COLUMN))
            hint = read_column_copy_hint(self.custom)
            if hint:
                status += '\n\n' + hint
        v.addWidget(self._text(status))

        line2 = QFrame(self)
        line2.setFrameShape(QFrame.Shape.HLine)
        v.addWidget(line2)
        v.addWidget(self._heading('Automatic sync'))
        self.auto_sync = QCheckBox('Sync automatically', self)
        self.auto_sync.setChecked(settings['auto_sync'])
        v.addWidget(self.auto_sync)
        v.addWidget(self._text(
            'When calibre starts, when you open this library, and when the app\'s files in the '
            'library folder change (checked every 5 minutes). A short note appears in the status '
            'bar; nothing pops up. The Sync Read Status button still works any time.'))

        v.addWidget(self._text(
            'These settings are saved in this library, so they follow it to other computers.'))
        v.addStretch(1)

    # -- building blocks ------------------------------------------------------

    def _heading(self, text):
        return QLabel('<b>{}</b>'.format(text), self)

    def _text(self, text):
        label = QLabel(text, self)
        label.setWordWrap(True)
        return label

    def _combo(self, kind, selected):
        combo = QComboBox(self)
        combo.addItem('Don\'t sync', '')
        for lookup, heading in eligible_columns(self.custom, kind):
            combo.addItem('{} ({})'.format(heading, lookup), lookup)
        if selected:
            index = combo.findData(selected)
            if index < 0:
                # Configured, but not a column of this library (yet): keep it
                # visible rather than silently switching the user to "Don't sync".
                combo.addItem('{} (not in this library; restart calibre if you just '
                              'created it)'.format(selected), selected)
                index = combo.count() - 1
            combo.setCurrentIndex(index)
        return combo

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
            combo = self.combos[kind]
            combo.addItem('{} (new: restart calibre to use it)'.format(message), message)
            combo.setCurrentIndex(combo.count() - 1)
            self.restart_label.setText(
                '<b>Click OK to keep this choice, then restart calibre.</b> The new column '
                'exists only after the restart; until then the sync skips it.')
            self.restart_label.setVisible(True)
        elif result != creator.Result.CANCELED:
            error_dialog(self, 'Could not create the column', message, show=True)

    def settings(self):
        return settings_to_store(self.combos['percent'].currentData(),
                                 self.combos['last_read'].currentData(),
                                 self.threshold.value(),
                                 self.auto_sync.isChecked())

    def save_settings(self):
        self.db.set_pref(SETTINGS_KEY, self.settings())

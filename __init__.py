# Last modified: 2026-09-15--0509
#
# CalibReader Sync — Calibre plugin
# Reads CalibReader_sync.json from the Calibre library folder (synced via
# Dropbox) and updates the #read_status custom column for each book whose UUID
# appears in the file.
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
# Installation:
#   1. Zip this file + sidecar_merge.py + plugin-import-name-calibreader_sync.txt
#      → CalibReaderSync.zip
#      (files must be at the zip root, not inside a subfolder)
#   2. Calibre → Preferences → Plugins → Load plugin from file → select zip
#   3. Restart Calibre. Add "Sync Read Status" to the toolbar if desired.
#
# Required custom column in Calibre:
#   Column lookup name : read_status   (creates #read_status)
#   Column type        : Yes/No
#   Column heading     : Read Status

from calibre.customize import InterfaceActionBase


class CalibReaderSyncPlugin(InterfaceActionBase):
    '''
    Wrapper plugin for CalibReader Sync.
    The actual toolbar action is CalibReaderSyncAction defined below.
    '''
    name                  = 'CalibReader Sync'
    description           = 'Sync read status from CalibReader mobile app via CalibReader_sync.json'
    supported_platforms   = ['windows', 'osx', 'linux']
    author                = 'CalibReader'
    version               = (1, 1, 0)
    minimum_calibre_version = (5, 0, 0)

    #: Points Calibre at the InterfaceAction class in this same module.
    actual_plugin = 'calibre_plugins.calibreader_sync:CalibReaderSyncAction'

    def is_customizable(self):
        return False


# ---------------------------------------------------------------------------
# Actual GUI action — only imported when the GUI is running.
# ---------------------------------------------------------------------------

from calibre.gui2.actions import InterfaceAction
from calibre.gui2 import error_dialog, info_dialog
from calibre.utils.config import JSONConfig
import json
import os

# Pure merge logic — no calibre imports, so it can be unit-tested outside the
# GUI. Inside calibre the plugin is loaded as the package `calibreader_sync`
# (see plugin-import-name-calibreader_sync.txt); the bare import is the
# fallback used when the module is exercised from this folder directly.
try:
    from calibre_plugins.calibreader_sync.sidecar_merge import (
        parse_sidecar, plan, load_applied, store_applied, parse_iso,
    )
except ImportError:  # pragma: no cover - standalone / test import
    from sidecar_merge import (
        parse_sidecar, plan, load_applied, store_applied, parse_iso,
    )

# Per-book bookkeeping lives in the plugin's OWN prefs, never in the user's
# library: adding a column to someone's library to track sync state is not
# ours to do, and this file has to survive switching libraries anyway.
PREFS = JSONConfig('plugins/calibreader_sync')
PREFS.defaults['applied'] = {}


class CalibReaderSyncAction(InterfaceAction):
    name = 'CalibReader Sync'
    action_spec = (
        'Sync Read Status',
        None,
        'Sync read status from CalibReader mobile app',
        None,
    )
    popup_type        = 0
    allowed_in_toolbar = True
    allowed_in_menu   = True

    def genesis(self):
        # get_icons is injected by Calibre's plugin loader at runtime.
        # It looks for the file inside the plugin zip's images/ directory.
        self.qaction.setIcon(get_icons('images/icon.png'))
        self.qaction.triggered.connect(self.sync_read_status)

    def sync_read_status(self):
        library_path = self.gui.library_path
        sync_file = os.path.join(library_path, 'CalibReader_sync.json')

        if not os.path.exists(sync_file):
            error_dialog(
                self.gui,
                'CalibReader Sync',
                (
                    'Sync file not found:\n{}\n\n'
                    'Make sure:\n'
                    '  1. Dropbox has finished syncing\n'
                    '  2. At least one book has been marked as read in the '
                    'CalibReader mobile app'
                ).format(sync_file),
                show=True,
            )
            return

        try:
            with open(sync_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as exc:
            error_dialog(
                self.gui, 'CalibReader Sync',
                'Failed to read sync file:\n{}'.format(exc),
                show=True,
            )
            return

        entries = parse_sidecar(data)
        if not entries:
            info_dialog(
                self.gui, 'CalibReader Sync',
                'No usable book entries found in CalibReader_sync.json.',
                show=True,
            )
            return

        db = self.gui.current_db.new_api

        # Verify the #read_status custom column exists.
        custom_columns = db.field_metadata.custom_field_metadata()
        if '#read_status' not in custom_columns:
            error_dialog(
                self.gui,
                'CalibReader Sync',
                (
                    'Custom column "#read_status" not found in this library.\n\n'
                    'Create it in Calibre → Preferences → Add your own columns:\n'
                    '  Column lookup name : read_status\n'
                    '  Column type        : Yes/No\n'
                    '  Column heading     : Read Status\n\n'
                    'Then restart Calibre and try again.'
                ),
                show=True,
            )
            return

        # Build UUID → book_id map (lowercased; sidecar keys are lowercased by
        # parse_sidecar, so matching stays case-insensitive as before).
        uuid_to_id = {}
        for book_id in db.all_book_ids():
            uuid = db.field_for('uuid', book_id)
            if uuid:
                uuid_to_id[uuid.lower()] = book_id

        applied = load_applied(PREFS.get('applied', {}))

        # Current state of every book the sidecar mentions AND this library has.
        book_state = {}
        for uuid in entries:
            book_id = uuid_to_id.get(uuid)
            if book_id is None:
                continue
            applied_stamp, applied_lm = applied.get(uuid, (None, None))
            book_state[uuid] = (
                self._read_status(db, book_id),
                applied_stamp,
                applied_lm,
                parse_iso(db.field_for('last_modified', book_id)),
            )

        updates_by_uuid, records, skipped, unmatched = plan(entries, book_state)

        if not updates_by_uuid:
            # Nothing to write, but RECORD verdicts still need persisting so a
            # retired entry is never reconsidered.
            self._persist(db, uuid_to_id, records)
            info_dialog(
                self.gui,
                'CalibReader Sync',
                self._summary(0, len(skipped), len(unmatched)),
                show=True,
            )
            return

        db.set_field(
            '#read_status',
            {uuid_to_id[uuid]: value for uuid, value in updates_by_uuid.items()},
        )
        # set_field bumps each book's last_modified; record the NEW value so
        # our own write is not mistaken for a desktop edit next time round.
        self._persist(db, uuid_to_id, records)
        self.gui.library_view.model().refresh()

        info_dialog(
            self.gui, 'CalibReader Sync',
            self._summary(len(updates_by_uuid), len(skipped), len(unmatched)),
            show=True,
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _read_status(db, book_id):
        '''Current #read_status as True / False / None (unset).'''
        value = db.field_for('#read_status', book_id)
        return None if value is None else bool(value)

    @staticmethod
    def _persist(db, uuid_to_id, records):
        '''Store, per book, the stamp we just honoured plus the book's
        last_modified as it stands AFTER the write.'''
        if not records:
            return
        blob = dict(PREFS.get('applied', {}))
        for uuid, entry in records.items():
            book_id = uuid_to_id.get(uuid)
            lm = parse_iso(db.field_for('last_modified', book_id)) if book_id else None
            store_applied(blob, uuid, entry.stamp, lm)
        PREFS['applied'] = blob   # assignment is what commits a JSONConfig

    @staticmethod
    def _summary(updated, skipped, unmatched):
        msg = 'Updated {} book(s).'.format(updated)
        if skipped:
            msg += ('\n\n{} entr(y/ies) were already applied, or older than '
                    'this library\'s own change — left alone.'.format(skipped))
        if unmatched:
            msg += ('\n\n{} UUID(s) were not found in this library.'
                    .format(unmatched))
        return msg

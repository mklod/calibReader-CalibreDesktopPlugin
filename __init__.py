# CalibReader Sync — Calibre plugin
# Reads CalibReader_sync.json from the Calibre library folder (synced via
# Dropbox) and updates the #read_status custom column for each book whose UUID
# appears in the file.
#
# Installation:
#   1. Zip this file + plugin-import-name-calibreader_sync.txt → CalibReaderSync.zip
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
    version               = (1, 0, 0)
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
import json
import os


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

        books_data = data.get('books', {})
        if not books_data:
            info_dialog(
                self.gui, 'CalibReader Sync',
                'No books found in CalibReader_sync.json.',
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

        # Build UUID → book_id map.
        uuid_to_id = {}
        for book_id in db.all_book_ids():
            uuid = db.field_for('uuid', book_id)
            if uuid:
                uuid_to_id[uuid.lower()] = book_id

        # Apply updates.
        updates  = {}
        not_found = 0
        for uuid, is_read in books_data.items():
            book_id = uuid_to_id.get(uuid.lower())
            if book_id is not None:
                updates[book_id] = bool(is_read)
            else:
                not_found += 1

        if not updates:
            info_dialog(
                self.gui,
                'CalibReader Sync',
                'No matching books found in this library.\n'
                '({} UUID(s) not matched — they may belong to a different '
                'library.)'.format(not_found),
                show=True,
            )
            return

        db.set_field('#read_status', updates)
        self.gui.library_view.model().refresh()

        msg = 'Updated {} book(s).'.format(len(updates))
        if not_found:
            msg += '\n\n{} UUID(s) were not found in this library.'.format(not_found)
        info_dialog(self.gui, 'CalibReader Sync', msg, show=True)

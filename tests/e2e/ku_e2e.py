# Last modified: 2026-11-05--1118
#
# ku_e2e.py — the Kobo Utilities (USB) route end to end through the PLUGIN path, under
# calibre-debug with the plugin INSTALLED (from the 1.7.0 zip) in an isolated calibre config
# folder, on a scratch library whose KU-style columns were filled by hand (calibredb). Not shipped.
#
#   calibre-debug -e ku_e2e.py -- settings <library> --location '#kobo_reading_location'
#                                          [--percent '#kobo_percent_read'] [--last-read '#kobo_last_read']
#       = Settings -> Kobo, "Kobo Utilities (USB)": pick the columns, show the preview, OK
#         (config.ConfigWidget.save_settings).
#   calibre-debug -e ku_e2e.py -- run <library>
#       = the Sync Read Status button (ui.CalibReaderSyncAction._run, manual) with a stand-in main
#         window; prints every kobo-utilities entry with the EPUB text at its textAnchor.
import argparse
import hashlib
import json
import os

from calibre.db.legacy import LibraryDatabase
from calibre.gui2 import Application

from calibre_plugins.calibreader_sync import config, kobo_ku, ui
from calibre_plugins.calibreader_sync.kobo_position import Book


def gui_for(library):
    db = LibraryDatabase(library)

    class StatusBar(object):
        def show_message(self, text, ms=0):
            print('status bar:', text)

    class Model(object):
        def refresh(self):
            pass

    class View(object):
        def model(self):
            return Model()

    class Gui(object):
        library_path = library
        current_db = db
        status_bar = StatusBar()
        library_view = View()
        iactions = {}
    return Gui(), db


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def settings(a):
    Application([])
    gui, db = gui_for(a.library)
    try:
        w = config.ConfigWidget(gui)
        print('location column choices:', [w.ku_location.itemData(i) for i in range(w.ku_location.count())])
        print('selected before (never pre-selected):', repr(w.ku_location.currentData()))
        print('CalibReader's own % / last-read pre-selected:', repr(w.combos['percent'].currentData()),
              repr(w.combos['last_read'].currentData()))
        if a.own_percent:
            w.combos['percent'].setCurrentIndex(w.combos['percent'].findData(a.own_percent))
        if a.own_last_read:
            w.combos['last_read'].setCurrentIndex(w.combos['last_read'].findData(a.own_last_read))
        w.ku_location.setCurrentIndex(w.ku_location.findData(a.location))
        print('preview:\n  ' + w.ku_preview.text().replace('\n', '\n  '))
        if a.percent:
            w.ku_percent.setCurrentIndex(w.ku_percent.findData(a.percent))
        if a.last_read:
            w.ku_last_read.setCurrentIndex(w.ku_last_read.findData(a.last_read))
        w._show_ku_error = lambda message: print('ERROR DIALOG:', message)
        print('warning shown:', repr(w.ku_clash_note.text()))
        ok = w.validate()                                   # OK: calibre asks validate() first
        print('validate (dialog may close):', ok)
        if ok:
            w.save_settings()
        from calibre_plugins.calibreader_sync.sync_core import SETTINGS_KEY
        print('saved CalibReader settings:', json.dumps(db.new_api.pref(SETTINGS_KEY, None), sort_keys=True))
        print('PREFS kobo_ku entry:', json.dumps((ui.PREFS.get('kobo_ku', {}) or {}).get(a.library), sort_keys=True))
        print('status:', config.ConfigWidget(gui).ku_status.text())
    finally:
        db.close()


def run(a):
    Application([])
    metadata = os.path.join(a.library, 'metadata.db')
    before = sha(metadata)
    gui, db = gui_for(a.library)

    class Kick(object):
        def start(self, ms):
            print('follow-up kick asked ({} ms)'.format(ms))

    class Action(object):
        pass
    for name, member in vars(ui.CalibReaderSyncAction).items():
        if callable(member) and name.startswith('_') and not name.startswith('__'):
            setattr(Action, name, member)
    action = Action()
    action.gui, action._kick = gui, Kick()
    try:
        res = ui.CalibReaderSyncAction._run(action, db.new_api, None, manual=True)
        print('kobo_ku_imported={}'.format(res.kobo_ku_imported))
        for line in res.kobo_ku_lines:
            print('  line:', line)
        for n in res.notes:
            print('  note:', n)
        positions = os.path.join(a.library, 'CalibReader_positions.json')
        if os.path.exists(positions):
            print('positions sha256', sha(positions)[:16])
            with open(positions, encoding='utf-8') as f:
                table = json.load(f)['positions']
            api = db.new_api
            by_uuid = {api.field_for('uuid', i).lower(): i for i in api.all_book_ids()}
            for uuid, e in sorted(table.items(), key=lambda kv: kv[1].get('updated_at', '')):
                if e.get('deviceId') != kobo_ku.KU_DEVICE_ID:
                    continue
                book_id = by_uuid.get(uuid.lower())
                b = Book(api.format_abspath(book_id, 'EPUB'))
                try:
                    text = b.text(e['chapter']).text
                finally:
                    b.close()
                print('  {} ({}): chapter {} textAnchor {} precision {} progress {} updated_at {} kepub {}\n'
                      '      at the anchor: {!r}'.format(api.field_for('title', book_id), book_id, e['chapter'],
                                                         e['textAnchor'], e['kobo']['precision'], e['progress'],
                                                         e['updated_at'], e['kobo'].get('kepub'),
                                                         ' '.join(text[e['textAnchor']:e['textAnchor'] + 70].split())))
    finally:
        db.close()
    print('metadata.db unchanged by the run:', sha(metadata) == before)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd')
    s = sub.add_parser('settings')
    s.add_argument('library')
    s.add_argument('--location', required=True)
    s.add_argument('--percent', default='')
    s.add_argument('--last-read', default='')
    s.add_argument('--own-percent', default='')
    s.add_argument('--own-last-read', default='')
    r = sub.add_parser('run')
    r.add_argument('library')
    a = ap.parse_args()
    {'settings': settings, 'run': run}[a.cmd](a)


if __name__ == '__main__':
    main()

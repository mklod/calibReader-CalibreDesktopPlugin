# e2e_plugin.py — the PLUGIN path, run under calibre-debug with the plugin installed (from
# the 1.6.0 zip) in an isolated calibre config folder. Not shipped. Two commands:
#
#   calibre-debug -e e2e_plugin.py -- settings <library> --app-db X --user U --cw URL --push on|off --service on|off
#       = Settings -> Kobo, set the fields, OK (config.ConfigWidget.save_kobo: sets the job up or down).
#   calibre-debug -e e2e_plugin.py -- run <library> [--slow-put SECONDS]
#       = the Sync Read Status button (ui.CalibReaderSyncAction._run, manual) with a stand-in
#         main window; --slow-put holds each PUT that long (the lock test).
#
# Prints no token (the plugin never has one in a note; the Kobo token lives only in app.db).
import argparse
import json
import os
import sys
import time
from unittest import mock

from calibre.constants import config_dir
from calibre.db.legacy import LibraryDatabase
from calibre.gui2 import Application

from calibre_plugins.calibreader_sync import config, kobo_push, kobo_service, ui


def stamp():
    return time.strftime('%H:%M:%S', time.localtime()) + '.{:03d}'.format(int(time.time() * 1000) % 1000)


def gui_for(library):
    db = LibraryDatabase(library)

    class StatusBar(object):
        def show_message(self, text, ms=0):
            print(stamp(), 'status bar:', text)

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


def settings(a):
    Application([])
    gui, db = gui_for(a.library)
    try:
        w = config.ConfigWidget(gui)
        print('status line before:', w.kobo_service_status.text())
        w.kobo_db.setText(a.app_db)
        w.kobo_user.setText(a.user)
        w.kobo_url.setText(a.cw)
        w.kobo_push.setChecked(a.push == 'on')
        w.kobo_service.setChecked(a.service == 'on')
        w._show_error = lambda message: print('ERROR DIALOG:', message)
        w.save_kobo()                                       # OK
        entry = (ui.PREFS.get('kobo', {}) or {}).get(a.library)
        print('PREFS kobo entry:', json.dumps(entry, sort_keys=True))
        print('status line after:', config.ConfigWidget(gui).kobo_service_status.text())
    finally:
        db.close()


def run(a):
    app = Application([])
    gui, db = gui_for(a.library)

    class Kick(object):
        def start(self, ms):
            print(stamp(), 'follow-up kick asked ({} ms)'.format(ms))

    class Action(object):
        pass
    for name, member in vars(ui.CalibReaderSyncAction).items():
        if callable(member) and name.startswith('_') and not name.startswith('__'):
            setattr(Action, name, member)
    action = Action()
    action.gui, action._kick = gui, Kick()
    patches = []
    if a.slow_put:
        real = kobo_push._send_within

        def slow(*args, **kw):
            print(stamp(), 'PUT held for {} s (lock test)'.format(a.slow_put), flush=True)
            time.sleep(a.slow_put)
            return real(*args, **kw)
        patches.append(mock.patch.object(kobo_push, '_send_within', side_effect=slow))
    for p in patches:
        p.start()
    try:
        print(stamp(), 'plugin run starts (pid {})'.format(os.getpid()), flush=True)
        res = ui.CalibReaderSyncAction._run(action, db.new_api, None, manual=True)
        print(stamp(), 'import: kobo_imported={} push={}'.format(res.kobo_imported, action._push_status), flush=True)
        for n in res.notes:
            print('  note:', n)
        end = time.monotonic() + 600
        while getattr(action, '_push_pending', False) and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.05)
        app.processEvents()
        pres = getattr(action, '_last_push', None)
        if pres is not None:
            print(stamp(), 'push: sent={}'.format(pres.sent))
            for n in pres.notes:
                print('  push note:', n)
            for k, why in sorted(pres.skipped.items()):
                print('  not sent {}: {}'.format(k, why))
            for line in pres.lines:
                print(' ', line)
        lock = kobo_service.LibraryLock(a.library, 'e2e check')
        print(stamp(), 'plugin run ends; lock free again:', lock.acquire())
        lock.release()
    finally:
        for p in patches:
            p.stop()
        db.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd')
    s = sub.add_parser('settings')
    s.add_argument('library')
    s.add_argument('--app-db', required=True)
    s.add_argument('--user', default='')
    s.add_argument('--cw', default='')
    s.add_argument('--push', choices=('on', 'off'), default='off')
    s.add_argument('--service', choices=('on', 'off'), default='off')
    r = sub.add_parser('run')
    r.add_argument('library')
    r.add_argument('--slow-put', type=float, default=0)
    a = ap.parse_args()
    print('calibre config:', config_dir, '| plugin from:', os.path.dirname(ui.__file__))
    {'settings': settings, 'run': run}[a.cmd](a)


main()

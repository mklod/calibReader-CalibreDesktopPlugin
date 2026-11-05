# Last modified: 2026-11-05--0417
#
# cw_setup.py — the e2e harness, acting as Calibre-Web's ADMIN with Calibre-Web
# STOPPED: points a throwaway Calibre-Web app.db at a scratch calibre library,
# turns Kobo sync on (proxy off), sets or clears the kepubify path, and gives
# the admin user a Kobo token (what Calibre-Web's "Create Kobo auth token"
# does). The token goes into --token-file and is never printed. Not shipped.
#
#   python cw_setup.py --app-db <app.db> --library <folder> --kepubify <exe or ''> --token-file <file>

import argparse
import binascii
import datetime
import os
import sqlite3


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--app-db', required=True)
    ap.add_argument('--library', required=True)
    ap.add_argument('--kepubify', default='')
    ap.add_argument('--token-file', required=True)
    a = ap.parse_args(argv)
    con = sqlite3.connect(a.app_db)
    try:
        con.execute('UPDATE settings SET config_calibre_dir=?, config_kobo_sync=1, config_kobo_proxy=0, '
                    'config_kepubifypath=?', (a.library, a.kepubify))
        if os.path.exists(a.token_file):
            with open(a.token_file) as f:
                token = f.read().strip()
        else:
            token = binascii.hexlify(os.urandom(16)).decode('ascii')
            with open(a.token_file, 'w') as f:
                f.write(token)
        if not con.execute('SELECT 1 FROM remote_auth_token WHERE auth_token=?', (token,)).fetchone():
            con.execute('INSERT INTO remote_auth_token (auth_token, user_id, verified, expiration, token_type) '
                        'VALUES (?, 1, 0, ?, 1)', (token, datetime.datetime.max.strftime('%Y-%m-%d %H:%M:%S.%f')))
        con.commit()
        row = con.execute('SELECT config_calibre_dir, config_kobo_sync, config_kobo_proxy, config_kepubifypath '
                          'FROM settings').fetchone()
        users = con.execute('SELECT id, name FROM user').fetchall()
        n = con.execute('SELECT count(*) FROM remote_auth_token WHERE token_type=1').fetchone()[0]
    finally:
        con.close()
    print('settings:', row)
    print('users:', users)
    print('Kobo tokens (type 1):', n, '(value in --token-file, not shown)')


if __name__ == '__main__':
    main()

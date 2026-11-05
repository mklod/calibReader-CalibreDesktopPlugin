# Last modified: 2026-11-05--0417
#
# kobo_sim.py — act as a Kobo against a LOCAL Calibre-Web (end-to-end test of
# step 2, CalibReader -> Kobo). Not shipped in the plugin.
#
#   python kobo_sim.py --cw http://127.0.0.1:8083 --token-file <file> --state <dir> init
#   ... sync                      library/sync, delta (keeps the x-kobo-synctoken in <dir>)
#   ... put <uuid> <type> <value> <source> <ProgressPercent> <ContentSourceProgressPercent>
#   ... get <uuid>                GET v1/library/<uuid>/state
#
# The Kobo token is read from --token-file and never printed: every URL in the
# output shows <token>.

import argparse
import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.request


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Kobo(object):
    def __init__(self, cw, token, state_dir):
        self.cw, self.token, self.state_dir = cw.rstrip('/'), token, state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.sync_token_file = os.path.join(state_dir, 'x-kobo-synctoken.txt')

    def url(self, path):
        return '{}/kobo/{}/{}'.format(self.cw, self.token, path.lstrip('/'))

    def shown(self, path):
        return '{}/kobo/<token>/{}'.format(self.cw, path.lstrip('/'))

    def request(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode('utf-8') if body is not None else None
        h = {'Content-Type': 'application/json', 'User-Agent': 'Kobo simulator (CalibReader e2e)'}
        h.update(headers or {})
        req = urllib.request.Request(self.url(path), data=data, method=method, headers=h)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=60) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def init(self):
        status, _, raw = self.request('GET', 'v1/initialization')
        print('GET', self.shown('v1/initialization'), status, len(raw), 'bytes')

    def sync(self):
        token = open(self.sync_token_file).read().strip() if os.path.exists(self.sync_token_file) else None
        out = []
        while True:
            status, headers, raw = self.request('GET', 'v1/library/sync',
                                                headers={'x-kobo-synctoken': token} if token else None)
            low = {k.lower(): v for k, v in headers.items()}
            token = low.get('x-kobo-synctoken', token)
            items = json.loads(raw.decode('utf-8') or '[]') if status == 200 else []
            print('GET', self.shown('v1/library/sync'), status, '{} item(s)'.format(len(items)),
                  'x-kobo-sync:', low.get('x-kobo-sync'))
            out.extend(items)
            if low.get('x-kobo-sync') != 'continue':
                break
        if token:
            with open(self.sync_token_file, 'w') as f:
                f.write(token)
        for item in out:
            kind = next(iter(item))
            body = item[kind]
            if kind in ('NewEntitlement', 'ChangedEntitlement'):
                ent = body['BookEntitlement']
                meta = body.get('BookMetadata', {})
                fmts = sorted({d['Format'] for d in meta.get('DownloadUrls', [])})
                rs = body.get('ReadingState')
                print(' ', kind, ent['Id'], meta.get('Title'), fmts,
                      'ReadingState.CurrentBookmark=' + json.dumps((rs or {}).get('CurrentBookmark')) if rs else '')
            elif kind == 'ChangedReadingState':
                rs = body['ReadingState']
                print(' ', kind, rs['EntitlementId'], 'CurrentBookmark=' + json.dumps(rs.get('CurrentBookmark')),
                      'StatusInfo=' + json.dumps(rs.get('StatusInfo')))
            else:
                print(' ', kind)
        return out

    def put(self, uuid, ltype, value, source, pct, cpct):
        t = now()
        body = {'ReadingStates': [{
            'EntitlementId': uuid, 'Created': t, 'LastModified': t, 'PriorityTimestamp': t,
            'StatusInfo': {'LastModified': t, 'Status': 'Reading', 'TimesStartedReading': 1},
            'Statistics': {'LastModified': t, 'SpentReadingMinutes': 12, 'RemainingTimeMinutes': 300},
            'CurrentBookmark': {'LastModified': t, 'ProgressPercent': float(pct),
                                'ContentSourceProgressPercent': float(cpct),
                                'Location': {'Value': value, 'Type': ltype, 'Source': source}}}]}
        status, _, raw = self.request('PUT', 'v1/library/{}/state'.format(uuid), body)
        print('PUT', self.shown('v1/library/{}/state'.format(uuid)), status, raw.decode('utf-8', 'replace')[:300])

    def get(self, uuid):
        status, _, raw = self.request('GET', 'v1/library/{}/state'.format(uuid))
        print('GET', self.shown('v1/library/{}/state'.format(uuid)), status, raw.decode('utf-8', 'replace')[:600])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--cw', default='http://127.0.0.1:8083')
    ap.add_argument('--token-file', required=True)
    ap.add_argument('--state', required=True)
    ap.add_argument('cmd')
    ap.add_argument('args', nargs='*')
    a = ap.parse_args(argv)
    with open(a.token_file) as f:
        token = f.read().strip()
    k = Kobo(a.cw, token, a.state)
    if a.cmd == 'init':
        k.init()
    elif a.cmd == 'sync':
        k.sync()
    elif a.cmd == 'put':
        k.put(*a.args)
    elif a.cmd == 'get':
        k.get(*a.args)
    else:
        raise SystemExit('unknown command ' + a.cmd)


if __name__ == '__main__':
    sys.exit(main())

#!/bin/bash
# After (e): the Kobo token scan (counts only, the token is never printed), Calibre-Web stopped by
# its own PID, and a check that nothing of this e2e is left running or loaded. Not shipped.
source "$HOME/builds/kobo-service-test/env.sh"
cd "$T"
echo "--- $(ts) token scan (occurrences of the Kobo token, value not shown)"
python3 - <<'PY'
import glob, os
T = os.path.expanduser('~/builds/kobo-service-test')
token = open(os.path.join(T, 'cw', 'token.txt')).read().strip()
files = (glob.glob(T + '/logs/*') + glob.glob(T + '/home/state/*') + glob.glob(T + '/home/locks/*')
         + glob.glob(T + '/home/service/*') + glob.glob(T + '/calibre-config/plugins/*.json')
         + [T + '/c1-plugin.txt', T + '/c2-job.txt', T + '/library/CalibReader_positions.json']
         + glob.glob(os.path.expanduser('~/Library/LaunchAgents/com.calibreader.*')))
hits = 0
for f in files:
    if os.path.isfile(f):
        n = open(f, 'rb').read().count(token.encode())
        hits += n
        if n:
            print('HIT', f, n)
print('{} files scanned, token hits: {}'.format(len([f for f in files if os.path.isfile(f)]), hits))
PY
echo "--- $(ts) stop the e2e's Calibre-Web"
bash cw.sh stop
echo "--- $(ts) left behind?"
echo "launchctl list | grep calibreader.sync: [$(launchctl list | grep calibreader.sync)]"
echo "plists: [$(ls "$HOME/Library/LaunchAgents" | grep -i calibreader.sync)]"
echo "processes of this e2e: [$(pgrep -fl 'kobo-service-test|e2e_plugin|kobo_service.py' | grep -v pgrep)]"
echo "port 8083: [$(lsof -nP -iTCP:8083 -sTCP:LISTEN 2>/dev/null | tail -n +2)]"

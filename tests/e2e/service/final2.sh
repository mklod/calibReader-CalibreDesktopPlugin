#!/bin/bash
# Fix round 1 clean-up, after the logs were copied back: token scan (counts only), Calibre-Web stopped
# by its own PID, then everything this e2e created removed with python (never rm): the job's real
# folders, its plist (already gone after the untick) and ~/builds/kobo-service-test. Not shipped.
source "$HOME/builds/kobo-service-test/env2.sh"
cd "$T"
echo "--- $(ts) token scan (value never shown)"
python3 - <<'PY'
import glob, os
H = os.path.expanduser('~')
T = os.path.join(H, 'builds', 'kobo-service-test')
token = open(os.path.join(T, 'cw', 'token.txt')).read().strip().encode()
files = [p for p in glob.glob(H + '/Library/Logs/CalibReaderSync/**', recursive=True)
         + glob.glob(H + '/Library/Application Support/CalibReaderSync/**', recursive=True)
         + glob.glob(T + '/calibre-config/plugins/*.json') + glob.glob(T + '/*.json')
         + glob.glob(H + '/Library/LaunchAgents/com.calibreader.sync.*') if os.path.isfile(p)]
hits = sum(open(p, 'rb').read().count(token) for p in files)
print('{} files scanned, token hits: {}'.format(len(files), hits))
PY
echo "--- $(ts) stop the e2e's Calibre-Web"
bash cw.sh stop
echo "--- $(ts) remove what this e2e created"
python3 - <<'PY'
import os, shutil
H = os.path.expanduser('~')
for p in (H + '/Library/Logs/CalibReaderSync', H + '/Library/Application Support/CalibReaderSync',
          H + '/builds/kobo-service-test'):
    shutil.rmtree(p, ignore_errors=True)
    print('removed' if not os.path.exists(p) else 'STILL THERE', p)
for name in os.listdir(H + '/Library/LaunchAgents'):
    if name.startswith('com.calibreader.sync.'):
        print('LEFT:', name)
PY
echo "launchctl list | grep calibreader.sync: [$(launchctl list | grep calibreader.sync)]"
echo "processes: [$(pgrep -fl 'kobo-service-test|e2e_plugin|kobo_service.py' | grep -v pgrep)]"
echo "port 8083: [$(lsof -nP -iTCP:8083 -sTCP:LISTEN 2>/dev/null | tail -n +2)]"

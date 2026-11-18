#!/bin/bash
# (c) one writer: calibre's run and the job never do Kobo work at the same time. Not shipped.
source "$HOME/builds/kobo-service-test/env.sh"
cd "$T"
D=$(uuid_of Dracula)
runs() { grep -c '^2026' logs/kobo.log; }
LOCK=$(ls home/locks/kobo-*.lock)
echo "--- $(ts) (c1) calibre holds the lock (its PUT held 150 s); launchd starts the job meanwhile"
python3 app_reads.py "$LIB" "$D" 11 16500 0.256
plugin run "$LIB" --slow-put 150 > c1-plugin.txt 2>&1 &
PP=$!
until grep -q "PUT held" c1-plugin.txt; do sleep 0.5; done
echo "$(ts) lock file says: $(head -c 80 "$LOCK" | tr -s ' ')"
R=$(runs)
echo "$(ts) kickstart the job"
launchctl kickstart -k "gui/$(id -u)/com.calibreader.sync.kobo.TEST"
until [ "$(runs)" -gt "$R" ]; do sleep 0.5; done
awk -v n="$R" '/^2026/{c++} c>n' logs/kobo.log
wait "$PP"
echo "--- calibre's run (plugin path):"
cat c1-plugin.txt
R=$(runs)
echo "--- $(ts) calibre done; kickstart the job again"
launchctl kickstart -k "gui/$(id -u)/com.calibreader.sync.kobo.TEST"
until [ "$(runs)" -gt "$R" ]; do sleep 0.5; done
awk -v n="$R" '/^2026/{c++} c>n' logs/kobo.log

echo
echo "--- $(ts) (c2) the job holds the lock (its exact command line, held 150 s); calibre's button meanwhile"
python3 app_reads.py "$LIB" "$D" 11 17000 0.258        # new input: a full run, which takes the lock
BEFORE=$(head -c 200 "$LOCK")
"$BIN/calibre-debug" -e "$T/home/service/kobo_service.py" -- run --library "$LIB" \
    --config-dir "$CALIBRE_CONFIG_DIRECTORY" --hold-seconds 150 > c2-job.txt 2>&1 &
JP=$!
until [ "$(head -c 200 "$LOCK")" != "$BEFORE" ] && head -c 80 "$LOCK" | grep -q "the background job"; do sleep 0.3; done
echo "$(ts) lock file says: $(head -c 80 "$LOCK" | tr -s ' ') (job started as PID $JP)"
echo "--- $(ts) calibre: Sync Read Status"
plugin run "$LIB"
R=$(runs)
echo "--- $(ts) launchd's own job meanwhile (kickstart)"
launchctl kickstart -k "gui/$(id -u)/com.calibreader.sync.kobo.TEST"
until [ "$(runs)" -gt "$R" ]; do sleep 0.5; done
wait "$JP"
echo "$(ts) the held job exited $?: $(cat c2-job.txt | tail -1)"
echo "--- job log since (c2):"
awk -v n="$((R-0))" '/^2026/{c++} c>n-0' logs/kobo.log | tail -n 20

#!/bin/bash
# cw.sh start|stop — the e2e's own Calibre-Web (127.0.0.1:8083), by the PID it records. Not shipped.
T="$HOME/builds/kobo-service-test"
R="$HOME/builds/kobo-sync"
case "$1" in
start)
    cd "$T/cw"
    CALIBRE_DBPATH="$T/cw" nohup "$R/cw-venv/bin/cps" -p "$T/cw/app.db" -i 127.0.0.1 -o "$T/cw/calibre-web.log" \
        > "$T/cw/stdout.log" 2>&1 &
    echo $! > "$T/cw/pid"
    echo "started Calibre-Web PID $(cat "$T/cw/pid")"
    for i in $(seq 1 120); do
        if curl -s -o /dev/null -m 2 http://127.0.0.1:8083/; then echo "up after ${i} tries"; exit 0; fi
        sleep 0.5
    done
    echo "NOT up"; exit 1 ;;
stop)
    PID=$(cat "$T/cw/pid")
    kill "$PID" && echo "stopped Calibre-Web PID $PID"
    sleep 2
    ps -p "$PID" > /dev/null && echo "still running" || echo "gone" ;;
esac

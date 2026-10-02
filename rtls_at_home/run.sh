#!/bin/sh
# The RTLS@Home App: prepare /data (the ingest token, a one-time import), tell Home Assistant where the engine is, run
# the engine. An Apply ends the engine with code 3 to start it on the new house: it is restarted here, not the
# container. Any other exit ends the App, and the watchdog restarts it.
cd "${RTLS_ROOT:-/opt/rtls}" || exit 1
PY="${PYTHON:-python}"
"$PY" app/rtls/ha_app.py prepare || exit 1
"$PY" app/rtls/ha_app.py announce
child=""
trap 'if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null; wait "$child"; fi; exit 0' TERM INT
while true; do
  "$PY" app/rtls/server.py --host 0.0.0.0 --port "${RTLS_PORT:-8765}" --sessions "${RTLS_SESSIONS:-/data}" &
  child=$!
  wait "$child"
  code=$?
  child=""
  if [ "$code" -ne 3 ]; then
    exit "$code"
  fi
  echo "app: the engine restarts on the applied house"
done

#!/bin/sh
# An isolated headed display for a person-controlled Chrome process.
set -eu
Xvfb "${DISPLAY:-:99}" -screen 0 1440x1000x24 -nolisten tcp &
display_pid=$!
child_pid=
cleanup() {
  if [ -n "$child_pid" ]; then kill -TERM "$child_pid" 2>/dev/null || true; fi
  kill -TERM "$display_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
tries=0
until xdpyinfo -display "${DISPLAY:-:99}" >/dev/null 2>&1; do
  kill -0 "$display_pid"
  tries=$((tries + 1))
  [ "$tries" -lt 100 ] || exit 1
  sleep 0.1
done
"$@" &
child_pid=$!
wait "$child_pid"

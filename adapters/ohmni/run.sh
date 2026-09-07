#!/system/bin/sh
# Android launcher for the packaged musl Python runtime.
set -eu
cd /data/local/sweep
case "${1:-start}" in
  stop)
    if [ -f node.pid ]; then
      pid=$(cat node.pid)
      case "$pid" in ''|*[!0-9]*) echo 'invalid node pid' >&2; exit 1;; esac
      if [ -r "/proc/$pid/cmdline" ]; then
        process=$(tr '\000' ' ' < "/proc/$pid/cmdline")
        case "$process" in
          *python3.12*' -m adapters.ohmni'*) ;;
          *) echo 'Recorded PID belongs to another process; leaving it alone.' >&2; exit 1;;
        esac
        kill -TERM "$pid" 2>/dev/null || true
      fi
      # SIGTERM reaches the runtime handler, which stops and disables local drive.
      tries=0
      while kill -0 "$pid" 2>/dev/null; do
        tries=$((tries + 1))
        if [ "$tries" -ge 20 ]; then
          echo 'Node has not exited. Confirm local stop before proceeding.' >&2
          exit 1
        fi
        sleep 1
      done
      rm -f node.pid
    fi
    exit 0;;
  start) ;;
  *) echo 'usage: run.sh [start|stop]' >&2; exit 2;;
esac
if [ -f node.pid ] && kill -0 "$(cat node.pid)" 2>/dev/null; then
  echo 'Node already running' >&2
  exit 1
fi
[ -f node.env ] || { echo 'Missing private node.env' >&2; exit 1; }
chmod 600 node.env
set -a
. ./node.env
set +a
export PYTHONPATH=/data/local/sweep
umask 077
nohup ./lib/ld-musl-x86_64.so.1 ./python/bin/python3.12 -m adapters.ohmni >node.log 2>&1 &
echo "$!" > node.pid
echo 'Node started; inspect node.log for relay and safety state.'

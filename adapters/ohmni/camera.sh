#!/system/bin/sh
set -eu
cd /data/local/sweep
case "${1:-start}" in
  stop)
    if [ -f camera.pid ]; then
      pid=$(cat camera.pid)
      case "$pid" in ''|*[!0-9]*) echo 'invalid camera pid' >&2; exit 1;; esac
      if [ -r "/proc/$pid/cmdline" ]; then
        process=$(tr '\000' ' ' < "/proc/$pid/cmdline")
        case "$process" in
          *python3.12*' -m adapters.ohmni.camera_runner'*) ;;
          *) echo 'Recorded PID belongs to another process; leaving it alone.' >&2; exit 1;;
        esac
        kill -TERM "$pid" 2>/dev/null || true
      fi
      tries=0
      while kill -0 "$pid" 2>/dev/null; do
        tries=$((tries + 1))
        if [ "$tries" -ge 10 ]; then
          echo 'Camera has not exited.' >&2
          exit 1
        fi
        sleep 1
      done
      rm -f camera.pid
    fi
    exit 0;;
  start|probe) ;;
  *) echo 'usage: camera.sh [start|stop|probe]' >&2; exit 2;;
esac
if [ -f camera.pid ] && kill -0 "$(cat camera.pid)" 2>/dev/null; then
  echo 'Camera already running' >&2
  exit 1
fi
[ -f node.env ] || { echo 'Missing private node.env' >&2; exit 1; }
[ -f camera.env ] || { echo 'Missing private camera.env' >&2; exit 1; }
chmod 600 node.env camera.env
set -a
. ./node.env
node_device_unit=${SWEEP_DEVICE_UNIT:?Missing SWEEP_DEVICE_UNIT in node.env}
. ./camera.env
set +a
if [ "${SWEEP_DEVICE_UNIT:-}" != "$node_device_unit" ]; then
  echo 'camera.env must not change the node device ID.' >&2
  exit 1
fi
export SWEEP_DEVICE_UNIT="$node_device_unit"
export PYTHONPATH=/data/local/sweep
umask 077
if [ "${1:-start}" = probe ]; then
  # Foreground, one attempt, no retry loop or background PID record.
  exec ./lib/ld-musl-x86_64.so.1 ./python/bin/python3.12 -m adapters.ohmni.camera_runner --probe --timeout 10
fi
nohup ./lib/ld-musl-x86_64.so.1 ./python/bin/python3.12 -m adapters.ohmni.camera_runner >camera.log 2>&1 &
echo "$!" > camera.pid
echo 'Camera publisher started; inspect camera.log for failures.'

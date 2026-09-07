#!/bin/sh
# Install only. Never starts the process, enables wheels, or launches a motion.
set -eu
if [ "$#" -ne 2 ]; then
  echo 'usage: install.sh ADB_SERIAL PAYLOAD_TAR' >&2
  exit 2
fi
serial=$1
payload=$2
[ -f "$payload" ] || exit 2
adb=${ADB:-adb}
"$adb" -s "$serial" get-state >/dev/null
root_shell() {
  printf '%s\n' "$1" | "$adb" -s "$serial" shell -T su 0 sh
}
[ "$(root_shell 'id -u' | tr -d '\r')" = 0 ] || {
  echo 'Installation requires the Ohmni root shell.' >&2
  exit 1
}
# Existing runtime must be stopped by the operator before replacing hardware code.
if ! root_shell '
  if [ -e /data/local/sweep/node.pid ]; then
    pid=$(cat /data/local/sweep/node.pid)
    case "$pid" in ""|*[!0-9]*) exit 1;; esac
    if kill -0 "$pid" 2>/dev/null; then exit 1; fi
    rm /data/local/sweep/node.pid
  fi
' >/dev/null 2>&1; then
  echo 'Stop the existing node with run.sh stop before installing.' >&2
  exit 1
fi
# Legacy nodes may lack a pidfile. Match the executable before inspecting arguments
# so a shell containing the node command is never mistaken for a running node.
if ! root_shell '
  for process in /proc/[0-9]*; do
    executable=$(readlink "$process/exe" 2>/dev/null || true)
    [ "$executable" = /data/local/sweep/lib/ld-musl-x86_64.so.1 ] || continue
    args=$(tr "\000" " " < "$process/cmdline" 2>/dev/null || true)
    case "$args" in
      *ohmni_node.py*|*" -m adapters.ohmni"*) exit 1;;
    esac
  done
' >/dev/null; then
  echo 'An Ohmni node is still running; stop it through its existing launcher first.' >&2
  exit 1
fi
staging=$("$adb" -s "$serial" shell 'mktemp -d /data/local/tmp/sweep-install.XXXXXXXX' | tr -d '\r')
suffix=${staging#/data/local/tmp/sweep-install.}
case "$suffix" in
  ''|*[!a-zA-Z0-9]*) echo 'Invalid device staging directory.' >&2; exit 1;;
esac
[ "$staging" = "/data/local/tmp/sweep-install.$suffix" ] || exit 1
trap 'root_shell "rm -rf $staging" >/dev/null 2>&1 || true' 0
trap 'exit 130' 1 2 15
"$adb" -s "$serial" push "$payload" "$staging/payload.tar" >/dev/null
root_shell "
  set -eu
  mkdir -p /data/local/sweep
  chmod 700 /data/local/sweep
  cd /data/local/sweep
  toybox tar xf $staging/payload.tar
  chmod 700 run.sh ffmpeg lib/ld-musl-x86_64.so.1
"
echo 'Installed with wheels disabled. Supply private node.env; start only after spotter and calibration checks.'

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
adb -s "$serial" get-state >/dev/null
# Existing runtime must be stopped by the operator before replacing hardware code.
if adb -s "$serial" shell 'test -e /data/local/sweep/node.pid' >/dev/null 2>&1; then
  echo 'Stop the existing node with run.sh stop before installing.' >&2
  exit 1
fi
# A legacy bring-up process may not have a pidfile. Inspect only processes whose
# executable is this deployment's musl loader, so a shell containing these strings
# is never mistaken for a running node.
if ! adb -s "$serial" shell '
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
adb -s "$serial" shell 'mkdir -p /data/local/sweep; chmod 700 /data/local/sweep'
adb -s "$serial" push "$payload" /data/local/sweep/payload.tar >/dev/null
adb -s "$serial" shell 'cd /data/local/sweep && toybox tar xf payload.tar && rm payload.tar && chmod 700 run.sh ffmpeg lib/ld-musl-x86_64.so.1'
echo 'Installed; no node was started. Supply private node.env and complete local commissioning before startup.'

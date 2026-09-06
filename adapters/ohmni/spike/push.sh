#!/usr/bin/env bash
# Push the spike scripts to an Ohmni robot over adb and print the in-container commands.
#
#   adapters/ohmni/spike/push.sh <robot_ip> [adb_port]
#
# The robot's /var/dockerhome is mounted as /home/ohmnidev inside `dockerenv`, so the scripts
# land at /home/ohmnidev/sweep-spike in the container. If the direct push is refused, the
# script pushes to /data/local/tmp and prints the root-shell copy to run instead.
set -euo pipefail

ip="${1:?usage: push.sh <robot_ip> [adb_port]}"
port="${2:-5555}"
serial="$ip:$port"
here="$(cd "$(dirname "$0")" && pwd)"
remote=/var/dockerhome/sweep-spike
staging=/data/local/tmp/sweep-spike

adb connect "$serial" >/dev/null
adb -s "$serial" get-state >/dev/null

# Remove the old copy so `adb push <dir>` recreates it instead of nesting a second `spike/`.
adb -s "$serial" shell "su -c 'rm -rf $remote'" >/dev/null 2>&1 || true

if adb -s "$serial" push "$here" "$remote" >/dev/null 2>&1; then
  echo "pushed $here to $serial:$remote"
else
  echo "direct push to $remote refused; staging under $staging"
  adb -s "$serial" shell "rm -rf $staging" >/dev/null 2>&1 || true
  adb -s "$serial" push "$here" "$staging" >/dev/null
  echo "then, in 'adb -s $serial shell' after 'su':"
  echo "  cp -r $staging $remote"
fi

cat <<EOF

On the robot:
  adb -s $serial shell
  su
  docker images | grep ohmni        # record the image tags
  dockerenv
  cd /home/ohmnidev/sweep-spike
  python3 --version
  python3 botshell.py battery
  python3 botshell.py say "sweep spike"
  python3 botshell.py raw scan_lidar_device
  python3 drive_probe.py --dry-run
  python3 drive_probe.py --log drive.jsonl
  python3 lidar_probe.py --seconds 10 --record scans.jsonl
  python3 camera_probe.py --seconds 10 --out frames.raw
  python3 camera_probe.py --seconds 60 --publish rtsp://<laptop_ip>:8554/ground1

Copy results back:
  adb -s $serial pull $remote/drive.jsonl .
  adb -s $serial pull $remote/scans.jsonl .
  adb -s $serial pull $remote/frames.raw .
EOF

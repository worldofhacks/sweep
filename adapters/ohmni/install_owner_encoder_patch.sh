#!/bin/sh
# Stage a reviewed owner-side encoder patch. It never restarts the vendor service.
set -eu
if [ "$#" -ne 2 ]; then
  echo 'usage: install_owner_encoder_patch.sh ADB_SERIAL VENDOR_NODE_DIRECTORY' >&2
  exit 2
fi
serial=$1
node_dir=$2
case "$node_dir" in
  /data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files) ;;
  *) echo 'Unexpected vendor node directory.' >&2; exit 2 ;;
esac
adb=${ADB:-adb}
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
module="$root/adapters/ohmni/vendor/sweep_paired_encoder_sampler.js"
patcher="$root/adapters/ohmni/tools/prepare_owner_encoder_patch.py"
[ -f "$module" ] && [ -f "$patcher" ] || exit 2
work=$(mktemp -d "${TMPDIR:-/tmp}/sweep-owner-patch.XXXXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
"$adb" -s "$serial" get-state >/dev/null
"$adb" -s "$serial" pull "$node_dir/telebot_node.js" "$work/telebot_node.js" >/dev/null
python3 "$patcher" "$work/telebot_node.js" "$work/telebot_node.patched.js"
stage=$("$adb" -s "$serial" shell 'mktemp -d /data/local/tmp/sweep-owner-patch.XXXXXXXX' | tr -d '\r')
case "$stage" in
  /data/local/tmp/sweep-owner-patch.[A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9]) ;;
  *) echo 'Invalid Android staging directory.' >&2; exit 1 ;;
esac
cleanup() {
  printf '%s\n' "rm -rf $stage" | "$adb" -s "$serial" shell -T su 0 sh >/dev/null 2>&1 || true
}
trap 'cleanup; rm -rf "$work"' EXIT HUP INT TERM
"$adb" -s "$serial" push "$work/telebot_node.patched.js" "$stage/telebot_node.js" >/dev/null
"$adb" -s "$serial" push "$module" "$stage/sweep_paired_encoder_sampler.js" >/dev/null
printf '%s\n' "
set -eu
node_dir=$node_dir
target=\$node_dir/telebot_node.js
backup=\$node_dir/telebot_node.js.sweep-owner-encoder.backup
[ ! -e \$backup ]
[ ! -e \$node_dir/sweep_paired_encoder_sampler.js ]
[ -f \$target ]
cp -p \$target \$backup
mv $stage/sweep_paired_encoder_sampler.js \$node_dir/sweep_paired_encoder_sampler.js
mv $stage/telebot_node.js \$target
chmod 600 \$node_dir/sweep_paired_encoder_sampler.js
" | "$adb" -s "$serial" shell -T su 0 sh
printf '%s\n' 'Patched source staged. Restart the vendor service only through a separately reviewed operation.'

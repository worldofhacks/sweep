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
vendor_owner="1000:1000"
vendor_mode=600
vendor_context="u:object_r:system_app_data_file:s0"
[ -f "$module" ] && [ -f "$patcher" ] || exit 2
work=$(mktemp -d "${TMPDIR:-/tmp}/sweep-owner-patch.XXXXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
"$adb" -s "$serial" get-state >/dev/null
source="$node_dir/telebot_node.js"
printf 'base64 %s\n' "$source" | "$adb" -s "$serial" shell -T su 0 sh | tr -d '\r' | base64 -d > "$work/telebot_node.js"
source_sha=$(sha256sum "$work/telebot_node.js" | cut -d ' ' -f 1)
remote_sha=$(printf 'sha256sum %s\n' "$source" | "$adb" -s "$serial" shell -T su 0 sh | awk '{print $1}' | tr -d '\r\n')
[ "$remote_sha" = "$source_sha" ] || {
  echo 'Vendor source changed or transport altered its bytes; refusing to install.' >&2
  exit 1
}
python3 "$patcher" "$work/telebot_node.js" "$work/telebot_node.patched.js"
patched_sha=$(sha256sum "$work/telebot_node.patched.js" | cut -d ' ' -f 1)
module_sha=$(sha256sum "$module" | cut -d ' ' -f 1)
stage=$(printf '%s\n' 'mktemp -d /data/local/tmp/sweep-owner-patch.XXXXXXXX' | "$adb" -s "$serial" shell -T su 0 sh | tr -d '\r')
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
cat <<EOF | "$adb" -s "$serial" shell -T su 0 sh
set -eu
node_dir=$node_dir
target=\$node_dir/telebot_node.js
backup=\$node_dir/telebot_node.js.sweep-owner-encoder.backup
module=\$node_dir/sweep_paired_encoder_sampler.js
[ "\$(sha256sum \$target | cut -d ' ' -f 1)" = $source_sha ]
[ "\$(sha256sum $stage/telebot_node.js | cut -d ' ' -f 1)" = $patched_sha ]
[ "\$(sha256sum $stage/sweep_paired_encoder_sampler.js | cut -d ' ' -f 1)" = $module_sha ]
[ ! -e \$backup ]
[ ! -e \$module ]
[ -f \$target ]
cp -p \$target \$backup
[ "\$(sha256sum \$backup | cut -d ' ' -f 1)" = $source_sha ]
mv $stage/sweep_paired_encoder_sampler.js \$module
mv $stage/telebot_node.js \$target
chown $vendor_owner \$target \$module
chmod $vendor_mode \$target \$module
chcon $vendor_context \$target \$module
[ "\$(sha256sum \$target | cut -d ' ' -f 1)" = $patched_sha ]
[ "\$(sha256sum \$module | cut -d ' ' -f 1)" = $module_sha ]
[ "\$(stat -c '%u:%g:%a' \$target)" = $vendor_owner:$vendor_mode ]
[ "\$(stat -c '%u:%g:%a' \$module)" = $vendor_owner:$vendor_mode ]
[ "\$(ls -Zd \$target | awk '{print \$1}')" = $vendor_context ]
[ "\$(ls -Zd \$module | awk '{print \$1}')" = $vendor_context ]
EOF
printf '%s\n' 'Patched source staged. Restart the vendor service only through a separately reviewed operation.'

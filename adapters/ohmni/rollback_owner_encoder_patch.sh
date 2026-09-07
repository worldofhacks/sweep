#!/bin/sh
# Restore the hash-pinned vendor source backup without restarting the vendor service.
set -eu
if [ "$#" -ne 2 ]; then
  echo 'usage: rollback_owner_encoder_patch.sh ADB_SERIAL VENDOR_NODE_DIRECTORY' >&2
  exit 2
fi
serial=$1
node_dir=$2
case "$node_dir" in
  /data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files) ;;
  *) echo 'Unexpected vendor node directory.' >&2; exit 2 ;;
esac
adb=${ADB:-adb}
reference_sha="f463feaab912999d3b4133fea049925ed95a6e32ee82856fb7ffa273cbe2ca3e"
patched_sha="7d7ec2647e083adb42040f5ebbfd274fe87b7775739ada162c7402d55ffea70d"
module_sha="f7dbd82f36df7a95ef13266bfe27e93b7df799f19d86dc855f398125ff4c260d"
vendor_owner="1000:1000"
vendor_mode=600
vendor_context="u:object_r:system_app_data_file:s0"
"$adb" -s "$serial" get-state >/dev/null
cat <<EOF | "$adb" -s "$serial" shell -T su 0 sh
set -eu
node_dir=$node_dir
target=\$node_dir/telebot_node.js
backup=\$node_dir/telebot_node.js.sweep-owner-encoder.backup
module=\$node_dir/sweep_paired_encoder_sampler.js
[ -f \$backup ]
[ -f \$target ]
[ -f \$module ]
[ "\$(sha256sum \$target | cut -d ' ' -f 1)" = $patched_sha ]
[ "\$(sha256sum \$module | cut -d ' ' -f 1)" = $module_sha ]
[ "\$(sha256sum \$backup | cut -d ' ' -f 1)" = $reference_sha ]
mv \$target \$node_dir/telebot_node.js.sweep-owner-encoder.disabled
mv \$backup \$target
chown $vendor_owner \$target
chmod $vendor_mode \$target
chcon $vendor_context \$target
[ "\$(sha256sum \$target | cut -d ' ' -f 1)" = $reference_sha ]
[ "\$(stat -c '%u:%g:%a' \$target)" = $vendor_owner:$vendor_mode ]
[ "\$(ls -Zd \$target | awk '{print \$1}')" = $vendor_context ]
EOF
printf '%s\n' 'Vendor source restored. Restart the vendor service only through a separately reviewed operation.'

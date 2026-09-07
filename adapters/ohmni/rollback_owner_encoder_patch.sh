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
"$adb" -s "$serial" get-state >/dev/null
printf '%s\n' "
set -eu
node_dir=$node_dir
target=\$node_dir/telebot_node.js
backup=\$node_dir/telebot_node.js.sweep-owner-encoder.backup
module=\$node_dir/sweep_paired_encoder_sampler.js
[ -f \$backup ]
[ -f \$target ]
[ -f \$module ]
[ \"\$(sha256sum \$target | cut -d ' ' -f 1)\" = 366139bc4c6c8c1b2ba082b8e7c4fab10ea8dab39bb8cf280ecf167ea73b0d4d ]
[ \"\$(sha256sum \$module | cut -d ' ' -f 1)\" = 86e29de49d17bf8f2ce16d3cab33026f50900acdbc2967cb8e0ce627fcedac29 ]
mv \$target \$node_dir/telebot_node.js.sweep-owner-encoder.disabled
mv \$backup \$target
" | "$adb" -s "$serial" shell -T su 0 sh
printf '%s\n' 'Vendor source restored. Restart the vendor service only through a separately reviewed operation.'

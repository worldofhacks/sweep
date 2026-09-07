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
crlf_reference_sha="f463feaab912999d3b4133fea049925ed95a6e32ee82856fb7ffa273cbe2ca3e"
lf_reference_sha="e128a740200b7f8d538414c8963109f1ee2f0475b340f9369814ba8446891300"
crlf_patched_sha="0394a830141bf8ce4343944b768de17887531f3c1d89e3521216b5f7ca5b82ea"
lf_patched_sha="ee0a0665dc1a5931960d97032405cb4e7baf731d0cc6b08738a4d33302a5bf42"
module_sha="6722b1bc201942d825b3dffc184546ae3058a200df8b09df47f57b2769dc6944"
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
case "\$(sha256sum \$target | cut -d ' ' -f 1)" in
  $crlf_patched_sha) patched_sha=$crlf_patched_sha; reference_sha=$crlf_reference_sha ;;
  $lf_patched_sha) patched_sha=$lf_patched_sha; reference_sha=$lf_reference_sha ;;
  *) echo 'Installed vendor source does not match a reviewed owner patch.' >&2; exit 1 ;;
esac
[ "\$(sha256sum \$module | cut -d ' ' -f 1)" = $module_sha ]
[ "\$(sha256sum \$backup | cut -d ' ' -f 1)" = \$reference_sha ]
mv \$target \$node_dir/telebot_node.js.sweep-owner-encoder.disabled
mv \$backup \$target
chown $vendor_owner \$target
chmod $vendor_mode \$target
chcon $vendor_context \$target
[ "\$(sha256sum \$target | cut -d ' ' -f 1)" = \$reference_sha ]
[ "\$(stat -c '%u:%g:%a' \$target)" = $vendor_owner:$vendor_mode ]
[ "\$(ls -Zd \$target | awk '{print \$1}')" = $vendor_context ]
[ "\$(sha256sum \$node_dir/telebot_node.js.sweep-owner-encoder.disabled | cut -d ' ' -f 1)" = \$patched_sha ]
rm \$module \$node_dir/telebot_node.js.sweep-owner-encoder.disabled
EOF
printf '%s\n' 'Vendor source restored. Restart the vendor service only through a separately reviewed operation.'

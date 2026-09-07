#!/bin/sh
# Remove only the hash-pinned encoder plugin without modifying vendor assets.
set -eu
if [ "$#" -ne 1 ]; then
  echo 'usage: rollback_owner_encoder_plugin.sh ADB_SERIAL' >&2
  exit 2
fi
serial=$1
adb=${ADB:-adb}
source="/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files/telebot_node.js"
plugin_dir="/data/data/com.ohmnilabs.telebot_rtc/files/plugins"
private_dir="$plugin_dir/sweep_encoder_plugin"
target="$plugin_dir/sweep_encoder_plugin.js"
target_sampler="$private_dir/sampler.js"
manifest="$plugin_dir/sweep_encoder_plugin.install"
source_sha="e128a740200b7f8d538414c8963109f1ee2f0475b340f9369814ba8446891300"
plugin_sha="$(sha256sum "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/vendor/sweep_encoder_plugin.js" | cut -d ' ' -f 1)"
sampler_sha="$(sha256sum "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/vendor/sweep_encoder_plugin/sampler.js" | cut -d ' ' -f 1)"
"$adb" -s "$serial" get-state >/dev/null
cat <<EOF2 | "$adb" -s "$serial" shell -T su 0 sh
set -eu
source=$source
private_dir=$private_dir
target=$target
target_sampler=$target_sampler
manifest=$manifest
[ "\$(sha256sum \$source | cut -d ' ' -f 1)" = $source_sha ]
[ -f \$target ]
[ -f \$target_sampler ]
[ -f \$manifest ]
[ "\$(sha256sum \$target | cut -d ' ' -f 1)" = $plugin_sha ]
[ "\$(sha256sum \$target_sampler | cut -d ' ' -f 1)" = $sampler_sha ]
grep -Fx 'vendor_source_sha=$source_sha' \$manifest
grep -Fx 'plugin_sha=$plugin_sha' \$manifest
grep -Fx 'sampler_sha=$sampler_sha' \$manifest
rm \$target \$target_sampler \$manifest
rmdir \$private_dir
EOF2
printf '%s\n' 'Plugin removed. The vendor source was left unchanged.'

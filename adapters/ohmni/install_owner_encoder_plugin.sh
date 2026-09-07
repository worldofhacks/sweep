#!/bin/sh
# Install the encoder sampler as an OhmniJS plugin without modifying vendor assets.
set -eu
if [ "$#" -ne 1 ]; then
  echo 'usage: install_owner_encoder_plugin.sh ADB_SERIAL' >&2
  exit 2
fi
serial=$1
adb=${ADB:-adb}
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
plugin="$root/adapters/ohmni/vendor/sweep_encoder_plugin.js"
sampler="$root/adapters/ohmni/vendor/sweep_encoder_plugin/sampler.js"
source="/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files/telebot_node.js"
plugin_dir="/data/data/com.ohmnilabs.telebot_rtc/files/plugins"
private_dir="$plugin_dir/sweep_encoder_plugin"
target="$plugin_dir/sweep_encoder_plugin.js"
target_sampler="$private_dir/sampler.js"
manifest="$plugin_dir/sweep_encoder_plugin.install"
source_sha="e128a740200b7f8d538414c8963109f1ee2f0475b340f9369814ba8446891300"
vendor_owner="1000:1000"
vendor_context="u:object_r:system_app_data_file:s0"
[ -f "$plugin" ] && [ -f "$sampler" ] || exit 2
plugin_sha=$(sha256sum "$plugin" | cut -d ' ' -f 1)
sampler_sha=$(sha256sum "$sampler" | cut -d ' ' -f 1)
"$adb" -s "$serial" get-state >/dev/null
remote_sha=$(printf 'sha256sum %s\n' "$source" | "$adb" -s "$serial" shell -T su 0 sh | awk '{print $1}' | tr -d '\r\n')
[ "$remote_sha" = "$source_sha" ] || {
  echo 'The vendor source is not the reviewed original; refusing plugin installation.' >&2
  exit 1
}
stage=$(printf '%s\n' 'mktemp -d /data/local/tmp/sweep-encoder-plugin.XXXXXXXX' | "$adb" -s "$serial" shell -T su 0 sh | tr -d '\r')
case "$stage" in
  /data/local/tmp/sweep-encoder-plugin.[A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9]) ;;
  *) echo 'Invalid Android staging directory.' >&2; exit 1 ;;
esac
cleanup() {
  printf '%s\n' "rm -rf $stage" | "$adb" -s "$serial" shell -T su 0 sh >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM
stage_file() {
  source_file=$1
  destination=$2
  {
    printf "set -eu\numask 077\nbase64 -d > %s <<'EOF'\n" "$destination"
    base64 "$source_file"
    printf 'EOF\n'
  } | "$adb" -s "$serial" shell -T su 0 sh
}
stage_file "$plugin" "$stage/sweep_encoder_plugin.js"
stage_file "$sampler" "$stage/sampler.js"
cat <<EOF2 | "$adb" -s "$serial" shell -T su 0 sh
set -eu
source=$source
plugin_dir=$plugin_dir
private_dir=$private_dir
target=$target
target_sampler=$target_sampler
manifest=$manifest
[ "\$(stat -c '%u:%g:%a' $stage)" = 0:0:700 ]
[ "\$(stat -c '%a' $stage/sweep_encoder_plugin.js)" = 600 ]
[ "\$(stat -c '%a' $stage/sampler.js)" = 600 ]
[ "\$(sha256sum \$source | cut -d ' ' -f 1)" = $source_sha ]
[ "\$(sha256sum $stage/sweep_encoder_plugin.js | cut -d ' ' -f 1)" = $plugin_sha ]
[ "\$(sha256sum $stage/sampler.js | cut -d ' ' -f 1)" = $sampler_sha ]
[ ! -L \${plugin_dir} ]
if [ -e \$plugin_dir ]; then
  [ -d \$plugin_dir ]
  [ "\$(stat -c '%u:%g:%a' \$plugin_dir)" = $vendor_owner:700 ]
  [ "\$(ls -Zd \$plugin_dir | awk '{print \$1}')" = $vendor_context ]
  plugin_dir_created=0
else
  mkdir \$plugin_dir
  chown $vendor_owner \$plugin_dir
  chmod 700 \$plugin_dir
  chcon $vendor_context \$plugin_dir
  [ "\$(stat -c '%u:%g:%a' \$plugin_dir)" = $vendor_owner:700 ]
  [ "\$(ls -Zd \$plugin_dir | awk '{print \$1}')" = $vendor_context ]
  plugin_dir_created=1
fi
[ ! -e \$target ]
[ ! -e \$private_dir ]
[ ! -e \$manifest ]
mkdir \$private_dir
mv $stage/sweep_encoder_plugin.js \$target
mv $stage/sampler.js \$target_sampler
printf '%s\n' \\
  'vendor_source_sha=$source_sha' \\
  'plugin_sha=$plugin_sha' \\
  'sampler_sha=$sampler_sha' \\
  "plugin_dir_created=\$plugin_dir_created" > \$manifest
chown $vendor_owner \$target \$target_sampler \$manifest
chown $vendor_owner \$private_dir
chmod 700 \$private_dir
chmod 600 \$target \$target_sampler \$manifest
chcon $vendor_context \$target \$target_sampler \$manifest \$private_dir
[ "\$(sha256sum \$source | cut -d ' ' -f 1)" = $source_sha ]
[ "\$(sha256sum \$target | cut -d ' ' -f 1)" = $plugin_sha ]
[ "\$(sha256sum \$target_sampler | cut -d ' ' -f 1)" = $sampler_sha ]
[ "\$(stat -c '%u:%g:%a' \$target)" = $vendor_owner:600 ]
[ "\$(stat -c '%u:%g:%a' \$target_sampler)" = $vendor_owner:600 ]
[ "\$(stat -c '%u:%g:%a' \$private_dir)" = $vendor_owner:700 ]
[ "\$(stat -c '%u:%g:%a' \$manifest)" = $vendor_owner:600 ]
[ "\$(ls -Zd \$target | awk '{print \$1}')" = $vendor_context ]
EOF2
printf '%s\n' 'Plugin staged. The next normal vendor launch loads it through the existing plugin directory.'

# Ohmni handback manifest and procedure

This procedure has two endpoints. Project handback restores the captured vendor source and removes hash-identified Sweep additions. Deployment rollback restores the recorded pre-patch Sweep files for the active project stage. Neither endpoint recreates the complete device image. Vendor app data, network state, account state, camera calibration, audio settings, hardware wear, and any setting that was not captured remain outside its evidence.

Create one private, host-side capture for each robot before installing the owner encoder patch or replacing a Sweep payload. The capture contains an exact copy of the vendor Node source, its SHA-256, owner, mode, SELinux context, a complete path inventory of `/data/local/sweep`, hashes and metadata for direct Sweep and handback files, the host's existing ADB reverse mappings, and current vendor and Sweep process IDs. It does not copy `node.env`, `camera.env`, or any other private environment content.

```sh
python3 adapters/ohmni/tools/capture_handback_manifest.py \
  --serial "$ADB_SERIAL" \
  --unit 12 \
  --output /private/ohmni-handback/ohmni-12-before-owner-patch
```

The output directory must be new and retained outside the repository. It receives mode `0700`; `manifest.json` and `telebot_node.js.original` receive mode `0600`. The tool rejects a source snapshot whose locally computed SHA-256 differs from the hash reported through the same root-shell transport. Each ADB operation has a 30-second timeout and returns no command output on timeout.

## Known robot records

| Robot | Current evidence | Vendor-source status | Sweep and camera state that must be accounted for |
| --- | --- | --- | --- |
| Ohmni 12 (`10.10.0.74:5555`) | Runtime evidence: `/var/tmp/gauntlet/sweep-production/ohmni12-stationary-20260907T130411Z/evidence.json`. Camera evidence: `/var/tmp/gauntlet/sweep-production/ohmni12-camera-20260907T132520Z/evidence.json`. | The reviewed root-shell representation is LF, 37,444 bytes, SHA-256 `e128a740200b7f8d538414c8963109f1ee2f0475b340f9369814ba8446891300`; observed metadata is `1000:1000`, mode `0600`, context `u:object_r:system_app_data_file:s0`. Capture it again before any install. | The 13:04 record names the pre-deployment backups listed below. The 13:25 camera trial used `/data/local/sweep/camera-trial-20260907T132520Z`, `adb reverse tcp:8554 tcp:18554`, and a UYVY 1280×720 source at 8 fps. It records unchanged V4L2 state and protected vendor processes. The current handback capture at `/var/tmp/gauntlet/sweep-production/ohmni12-handback-20260907T141030Z/manifest.json` records `camera.env.before-camera-trial-20260907T132520Z` with SHA-256 `2938a47ced6e24c7e17c2f41ca0f52934034f86e5ca3c6061b35fa7f8e8612d5`, mode `0600`, and context `u:object_r:unlabeled:s0`. Its contents remain private. The capture identifies the vendor app and native Node owner from their executable and working directory, and separately records the observed camera process; it found no active Sweep runtime. |
| Ohmni 11 | The robot is unavailable. | `/var/tmp/gauntlet/sweep-production/odometry-probe-20260907/native-node-source-11/telebot_node.js` is a 38,700-byte CRLF transport capture, SHA-256 `f463feaab912999d3b4133fea049925ed95a6e32ee82856fb7ffa273cbe2ca3e`. It is useful for reviewed patch construction only. It does not prove the file bytes or metadata currently stored on the robot. | No handback capture exists. Do not install the owner patch, replace a payload, or remove a mapping until the capture command has completed against the available robot. |

For Ohmni 12, `/var/tmp/gauntlet/sweep-production/ohmni12-stationary-20260907T130411Z/evidence.json` records these remote pre-deployment backups, all mode `0600`:

- `/data/local/sweep/run.sh.before-20260907T130411Z`
- `/data/local/sweep/adapters/ohmni/runtime.py.before-20260907T130411Z`
- `/data/local/sweep/adapters/ohmni/return_controller.py.before-20260907T130411Z`
- `/data/local/sweep/node.env.before-20260907T130411Z`

Before using any listed backup, record its current SHA-256, owner, mode, and SELinux context in the handback capture. The 13:04 evidence records the paths but not those original hashes. Leave a missing or changed backup in place and escalate it rather than overwriting a file.

The source obtained through `adb exec-out` can acquire CRLF line endings in transit. The owner-patch installer and handback capture use `adb shell -T su 0 sh` plus base64 for source bytes and root-shell `sha256sum` for the comparison. Do not authorize a restore from the Ohmni 11 CRLF transport capture alone.

## Sweep-owned inventory

Sweep starts two optional processes from `/data/local/sweep`: `run.sh` records `node.pid` and `node.log`; `adapters/ohmni/camera.sh` records `camera.pid` and `camera.log`. They use PID validation and have no Android init, systemd, package, or boot-time service. A deployed payload can add `python/`, `lib/`, `ffmpeg`, `adapters/`, `planner/`, `relay/`, `run.sh`, and camera launcher files under that root. The handback capture records every pre-existing file path, plus hashes and metadata for direct Sweep launchers, configuration, and handback files. It records only selected process IDs and does not inspect Android service registrations. Deployment hashes identify removable additions without rehashing the bundled Python runtime.

The owner patch has three exact vendor paths:

- `telebot_node.js`, replaced only after its original bytes are copied to `telebot_node.js.sweep-owner-encoder.backup`.
- `telebot_node.js.sweep-owner-encoder.backup`, created from the vendor source with its metadata preserved after the installer verifies that no file already uses that path.
- `sweep_paired_encoder_sampler.js`, created only when that path did not already exist. The installer also rejects a pre-existing disabled-patch path.

The camera trial may create its named trial directory, the camera PID and log, and the host ADB mapping `tcp:8554 -> tcp:18554`. It does not change V4L2 configuration. The camera launcher reads existing private environment files and must leave them untouched.

## Restoration procedure

Perform this procedure only during a supervised handover window. It is a plan for that window; it does not authorize an immediate device change.

1. Open the private capture for that robot. Confirm the serial, the vendor source snapshot SHA-256, metadata, pre-existing Sweep inventory, and pre-existing ADB reverse list. Capture fresh metadata for the known 13:04 backup paths on Ohmni 12.
2. Stop only the Sweep processes through their own launchers: `camera.sh stop`, then `run.sh stop`. Each launcher refuses to terminate a PID that does not identify its own command. Confirm no `node.pid` or `camera.pid` process remains. Do not stop the normal Ohmni app or its native Node process by PID.
3. If the owner patch is installed, compare the current `telebot_node.js`, its backup, and the sampler module with the manifest and the hash-pinned rollback script. Run `rollback_owner_encoder_patch.sh` only when all three match. It restores the original vendor source and metadata, then removes the matching sampler module and matching disabled patched source. Any mismatch leaves the file untouched for review.
4. Restore a pre-existing Sweep file only from its matching recorded backup after its hash and metadata have been captured. Compare the current `/data/local/sweep` inventory with the pre-install manifest. Remove an added file only when it is recorded as Sweep-created for this deployment and its hash matches the deployment record. Leave unknown, changed, or user-created paths in place. Never use `rm -rf /data/local/sweep`.
5. Compare `adb reverse --list` with the capture list. The unit 12 capture already contains `host tcp:8554 tcp:18554`; its current presence does not prove origin. The 13:25 camera operation record identifies the Sweep camera trial as the operation that added the mapping after reboot. Remove it only after confirming that record and the current mapping. Keep every pre-existing or unrecognized mapping.
6. Leave `node.env`, `camera.env`, user recordings, and camera configuration untouched unless a separate capture proves a Sweep-owned replacement and supplies its original backup. Remove only the named camera trial directory when its manifest says Sweep created it and its contents match the recorded trial inventory.
7. Use the normal Ohmni app's standard launch or reboot procedure after the vendor source restoration. The encoder scripts deliberately do not restart that owner.

## Functional handover checklist

Record the result for each item after restoration:

- The normal Ohmni app starts and presents the expected robot identity without a Sweep process.
- Both normal camera views render through the Ohmni app. The test does not rely on the ADB tunnel or the Sweep publisher.
- The normal operator controls can enable and stop wheel motion under local supervision.
- The normal operator controls move and stop the head or neck through its expected range under local supervision.
- `adb reverse --list` matches the pre-capture list, or every remaining difference has an owner and reason.
- No Sweep `node.pid`, `camera.pid`, owner sampler module, patched vendor source, or known Sweep-created trial directory remains unless it is intentionally retained and recorded.
- The vendor source SHA-256, owner, mode, and SELinux context equal the handback capture.

This checklist leaves unknown original settings explicit. It does not establish a bitwise-identical device image or certify unrecorded vendor, network, account, audio, calibration, or physical-hardware state.

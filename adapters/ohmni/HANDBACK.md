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
| Ohmni 12 (`10.10.0.74:5555`) | Runtime evidence: `/var/tmp/gauntlet/sweep-production/ohmni12-stationary-20260907T130411Z/evidence.json`. Camera evidence: `/var/tmp/gauntlet/sweep-production/ohmni12-camera-20260907T132520Z/evidence.json`. Encoder handback and frozen trace: `/var/tmp/gauntlet/sweep-production/ohmni12-handback-20260907T173000Z/`. | The vendor source remains LF, 37,444 bytes, SHA-256 `e128a740200b7f8d538414c8963109f1ee2f0475b340f9369814ba8446891300`, owner `1000:1000`, mode `0600`, context `u:object_r:system_app_data_file:s0`. | The frozen trace showed deferred vendor address-59 reads, originally 5 ms apart, replayed 112 µs apart after an address-58 pair. The vendor source requires that spacing to avoid a serial-bus conflict. At 17:53 UTC, private backup `plugins-before-pacing.tar` (18,944 bytes) was captured, the prior hash-pinned plugin was removed, and the paced plugin was staged with loader SHA-256 `088506dd6c88adaf2975d0255712c2f87a44e6ce0c903d55a051a606c37ed757`, sampler SHA-256 `3b92be973bc73ce95243a1c305078917d59fd53d96c47de6ba822a8409740f39`, and trace SHA-256 `67b71a480b70387d1bdc931b8c54c90f9e6a86f08bb8e9972bd66828036d3acd`. Before a separately supervised reboot at about 18:17 UTC, private backup `plugins-before-native-arbitration.tar` (20,480 bytes, SHA-256 `ff1a0016c74efdd746493fa51782ab90e2661ca626897c6eee8e38404e6fd65c`) was captured. The old plugin was removed by its hash-pinned rollback, then the normal-vendor-query sampler SHA-256 `0af211c41eb9c22531008d15a1a985d78cb28404aaf6a9fae84fd68c8b0d66d6` was verified in place. The vendor source remained unchanged, and the vendor app and native Node PIDs were unchanged before reboot. The paced staging verified owner `1000:1000`, mode `0600`, and the vendor app-data context for its plugin files. Post-reboot encoder qualification remains pending. The 13:04 record names the pre-deployment backups listed below. The 13:25 camera trial used `/data/local/sweep/camera-trial-20260907T132520Z`, `adb reverse tcp:8554 tcp:18554`, and a UYVY 1280×720 source at 8 fps. It records unchanged V4L2 state and protected vendor processes. The current handback capture at `/var/tmp/gauntlet/sweep-production/ohmni12-handback-20260907T141030Z/manifest.json` records `camera.env.before-camera-trial-20260907T132520Z` with SHA-256 `2938a47ced6e24c7e17c2f41ca0f52934034f86e5ca3c6061b35fa7f8e8612d5`, mode `0600`, and context `u:object_r:unlabeled:s0`. Its contents remain private. The capture identifies the vendor app and native Node owner from their executable and working directory, and separately records the observed camera process; it found no active Sweep runtime. |
| Ohmni 11 (`10.10.1.110:5555`) | Handback capture: `/var/tmp/gauntlet/sweep-production/ohmni11-handback-20260907T150000Z/manifest.json`. The eight-second lidar probe returned a valid scan descriptor; visual rotation and point delivery remain unconfirmed. | The captured and post-plugin vendor source is LF, 37,444 bytes, SHA-256 `e128a740200b7f8d538414c8963109f1ee2f0475b340f9369814ba8446891300`, owner `1000:1000`, mode `0600`, context `u:object_r:system_app_data_file:s0`. A fresh private copy is `telebot_node.js.before-plugin`. | A normal boot recorded valid address-58 replies for both sides with 7–16 ms latency before the sampler reported `missing_encoder_reply` at poll 35. At 16:42 UTC, `frozen-trace-install-verification.json` verified the unchanged vendor source, plugin SHA-256 `088506dd6c88adaf2975d0255712c2f87a44e6ce0c903d55a051a606c37ed757`, sampler SHA-256 `43a7e3b6c1223f97636be9955ed80e5017928e423ea4034d6a1e4018b389b963`, trace SHA-256 `67b71a480b70387d1bdc931b8c54c90f9e6a86f08bb8e9972bd66828036d3acd`, and the hash-recorded manifest. All files have owner `1000:1000`, mode `0600`, and the vendor app-data context. The private pre-frozen-trace backup and its hash record were verified. The next normal vendor launch, reboot persistence, and encoder-sampling qualification remain pending. |

For Ohmni 12, `/var/tmp/gauntlet/sweep-production/ohmni12-stationary-20260907T130411Z/evidence.json` records these remote pre-deployment backups, all mode `0600`:

- `/data/local/sweep/run.sh.before-20260907T130411Z`
- `/data/local/sweep/adapters/ohmni/runtime.py.before-20260907T130411Z`
- `/data/local/sweep/adapters/ohmni/return_controller.py.before-20260907T130411Z`
- `/data/local/sweep/node.env.before-20260907T130411Z`

Before using any listed backup, record its current SHA-256, owner, mode, and SELinux context in the handback capture. The 13:04 evidence records the paths but not those original hashes. Leave a missing or changed backup in place and escalate it rather than overwriting a file.

The source obtained through `adb exec-out` can acquire CRLF line endings in transit. The owner-patch installer and handback capture use `adb shell -T su 0 sh` plus base64 for source bytes and root-shell `sha256sum` for the comparison. Do not authorize a restore from the Ohmni 11 CRLF transport capture alone.

## Sweep-owned inventory

Sweep starts two optional processes from `/data/local/sweep`: `run.sh` records `node.pid` and `node.log`; `adapters/ohmni/camera.sh` records `camera.pid` and `camera.log`. They use PID validation and have no Android init, systemd, package, or boot-time service. A deployed payload can add `python/`, `lib/`, `ffmpeg`, `adapters/`, `planner/`, `relay/`, `run.sh`, and camera launcher files under that root. The handback capture records every pre-existing file path, plus hashes and metadata for direct Sweep launchers, configuration, and handback files. It records only selected process IDs and does not inspect Android service registrations. Deployment hashes identify removable additions without rehashing the bundled Python runtime.

The current encoder installer writes four files under the vendor-managed plugin directory and leaves `telebot_node.js` untouched:

- `sweep_encoder_plugin.js`, the loader entrypoint.
- `sweep_encoder_plugin/sampler.js`, kept in a private subdirectory so the vendor loader does not instantiate it as a second plugin.
- `sweep_encoder_plugin/trace.js`, a passive record of encoder-address traffic for later diagnosis.
- `sweep_encoder_plugin.install`, containing the vendor source and installed file hashes used by rollback.

It refuses a changed vendor source, an existing plugin file, private directory, or manifest. The earlier owner-patch paths may exist only on a unit previously installed through that retired procedure; preserve them for its matching rollback instead of mixing procedures.

The camera trial may create its named trial directory, the camera PID and log, and the host ADB mapping `tcp:8554 -> tcp:18554`. It does not change V4L2 configuration. The camera launcher reads existing private environment files and must leave them untouched.

## Restoration procedure

Perform this procedure only during a supervised handover window. It is a plan for that window; it does not authorize an immediate device change.

1. Open the private capture for that robot. Confirm the serial, the vendor source snapshot SHA-256, metadata, pre-existing Sweep inventory, and pre-existing ADB reverse list. Capture fresh metadata for the known 13:04 backup paths on Ohmni 12.
2. Stop only the Sweep processes through their own launchers: `camera.sh stop`, then `run.sh stop`. Each launcher refuses to terminate a PID that does not identify its own command. Confirm no `node.pid` or `camera.pid` process remains. Do not stop the normal Ohmni app or its native Node process by PID.
3. If the encoder plugin is installed, compare `sweep_encoder_plugin.js`, `sweep_encoder_plugin/sampler.js`, `sweep_encoder_plugin/trace.js`, and `sweep_encoder_plugin.install` with the hash-pinned rollback script. Run `rollback_owner_encoder_plugin.sh` only when all match. It removes those plugin files and leaves the vendor source unchanged. For an earlier owner-patch installation, use only its matching legacy rollback procedure. Any mismatch leaves the file untouched for review.
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

# Integration reconciliation — September 7, 2026

This records the console/platform work reconciled during September 6–7, 2026
(America/Chicago). The integrated source is
[PR #321](https://github.com/worldofhacks/sweep/pull/321),
`codex/unified-fleet-control`. Its tested tree incorporates the prerequisite
navigation/observation stack and selected ground and aircraft changes. Individual
prerequisite PRs must not be replayed into this tree merely because they remain open.

## Integrated software

- One built console on port 5173, with build/session identity and the preserved
  gesture, map, camera wall and device modules. The composed relay uses port 8010.
- Real-device-only deployment, explicit identities and per-camera stream mappings,
  current-epoch observations, and media freshness based on advancing traffic.
  A ground transport receipt is not displayed as measured radio quality.
- #248 map authoring, immutable versions, validation, audited approval and active
  selection; #143 frozen destination reviews with invalidation and confirmation.
  These platform reviews do not dispatch navigation.
- Ground manual, gesture and confirmed language paths for bounded forward/yaw
  pulses, with current source/pose/authority checks, mandatory measured LiDAR
  clearance and truthful STOP handling. Speech requires explicit qualification.
- Aircraft authority, signed height/freshness policy and landing recovery safeguards,
  preserving the separate RC-takeover latch and qualified deployment requirements.
- Ground camera publishing permissions and explicit additive stream provisioning;
  Android WebRTC 16 KB compatibility with binary alignment checks in CI.
- Documentation of the additive fleet, two cameras and one LiDAR per ground robot,
  and one camera plus an owner-reported, unverified infrared sensor per aircraft.

The original issue branches remain available:
`codex/issue-143-console-navigation` and `codex/issue-248-console-map-authoring`.
The older [PR #268](https://github.com/worldofhacks/sweep/pull/268) is divergent:
its map/platform work and selected compatibility fixes were incorporated, but its
entire tree was not. Aircraft body-pulse controls remain unsupported in this backend.

## Preserved unfinished work

The older checkout's 88 modified/untracked source paths were preserved unchanged in
[archive commit a7e120c](https://github.com/worldofhacks/sweep/commit/a7e120c),
with an additional status document, on
`codex/archive-ground-telemetry-20260907`. Its earlier node join/status protocol is
incompatible with the current field runtime. This is an archive, not a deployment
or a merge candidate. The original local checkout was left intact.

[Issue #327](https://github.com/worldofhacks/sweep/issues/327) tracks selective
migration of richer battery/sensor/encoder telemetry; neck/lights/speech/screen
controls; the local stop/spotter interface; automatic two-camera publisher lifecycle;
LiDAR ownership, health discovery and reconnect recovery; and complete aircraft
camera-control wiring where supported. Current host camera permissions alone do
not start the robot's second publisher. Older sparse-scan avoidance and direct
encoder polling must not replace the current qualified motion path.

## Qualification and deployment

Software validation covers Python, console, JVM/Android, browser and recording
contracts. It does not establish physical robot or aircraft acceptance. The Android
alignment check establishes binary compatibility properties; installation and
physical operation remain separate. Private configurations, captured device data,
logs and APKs are excluded from this source reconciliation.

During the stationary G-01 work, one real camera produced advancing encoded video
and the node delivered authenticated observations. Encoder faults prevented
qualified pose/drive readiness. Second-camera and browser decoded-frame acceptance,
continuous encoder operation, measured LiDAR clearance, drive/STOP tests, phone
installation and aircraft tests remain outstanding. No positive wheel-motion or
flight command was issued during this integration.

The local session was left disarmed with no connected devices. The console serves
an immutable build, so a GitHub merge does not itself redeploy or reconnect the
devices. Full autonomous operation additionally requires the qualified localization,
map/route approvals, measured policies and per-device acceptance described in the
[unified fleet control guide](unified-fleet-control.md). Hardware-dependent issues
remain open.

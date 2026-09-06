# Mixed-fleet console checkpoint

The `codex/mixed-fleet-console` integration combines the two-drone bridge/readiness
work with class-aware relay, console, and autonomy changes. It is an integration
checkpoint, not physical flight or ground-motion acceptance.

## Live wall

`Live → All devices` is the default view. Each reported device gets one tile;
joins add tiles, offline devices remain visible, and a changed connection epoch
restarts only that device's player. Aircraft and ground views remain available.
The supported registry and media configuration cover four aircraft and four
ground vehicles. Each deployment still provisions individual publisher keys.

For the two-drone/three-robot setup, configure the following identities in one
relay session:

| Device ID | Class | Unit | Media path |
| --- | --- | --- | --- |
| 1 | aircraft | 1 | drone1 |
| 2 | aircraft | 2 | drone2 |
| 11 | ground_vehicle | 1 | ground1 |
| 12 | ground_vehicle | 2 | ground2 |
| 13 | ground_vehicle | 3 | ground3 |

Selection and signed commands retain the global device ID. Tiles derive media
paths from the authenticated class/unit mapping. Source availability alone does
not prove browser playback: the player waits for an actual frame, detects a
visible three-second video stall, and reconnects failed sessions independently.
See [console playback](../console/README.md#live-playback) for timing and cleanup.

## Evidence recorded on 2026-09-06

- The production WHEP player simultaneously rendered all three real ground
  camera streams at 640×480 and approximately 15 fps. A sample after 31 seconds
  recorded 476, 471, and 478 total frames. This was a camera transport check with
  no motion controls; it did not establish a shared five-device control roster.
- The live robot publisher scripts lacked an explicit short keyframe interval.
  Delayed first video caused the old five-second startup deadline to fail despite
  established ICE and incoming RTP. Initial frame acquisition now has its own
  bounded 20-second window. Already-playing feeds use the shorter stall check.
- The D02 Pixel has the native-compatibility/readiness APK from `5a9b2a3` installed
  and its installed package hash verified. The phone subsequently left USB before
  provisioning the new mixed session. D01 was not available over ADB.
- No physical aircraft or ground motion was issued from this integration.

## Remaining handoff

Connect each drone phone to the laptop long enough to verify its installed build,
unique device ID/key, relay URL, and identical session ID. Then verify the phone
through its own RC/aircraft, current-epoch telemetry, camera publisher, and genuine
operator/control-authority readiness. Positive GPS quality alone does not qualify
a common indoor world frame or physical clearance.

The ground hardware package remains owned by its active implementation lane.
Before deploying it, verify measured stop completion after acknowledgments,
qualified pose/frame evidence, calibrated motion, local deadman behavior, and
collision protection. Do not adopt the temporary flight-state shim, hard-coded
home offsets, or claimed readiness from the hardware spike.

Current tracker requirements remain in [#239](https://github.com/worldofhacks/sweep/issues/239),
[#246](https://github.com/worldofhacks/sweep/issues/246), and
[#94](https://github.com/worldofhacks/sweep/issues/94). The shared observation
envelope and physical world-frame qualification remain separate integration
dependencies. This branch must not be treated as completing those issues.

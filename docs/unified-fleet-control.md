# Unified fleet control integration

One console on port 5173 preserves the existing map, gesture, live camera wall, device
inspection, manual and speech modules. The composed relay on port 8010 routes real device
commands and observations. The production profile uses authenticated device observations
and configured media paths. Test devices and sample feeds remain confined to isolated tests.

## Integrated input and control paths

| Path | Implemented behavior | Required live evidence/configuration |
|---|---|---|
| Ground manual | Confirmed bounded forward or yaw pulse; HOLD/ESTOP | Current signed pose source, drive readiness, spotter, local stop and full LiDAR clearance |
| Ground gestures | Ground profile stages the same confirmed pulse | Same physical gates; fresh preview and neutral-release behavior |
| Typed language | Explicit pulse forward/left/right and ground return phrases | Same exact selected device/epoch/source and confirmation gates |
| Spoken language | Transcription, audited compiler plan, one-shot language binding | Provider keys and explicit qualified input/intent allowlist; empty means unavailable |
| Ground return | Execute one externally approved fixed return corridor | Signed route approval, matching session/device/epoch/origin, measured geometry and clearance |
| Aircraft supervised vertical | Signed bounded takeoff, hold and land | Compatible phone build, current local height, physical RC/operator, authority and explicit vertical policy |
| World navigation | Existing separate navigation deployment path | Signed deployment, qualified map/world localization and safety configuration |
| Console map/zone workflow | Authenticated authoring, approval, active map and frozen destination review | Review remains non-dispatchable unless one aircraft has a route qualified by the loaded flight deployment |

Aircraft timed body-pulse controls from the preserved interface remain unsupported by
this backend and are visibly disabled. Ground pulses have no reverse or combined
linear/angular mode.

Ground controls never send takeoff or aircraft body pulses. Pulses are short one-shot
requests; retries require a new preview and confirmation. Selection changes, reconnects,
pose-source changes and authority loss invalidate earlier motion requests. The relay checks
ground readiness again under its lock immediately before signing. Local checks remain
independent of the host and re-run during motion.

Ground avoidance has no no-LiDAR mode. Measured footprint, sensor mounting, stopping
distance and clearance margin are mandatory. All 360 angular bins need fresh positive
returns outside the swept clearance. Missing returns are unknown space. A failed STOP
cannot be acknowledged as successful completion. When disabling is requested, STOP and
disable are attempted independently.
See the [Ohmni guide](../adapters/ohmni/README.md) and matching encoder installation evidence.

## Deployment policies

Choose an explicit world policy (`SWEEP_PLANNING_JSON`, `SWEEP_SAFETY_JSON` plus any
qualified localization/navigation assets) OR `SWEEP_SUPERVISED_VERTICAL_JSON`. They cannot
be combined. Neither policy is populated from the operational example file.

The supervised aircraft policy requires declared vertical clearance, battery/link limits,
freshness/clock bounds, operator timeout, conflict window, 1.8 m takeoff target and bounded
maximum height. Its signed TAKEOFF command carries the effective height ceiling and
local-height freshness limit to the phone. The phone retains them through climb, hover
and HOLD; incompatible old commands/builds refuse rather than use an implicit policy.
The [DJI guide](../adapters/dji_mini3/SUPERVISED_VERTICAL.md) is the exact parameter and
compatibility reference. A fresh signed virtual-stick-only drop can permit confirmed LAND;
actual RC takeover remains an independent latch and is never bypassed.

Ground controls can share either composed profile. An advertised `come_home` action does
not establish route availability. Ground dispatch requires a configured return ID, and the
node requires its matching external approval. The supervised profile adds ground return
only when the relay return ID is configured. The aircraft supervised profile does not
provide arbitrary horizontal or named-goal flight. Room models, local
odometry or a label in the map UI cannot substitute for qualified world localization.

## Current qualification boundary

The [integration reconciliation record](integration-reconciliation-2026-09-07.md)
accounts for the older telemetry/peripheral work preserved separately. Those source
files are not implemented capabilities of this runtime.

Source integration and automated tests do not establish a fleet demonstration. The local
composed configuration preserves the prior world-policy values, starts disarmed, and has
no qualified navigation assets or speech allowlist. Reusing those values does not qualify
them for the current devices or room. G-01 recently answered at its saved network address, but its ADB connection did not
complete. G-02 and G-03 remained unreachable at their saved addresses, and no DJI
controller/bridge was attached during those checks. The additional ground units are in scope but their
identities, addresses, camera publishers and sensor bindings are not provisioned here.

G-01 previously produced one camera stream and authenticated diagnostic observations.
Its retained owner-encoder fault prevents qualified odometry. Browser decoded-frame
acceptance, the second camera, new coherent encoder installation, measured clearance and
physical drive/stop acceptance remain pending. Prior stationary results are not a current
live feed. No flight or positive wheel-motion command was issued during this integration.

Before a physical demonstration, establish actual powered connections and local stop/RC
coverage, commission each camera and sensor, configure measured policies, then record
single-device manual stop/control, gesture and language acceptance before concurrent
operation. Autonomous routes additionally need their signed measured deployment assets.
Keep hardware-dependent issues open until those runs produce actual evidence.

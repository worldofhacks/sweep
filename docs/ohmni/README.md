# Mixed aircraft and ground fleet integration

**Delivery status: preserved integration branch, not shipped or installed.**
PR #255 was closed while the issue tracker was being aligned around the shared
observation contract and approved-map dependency chain. The original contract below
records the handoff design; it is not evidence that this entire implementation is an
accepted production architecture. See [delivery priorities](delivery-priorities.md)
for the compatibility decisions that must be resolved before focused changes land.

This work implements the owner's September 6, 2026 mixed-fleet request, using the
[preserved contract](contract-spec.md) and [MVP plan F.2](../mvp-plan.md). The current
[issue #239](https://github.com/worldofhacks/sweep/issues/239) describes a narrower,
named-zone demo. This integration does not close its mapping, registration, occupancy
veto, or hardware-evidence children.

## Contract provenance

`contract-spec.md`, `relay-map.md`, and `console-map.md` are exact copies of the
original scratchpad documents. The contract's SHA-256 is
`aa097d23875f7afc186eba9c3309a8e32d39fc07fc8ef821abc458fde95440f9`.
The maps refer to main at `cf73068`; line numbers have since moved.

The original scratchpad, live autonomy shim, and secret configuration were also
backed up privately on the host under `~/sweep-preserved/ohmni-2026-09-06/`.
The secret configuration is not part of the repository.

The later owner handoff corrects several measurements in section 12:

- `manual_move 250 -250` drives forward; `manual_move 250 250` turns clockwise.
  `pre_drive` and `pre_rot` are ineffective while the vendor wheel loop runs.
- G-01 and G-02 carry lidar kits; G-03 does not.
- Robot installation uses adb and the Android musl runtime, not Docker.
- RTSP publishing requires TCP on the current macOS Docker network.
- Lidar mounting offset and angular handedness need physical measurement before
  scans can truthfully be published in the robot frame.

## Controls and coordinate frames

Aircraft and robots use one positive device-id space, with stable per-class unit
labels: D-01, D-02, G-01, G-02, G-03. Manual buttons and the fleet gesture profile
emit Intent v1 through the relay, planner, arbiter, and authenticated node path.
Selection can address one device, a subset, a class, or the whole ready fleet.

Ground translation uses room/world X and Y axes. Aircraft retain the configured
`translation_frame`. Telemetry v1 is unchanged and supplies no yaw; this integration
does not invent a heading. Use a world-frame session when demonstrating mixed
directional language. Ground launch positions must be explicitly placed in the same
room frame; wheel odometry by itself does not register robots to one another.

Ground GOTO commands stay at z=0 with their own speed cap. Class groups form around
their own centroids, with a singleton class holding its position. Aircraft-only
actions targeted at robots receive a typed refusal. `land_all` addresses aircraft;
`hold` and `estop` cover both classes. Cross-class collision clearance is not provided
by the pinned contract's within-class spacing rule.

Main's C2 release gate is preserved: formation, spacing, sweep, and disarm capabilities
are currently exposed through the C2 simulator release. Connecting real robots does
not bypass that gate. Real fleet formation acceptance remains outstanding.

## Sensing and maps

Every connected device reports the sensors it actually has. Lidar packets are
bounded, rate-limited, and projected into the console with scan age and device epoch.
The map PNG includes world placement headers that are readable across the allowed
console origin. The console can reset the session map.

The occupancy raster is a live display artifact. It is not an approved static map,
SLAM, an obstacle-free-space certificate, or the expiring veto layer required by
issue #245. A single scan plane cannot detect obstacles above or below that plane.
An absent or stale scan never means clear space. Mini 3 aircraft do not gain lidar
hardware from this integration, and G-03 still requires a kit or the explicitly
configured spotter procedure.

## Acceptance evidence

Automated tests exercise a composed relay on an ephemeral localhost port with two
aircraft nodes and three ground nodes. They verify individual, mixed-subset, and
whole-fleet translation; scans from the two equipped ground fixtures; no fabricated
scan on the unequipped fixture; map PNG decoding; aircraft-only landing; and fleet
stop. This is protocol and software evidence, not physical motion evidence.

Before installing and enabling the new runtime on a robot:

1. Read the packaged adapter runbook and configure each launch pose and measured
   lidar offset/handedness. Check the local screen STOP with the wheels stationary.
2. Verify sensor freshness, forward-sector coverage, camera publishing, encoder
   direction and unwrapping, heartbeat hold, and latched failsafe with a spotter.
3. On one robot at a marked start line, record a one-foot forward move and a
   90-degree turn. Use an execution TTL long enough for the configured slow turn;
   retain the deadman independently. Record JSONL evidence and measured distances.
4. After individual acceptance, verify three-robot coordinated motion with confirmed
   room registration and the required capability release. Do not substitute passing
   simulation for this gate.

The integration checkout does not replace the running relay or its shim until the
new hardware runtime has been installed and checked. Port 8000 belongs to a separate
session and is outside this work.

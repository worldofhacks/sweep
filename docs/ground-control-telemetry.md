# Ground control and device telemetry

The relay accepts the nodekit/Ohmni telemetry and peripheral contracts used by the
console. This is software integration; it does not establish measured drive speeds,
LiDAR mounting, shared-world registration, stopping distance, or hardware acceptance.
Deployment remains separate and must retain explicitly recorded motion configuration.

## Reported facts

Authenticated, current-epoch `node_status` frames may include `device_telemetry`.
The optional JSON object is copied before storage, state projection, fanout, and audit.
Legacy frames without it remain valid. Disconnect and rejoin retire the old status;
an old-epoch report cannot restore it.

The shared `nodekit.telemetry.device_telemetry_payload` validator permits at most
16 KiB of UTF-8 JSON, four levels including the root, 4,096 values, 128 keys per
object, 512 entries per list, and 512 characters per string. Keys are bounded
snake_case identifiers. Numbers must be finite and within the JSON-safe integer
range. Missing hardware readings remain `null` or absent.

Custom readings never grant wheel or flight authority. The existing membership,
readiness, LiDAR, motion telemetry, operator, positioning, and class gates still
apply. An encoder-derived launch pose is not proof of registration to an approved
world map. A publisher's progressing frames are producer evidence, not proof that
MediaMTX or a browser received or displayed them.

## Confirmed peripheral requests

The console sends `robot_peripheral` with one explicit device ID and `confirm:true`.
The relay's C1 and C2 profiles implement this name; language, keyboard, and webcam
sources cannot emit it. The existing voice compiler schema remains unchanged.

| Kind | Exact arguments besides `kind` |
| --- | --- |
| `neck` | Integer `position`, 300–650 |
| `lights` | Integer `h`, `s`, `v`, each 0–255 |
| `speech` | Canonical printable `text`, 1–240 characters |
| `screen` | Canonical printable `text`, 0–240 characters |

The target must be a connected ground vehicle advertising `robot_peripheral_v1`
and that specific kind. Every request requires a present operator and current-epoch
node status no older than five seconds with a nominal watchdog lease. Neck movement
also requires the node's current control authority and is blocked by network stop.
The node repeats its local enable, spotter, and docking checks. Screen, light, and
speech requests remain independent of wheel/LiDAR authority while the node lease
is nominal.

The planner freezes one command with the exact target, epoch, roster, and arguments.
The arbiter checks those bindings at planning and immediately before adapter I/O.
The remote adapter emits the signed `robot_peripheral` command and returns the real
node acknowledgement. No simulator fallback, wheel wake, or motion recovery command
is generated for a failed peripheral request. A completion acknowledgement is command
handling evidence; it is not measured neck position, light state, or audible speech.

## Camera contract compatibility

The console-only `camera_control` intent retains the existing aircraft contract:
`ready`, `photo`, or `gimbal` with integer `pitch_mdeg`. It requires one explicitly
confirmed aircraft, `camera_control_v1`, current node authority and nominal lease,
an RC safety operator, and current reported support for the requested operation.
Gimbal requests additionally require reported pitch limits and an in-range target.
Existing signed `camera_ready`, `capture_photo`, and `set_gimbal_pitch` commands
carry the work. A photo acknowledgement does not create a retrieved media bundle.

This integration supplies no ground camera capture/tilt implementation and changes
no Android aircraft implementation. Nodes without the versioned camera capability
are refused. Camera failures cannot emit flight recovery commands.

The generic `navigate` intent remains unavailable for execution. The platform's
frozen navigation review and verification flow continues to report refusal or
invalidation with `dispatchEligible:false`. Existing ground translation/HOLD,
mixed-fleet stopping, C2 boundaries, and the 64-device identity limits are preserved.

## Local validation

Relay tests cover optional telemetry roundtrips, bounded input rejection, audit and
state projection, reconnect retirement, typed peripheral/camera command signing,
explicit-target and epoch binding, repeated capability/freshness checks, unsupported
operations, and failed-request isolation from wheel/flight recovery. Existing
planner, arbiter, bridge, voice, mixed-fleet, and platform tests remain regression gates.
All fixtures are isolated; these tests contact no devices or external services.

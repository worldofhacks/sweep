# Webcam-to-control-localization adapter

`perception/webcam_control_adapter.py` converts `webcam_localization` JSONL pose
records into the tag sensor records `control_publisher.enqueue()` accepts. It runs
as a pipe stage between the two:

```bash
python -m perception.webcam_localization --config webcam.json --output pose.jsonl \
    --duration 60
python -m perception.webcam_control_adapter --config adapter.json \
    --input pose.jsonl --connection-epoch 7 --run-id bench-01 \
    --output sensor.jsonl
```

The adapter output is replay-shaped tag records (`kind`, identities, `position_map_enu_m`,
`covariance_map_enu_m2`, `extrinsics`); it does not talk to the relay and grants no
control authority.

## Why a translation layer is needed

`webcam_localization` is a single-camera vision pipeline. It knows about a camera
stream, a tag map, and a filtered position estimate; it has no concept of "which
aircraft," "which relay session," or "which control-fusion frame." `control_publisher`
fuses evidence from many aircraft and sources and refuses anything that isn't
provenanced against its pinned configuration. The gap between the two is exactly the
set of identities and physical transforms that only deployment configuration or a
physical survey can supply -- see the field-by-field list below.

## Non-derivable fields and where each one comes from

| Field | Not in webcam_localization output because | Source |
| --- | --- | --- |
| `drone_id` | the vision pipeline has no aircraft concept, only a camera stream | deployment config: which aircraft owns this camera |
| `connection_epoch` | issued by the relay when an aircraft authenticates; changes on reconnect | relay state (the publisher's live `LiveBinding`, or a fixed recorded value for replay) |
| `map_id`, `geometry_id` | the control fuser pins these per aircraft; the tag map's own manifest ID (when present) is not guaranteed to match the control layer's naming | deployment config |
| `clock_id` | webcam_localization emits raw `time.monotonic()` seconds with no clock identity string | deployment config, paired with a real clock mapping (see below) |
| `source_id`, `camera_calibration_id`, `body_extrinsics_id` | these are labels the control fuser is pinned to; webcam_localization only has a `camera_serial` and a `calibration_sha256` | deployment config (the labels must match what the aircraft's `ControlLocalizationConfig` expects) |
| `source_verified`, `timing_verified` | `webcam_localization` explicitly emits `capture_time_verified: false` and `publisher_identity_verified: false` for every record -- it does not verify either | an owner decision, made only after real verification exists (see "Trust flags" below); never a default |
| `position_map_enu_m` frame | `T_map_body`/`T_world_body` is expressed in the tag map's own survey frame, which is only guaranteed `right_handed_z_up`, not aligned to whatever frame the aircraft's other sensor sources (telemetry, height) use | a physically surveyed `map_to_map_enu` rigid transform (deployment config, see below) |
| `covariance_map_enu_m2` | the PnP solve reports a reprojection RMS, not a calibrated position covariance | a fixed, configured baseline covariance (see "Covariance" below) -- explicitly not a live per-frame uncertainty estimate |
| `extrinsics.matrix` (capture-time body-to-camera) | `TagLocalizer` bakes in one static `T_body_camera` for the whole run; it never reads gimbal telemetry | a capture-alignment document (`body_to_gimbal`, `gimbal_to_camera`) plus a gimbal attitude, both deployment config -- see "Body extrinsics" below |

`event_id` is not in this table: the adapter mints it itself (one per converted
record), since nothing downstream needs it to trace back to anything physical.

### Clock identity

`webcam_localization` and `control_publisher` both read `time.monotonic()`, which is
the same clock across processes on one host and one boot. The adapter passes
`capture_time` through unchanged and assumes the deployment's `clock_id` /
`ClockMapping` is configured so that assumption holds (an identity mapping when
adapter and publisher share a host). Running them on separate hosts requires an
actual clock synchronization measurement; `perception/ohmni_clock_probe.py` already
implements that (`ClockProbe`, `MonotonicClockMapping`) and is out of scope for this
adapter.

### Trust flags: `source_verified` / `timing_verified`

These two booleans exist specifically to stop unverified evidence from reaching
control fusion; `control_localization.py` refuses any record where either is not
`True`. `webcam_localization`'s own capture time is an **estimate** (decode time
minus a measured p50 decode latency, not a hardware timestamp), and it has no
authenticated notion of "this frame really came from this aircraft's camera." Setting
either flag to `true` is an assertion that the owner has independently established
that verification -- e.g., an authenticated capture path and a measured, bounded
clock-latency budget -- not something this adapter can compute from pixels. The
config format requires them as explicit booleans with no default, precisely so this
choice cannot be made silently.

### Covariance

`control_localization.py` requires a positive-definite 3x3 covariance whose
eigenvalues fall inside a configured bound. The webcam PnP solve does not produce
one: it reports a reprojection RMS in pixels, and the Kalman-filtered position
covariance in `webcam_localization`'s own output is a fusion artifact of an assumed
`fix_variance_m2 = 0.01` filter tuning constant, not a measured uncertainty (see
`WEBCAM_LOCALIZATION.md`).

The adapter therefore uses **a fixed, deployment-configured covariance**
(`position_covariance_map_enu_m2`), applied identically to every fix. This is the
same shape of decision `world_localization.py` makes with its pinned
`MeasurementUncertainty` artifact, except that pipeline also adds a
range/angular-error-dependent term computed from live gimbal-tracking latency, which
this adapter cannot: there is no live gimbal telemetry to derive an angular error
from (see "Body extrinsics"). **Limitation:** a fixed covariance does not shrink for
a close, square-on, low-reprojection-error tag or grow for a distant, oblique, or
noisy one. It should be set conservatively -- sized from bench characterization at
the worst geometry the deployment expects to fly -- and re-measured if the mount,
lens, or expected tag range changes. It is not a substitute for a real per-frame
uncertainty model; that would require the same kind of live extrinsics-uncertainty
tracking `world_localization.py` has, which this pipeline does not.

## Body extrinsics: the capture-alignment document

`control_localization.py`'s `BodyExtrinsics` requires a rigid body-to-camera
transform stamped with the exact capture time of the fix it accompanies. On a
gimbal-mounted camera that transform genuinely changes as the gimbal moves, so it
cannot be a single constant pinned at startup the way `TagLocalizer.T_body_camera`
is today.

This adapter's capture-alignment document (schema below, template at
`perception/fixtures/webcam_capture_alignment.template.json`) has three parts:

- `body_to_gimbal`: the static, measured pose of the gimbal's rotation center
  relative to the aircraft body, when the gimbal reads `(0, 0, 0)`.
- `gimbal_to_camera`: the static, measured pose of the camera's optical center
  relative to the gimbal's own rotation center.
- a live gimbal attitude, `(yaw_deg, pitch_deg, roll_deg)` in the
  `intrinsic_zyx_degrees` convention (`R = Rz(yaw) @ Ry(pitch) @ Rx(roll)`, matching
  `world_localization.py`'s convention exactly).

The adapter computes `body_to_camera = body_to_gimbal @ R(attitude) @ gimbal_to_camera`
per fix.

**Limitation, stated explicitly:** `webcam_localization` has no live gimbal telemetry
feed. This adapter's config carries one fixed `gimbal_attitude_deg` for the entire
run, gated behind an explicit `gimbal_locked: true` acknowledgment -- it is only
correct if the gimbal is mechanically held at that attitude for the whole capture
session. If the gimbal moves during flight, this adapter's extrinsics are wrong for
every frame captured off that fixed attitude, and a live per-frame gimbal-attitude
source must be plumbed in instead (the document format already carries a place for
that: replace the fixed config attitude with a per-record live sample, matched to
each fix's `capture_time`, the way `world_localization.py`'s `_aligned_body_camera`
already does for the full MSDK pipeline).

### What to physically measure on the DJI Mini 3

Body axes follow this codebase's `forward_left_up` convention for a `body` frame
(`docs/observation-contract.md`): **X forward** (nose direction), **Y left**,
**Z up**, all in meters, origin at the aircraft's body reference point (its marked
center of gravity / IMU reference, not the gimbal).

1. **`body_to_gimbal` translation** (`x_m`, `y_m`, `z_m`): the straight-line offset,
   along those three body axes, from the body origin to the gimbal's mechanical
   roll-axis center (its yaw/pitch/roll rotation point, not the lens). Use a ruler
   or calipers with the gimbal centered (all axes at zero). `z_m` will typically be
   negative, since the gimbal hangs below the body reference point.
2. **`body_to_gimbal` rotation**: leave at the template's identity
   (`qx=qy=qz=0, qw=1`) unless the gimbal mount is deliberately twisted relative to
   the body -- true for almost no consumer gimbal mount, including the Mini 3's.
   If it is twisted, this needs an actual angle measurement and cannot be filled in
   from the template default.
3. **`gimbal_to_camera` translation** (`x_m`, `y_m`, `z_m`): the offset, along the
   *gimbal's own* forward/left/up axes (which equal the body axes when the gimbal
   reads zero), from the gimbal's roll-axis center to the camera lens's optical
   center. This is small on the Mini 3 -- measure it anyway; do not assume zero.
4. **`gimbal_to_camera` rotation**: the template pre-fills
   `(qx, qy, qz, qw) = (0.5, -0.5, 0.5, -0.5)`. This is **not a physical
   measurement** -- it is the fixed coordinate-convention rotation from the
   gimbal's `forward_left_up` axes to the camera's `right_down_forward` (OpenCV)
   optical axes that `docs/observation-contract.md` already declares for every
   camera frame in this codebase. Do not change it unless the camera is mounted at
   a deliberate angle inside the gimbal (again, not true for the Mini 3's
   integrated camera/gimbal unit).

Do not fill in the translation placeholders with guessed or spec-sheet numbers; they
must come from measuring the actual physical unit.

**Pitch sign is not assumed.** `intrinsic_zyx_degrees` fixes the rotation formula
(`R = Rz(yaw) @ Ry(pitch) @ Rx(roll)`) but not which physical direction is positive.
Whether the DJI Mini 3's reported gimbal pitch is more negative or more positive when
pointing down must be confirmed against the actual MSDK telemetry before trusting any
specific `gimbal_attitude_deg` value in flight; this adapter does not assume a
direction and its tests check only the rotation math, not a physical nadir claim.

## Adapter config document (v1)

```json
{
  "v": 1,
  "drone_id": 1,
  "map_id": "...",
  "geometry_id": "...",
  "clock_id": "...",
  "source_id": "...",
  "camera_calibration_id": "...",
  "body_extrinsics_id": "...",
  "source_verified": false,
  "timing_verified": false,
  "map_to_map_enu": {"measured": true, "matrix": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]},
  "position_covariance_map_enu_m2": [[0.01,0,0],[0,0.01,0],[0,0,0.01]],
  "gimbal_locked": true,
  "gimbal_attitude_deg": {"yaw_deg": 0.0, "pitch_deg": -90.0, "roll_deg": 0.0},
  "capture_alignment": { "...": "see perception/fixtures/webcam_capture_alignment.template.json" }
}
```

`map_to_map_enu` is the physically surveyed rigid transform from the tag map's own
survey frame into whatever frame the deployment's other sensor sources (telemetry,
height) already use and calls `map_enu`. It is required and explicit, never a
silent identity default, even when a survey shows the two frames coincide.

# Capture-alignment measurements needed from the owner

File: `webcam_capture_alignment.json` (copied from
`perception/fixtures/webcam_capture_alignment.template.json`, `alignment_id` filled in).
Four numbers are placeholders (`MEASURE_METERS_...` strings); everything else is
either a fixed coordinate-convention constant or a deployment identifier, not a
physical measurement.

## What to measure, on the physical DJI Mini 3

Axes are `forward_left_up`: **X forward** (nose direction), **Y left**, **Z up**, in
meters. Put the gimbal at its centered zero position (yaw/pitch/roll all zero, camera
level) for both measurements.

1. **`body_to_gimbal`** -- from the aircraft's body reference point (its marked
   center of gravity / IMU reference, not the gimbal) to the gimbal's mechanical
   roll-axis center (its yaw/pitch/roll rotation point, not the lens):
   - `x_m`: forward offset (usually near zero or slightly negative -- the gimbal sits
     close to the nose).
   - `y_m`: left offset (should be at or near zero -- the gimbal is centered).
   - `z_m`: vertical offset. Expect a **negative** number: the gimbal hangs below the
     body reference point.
2. **`gimbal_to_camera`** -- from the gimbal's roll-axis center to the camera lens's
   optical center, along the gimbal's own forward/left/up axes (equal to the body
   axes when the gimbal reads zero):
   - `x_m`, `y_m`, `z_m`: small on the Mini 3's integrated camera/gimbal unit, but
     measure them -- do not assume zero.

Use calipers or a ruler. Do not substitute a number from a DJI spec sheet or CAD
drawing; the field only accepts a caliper reading of this physical unit.

## What is already filled in, and why it is not a measurement

- `body_to_gimbal` rotation (`qx=qy=qz=0, qw=1`, identity): correct unless the gimbal
  mount is deliberately twisted relative to the body, which is not true for the
  Mini 3's stock mount.
- `gimbal_to_camera` rotation (`qx=0.5, qy=-0.5, qz=0.5, qw=-0.5`): the fixed
  coordinate-convention rotation from the gimbal's `forward_left_up` axes to the
  camera's `right_down_forward` (OpenCV) optical axes, per
  `docs/observation-contract.md`. Not a physical angle to measure.
- `alignment_id`: a deployment label, not a measurement.

## Gimbal attitude for this deployment

The gimbal is mechanically locked at `pitch_deg = -90` (straight down), `yaw_deg =
0`, `roll_deg = 0`, set in `config/webcam_control_adapter.json`'s
`gimbal_attitude_deg`, gated behind `gimbal_locked: true`. Before trusting that
`-90`, confirm against the real MSDK telemetry which sign the Mini 3 reports for
"gimbal pointing straight down" -- `perception/WEBCAM_CONTROL_ADAPTER.md` flags this
explicitly: the rotation formula is fixed, but its sign for this airframe is not
assumed anywhere in this codebase.

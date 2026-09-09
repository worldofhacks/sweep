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

## What was actually measured (2026-09-09) and how it was filled in

The owner reported a single ruler measurement, to the nearest centimeter, of the
body-to-camera offset with the gimbal locked at its flight attitude: **8 cm forward,
on the centerline, 5 cm down from the top of the fuselage**. This is not a caliper
reading of `body_to_gimbal` and `gimbal_to_camera` separately -- it is one combined
number, referenced to the top surface rather than the body reference point.

- **x_m (forward) = 0.08, y_m (lateral) = 0.0**: taken directly from the ruler
  reading. Nearest-centimeter resolution gives roughly **±0.5 cm** rounding
  uncertainty on each.
- **z_m (vertical) = -0.015**: derived, not measured directly. The owner's 5 cm is
  from the top surface; the Mini 3 fuselage is roughly 7 cm tall, so its center is
  assumed to sit ~3.5 cm below the top, putting the lens ~1.5 cm below body center.
  This body-height assumption is the dominant source of error here: **treat this
  z value as accurate to about ±1.5 cm, not ±0.5 cm** -- it is a stated assumption,
  not a caliper-grade figure.
- **Split across the two legs**: the gimbal is mechanically locked and will not move
  in flight, so `body_to_gimbal` and `gimbal_to_camera` cannot be separated from one
  combined measurement without an extra reference point nobody has. The whole
  measured translation went into `body_to_gimbal` (`x_m=0.08, y_m=0.0, z_m=-0.015`);
  `gimbal_to_camera`'s translation was set to `(0, 0, 0)`, keeping only its fixed
  coordinate-convention rotation. This choice makes `T_body_camera` exactly equal to
  the measured offset *regardless of the dynamic gimbal rotation fed into
  `body_to_camera()`* (see `_intrinsic_zyx_rotation` in
  `perception/webcam_control_adapter.py`: with `gimbal_to_camera`'s translation
  zeroed, that rotation has nothing left to act on). That is deliberate: it means
  this value stays correct even if the pitch-sign question below is never resolved,
  at the cost of being valid *only* for as long as the gimbal stays locked at this
  attitude -- if the gimbal is ever unlocked or repositioned, this split must be
  redone from a real two-point measurement.

## Gimbal attitude for this deployment, and an unresolved sign question

The owner reports the gimbal locked at `pitch_deg = -90` in the DJI/MSDK sense
(their convention for "camera pointing straight down"). That is a report of the
physical attitude, not necessarily the number this codebase's math wants.

Checked numerically: `perception.webcam_control_adapter._intrinsic_zyx_rotation`
composed with the fixed `gimbal_to_camera` quaternion above sends the camera's
optical forward axis to body `+Z` (up) at `pitch_deg=-90`, and to body `-Z` (down,
true nadir) at `pitch_deg=+90`. So the value that produces the physically correct
"camera looking at the floor" transform in this codebase is **`+90`, not the
DJI-reported `-90`** -- `config/webcam_control_adapter.json`'s `gimbal_attitude_deg`
and `compute_capture_alignment.py` were both set to `+90` on that basis.

This is exactly the sign ambiguity flagged before any of this was measured: it has
been resolved by computation against this codebase's own rotation formula, not by
confirmation against the aircraft. **Verify before flight**: command the gimbal to
this locked attitude, visually confirm the camera is looking straight down, and
read the raw pitch value the MSDK telemetry reports at that moment. If MSDK also
reports something other than `+90` there, the fix belongs in whatever code
translates live MSDK telemetry into a `GimbalAttitude` (negate it, or correct
`_intrinsic_zyx_rotation`/the fixed quaternion) -- not in silently swapping the
sign here again.

# Offline AprilTag localization

`python -m tools.tag_localize run.json frame.png` detects tag36h11 pixels in a
recorded 1280×720 frame and reports camera and body poses in the building frame.
It exits 1 for rejected observations or invalid configuration. Every report has
`flight_approved: false`. It sends no commands and does not feed the relay.

This implements the offline detection and position-replay portions of issue #84.
The detector uses OpenCV's AprilTag dictionary, sharing #83's OpenCV dependency.
The issue originally names pupil-apriltags; OpenCV supplies the detector here.
Tests render actual tag pixels from independent known poses, then run detection,
PnP and the command-line entry point. These are synthetic checks of geometry,
including decoded tag rotation, camera rotation and body-camera translation.
They do not measure Mini 3 accuracy.

## Run a captured frame

Create `run.json` with these fields. Replace paths, identities, hashes, times and
the example extrinsic with the exact artifacts for the captured frame:

```json
{
  "localizer": {
    "bundle": "/data/level1-v1",
    "accepted_versions": {"level1-v1": "<64-lowercase-hex-content-sha256>"},
    "calibration_path": "/data/camera-calibration.yaml",
    "calibration_sha256": "externally accepted SHA256 of calibration file bytes",
    "camera_serial": "aircraft camera serial",
    "pipeline": {
      "resolution_px": [1280, 720],
      "codec": "h264",
      "decoder_path": "exact #83 decoder_path",
      "camera_mode": "exact #83 camera_mode",
      "android_device_id": "exact #83 android_device_id",
      "network_id": "exact #83 network_id",
      "fov_bounds_deg": {
        "horizontal": ["independent minimum", "independent maximum"],
        "vertical": ["independent minimum", "independent maximum"]
      }
    },
    "T_body_camera": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
  },
  "timing": {"capture_time": 10.0, "decode_time": 10.1, "now": 10.2}
}
```

The identity extrinsic above is only a format example. Supply the measured
transform from optical camera coordinates into the declared body frame,
including the gimbal orientation **at frame capture**. A fixed extrinsic is valid
only while that configuration stays fixed. The output repeats the supplied
transform for traceability.

Calibration uses #83's JSON-formatted YAML fields `schema_version`, `camera_serial`,
`pipeline`, `image_size_px`, `camera_matrix` and `distortion_coefficients`.
The loader requires offline status, #83's synthetic or recorded-live evidence label,
at least 20 distinct source hashes, and fit RMS below 0.5 px. These fields record
#83's evidence; this consumer does not repeat fitting. It checks identity, resolution
and finite, plausible matrix structure. The test fixture supplies a complete synthetic
artifact with independently specified geometry.

An operator must pin accepted hashes externally. `accepted_versions` binds each
accepted map version to its manifest content SHA-256; `calibration_sha256` binds the
exact calibration bytes. Both artifacts are parsed from the same immutable byte
snapshots that were validated or hashed, so replacing either path during startup
cannot substitute unchecked map coordinates or camera intrinsics. Copying hashes
from whichever files happen to be present removes that protection.

Capture, decode and evaluation times are seconds in one upstream monotonic clock.
Capture time must describe the image exposure, with synchronization established
before recording. This tool cannot obtain it from a PNG or infer it from decode
time. Clock translation and estimating capture time from measured live latency
remain integration work. Negative, future or disordered times are rejected.
The default maximum frame age is 0.5 seconds.

For the offline regression run:

```bash
uv sync --locked
uv run pytest tests/test_tag_localization.py
```

## Pose computation

Map tag-local axes are printed right, printed up and outward. OpenCV returns
corners in decoded TL/TR/BR/BL order. It must preserve that decoded order when a
printed tag rotates in the image. Optical camera axes are right, down, forward.
PnP estimates map-to-camera; inversion produces `T_map_camera`.
`T_map_body = T_map_camera @ inverse(T_body_camera)`.

All detected known tags contribute their four map corners to one PnP solve.
Unknown or duplicate detected IDs reject the frame. Planar sets, including
multiple coplanar tags, use IPPE's candidate poses in their common plane.
Nonplanar sets use SQPnP. Candidates must put every observed corner in front of
the camera and the camera on the printed side of each surveyed tag normal.
The best candidate needs at most 2 px RMS reprojection error. When a second
candidate survives, its error must exceed the best by at least 0.5 px and be at
least twice the best error. These initial engineering thresholds still need
live-camera evaluation. A degenerate frontal view can be rejected without a pose.
Distortion coefficients enter the PnP and reprojection calculations directly.

`accepted: true` means this frame passed the offline geometric checks. It does
not assert map acceptance, body attitude accuracy or permission to move.

## Translation replay

`perception.position_replay.PositionReplay` is the legacy finite-recording model. It
provides a position-only linear
Kalman filter with isotropic process variance in square meters per second and
position-fix variance in square meters. Velocity controls must already be in the
building map frame, in meters per second. The implementation makes no assumption
about DJI body, navigation or velocity-axis conventions.

Add unique timestamped `velocity` and `fix` events, then call `at(now)`. A fix is
the body position from an accepted camera observation at **capture time**.
Replay sorts the complete event history, preserving later fixes when an earlier
fix arrives late. Equal timestamps for the same event kind are rejected. Future
events are retained but ignored until their timestamp. This complete offline
history has no memory bound and is intended for finite recordings.

Velocity is held for at most 0.2 seconds by default, after which propagation
stops using that control. Missing velocity or a fix older than 0.5 seconds makes
the output unaccepted. Confidence separately follows accepted-fix age: green
below 0.5 seconds, amber below 2 seconds, red thereafter. A position innovation
with squared Mahalanobis distance above 16.27 is rejected without refreshing fix
age. Rejected fixes remain in the history and their acceptance is recomputed on
replay. Covariance uses the Joseph update. These values are engineering defaults,
and synthetic tests cannot establish their physical noise model.

This is a translation filter. Full attitude estimation, ToF fusion, MSDK velocity
conversion and a calibrated live noise model remain open. It has no automatic
connection to the image CLI; callers must pass only accepted body-position fixes
with an appropriate measured variance.

Its velocity values are held controls rather than noisy state measurements, so it
is not the numerical primitive for new online fusion. The private
`perception._kalman_replay` core owns online constant-velocity measurement replay;
policy-bearing consumers must reach it through `ControlLocalization` or an
observation-only wrapper such as `WebcamFilter`.

## Required physical and runtime work

The printed-tag scan supplies surveyed poses and check measurements for #80/#81.
#83 needs frames from the actual delivered drone stream and a measured calibration
and latency artifact. Recorded frames can be exported to this laptop/server tool;
it does not require a console video panel.

`ControlLocalization` now supplies an unintegrated per-drone fusion and local
eligibility boundary. The remaining #84 acceptance still includes real
gimbal/body extrinsics and timestamp synchronization, measured recorded-frame
accuracy, the hand-carried route with no unhandled fix gap over 500 ms, and
relay/arbiter integration. Wrong/unpinned-map refusal to arm, stale-localization
HOLD then in-place LAND, and covered-tag drills must exercise that actual signed
control path in sim and on the bench. They remain open after these components
merge.

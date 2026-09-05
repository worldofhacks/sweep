# Camera calibration tools

This package creates offline artifacts for a single declared camera pipeline. It does
not connect to DJI hardware, receive a live feed, prove a camera serial, or satisfy a
hardware or flight gate.

`inner_corners` is the number of intersections inside the checkerboard, written as
columns x rows. It is not the number of black-and-white squares. A board with 10 by 7
squares has 9x6 inner corners. Measure one square edge in meters and provide that as
`square_size_m`. This follows OpenCV's [camera calibration tutorial](https://docs.opencv.org/4.13.0/dc/dbb/tutorial_py_calibration.html).
Both inner-corner dimensions must be at least three.

Create a pipeline declaration before processing decoded images. Use the actual values
for recorded data; use `not_applicable` only where a synthetic fixture has no device
or network.

```json
{
  "resolution_px": [1280, 720],
  "fov_bounds_deg": {"horizontal": [65, 75], "vertical": [40, 48]},
  "codec": "h264",
  "fps": 30,
  "encoder": "actual-encoder-and-settings",
  "phone": "actual-phone-model-and-os",
  "network_topology": ["controller", "phone", "router", "computer"],
  "decoder_path": "android-camera-stream-manager",
  "camera_mode": "fpv",
  "android_device_id": "android-serial-or-not_applicable",
  "network_id": "ssid-or-not_applicable"
}
```

Capture 20 to 30 sharp, decoded PNG/JPEG frames at the same resolution, with the
board tilted and located around the frame rather than repeated at one pose. The Mini 3
live-feed workflow uses 1280x720 images. Recalibrate when the camera serial, image
resolution, codec, decoder, camera mode, Android device, or network pipeline changes.
Record FPS, encoder settings, phone details, and network topology as well. Every
declared pipeline field is preserved in the artifact, including additional fields.
Intrinsics calibration requires `fov_bounds_deg` for both axes. The example bounds
belong to the synthetic 920/900-pixel camera fixture. Replace them with bounds from
independent measurements or specifications for the actual decoded crop, accounting
for zoom and stabilization. Each interval must satisfy `0 < minimum < maximum < 180`.
Do not derive these bounds from the calibration result being checked.
Then run:

```bash
uv run python -m calibration intrinsics \
  --images /path/to/decoded-checkerboards \
  --inner-corners 9x6 \
  --square-size-m 0.024 \
  --camera-serial CAMERA-SERIAL \
  --pipeline /path/to/pipeline.json \
  --evidence-kind recorded_live \
  --output calibration/intrinsics_CAMERA-SERIAL.yaml
```

The output is formatted as JSON, which is valid YAML, and must use a new path.
The tool writes and syncs a temporary file in the destination directory, then
publishes it atomically without replacing an existing file or symlink. Failed
writes leave the final path available for retry. It records source hashes, the
OpenCV camera matrix, distortion coefficients, and RMS reprojection error. The tool
requires at least 20 distinct detections and rejects an RMS error of 0.5 pixels or
more. Passing that offline quality check is not a hardware or flight acceptance claim.

Before fitting, the tool checks the independent intrinsic constraints from board
homographies using [Zhang's calibration method](https://www-users.cse.umn.edu/~hspark/CSci5980/zhang.pdf).
It centers image coordinates, scales them by the larger image dimension, normalizes
constraint rows, and requires a fifth-to-first singular-value ratio of at least
0.005. This is a conservative conditioning threshold, not an accuracy guarantee.
Noise can make poorly varied boards pass this rank check. After fitting, the tool
also requires each focal length's estimated standard deviation to be finite,
positive, and at most 5% of that focal length, using
[OpenCV's extended calibration output](https://docs.opencv.org/4.13.0/d9/d0c/group__calib3d.html).
The fitted horizontal and vertical pinhole FOV must lie within the declared bounds.
FOV is computed from both image edges about the fitted principal point; it describes
the pinhole projection before distortion. The artifact preserves the bounds and
records `focal_stddev_px`, `relative_focal_stddev`, and `pinhole_fov_deg`.

The 5% limit is an offline quality policy. OpenCV's local uncertainty estimate
assumes its model and detected corners are suitable; it cannot establish absolute
accuracy. Independent FOV bounds provide an additional configuration check. The
varied PNG fixture has focal standard deviations below 1%; noisy near-parallel
regressions exceed the 5% limit despite RMS errors below 0.5 pixels.

For latency, provide explicit measured samples, their capture-relative measurement
times, and the measured capture duration. Do
not substitute image decode or file timestamps: they do not establish glass-to-glass
latency.

```json
{"duration_ms": 60000, "samples_ms": [221.4, 225.8, 230.1], "sample_times_ms": [0, 30000, 60000]}
```

```bash
uv run python -m calibration latency \
  --samples /path/to/measured-latency.json \
  --camera-serial CAMERA-SERIAL \
  --pipeline /path/to/pipeline.json \
  --evidence-kind recorded_live \
  --output calibration/latency_CAMERA-SERIAL.yaml
```

The latency report calculates descriptive p50 and p95 from the supplied values.
`meets_60_second_capture_minimum` requires at least 20 samples whose measurement
times span at least 60 seconds. Times must be strictly increasing, match the sample
count, and lie within the declared duration. Twenty samples give 5% empirical
quantile resolution; this is not a statistical confidence guarantee.

The three-sample example above and legacy inputs without measurement times receive
`meets_60_second_capture_minimum: false`. The report includes the observed sample
span and never declares a latency target passed.

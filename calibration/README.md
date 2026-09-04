# Camera calibration tools

This package creates offline artifacts for a single declared camera pipeline. It does
not connect to DJI hardware, receive a live feed, prove a camera serial, or satisfy a
hardware or flight gate.

`inner_corners` is the number of intersections inside the checkerboard, written as
columns x rows. It is not the number of black-and-white squares. A board with 10 by 7
squares has 9x6 inner corners. Measure one square edge in meters and provide that as
`square_size_m`.

Create a pipeline declaration before processing decoded images. Use the actual values
for recorded data; use `not_applicable` only where a synthetic fixture has no device
or network.

```json
{
  "resolution_px": [1280, 720],
  "codec": "h264",
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

The output is formatted as JSON, which is valid YAML. It records the source hashes,
OpenCV camera matrix, distortion coefficients, and RMS reprojection error. The tool
requires at least 20 distinct detections and rejects an RMS error of 0.5 pixels or
more. Passing that offline quality check is not a hardware or flight acceptance claim.

For latency, provide explicit measured samples and the measured capture duration. Do
not substitute image decode or file timestamps: they do not establish glass-to-glass
latency.

```json
{"duration_ms": 60000, "samples_ms": [221.4, 225.8, 230.1]}
```

```bash
uv run python -m calibration latency \
  --samples /path/to/measured-latency.json \
  --camera-serial CAMERA-SERIAL \
  --pipeline /path/to/pipeline.json \
  --evidence-kind recorded_live \
  --output calibration/latency_CAMERA-SERIAL.yaml
```

The latency report calculates p50 and p95 from only those supplied values and records
whether the declared capture duration reached 60 seconds. It does not declare any
latency target passed.

# Live webcam localization

Run `python -m perception.webcam_localization` on the computer receiving a hand-carried
1280×720 camera through MediaMTX. This is an observation-only webcam software slice
of #84 built on the merged map/localization foundation. Use the calibration tool
from #103 and the existing `drone1`–`drone6` media paths.

The loop reuses #105's tag36h11 detector, joint PnP, ambiguity/normal checks,
map validation, and camera-to-body transform. It emits JSONL observations only.
It has no adapter, relay, or arbiter connection and cannot authorize movement or
certify fleet spacing. The existing spacing and safety contracts remain downstream
gates. Stale localization is visible as amber/red; HOLD/LAND drills through the
arbiter still need integration and bench evidence.

## Measurement and prediction contract

The webcam supplies PnP position fixes. It supplies no MSDK velocity, ToF, or IMU.
The six-state filter estimates map-frame position and velocity using a constant
velocity Kalman model, the linear specialization of an EKF. Initial velocity is
zero with 1 (m/s)² uncertainty. Default fix variance is 0.01 m² per axis and white
acceleration spectral density is 1 m²/s³. These are model assumptions, not measured
accuracy. Attitude is the latest PnP observation; it is not propagated or fused.

Each fix is applied at estimated capture time, then later retained fixes are replayed
in timestamp order. The filter uses a 16.27 squared Mahalanobis innovation gate and
Joseph covariance updates. Rejected observations do not refresh fix age. History is
bounded to two seconds and 256 events with a saved filter checkpoint; fixes before
the closed checkpoint are refused. This extends the delayed-replay approach in
#105 without supplying fabricated velocity-control events to its offline filter.

The RTSP worker continuously decodes with OpenCV/FFmpeg. A single shared frame
replaces older unread frames. Native blocking reads run in a separate process;
the main loop can keep reporting freshness every 100 ms without a new frame.
Decoder open/read timeouts and bounded worker shutdown limit recovery waits.

Capture time is **estimated**, using monotonic decode receipt minus the measured
p50 latency of this exact localization decoder pipeline. Freshness additionally
includes the measured p95-minus-p50 tail: green below 0.5 seconds, amber below two
seconds, red otherwise. A p95 is not a worst-case latency bound. The receiver cannot
prove capture freshness when a publisher freezes/replays images or its buffering
changes. Preserve a visible clock/video recording for the physical timing review.
Every record says `capture_time_verified: false`,
`publisher_identity_verified: false`, `flight_approved: false`,
`control_eligible: false`, and `spacing_certified: false`.

## Prepare the webcam and evidence

1. Start the repository's MediaMTX service on the receiving computer. Use a
   dedicated source path. The current development configuration has no media
   authentication and publishes RTSP on the host, so run it only on an isolated,
   trusted network; it is not a production or flight-ready deployment.

   ```bash
   docker compose up -d mediamtx
   ```
2. Publish the camera with FFmpeg on Linux:

   ```bash
   ffmpeg -f v4l2 -framerate 30 -video_size 1280x720 -i /dev/video0 \
     -c:v libx264 -preset ultrafast -tune zerolatency -bf 0 -g 30 \
     -f rtsp -rtsp_transport tcp \
     rtsp://127.0.0.1:8554/drone1
   ```

3. Save at least 20 sharp, varied-pose checkerboard frames through the same RTSP
   decoder, moving/tilting the board between captures:

   ```bash
   export SWEEP_LOCALIZATION_RTSP_URL="rtsp://127.0.0.1:8554/drone1"
   python -m perception.webcam_capture --output checkerboards --count 30 --interval 1 --duration 90
   ```

   The command writes PNGs and monotonic decode-receipt timestamps. It exits nonzero
   if it cannot collect the requested count. Run #103's intrinsics command on those
   decoded 1280×720 frames. Declare
   the real camera serial, encoder/FPS, phone/network details where applicable, and
   independently measured horizontal/vertical FOV bounds for the actual crop. Set
   `decoder_path: "opencv-ffmpeg-rtsp"` and `latency_endpoint: "localization_decode"`
   in the pipeline JSON. Use `evidence_kind: "recorded_live"`.
4. Measure latency from the visible source clock to **this decoder's receipt**, not
   console WHEP/HLS display. Record at least 20 latency values and strictly increasing
   capture-relative `sample_times_ms` spanning at least 60 seconds. Run #103's latency
   summary with the same serial and complete pipeline object. For a same-computer
   clock trial, display `time.monotonic()` on the receiving computer in the camera
   view, collect clock frames with `perception.webcam_capture`, and subtract the
   visible clock value from that frame's logged `decode_monotonic_s`. The source
   clock and receiver must share this clock domain; remote unsynchronized clocks
   cannot establish latency. Preserve the images for review. The live loop recomputes
   p50/p95 from the measured samples and refuses a p95 of 500 ms or more. Pin its file
   SHA-256, as well as the calibration file SHA-256.
5. Supply the validated #81 map bundle, its accepted version and manifest content
   hash, and the independently surveyed tag poses/sizes. For a webcam tracked in its
   own camera frame, `T_body_camera` can be identity. For a carried rig with a separate
   reference point, measure the rigid transform. Keep zoom, stabilization, resolution,
   and camera mounting fixed after calibration.

A configuration file looks like this; replace every placeholder and copy the complete
pipeline object from the calibration artifact:

```json
{
  "stream_path": "drone1",
  "localizer": {
    "bundle": "room-map",
    "accepted_versions": {
      "YOUR_ACCEPTED_VERSION": "MANIFEST_CONTENT_SHA256"
    },
    "calibration_path": "intrinsics_webcam.yaml",
    "calibration_sha256": "CALIBRATION_FILE_SHA256",
    "camera_serial": "YOUR_WEBCAM_SERIAL",
    "pipeline": {
      "resolution_px": [1280, 720],
      "decoder_path": "opencv-ffmpeg-rtsp",
      "latency_endpoint": "localization_decode",
      "codec": "h264",
      "camera_mode": "YOUR_FIXED_MODE",
      "android_device_id": "not_applicable",
      "network_id": "YOUR_NETWORK",
      "fov_bounds_deg": {"horizontal": [65, 75], "vertical": [40, 48]}
    },
    "T_body_camera": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
  },
  "latency_path": "latency_webcam.yaml",
  "latency_sha256": "LATENCY_FILE_SHA256"
}
```

The example FOV bounds belong to the synthetic test camera; replace them with
independent bounds for your camera. File paths resolve relative to the configuration.
The accepted-version mapping must come from operator-controlled configuration and
bind the selected bundle version to its exact manifest content digest. A changed
map, calibration, or latency file refuses startup until its pin is updated
deliberately. The URL path must match `stream_path`. The development media service
requires no credentials. If a later deployment uses credentials, keep them in the
URL environment variable; observations and errors omit the URL. Verify that the
publisher on that path is the calibrated camera; a declared serial alone cannot
identify the physical source, and this loop records that identity as unverified.

## Run and report the traverse

```bash
export SWEEP_LOCALIZATION_RTSP_URL="rtsp://127.0.0.1:8554/drone1"
python -m perception.webcam_localization --config webcam.json \
  --duration 120 --output traverse.jsonl
```

Use a fresh output path. Synthetic calibration or latency files are refused unless
`--allow-synthetic` is explicitly supplied; that mode always marks evidence synthetic.
The output includes the selected bundle identity and artifact pins, position/velocity,
covariance, rejected-fix decisions, pose observations, estimated capture times,
stream status, and freshness heartbeats. It does not repeat the complete accepted-map
allowlist in every heartbeat.

Walk the lobby-to-kitchen route with a time-synchronized reference recording. Mark
at least six independently surveyed held-out reference-point positions at their
`run_elapsed_s` times. Do not reuse tag-fit coordinates as held-out truth. At each
checkpoint, hold the reference point still long enough to identify its time and
position independently. Save this structure with six or more unique entries:

```json
{
  "map_sha256": "MANIFEST_CONTENT_SHA256",
  "evidence_kind": "recorded_live",
  "independent_survey": true,
  "checkpoints": [
    {"id": "checkpoint-1", "run_elapsed_s": 12.3, "position_map_m": [1.2, 0.4, 1.1]}
  ]
}
```

```bash
python -m perception.webcam_report traverse.jsonl --checkpoints heldout.json > traverse-report.json
```

The report includes startup/trailing and missing-heartbeat gaps, flags estimated
localization gaps above 500 ms, and compares checkpoints within 100 ms of a timely
observation against a 0.10 m tolerance. This is the M3A checkpoint check; it does not
earn the separate M3B flight p95-error or spacing gates. Coverage ends at the last
recorded heartbeat, so retain the full traverse recording and confirm logging covered
the whole route. Physical acceptance remains pending independent review even when
software checks pass.

Koby's required evidence is the real webcam calibration, decoder-specific measured
latency, pinned surveyed map and camera mounting, full walked-route recording/JSONL,
and independent held-out checkpoint file. Repeat with covered tags and publisher
loss; freshness must age without a new fix. Actual HOLD/LAND and wrong-map arming
refusal require the later relay/arbiter integration. No flight is needed for this
hand-carried procedure.

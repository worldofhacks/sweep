# Verified localization ingestion

`perception.verified_localization` is the only raw-input adapter that can produce verified tag, velocity, and height records for `ControlPublisher`. It reads one Android v3 run plus decoded 1280×720 frames, checks the evidence pins, runs the existing AprilTag detector and PnP implementation, then sends the resulting records through the publisher's normal fuser and signed-frame path. The output still carries `flight_approved: false`.

The Android v3 adapter in `sensor_records` remains diagnostic. Its callback receipt times do not establish sensor capture time, so it continues to emit unverified velocity and height records. This adapter requires separate measured timing artifacts before it changes either verification flag.

## Admission contract

The configuration contains `publisher`, `sensor`, and `localizer` sections plus `identity`, `timing`, and `camera` evidence. `sensor` is the published Android v3 configuration, including its measured NED-to-map rotation, velocity covariance, height datum, and height variance. It must describe the same publisher configuration.

`identity` pins the recording run, session, product and drone identities, connection generation and epoch, aircraft and RC firmware, SDK version, recorder configuration digest, decoded pipeline digest, and camera configuration ID. Every raw record must match it exactly. A live run binds its raw epoch to the authenticated relay epoch before accepting evidence.

Each frame, attitude, and telemetry timing artifact names a measured artifact digest, Android boot ID, receipt-to-capture offset, and maximum error. All three artifacts must belong to one boot. The camera section pins the serial, calibration bytes, pipeline digest, dynamic body-to-gimbal and gimbal-to-camera transforms, map-to-NED orientation, tag covariance, and bounds for attitude age, cross-stream skew, and orientation disagreement.

Every transform and covariance has a `measurement` object with a measurement ID, artifact digest, and `measured: true`; its matrix is in the adjacent `matrix` field. The calibration evidence must be labelled `recorded_live`. A synthetic calibration artifact is refused.

`decoded_frame` input identifies an immutable image file by SHA-256 and supplies its Android receipt and decode timestamps. Body and gimbal samples are stored with their independently mapped times. A frame is refused when either transform is absent or stale, their sampling times are too far apart, the tag pose contradicts aircraft attitude, the image hash or configuration differs, or PnP rejects the frame. The dynamic body-to-camera transform is then bound to the frame capture time for the existing strict `BodyExtrinsics` check.

## Running it

Replay input is JSONL. Each line has exactly `now_s` and `raw`; `raw` is either an Android v3 velocity, height, or attitude record, or a `decoded_frame` record. A frame record contains the pinned identity fields, `event_id`, `received_at_android_elapsed_realtime_ms`, `decoded_at_android_elapsed_realtime_ms`, `frame_path`, `frame_sha256`, `camera_serial`, `camera_configuration_id`, and `pipeline_sha256`.

```sh
LOCALIZATION_KEY_1='dedicated-localization-secret' \
  python -m perception.verified_localization \
  --config verified-ingestion.json --input raw-and-frames.jsonl \
  --replay-output signed-localization.jsonl
```

With a live publisher configuration, omit `--replay-output`. The executable authenticates the publisher, binds the live epoch, and publishes each admitted record through its authenticated localization socket.

No qualifying physical evidence is included here. The measured clock mappings, latency residuals, dynamic extrinsics, telemetry uncertainty, actual camera calibration, walked-camera coverage run, covered-tag drill, and bench HOLD/LAND evidence must be recorded for the selected firmware and configuration before an operational deployment is prepared.

# Verified localization ingestion

`perception.verified_localization` is the only raw-input adapter that can produce verified tag, velocity, and height records for `ControlPublisher`. It reads one Android v3 run plus decoded 1280×720 frames, checks the evidence pins, runs the existing AprilTag detector and PnP implementation, then sends the resulting records through the publisher's normal fuser and signed-frame path. The output still carries `flight_approved: false`.

The Android v3 adapter in `sensor_records` remains diagnostic. Its callback receipt times do not establish sensor capture time, so it continues to emit unverified velocity and height records. This adapter requires separate measured timing artifacts before it changes either verification flag.

## Admission contract

The configuration contains `publisher`, `sensor`, and `localizer` sections plus `identity`, `timing`, and `camera` evidence. `evidence_scope` is `hardware` for an operational configuration or `test_double` for a replay-only fixture. `sensor` is the published Android v3 configuration, including its measured NED-to-map rotation, velocity covariance, height datum, and height variance. It must describe the same publisher configuration.

`identity` pins the recording run, session, product and drone identities, connection generation and epoch, aircraft and RC firmware, SDK version, recorder configuration digest, decoded pipeline digest, and camera configuration ID. Its `raw_run` measurement artifact contains those same fields and the Android boot ID. Every raw record must match the configured identity exactly. A live run binds its raw epoch to the authenticated relay epoch before accepting evidence. `ControlPublisher` also checks the configured process-monotonic capture clock against the current host boot before it publishes.

The publisher map ID must equal the validated map bundle content hash. Geometry remains pinned by the publisher configuration and is copied into every observation.

Each frame, attitude, and telemetry timing artifact names a measured artifact digest, Android boot ID, receipt-to-capture offset, and maximum error. All three artifacts must belong to one boot. `max_telemetry_timing_error_s` rejects a telemetry mapping whose measured error exceeds the deployment budget. The camera section pins the serial, calibration bytes, pipeline digest, dynamic body-to-gimbal and gimbal-to-camera transforms, map-to-NED orientation, tag covariance, and bounds for attitude age, cross-stream skew, and orientation disagreement.

Every measurement has a measurement ID, an `artifact_path`, its SHA-256, and `measured: true`. The artifact is a JSON object with exactly `measurement_id` and `value`; startup reads it, checks its digest, and requires its value to equal the configured timing value, transform, or covariance. This makes the configured values traceable to the evidence file rather than accepting a free-standing digest. The calibration evidence must be labelled `recorded_live`. A synthetic calibration artifact is refused.

`decoded_frame` input identifies an immutable image file by SHA-256 and supplies its Android receipt and decode timestamps. The adapter hashes and decodes the same in-memory bytes. Frame timing uncertainty is added conservatively to the tag position covariance using the configured maximum speed.

Android v3 records `KeyGimbalAttitude` as `raw_sdk_axes`. This adapter refuses it because the SDK has not established a body-relative rotation convention or a capture-aligned resampling method. It therefore cannot construct a verified tag record from the currently exported gimbal samples. A future adapter must use a measured convention and bracketed interpolation at the frame capture time, including its residual uncertainty. It must not relabel a nearest gimbal sample as capture-time evidence.

## Running it

Replay input is JSONL. Each line has exactly `now_s` and `raw`; `raw` is either an Android v3 velocity, height, or attitude record, or a `decoded_frame` record. A frame record contains the pinned identity fields, `event_id`, `received_at_android_elapsed_realtime_ms`, `decoded_at_android_elapsed_realtime_ms`, `frame_path`, `frame_sha256`, `camera_serial`, `camera_configuration_id`, and `pipeline_sha256`. The Android app does not currently export decoded frames, so a hardware run needs a separately recorded decoder output that preserves these pins before this command can receive frame input.

```sh
LOCALIZATION_KEY_1='dedicated-localization-secret' \
  python -m perception.verified_localization \
  --config verified-ingestion.json --input raw-and-frames.jsonl \
  --replay-output signed-localization.jsonl
```

With a live publisher configuration, omit `--replay-output`. The executable authenticates the publisher, binds the live epoch, and passes admitted records into the publisher's bounded queue, periodic stale-frame cadence, and reconnect loop.

No qualifying physical evidence is included here. The measured clock mappings, latency residuals, dynamic extrinsics, telemetry uncertainty, actual camera calibration, walked-camera coverage run, covered-tag drill, and bench HOLD/LAND evidence must be recorded for the selected firmware and configuration before an operational deployment is prepared.

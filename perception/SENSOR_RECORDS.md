# Android sensor-record adapter

`perception.sensor_records` reads the Android raw-phone JSONL schema version 3 and writes the
velocity and height records accepted by `perception.control_publisher`'s shape checks. Android
records only callback receipt time. The adapter uses that receipt time as the required
`capture_time` field and always sets `source_verified` and `timing_verified` to `false`.
`ControlPublisher` refuses those records before they enter `ControlLocalization`, so this path
cannot create a ready localization pose.

The raw log retains aircraft and gimbal attitude samples. The adapter validates their Android
schema and omits them from publisher input. It needs a measured, synchronized body-to-camera
extrinsic and a capture-time mapping before it can construct a tag observation. Camera bench
`StreamInfo` records also remain outside this adapter because their presentation and receipt
times do not establish capture time, decode time, calibration, or extrinsics.

## Configuration

The `publisher` object is a normal `ControlPublisherConfig`. Its selected drone must use
`android_elapsed_realtime` as its fuser clock and the configured velocity and height source IDs.
The adapter requires each map rotation, covariance, height datum, and height variance to carry a
measurement ID and `measured: true`.

```json
{
  "publisher": { "mode": "replay", "session": "run-42", "websocket_url": null, "drones": [] },
  "phone": {
    "drone_id": 1,
    "velocity": {
      "source_id": "msdk-velocity",
      "sdk_key": "KeyAircraftVelocity",
      "map_rotation": {
        "measurement": { "measurement_id": "ned-to-map-survey-1", "measured": true },
        "matrix": [[0, 1, 0], [1, 0, 0], [0, 0, -1]]
      },
      "covariance_map_enu_m2ps2": {
        "measurement": { "measurement_id": "velocity-noise-flight-1", "measured": true },
        "matrix": [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 0.01]]
      }
    },
    "height": {
      "source_id": "tof-height",
      "sdk_key": "KeyUltrasonicHeight",
      "map_datum": {
        "measurement": { "measurement_id": "height-datum-survey-1", "measured": true },
        "offset_m": 1.0
      },
      "variance_m2": {
        "measurement": { "measurement_id": "height-noise-flight-1", "measured": true },
        "value": 0.01
      }
    }
  }
}
```

The full `publisher.drones` array is required. The abbreviated publisher object only shows where
it belongs in the file.

## Raw input and conversion

The app writes records under its internal `sensor-records` directory. Export a completed JSONL
run, preserve it unchanged, and use the matching recorder configuration digest when preparing the
adapter configuration. The accepted raw records carry these common fields:

```text
record_schema_version: 3
time_basis: android_callback_receipt_elapsed_realtime_ms
source_timestamp_status: not_provided_by_msdk_key_listener
received_at_android_elapsed_realtime_ms: integer
```

Velocity records add `coordinate_frame: "ned"` and `north_mps`, `east_mps`, and `down_mps`.
Height records use either `KeyAltitude` with metres or `KeyUltrasonicHeight` with decimetres.
The adapter converts decimetres to metres. It selects one configured height key and skips the
other after validating it.

```sh
uv run python -m perception.sensor_records \
  --config recording.json \
  --input phone-raw.jsonl \
  --output publisher-input.jsonl
```

Treat `publisher-input.jsonl` as a diagnostic artifact until the missing evidence is recorded.
The next flight needs a measured NED-to-map rotation, height datum, velocity and height uncertainty,
source-key provenance, and a source-capture to Android-clock mapping. Tag localization also needs
camera calibration, measured body-to-camera extrinsics, synchronized aircraft and gimbal attitude,
and a camera capture-time mapping. Record the measurement IDs in `recording.json` with the raw
run and its recorder configuration digest.

## Evidence for a future acceptance run

A future acceptance validator should take an immutable evidence manifest alongside the raw JSONL.
The manifest should bind the recording run ID and recorder configuration digest to the following
artifacts:

- the survey result for the NED-to-map rotation and height datum;
- the flight analysis that produced velocity covariance and height variance;
- a source-capture to Android-clock mapping with its measured residual bound;
- for tag fixes, camera calibration, body-to-camera extrinsics, synchronized aircraft and gimbal
  attitude, and the camera capture-time mapping.

Each manifest entry should name its measurement ID and artifact digest. The validator can replay the
raw run against those pins, check clock residuals and synchronization, and issue verified publisher
records only for samples covered by the evidence. Runs without a complete manifest remain diagnostic
artifacts produced by this adapter.

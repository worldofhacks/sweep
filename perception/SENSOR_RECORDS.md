# Sensor records for control localization

`perception.sensor_records` converts raw phone telemetry and decoded AprilTag frames into the
JSONL records consumed by `perception.control_publisher`. It takes identity, clock, covariance,
calibration, and body-transform pins from its configuration. It does not estimate covariance from
the tag reprojection error.

The probe app writes its SDK listener values to `filesDir/sensor-records/phone-raw-*.jsonl`. Each
line carries an Android monotonic receipt time. The callback has no source capture timestamp, so
the converter writes `timing_verified: false` and preserves the `android_elapsed_realtime` clock
ID. It never labels receipt time as the publisher's map clock. RTSP frames currently have the
same limitation. The control fuser rejects these observations and cannot publish a `ready` pose
from them.

The `publisher` object is the normal `ControlPublisherConfig`. Its fuser source IDs, map ID,
calibration digest, body-extrinsics ID, and clock ID are the recording adapter's identities. The
adapter checks them against the phone and tag source configuration at startup. Phone conversion
also requires a measured NED-to-map rotation and height datum. A tag source pins the exact
`TagLocalizer` configuration, a body-extrinsics identity, and a positive definite covariance
matrix selected from measurement evidence. Each eligible tag frame carries its own measured
extrinsics plus independently sampled gimbal and attitude times equal to its capture time.

```json
{
  "publisher": { "mode": "replay", "session": "run-42", "websocket_url": null, "drones": [] },
  "phone": {
    "drone_id": 1,
    "velocity_ned_to_map_rotation": [[0, 1, 0], [1, 0, 0], [0, 0, -1]],
    "height_datum_m": 1.0,
    "velocity": {
      "source_id": "msdk-velocity",
      "sdk_key": "KeyAircraftVelocity",
      "source_verified": true,
      "max_sample_age_s": 0.2,
      "covariance_m2ps2": [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 0.01]]
    },
    "height": {
      "source_id": "tof-height",
      "sdk_key": "KeyAltitude",
      "source_verified": true,
      "max_sample_age_s": 0.2,
      "variance_m2": 0.01
    }
  },
  "tag": {
    "source_id": "tag-camera",
    "source_verified": true,
    "timing_evidence_verified": true,
    "max_frame_age_s": 0.5,
    "covariance_map_enu_m2": [[0.012, 0, 0], [0, 0.012, 0], [0, 0, 0.012]],
    "body_extrinsics": {
      "extrinsics_id": "measured-body-camera-v1",
      "require_measured": true
    },
    "localizer": { "bundle": "...", "accepted_versions": {}, "calibration_path": "..." }
  }
}
```

The complete `publisher.drones` and `tag.localizer` objects are required. The abbreviated values
above only show the recording-specific fields.

## Raw JSONL input

Each input line is one sample. The probe app produces the phone records below. It flushes each
line and stops at 16 MiB, then records that condition in the app's event log. Each source pin
selects one exact SDK key, so a mixed altitude and ultrasonic log cannot be treated as one sensor.
Its converter uses
the configured drone identity, NED-to-map rotation, height datum, source IDs, and covariance;
the raw log cannot replace them. Copy the phone JSONL from the app's internal `sensor-records`
directory before conversion.

```json
{"kind":"phone_velocity_raw","event_id":"phone_velocity_raw-41","received_at_monotonic_ms":15030,"sdk_key":"KeyAircraftVelocity","velocity_ned_mps":[0.1,0,0]}
{"kind":"phone_height_raw","event_id":"phone_height_raw-41","received_at_monotonic_ms":15040,"sdk_key":"KeyAltitude","height_m":1.6}
{"kind":"tag_frame","event_id":"tag-41","drone_id":1,"image_path":"/runs/41/tag-41.png","sdk_capture_time_s":15.02,"decode_time_s":15.04,"received_at_s":15.05,"body_extrinsics":{"extrinsics_id":"measured-body-camera-v1","source_id":"tag-camera","matrix":[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],"gimbal_time_s":15.02,"attitude_time_s":15.02,"measured":true}}
```

`tag_frame` reads the image with OpenCV and passes its decoded pixels to `TagLocalizer`. A stale,
undecodable, ambiguous, or tag-free frame stops conversion with an error. Missing, unmeasured,
or unsynchronized frame extrinsics produce an unverified record. Synthetic calibration evidence
also remains unverified even when the source configuration says `source_verified: true`.

## Commands

Convert an offline JSONL capture into publisher input:

```sh
uv run python -m perception.sensor_records --config recording.json --input raw.jsonl --output publisher-input.jsonl
uv run python -m perception.control_publisher --config publisher.json --replay-output relay-frames.jsonl < publisher-input.jsonl
```

`publisher.json` contains the `publisher` object from `recording.json`. For an RTSP source, use
the same recording configuration and request a bounded number of frames:

```sh
uv run python -m perception.sensor_records --config recording.json --rtsp-url rtsp://host/drone1 --frames 20 --timeout-s 10 --output publisher-input.jsonl
```

The RTSP URL is passed to `WebcamStream`. Its current decoder exposes receipt timestamps, so this
command produces tag records with unverified timing until the stream has a verified source-timing
implementation.

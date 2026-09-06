# Control localization publisher

`python -m perception.control_publisher --config publisher.json --replay-output frames.jsonl` reads sensor records from standard input and writes signed control-localization frames. Replay mode never opens a network connection. It is for bench and hand-carried recordings only and does not claim production readiness.

Live deployments construct `ControlPublisher` with a WebSocket transport, call `bind_live_credentials`, then feed `enqueue` and `publish_live`. Authentication uses a separate per-aircraft localization credential. The first WebSocket frame is `{v: 1, type: "auth", source: "localization", drone_id, token}`. Every later frame has only the control-localization wire fields plus `t` and `session`; `source` is not included in data frames.

The deployment file contains a fuser configuration, exact measured clock mapping, configured P95 position-uncertainty bound, session, and a key environment-variable name for each drone. It pins map, geometry, camera calibration, body extrinsics, source identities, connection epoch, and capture clock. A changed phone epoch requires a new deployment file and a new publisher binding.

Each `fuser` object must declare its physical and uncertainty limits. The publisher rejects a deployment that omits them.

```json
{
  "position_bounds_map_enu_m": [[-10, 10], [-10, 10], [0, 3]],
  "height_bounds_map_enu_m": [0, 3],
  "max_speed_mps": 5,
  "position_variance_bounds_m2": [0.000001, 0.0625],
  "velocity_variance_bounds_m2ps2": [0.000001, 1],
  "height_variance_bounds_m2": [0.000001, 0.0625]
}
```

These values must come from the accepted flight envelope and sensor uncertainty budget. The example values describe the test fixture only.

Each live drone also requires this exact `live_capture_clock` object:

```json
{
  "source": "process_monotonic",
  "boot_id": "<value from /proc/sys/kernel/random/boot_id during calibration>",
  "monotonic_reference_s": 12345.678,
  "capture_reference_s": 456.789
}
```

`capture_reference_s` must equal the measured `clock_mapping.capture_reference_s`. At runtime the publisher derives current capture-clock seconds as `capture_reference_s + (time.monotonic() - monotonic_reference_s)`, then converts that value through the measured mapping for relay time. The boot ID pins the monotonic origin: a process after reboot must receive a newly measured reference and deployment file. Live startup rejects a missing or mismatched boot ID. Replay never uses this clock; it reads the recorded `now_s` only.

Each JSONL sensor record has `kind` (`tag`, `velocity`, or `height`), `drone_id`, `event_id`, `connection_epoch`, map and geometry identities, `clock_id`, `capture_time`, source identity, and explicit `source_verified` and `timing_verified` booleans. Tag records also include camera calibration, map-ENU position/covariance, and a measured rigid `extrinsics` object whose gimbal and attitude times equal capture time. Velocity and height records include their map-frame measurement and covariance or variance.

The publisher uses the actual bounded fuser. It keeps only the newest configured number of input records per drone, publishes hold or land snapshots when inputs age, and emits a hold for malformed or unverified records. Live mode reads standard input on a bounded background queue and publishes each configured drone at 10 Hz even while input stalls, so the fuser reaches its hold and land states. Invalid JSON and records without a configured drone fail the process; valid records that fail the fuser become typed hold output. An estimated webcam capture time or unknown publisher identity cannot become a verified tag fix. Capture and evaluation times remain separate from relay transport time; the configured clock mapping creates the transport timestamp and the relay applies its own conservative freshness check.

The fuser holds after its latest accepted tag reaches 0.5 seconds of age, reports red confidence at 2 seconds, and lands after 3 seconds of continuous localization loss. Relay freshness limits remain a separate deployment control.

Physical production still needs a measured source-to-relay clock mapping, camera calibration, body-camera mounting survey, map and tag survey pins, and an accepted connection epoch. The publisher supplies the software boundary for those measured artifacts; it does not create them.

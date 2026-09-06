# Control localization payload

Set `SWEEP_LOCALIZATION_KEYS_JSON` to a JSON object mapping each positive drone ID to
its dedicated localization-producer secret. For example, `{"1":"..."}` authorizes
only that secret to authenticate as `source: "localization"` for drone 1. These keys
are separate from `SWEEP_ADAPTER_KEYS_JSON`; a missing entry rejects localization input.

`relay.control_localization` accepts a versioned, signed `control_localization` payload only after the relay transport authenticates its drone and connection epoch. `to_wire_payload` requires the adapter signature; the transport verifies it before calling the store. The payload is an adapter boundary for the control-localization fuser. Webcam observations cannot enter it because their capture times and publisher identity are explicitly unverified.

Each payload has a unique event ID and carries the map, geometry, camera-calibration, body-extrinsics, source, and capture-clock pins. It carries a measured mapping from the capture clock to the relay monotonic clock, including its maximum conversion error. The deployment pin stores that exact measured mapping. The store rejects changed offsets, rates, clock identities, duplicate event IDs, and clock uncertainty above the configured bound.

The fuser's last accepted tag capture time is the only value used for `position_last_seen_ms`. Its mapped time subtracts the measured conversion-error bound, so it never reports a future observation. The capture-clock mapping also produces `evaluated_at_relay_ms`, the valid predicted-pose timestamp. Evaluation time and relay receipt time never refresh position freshness. A ready snapshot also requires fresh verified velocity and height measurements and covariance below the deployment's position-uncertainty bound. A hold, land, stale, rejected, malformed, or mismatched payload gives the aircraft position quality `0.0`; relay telemetry cannot restore it.

The relay integration uses `ControlLocalizationStore.ingest(raw, authenticated_drone_id, authenticated_connection_epoch, now_ms)`, then `apply(fleet_snapshot)`. `apply` replaces the affected aircraft's map-frame pose, quality, last-seen time, and immutable control provenance with the localization evidence. It leaves link telemetry independent. The caller owns payload signing and should call `ingest` only after authenticating the localization producer principal.

The deployment pin for each drone is `ControlLocalizationPins`. It includes the selected map, geometry, camera-calibration and body-extrinsics identities, the capture and relay clock identities, connection epoch, and ordered source IDs. `ControlProvenance` is the immutable value that can be attached to the root-owned aircraft state when that field lands.

## Deployment configuration

When `SWEEP_LOCALIZATION_KEYS_JSON` authorizes one or more localization producers, set
`SWEEP_CONTROL_LOCALIZATION_CONFIG` to a local JSON file. Startup rejects an absent file,
invalid schema, or a pin set that does not exactly match the authorized producer IDs. The
relay creates an independent `ControlLocalizationStore` for each session from that file.

The file contains exactly `limits` and `drones`:

```json
{
  "limits": {
    "max_clock_error_ms": 5,
    "max_fix_age_ms": 500,
    "max_position_uncertainty_m": 0.2,
    "land_after_fix_age_ms": 2000
  },
  "drones": [{
    "drone_id": 1,
    "connection_epoch": 1,
    "map_id": "map-sha",
    "geometry_id": "geometry-sha",
    "camera_calibration_id": "camera-calibration-sha",
    "body_extrinsics_id": "body-extrinsics-sha",
    "capture_clock_id": "camera-clock",
    "relay_clock_id": "relay-monotonic",
    "source_ids": ["tag-camera", "msdk-velocity", "tof-height"],
    "clock_mapping": {
      "capture_clock_id": "camera-clock",
      "relay_clock_id": "relay-monotonic",
      "capture_reference_s": 0,
      "relay_reference_ms": 100000,
      "milliseconds_per_capture_second": 1000,
      "max_error_ms": 5,
      "measured": true
    }
  }]
}
```

A frame with a stale capture time or different pinned identity is retained in the audit
trail but marks that aircraft's localization unavailable. The next autonomy snapshot then
has zero position quality, so position-requiring motion is refused.

## Proposed diagnostic control pose

`ControlRuntime.control_pose` emits a signed `control_pose` packet for phone diagnostics. Every
packet has `flight_approved: false` and `position_frame: "map_enu"`; nodes must keep treating it
as display evidence. The packet's timestamps satisfy `t >= pose_time_ms >= fix_time_ms` and hold
or land packets retain the last genuine pose and its original times. No packet is emitted before
there is retained localization evidence.

`position_uncertainty_mm` is the conservative three-dimensional 95 percent Gaussian radius. It
uses `ceil(1000 * sqrt(max_eigenvalue(covariance)) * 2.796)`. The 2.796 multiplier is the square
root of the 95 percent chi-square value for three degrees of freedom. [NIST Engineering
Statistics Handbook](https://www.itl.nist.gov/div898/handbook/eda/section3/eda3674.htm) lists the
chi-square distribution values used for this bound.

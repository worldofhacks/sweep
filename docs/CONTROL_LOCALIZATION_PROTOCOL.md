# Control localization payload

`relay.control_localization` accepts a versioned, signed `control_localization` payload only after the relay transport authenticates its drone and connection epoch. `to_wire_payload` requires the adapter signature; the transport verifies it before calling the store. The payload is an adapter boundary for the control-localization fuser. Webcam observations cannot enter it because their capture times and publisher identity are explicitly unverified.

Each payload carries the map, geometry, camera-calibration, body-extrinsics, source, and capture-clock pins. It carries a measured mapping from the capture clock to the relay monotonic clock, including its maximum conversion error. The store checks every pin against the deployment configuration and rejects clock uncertainty above the configured bound.

The fuser's last accepted tag capture time is the only value used for `position_last_seen_ms`. Evaluation time and relay receipt time never refresh position freshness. A ready snapshot also requires fresh verified velocity and height measurements. A hold, land, stale, rejected, malformed, or mismatched payload gives the aircraft position quality `0.0`; relay telemetry cannot restore it.

The relay integration uses `ControlLocalizationStore.ingest(raw, authenticated_drone_id, authenticated_connection_epoch, now_ms)`, then `apply(fleet_snapshot)`. `apply` replaces the affected aircraft's map-frame pose, quality, and last-seen time with the localization evidence. It leaves link telemetry independent. The caller owns payload signing and should call `ingest` only after authenticating the source.

The deployment pin for each drone is `ControlLocalizationPins`. It includes the selected map, geometry, camera-calibration and body-extrinsics identities, the capture and relay clock identities, connection epoch, and ordered source IDs. `ControlProvenance` is the immutable value that can be attached to the root-owned aircraft state when that field lands.

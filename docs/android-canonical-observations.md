# Canonical phone telemetry

The phone bridge can publish the shared observation envelope on its existing authenticated adapter connection. Legacy telemetry continues to feed the aircraft control path. The canonical stream preserves the snapshot's ENU position, velocity and quality, identifies its source, and leaves `t_capture` and `clock_mapping_id` null. Its receipt timestamp records when the publisher sampled the phone snapshot. It provides no capture-time or latency proof.

To enable it, place this file at `files/observation-source.json` in the installed app's private storage before starting its relay link:

```json
{
  "v": 1,
  "telemetry_source_id": "dji-telemetry",
  "frame_id": "dji_enu",
  "clock_id": "phone_snapshot_wall_ms"
}
```

The file is optional. The three identifiers are fixed to the values shown, which name this producer's measurements and clock. An invalid file prevents the link from starting and produces a configuration error in the phone log. Reads are bounded to 4 KiB and reject symlinks and non-regular files.

In the relay's observation configuration, bind `dji-telemetry` to the device, session and current connection epoch, with `producer_role: "adapter"`, `node_type: "aircraft"`, `allowed_frames: ["dji_enu"]` and `allowed_payload_kinds: ["telemetry"]`. Declare `dji_enu` as a source-scoped `legacy_map_enu` frame with `axis_convention: "east_north_up"` and metric units. Rejoining requires a binding for the new epoch.

The snapshot already converts the SDK's NED velocity into ENU. This producer preserves those values. It skips disconnected or invalid numeric snapshots. Mapping this local frame into an approved world requires a measured registration tied to the source's origin. Live localization also needs capture-time measurements, a clock mapping and measured uncertainty; this receipt-only stream cannot satisfy those gates.

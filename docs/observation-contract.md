# Shared observation contract

`relay.observations` defines the bounded v1 body for aircraft and ground evidence. A producer submits an observation without `t_ingest`; the relay authenticates the producer, validates a host-owned authenticated source binding plus frame and clock declarations, then adds its own `t_ingest` timestamp before audit, fan-out, or MCAP export. The same canonical encoded event is the storage and replay unit.

The canonical static-map frame is named `world`. Its direction convention is declared as `right_handed_z_up`, with metric metre coordinates, an immutable map ID and version, and a physical datum. `world` does not imply geographic east/north axes. A source may publish in an explicitly declared local `odom`, `body`, `camera`, `lidar`, `tag`, or compatibility `legacy_map_enu` frame. Local observations remain diagnostic until a downstream registration consumer supplies a measured transform to `world`.

Every local frame declaration is bound to the session, device ID, connection epoch, and source ID that may use it. The host rejects an unknown frame or a frame from another epoch. W0 stores declarations and checks their scope. Registration transforms and dynamic pose estimates belong to consumers that have the relevant calibration and map evidence.

## Event body

An observation submission has exactly these fields:

| Field | Meaning |
| --- | --- |
| `v` | Integer `1`. |
| `type` | Literal `observation`. |
| `event_id` | Bounded producer event identity. |
| `session`, `device_id`, `connection_epoch`, `source_id` | Authenticated source identity. `device_id` is a positive signed 32-bit integer. |
| `node_type` | `aircraft` or `ground`. The relay checks this against host configuration rather than deriving it from a device ID. |
| `frame` | Declared coordinate frame for the observation. |
| `confidence` | Finite numeric confidence in `[0, 1]`. Qualitative localization labels stay in their specialized payload. |
| `t_capture` | Nullable source timestamp. `null` means the producer has no capture timestamp. |
| `t_source_receipt` | Required source timestamp for receipt at the producing callback or driver. It is not exposure time. |
| `clock_mapping_id` | Nullable reference to a host-pinned clock mapping. |
| `payload` | One of the closed payload kinds below. |

The relay appends a non-negative integer `t_ingest` in Unix milliseconds. Producers cannot set it. Event JSON is canonical UTF-8, rejects duplicate keys, and is limited to 64 KiB.

A timestamp is `{ "clock_id": string, "unit": "ms" | "ns", "value": integer }`. When capture time is present, it uses the same declared clock as source receipt and must be no later than that receipt. A host-owned `ClockMapping` converts one declared source clock to relay milliseconds using integer numerator and denominator fields, reference points, and a bounded measured error. A submission only references that mapping by ID. Without a configured mapping, the contract makes no cross-clock capture-to-ingest claim.

## Frame declarations

A declaration has `frame_id`, `kind`, `axis_convention`, and metric `unit`. The sole global declaration has `frame_id: "world"`, `kind: "world"`, `axis_convention: "right_handed_z_up"`, `map_id`, `map_version`, and `physical_datum`.

A local declaration has kind `odom`, `body`, `camera`, `lidar`, `tag`, or `legacy_map_enu`; one of `right_handed_z_up`, `east_north_up`, `forward_left_up`, `right_down_forward`, or `right_up_outward` as appropriate. OpenCV camera coordinates use `right_down_forward`; a printed tag can use `right_up_outward`. Every local declaration has a complete session/device/epoch/source scope. Several sources may declare the same local frame ID; resolution uses the complete scope. `legacy_map_enu` preserves old aircraft semantics as a named local compatibility frame. It does not assert that a DJI local ENU origin equals `world`.

A host-owned `SourceBinding` names the authenticated session, positive device ID, epoch, source ID, node type, exact frame IDs, closed payload kinds, and approved clock-mapping IDs this source may publish. It supplies the exact map pins required to authorize `world`. Ingestion compares the observation identity and node type with this binding before resolving every envelope or payload frame. A producer cannot claim `world` merely because the declaration exists.

A `FramedVector` is `{frame, x_m, y_m, z_m}`. A `FramedPose` is `{parent_frame, child_frame, x_m, y_m, z_m, qx, qy, qz, qw}` with a unit quaternion in `[x, y, z, w]` order. Both frame IDs must be current declarations for the observation source. A pose reports a dynamic relationship. It does not register either frame to `world`.

## Payload kinds

`payload.kind` is a closed union:

| Kind | Body |
| --- | --- |
| `telemetry` | Aircraft or ground framed position in metres and velocity in metres per second, plus battery, link, position quality, and state. An accompanying `pose` observation carries orientation when available. |
| `pose` | A framed parent/child pose. |
| `range_scan` | Lidar frame, associated `sensor_pose`, angular bounds, metric ranges, and mount ID. At most 720 ranges are admitted. |
| `camera_frame` | Image identity and digest, dimensions, and calibration ID. It carries no fabricated map pose. |
| `tag_observation` | A `tag36h11` tag and image identity, four bounded pixel corners, pixel frame, pose-admission result, optional `T_camera_tag` pose and covariance, size, reprojection RMS, and a typed reason. |
| `status` | Bounded status text and capabilities. |

The event frame is validated first. Payload frames must agree with it where they express the same quantity. A range scan uses the outer lidar frame and a sensor pose whose child is that lidar frame. Camera observations stay in their camera frame. A tag observation uses `tag36h11` IDs from 0 through 586 and a declared source-scoped `tag:<id>` child frame. An accepted pose has `reason: "pose"`, a positive tag size, and `T_camera_tag`; covariance may remain null until calibration supports it. A rejected pose carries its detector reason and cannot contain a pose or covariance. It remains diagnostic until a registration consumer establishes its map relationship.

## Reuse boundary

`schemas/observation-v1.schema.json` is the canonical minified UTF-8 schema artifact and has no trailing newline; MCAP stores those exact bytes in its Schema record. This module owns encoding, decoding, duplicate-key rejection, frame-scope checks, clock-reference checks, timing skew checks, and a pure `RatePolicy` helper that rejects a timestamp earlier than the prior admitted event. The [relay ingress](observation-ingress.md) owns authenticated admission, replay watermarks, rate state, console and device-scoped localization fan-out, and audit writes. Consumers must explicitly authorize an observation kind. Observation admission does not authorize motion.

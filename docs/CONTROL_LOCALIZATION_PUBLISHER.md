# Control-localization publisher

`python -m perception.control_publisher` converts exact sensor JSONL records into
the signed, diagnostic-only `control_localization` envelope documented in
[`CONTROL_LOCALIZATION_PROTOCOL.md`](CONTROL_LOCALIZATION_PROTOCOL.md). It is an
adapter around `perception.control_localization.ControlLocalization`, not another
safety runtime. It cannot alter relay state, plan motion, emit `control_pose`, or
approve flight. Every produced localization body and every downstream pose retains
`flight_approved: false`.

The production path is intentionally one chain:

```text
exact sensor record
  -> per-aircraft ControlLocalization fuser
  -> to_wire_payload(snapshot, clock_mapping)
  -> ControlLocalizationWire.from_mapping(body)
  -> sign_localization_frame(..., per-aircraft key)
  -> authenticated relay localization socket
```

The host-pinned relay projector remains the only component that checks wire
freshness, p95 three-dimensional covariance, deployment pins, replay order, and the
diagnostic integer pose contract. The publisher does not duplicate or weaken those
checks.

## Configuration and credentials

The top-level JSON object has exactly these fields:

- `mode`: `"live"` or `"replay"`.
- `session`: the bounded relay session ID.
- `websocket_url`: a credential-free `ws` or `wss` base ending in `/ws` for live
  mode, otherwise `null`.
- `audit_dir`: a local directory for the repository's transactional SQLite audit
  and recoverable append-only JSONL mirror. It is mandatory in both modes.
- `queue_limit`: 1 through 4096 records per aircraft.
- `drones`: 1 through 4 exact drone entries, matching the physical relay cap.

Each drone entry has exactly `fuser`, `clock_mapping`, `key_environment`, and
`live_capture_clock`. `fuser` uses the complete
`ControlLocalizationConfig` contract. The environment variable named by
`key_environment` supplies that aircraft's dedicated localization secret; secret
values never enter the config identity or publisher audit.

Live fuser templates must set `connection_epoch` to `0`. The publisher first
receives `auth.accepted` and the relay's initial authoritative `state`, finds the
authenticated aircraft's current positive epoch, and creates the fuser for that
epoch. It continuously drains later state events. An epoch change discards the old
fuser state and creates a new instance before more evidence can be signed. A prior
epoch's queued record is processed as a refusal; it is never relabeled. Reconnects
repeat this handshake, so an epoch in a deployment file cannot outlive the phone
connection that earned it.

Replay fusers instead require the positive epoch recorded with the input and have
no live clock or network URL. Replay still requires an explicit per-aircraft key;
there is no built-in demo secret. Given the same config and records, replay event
IDs and output are deterministic. Live IDs include a per-process UUID plus a
bounded, non-wrapping sequence, so a restart cannot reuse the prior process's
event IDs.

## Live clock boundary

Each live drone supplies an exact measured clock object:

```json
{
  "source": "process_monotonic",
  "boot_id": "<measured current-boot identity>",
  "monotonic_reference_s": 12345.678,
  "capture_reference_s": 456.789
}
```

`capture_reference_s` must equal the corresponding `ClockMapping` reference. The
publisher checks the current boot through an OS adapter (Linux boot ID or macOS
`kern.boottime`) before connecting. Tests and embedded deployments can inject an
equivalent boot-identity provider. A missing, changed, negative, regressed, or
non-finite clock value fails closed. A new boot requires a newly measured clock
artifact.

## Sensor records and overflow

Every sensor record has an exact schema for `tag`, `velocity`, or `height`, including
drone/epoch/map/geometry/clock/source identities, capture time, and literal
`source_verified: true` and `timing_verified: true`. Tag evidence additionally
contains the exact camera calibration, map-ENU position and covariance, and measured
capture-time body extrinsics. Velocity and height values are explicitly map ENU.
Unknown, missing, duplicate JSON fields, non-standard JSON constants, non-finite
values, and unverified evidence are refused.

Records are converted into frozen observation objects at admission, so caller
mutation cannot change queued evidence. Queues never use silent `deque(maxlen=...)`
eviction. A `queue_full` refusal is transactionally recorded and fsynced before the
error returns, and the queue remains unchanged. Upstream owns the durable accepted
sensor source and should backpressure or retry from it; the publisher does not
duplicate every successful high-rate record or relay frame in its audit. Invalid
inputs, fuser refusals, epoch changes, and transport failures remain audited. In live
mode, a malformed line is audited and ignored; it cannot latch a drone in HOLD
forever or refresh any measurement. Replay instead stops at malformed input so a
corrupt evidence file cannot produce a partial result that looks complete. The
fuser's fixed freshness and continuous-loss policy remains authoritative: HOLD
begins when fresh tag evidence is lost and LAND is reported after three continuous
seconds.

Live mode gives each aircraft its own socket, state drain, fuser, queue, key, and
audit identity. An explicit send or reconnect failure is contained to that aircraft,
and the scheduler continues attempting the remaining aircraft. Delivery failures
are never reported as successful.

## Running replay

Replay input adds `now_s` to each line solely as the recorded fuser evaluation time;
it is removed before the exact sensor schema is parsed:

```bash
LOCALIZATION_KEY_1='replace-with-dedicated-secret' \
  uv run python -m perception.control_publisher \
  --config publisher.json \
  --replay-output control-localization.jsonl < sensor-records.jsonl
```

The output path is created without overwriting an existing file. Replay is bench and
hand-carried evidence only. Synthetic tests do not establish the measured camera,
body extrinsics, map survey, clock error, tag coverage, physical HOLD/LAND behavior,
or flight approval still required by the PRD and issues #84 and #94.

# Shared-world replay

The replay sidecar reads the relay's committed audit history, records it as MCAP,
and serves a read-only Foxglove connection on localhost. Each original event keeps
its audit sequence, session, device, connection epoch, source, frame, and payload.
Foxglove's 3D panel can display derived poses, accepted tags, scan returns, and
authorized routes alongside the original records.

Run this in a separate process beside the relay. Use the actual session identifier
and its existing JSONL file from the relay log directory. Keep its matching
`.sqlite3`, `-wal`, and `-shm` files with the active audit. The reader checks each
line against the committed SQLite sequence, length, and SHA-256 digest. It never
creates a relay session or sends commands to the relay or devices.

## Live view and recording

```bash
uv run python -m tools.world_replay_live \
  --audit /absolute/path/to/session-hash.jsonl \
  --session actual-session-id \
  --output /absolute/path/to/new-session.mcap \
  --seconds 3600
```

In Foxglove Desktop, choose **Open connection**, select **Foxglove WebSocket**, and
enter `ws://127.0.0.1:8765`. Add a 3D panel, select the `world` fixed frame, and enable
`/sweep/scene`. Add Raw Messages panels for `/sweep/roster`, `/sweep/plans`, and
`/sweep/safety`. The connection uses the
[Foxglove WebSocket v1 protocol](https://github.com/foxglove/ws-protocol/blob/main/docs/spec.md)
with subscriptions only. Client publication, parameters, services, and asset reads
are disabled.

The sidecar begins at the first committed audit record, then follows new appends.
Ctrl-C or the duration limit closes the recording. Read failures terminate the
sidecar and preserve the verified prefix already recorded. The console error and
recorded audit sequence identify where collection stopped. A disconnected viewer
does not stop recording. Restart with a new output filename to collect again.

## Recorded replay

Export the current committed history without starting a live connection:

```bash
uv run python -m tools.world_replay export \
  --audit /absolute/path/to/session-hash.jsonl \
  --session actual-session-id \
  --output /absolute/path/to/new-session.mcap

uv run python -m tools.world_replay verify \
  --input /absolute/path/to/new-session.mcap \
  --session actual-session-id
```

Open the resulting `.mcap` with Foxglove Desktop's **Open local file** command and
use the same panels. Foxglove reads the embedded
[JSON schemas](https://docs.foxglove.dev/docs/getting-started/custom/custom-schema-encodings)
directly. `read_replay(path, session)` returns the original audit records after
verifying the complete file, its data and summary checksums, channel schemas,
identity, ordering, timestamps, and derived scene correspondence. It has no relay
dispatch path.

## Channels and spatial meaning

| Channel | Retained evidence |
| --- | --- |
| `/sweep/roster` | Membership, node class, epochs, readiness, and state snapshots |
| `/sweep/plans` | Intent records, commands, route authorizations, and planner results |
| `/sweep/acknowledgements` | Original command and intent lifecycle acknowledgements |
| `/sweep/aircraft`, `/sweep/ground` | Canonical poses and telemetry, including confidence and timing |
| `/sweep/tags`, `/sweep/observations` | Accepted/rejected tag estimates, scans, camera and status observations |
| `/sweep/safety` | Refusals, audited safety events, and hold/hover/estop commands |
| `/sweep/registration`, `/sweep/map` | Approved registration, residual evidence, and map identity from qualified world observations |
| `/sweep/occupancy` | Explicit occupancy/expiry audit events, when emitted |
| `/sweep/events` | Other original audit events |
| `/sweep/scene` | Derived `foxglove.SceneUpdate` display geometry |

MCAP log and publish time use the event’s `t_ingest` when present, otherwise its
`t`, converted from Unix milliseconds to nanoseconds. Channel metadata declares
`clock=unix_ns` and `timestamp_policy=t_ingest_else_t`. The relay retains arrival
time separately for incoming membership, telemetry, acknowledgements, node frames,
and localization frames. Wire events keep their original source time. Historical
audit events without `t_ingest` retain their original `t`; their arrival time is
unknown. Regenerate older MCAP exports from their audit JSONL to use this metadata.

Native capture time, source receipt time, clock identifiers, and mapping identifiers
stay unchanged inside each observation. A missing capture time stays null. Native
monotonic time is never presented as a Unix timestamp.

World observations share the `world` frame. Local observations use a frame scoped
to session, device, epoch, and source; choose that frame in a separate 3D panel to
inspect it. Scans apply their recorded sensor pose and omit unknown returns from
display geometry while retaining every null in the original scan. No static grid
or free-space clearance is inferred from those returns.

The qualified world-observation endpoint records its accepted projection and the
approved map and registration context in the session audit. Navigation delivery
records aircraft route authorizations and navigation poses before sending them
to the node, including subsequent pose updates. Authentication signatures are
excluded from these replay records; the verified identity and pinned hashes remain.

Aircraft route authorizations retain their map, geometry, navigation, calibration,
and transform hashes. Their line display stays in the recorded `map_enu` frame.
Ground navigation commands retain their full route document and map reference;
the line display projects world XY onto z=0 and labels the floor and projection.
Neither display invents a measured height or an unrecorded world transform.

Pose, tag, and scan markers expire visually after one second of the recording
timeline. This display lifetime has no control authority. Route markers use the
recorded authorization expiry. Raw confidence, stale-epoch refusals, frame
refusals, missing capture times, and expiry reasons remain independently visible.
The shared live occupancy veto is outside this change; occupancy channels stay
empty until an actual producer emits those audit events.

## Resource limits and evidence

Each read batch contains at most 16 audit records, each at most 1 MiB. A recording
stops at 100,000 original events or 256 MiB. The live process runs for at most one
hour. Four viewers can connect; each has bounded inbound messages/subscriptions
and a 250 ms outbound send deadline. Slow viewers lose their connection. These
limits and filesystem failures affect the sidecar process independently of control.

Tests confirm a two-aircraft platform route through the production composition,
signed node wire, audit storage, and actual MCAP decoding. Ground tests run the
HTTP preview and confirmation endpoints through the node controller and a fake
device, checking arrival STOP and STOP after tracking or host approval loss before
decoding the recorded world poses, map identity, registration, and routes.
The existing scan/tag fixtures also pass through
observation admission before export. Live tests subscribe over a real socket,
refuse client publication, record concurrent appends, and keep a healthy viewer
and audit writer progressing while another viewer stops reading. Hardware demo
acceptance still requires replaying the retained physical session.

## World observation records

Live world observations use the `relay.observations` v1 envelope. The relay admits the source binding, frame declaration, clock mapping, and epoch before the platform verifies its approved-map registration. Replay keeps the complete admitted envelope, including source timestamps, clock mapping identity, pose quaternion, and payload evidence. Console map positions derive their capture time from the host-pinned clock mapping.

Older `world_observation` audit records remain readable by replay and scene export. They are historical evidence only; the platform API does not accept their short envelope.

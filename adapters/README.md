# adapters

Capability area: Autonomy. Milestones: M1 (`sim`), M2 (hardware).

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to the adapter interface name one change owner and require cross-review.

The accepted MVP has two concrete implementations (PRD Appendix C):

| Package | Target | Milestone |
|---|---|---|
| `sim/` | Kinematic deterministic flight and camera fixtures for registry sizes 1–4 now, then 4–6; the first-class CI implementation before hardware | M1 |
| `dji_mini3/` | one Android node per Mini 3 and RC-N1 pair via the DJI Mobile SDK, proven on one exact hardware combination before duplication; the relay-side remote adapter and a fake node land first | M2 |

## Frozen protocols and dispatch

`SwarmAdapter` exposes `takeoff`, `goto`, `rotate_to`, `hover`, `land`, `estop`, and
`telemetry`. `CameraCapture` separately negotiates capabilities and exposes gimbal,
readiness, panorama/photo capture, and media retrieval. Camera results and media carry
the aircraft identity, connection epoch, capture/file correlation IDs, pose, actual
yaw, gimbal pitch, intrinsics, checksum, storage reference, and typed completion or
failure. `capture_room` bundles retain both `room_id` and `capture_id`.

`AdapterDispatcher.dispatch()` requires an already checked `Plan` and accepts a
current-snapshot provider. It rechecks roster and connection epoch before each I/O and
again before accepting every acknowledgement or media result; camera missions repeat
the full live safety and pose-lock gate after each result. Returned aircraft, capture,
and file identities and runtime types must match exactly (booleans never stand in for
integer IDs or epochs). Only strict `accepted` plans cross the
boundary; malformed booleans, IDs, enums, command shapes, and mutable or
nondeterministic JSON parameters are refused before I/O. Whole-plan preflight and the
live command gate both model actual sequential occupancy rather than assuming every
aircraft is already at its future target. Unsafe/stale results fail closed, and a
failed or timed-out target is held and removed from projected-position calculations
while safe unaffected targets may continue.

Flight acknowledgements may be `accepted` or `executing`; those are nonterminal and
stop dependent work. When a matching terminal completion arrives, call
`resume_after_completion()`; it removes the waiting command so accepted work is not
blindly resent. The caller authenticates that terminal acknowledgement before the
dispatcher rechecks its domain identity. If the roster changes while work is pending,
the plan becomes `invalidated` and the dispatcher best-effort holds every aircraft with
proven completed motion before returning `stale_roster`. M1.2 camera methods return
terminal typed results synchronously because their media context cannot be reconstructed
from a bare asynchronous acknowledgement. The transport layer may wrap these domain
objects but must not redefine their status or reason semantics.

## Deterministic simulation

`SimFlightAdapter` provides deterministic kinematics, telemetry, injected failure and
timeout fixtures, and a configurable node-local relay/LAN watchdog. A node records its
own last authenticated activity in `NodeWatchdogState`; elapsed local time causes hold
and then the configured adapter failsafe without depending on a relay loss callback or
sending a central command to a disconnected node. Roster reconciliation's
`LossResponse` is audit/integration metadata, while #17/M1.4 owns production runtime
wiring. `SimCamera`
provides a full 2:1 equirectangular `pano_360`, an acknowledged-yaw `reconstruct_8`
sequence whose retrieved files must match the eight requested headings in order within
the plan's explicit measured yaw tolerance and measured overlap target. Completion also
requires a shared approved pose, the acknowledged gimbal setpoint, unique file IDs,
strict timestamps, calibrated intrinsics, SHA-256 checksums, storage references, and
matching capture/drone/epoch identity. Simulation dimensions, pose/gimbal tolerances,
gimbal bounds, timing, storage, watchdog timing, and loss behavior are explicit
configuration rather than claimed hardware defaults.

Hardware is a configuration choice behind these protocols; every earned feature is
built and tested against `sim` first.

## Remote bridge adapter

`adapters.dji_mini3.remote.RemoteBridgeAdapter` implements the same `SwarmAdapter` and
`CameraCapture` protocols as the simulator over a small `NodeLink`: the link reports a
node's live connection epoch, sends a `CommandRequest`, awaits the acknowledgements for
a `command_id` with a timeout, and retains the node's latest `capabilities` frame and
`media_file` records. The link owns the wire envelope, the per-node sequence, and the
signature; `relay.bridge.RelayNodeLink` is the implementation over a live relay runtime
and must be driven from a worker thread, never the relay event loop. The relay setting
`SWEEP_ADAPTER_BACKEND` selects `sim` or `remote` for dispatch; `relay/README.md`
documents the node protocol the adapter speaks.

Wrap dispatch in `adapter.for_intent(intent_id, roster_version)` so every command in
the block carries the intent and roster it belongs to; the wire `command_id` is
generated per request. Flight arguments travel as integer millimetre and millidegree
units. Before sending, the adapter compares the connection epoch it was given (from the
snapshot, or `update_connection_epoch`) with the link's live epoch and refuses without
sending when they differ; the dispatcher then reports `stale_connection_epoch`. Silence
for the configured timeout raises `AdapterTimeout`. A nonterminal `accepted` or
`executing` acknowledgement followed by silence is returned as is so the dispatcher stops
dependent work and resumes on the later terminal fact. A `failed` acknowledgement keeps
the node's reason in `detail` (for example `out_of_order_command`) and is never resent.
Camera capabilities, captures, and retrievals require the node's `capabilities` or
`media_file` frame to have arrived before the terminal acknowledgement; otherwise the
adapter fails closed. `telemetry()` yields nothing because node telemetry reaches the
relay registry directly over the node socket.

`adapters.dji_mini3.fake_node` behaves like the phone on the wire without hardware:
`just fake-node` connects one to a running relay so the console shows a real registry
entry, and `relay/tests/test_bridge_roundtrip.py` drives it end to end.

The existing `crazyswarm2/` and `mavlink/` packages remain inactive placeholder stubs. They are not accepted hardware implementations and do not drive an abstraction change until a concrete second hardware integration is specified and proven.

PRD: sections 4.5, 5.6, Appendix C.

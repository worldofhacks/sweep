# adapters

Sweep uses these shared boundaries for a modular aerial/ground fleet and attached cameras and sensors. See the [integration guide](../docs/modular-fleet.md). Vendor-specific implementations must declare what they actually support; an adapter name or test fixture is not hardware qualification. The operator runtime never substitutes the simulator for a disconnected device.

Capability area: Autonomy. Milestones: M1 (`sim`), M2 (hardware).

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to the adapter interface name one change owner and require cross-review.

The current package inventory below distinguishes shared implementations from hardware acceptance (PRD Appendix C):

| Package | Target | Milestone |
|---|---|---|
| `sim/` | Kinematic deterministic flight and camera fixtures; historical 1–6-aircraft cases are isolated CI evidence, not a product fleet limit | M1 |
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
wiring. `adapters.sim.app:app` is an isolated test entry point, requires
`SWEEP_ALLOW_TEST_ADAPTERS=true`, and must use a separate test environment, relay endpoint,
session, log directory, and test-only credentials. It never belongs behind the operator
console. C1 defaults to two aircraft. The explicit C2 simulator requires
`SWEEP_SIM_AIRCRAFT_COUNT` from 4 through 6 and independent adapter credentials for every
configured ID; each new relay session registers signed test nodes and streams synthetic
telemetry at the relay cadence. It binds the shared relay, autonomy controller, arbiter,
simulator, explicit safety enrichment, and the configured hold-then-failsafe watchdog.
The 4–6-aircraft software mission is simulator evidence only; it does not earn #44's
deferred production/hardware exit. `SimCamera`
provides a full 2:1 equirectangular `pano_360`, an acknowledged-yaw `reconstruct_8`
sequence whose retrieved files must match the eight requested headings in order within
the plan's explicit measured yaw tolerance and measured overlap target. Completion also
requires a shared approved pose, the acknowledged gimbal setpoint, unique file IDs,
strict timestamps, calibrated intrinsics, SHA-256 checksums, storage references, and
matching capture/drone/epoch identity. Simulation dimensions, pose/gimbal tolerances,
gimbal bounds, timing, storage, watchdog timing, and loss behavior are explicit
configuration rather than claimed hardware defaults.

Vendor hardware integrates behind these contracts. Test supported semantics in isolation before hardware commissioning; the aircraft simulator does not qualify or impersonate a ground adapter.

## Remote bridge adapter

`adapters.dji_mini3.remote.RemoteBridgeAdapter` implements the same `SwarmAdapter` and
`CameraCapture` protocols as the simulator over a small `NodeLink`: the link reports a
node's live connection epoch, sends a `CommandRequest`, awaits the acknowledgements for
a `command_id` with a timeout, and retains the node's latest `capabilities` frame and
`media_file` records. The link owns the wire envelope, the per-node sequence, and the
signature; `relay.bridge.RelayNodeLink` is the implementation over a live relay runtime
and must be driven from a worker thread, never the relay event loop: both `send` and
`await_acknowledgement` block the calling thread and refuse the loop thread.
`relay.bridge.build_adapters` reads the relay setting `SWEEP_ADAPTER_BACKEND` and
returns the session's flight and camera pair: `sim` builds the simulator from the
snapshot with an explicit `SimCameraConfig`, `remote` builds one `RemoteBridgeAdapter`
over a `RelayNodeLink` whose delivery and acknowledgement waits are bounded by
`SWEEP_COMMAND_TTL_MS`; `build_dispatcher` wraps that pair in an `AdapterDispatcher`.
`relay.autonomy` calls it once per accepted intent from the snapshot the arbiter checks,
so the adapter's connection epochs are the ones the plan was built against.
`relay/README.md` documents the node protocol the adapter speaks.

`AdapterDispatcher` opens `adapter.for_intent(intent_id, roster_version)` around every
command it executes, including best-effort holds and estop, so each wire command carries
the intent and roster it belongs to; a caller driving the adapter directly opens the
scope itself, and scopes do not nest. Dispatcher-owned calls preserve the planner's
`command_id` on the wire; direct diagnostic calls generate a unique ID per request.
Flight arguments travel as integer millimetre and millidegree
units. Before sending, the adapter compares the connection epoch it was given (from the
snapshot, or `update_connection_epoch`) with the link's live epoch and refuses without
sending when they differ; the dispatcher then reports `stale_connection_epoch`. Silence
for the configured timeout raises `AdapterTimeout`. A nonterminal `accepted` or
`executing` acknowledgement followed by silence is returned as is so the dispatcher stops
dependent work and resumes on the later terminal fact. A `failed` acknowledgement keeps
the node's reason in `detail` (for example `out_of_order_command`) and is never resent.
`estop()` sends to every aircraft before waiting on any acknowledgement; a node that
stays silent is reported as a failed `adapter_timeout` acknowledgement rather than
aborting the fleet stop.
Camera capabilities, captures, and retrievals require the node's `capabilities` or
`media_file` frame to have arrived before the terminal acknowledgement; otherwise the
adapter fails closed. `telemetry()` yields nothing because node telemetry reaches the
relay registry directly over the node socket.

`adapters.dji_mini3.fake_node` exercises the phone wire without hardware only in an
isolated test runtime. Use the explicit `just test-fake-node` procedure in
[relay setup](../relay/README.md#run-the-relay): the relay and fake-node environment
require `SWEEP_ALLOW_TEST_ADAPTERS=true`, and the CLI also requires `--test-only` (the
recipe supplies it). Test-only credentials, an explicit test relay/session, and separate
logs are required; never connect a fake to the operator relay. The in-process
`relay/tests/test_bridge_roundtrip.py` dispatches through `build_dispatcher` on the
`remote` backend end to end; `relay/tests/test_autonomy_roundtrip.py` runs the M2.0
workflow from test intents through `relay.autonomy` to two fake nodes.

The existing `crazyswarm2/` and `mavlink/` packages remain inactive placeholder stubs. They are not accepted hardware implementations and do not drive an abstraction change until a concrete second hardware integration is specified and proven.

PRD: sections 4.5, 5.6, Appendix C.

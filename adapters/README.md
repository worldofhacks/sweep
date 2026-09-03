# adapters

Capability area: Autonomy. Milestones: M1 (`sim`), M2 (hardware).

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to the adapter interface name one change owner and require cross-review.

The accepted MVP has two concrete implementations (PRD Appendix C):

| Package | Target | Milestone |
|---|---|---|
| `sim/` | Kinematic deterministic flight and camera fixtures for registry sizes 1–4 now, then 4–6; the first-class CI implementation before hardware | M1 |
| `dji_mini3/` | one Android node per Mini 3 and RC-N1 pair via the DJI Mobile SDK, proven on one exact hardware combination before duplication | M1.9 |

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

The existing `crazyswarm2/` and `mavlink/` packages remain inactive placeholder stubs. They are not accepted hardware implementations and do not drive an abstraction change until a concrete second hardware integration is specified and proven.

PRD: sections 4.5, 5.6, Appendix C.

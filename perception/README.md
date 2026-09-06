# perception

Capability area: Interaction. Milestone: M3.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to the detection-event shape name one change owner and require cross-review.

Samples frames at 5 to 10 fps per stream from MediaMTX, runs a small detector (YOLO-class, people and common objects; thermal if mounted), and emits detection events with a world-position estimate from drone pose and camera geometry. Detections go to the relay as events, never as commands. Confidence >= 0.6 is shown, >= 0.8 is auto-promoted to focus, nothing is auto-acted on.

PRD: sections 4.8, 5.7.

## Localization ownership

`control_localization.ControlLocalization` is the sole online boundary in this
package for combining tag positions, map-frame velocity measurements, and map-frame
height measurements into a control-eligibility signal. It validates exact drone,
connection-epoch, map, geometry, clock, source, calibration, and body-extrinsics
identities before replay. Physical position, height, speed, and uncertainty limits
are required configuration. A contradictory physical measurement holds control
until that same source supplies an accepted recovery measurement. An expected source
claiming the wrong map, geometry, clock, calibration, or extrinsics is contradictory
too. Wrong drone, epoch, or source traffic, duplicates, and expired events are
diagnostic only and cannot indefinitely poison an otherwise current state.

The private `_kalman_replay` module is the one numerical replay implementation for
online constant-velocity measurement filters. `ControlLocalization` owns the trust
and control policy; `WebcamFilter` remains observation-only but now delegates its
prediction, update, capture-time ordering, and checkpoint pruning to that same core.
`PositionReplay` is retained only for finite offline recordings whose velocity is a
piecewise control input, not a noisy state measurement. New online consumers must
not build on that legacy model.

Event IDs provide idempotency only inside one connection epoch's retained replay
window. Once a checkpoint closes old capture times, those times are refused; the
signed transport remains responsible for epoch-wide sequence/replay protection.
Velocity and height measurements that precede the first tag cannot initialize the
state. They remain explicitly pending only inside the bounded replay horizon, so an
earlier delayed tag can still make them usable; otherwise they expire without
affecting state. Measurements at the tag's exact capture time are replayed tag-first
regardless of arrival order.

Accepted tag-fix age has fixed PRD confidence bands: green below 0.5 seconds, amber
below 2 seconds, and red thereafter. Loss begins when green freshness ends. The
component reports HOLD for exactly 3 seconds of continuous loss, then LAND; it never reports
RTH. Enforcing the PRD's physical hold/landing behavior remains the flight-control
integration's responsibility.

`control_eligible` means only that this component has fresh, internally consistent
inputs. Relay output always carries `flight_approved: false`. The integration layer
may set `production_evidence_verified` only after independently pinning and checking
the selected map and geometry, measured camera/body calibration, source identities,
clock provenance, and recorded physical evidence. Signed transport, relay/arbiter
wiring, active-tag coverage drills, and real flight approval remain outside this
component.

The bounded COCO detector library and its event payloads are documented in
[OBJECT_DETECTION.md](OBJECT_DETECTION.md). Relay logging, attention promotion, confirmation,
pose projection, and real-aircraft camera integration are not wired by that library slice.

`control_publisher.ControlPublisher` is the narrow JSONL/live adapter around that
fuser. It uses the canonical relay wire encoder and signer; it has no planner,
arbiter, telemetry, control-pose, or flight-dispatch surface. See
[`docs/CONTROL_LOCALIZATION_PUBLISHER.md`](../docs/CONTROL_LOCALIZATION_PUBLISHER.md)
for its exact input, audit, epoch, and replay boundaries.

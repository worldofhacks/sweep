# planner

Capability area: Autonomy. Milestone: M1.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to safety-relevant planner paths name one change owner and require cross-review.

## M1.2 API

`DeterministicPlanner` consumes validated `relay.intent_v1.IntentV1` values and a
transport-neutral `FleetSnapshot`. It supports the checkpoint operations `arm`,
`select`, `takeoff`, `translate`, `hold`, `come_home`, `land`, `land_all`, `estop`, and
`capture_room`. Call `supports()` before state-dependent checks: other valid Intent v1
names return the stable `unsupported` reason and never become a plan. `come_home` is a
normal `goto` expansion; the flight adapter has no special return-home method. Confirmed `land`
expands only the current selection; `land_all` expands every reachable airborne aircraft.

`Plan`, `Command`, `CommandAcknowledgement`, `Refusal`, and `ExecutionResult` in
`planner.models` are the semantic contract. Their `to_dict()` projections are
deterministic, JSON-native values. Plans and commands carry stable IDs,
`roster_version`, `drone_id`, and the frozen per-aircraft `connection_epoch`. Lifecycle
values are `accepted`, `refused`, `executing`, `completed`, `failed`, and `invalidated`;
reason codes are stable snake_case and consumers must not parse human-readable
`detail` text. A transport package with its own status enum must cross this seam with
`status.value`, not Python enum identity. Command parameter trees are recursively
frozen; `to_dict()` creates ordinary deterministic JSON dictionaries and lists.

`PlanningConfig` requires explicit capture pose tolerance, measured yaw tolerance, and
minimum image overlap. Each `reconstruct_8` rotation freezes its heading and evidence
limits; the planner rejects zero overlap, orders uniform translations
leading-aircraft first for sequential dispatch, and bundle validation rejects
duplicate or cross-linked files whose pose, gimbal, actual-yaw sequence, or measured
FOV coverage does not match the approved mission.

`AutonomyController` orders capability gating, intent arbitration, planning, whole-plan
arbitration, and dispatch. Its snapshot-provider hook must return the current enriched
state immediately before each adapter call and acknowledgement. Two motion intents
inside the configured conflict window are both dropped and cause a fleet hold; a later
selection wins. Position loss holds the fleet, then lands in place after the configured
dwell. Planner exceptions become a typed failure plus a safety hold and do not escape.

## Relay and registry boundary

`FleetSnapshot.from_relay_state(raw, enrichment=...)` adapts #14's state projection.
The relay supplies registry identity, membership, roster and connection epochs,
telemetry, authority, session arm authorization, and RC-operator presence. The caller
must explicitly enrich physical armed evidence, camera readiness, storage, active-task
state, physical-RC availability, operator activity, and any last-known values needed
when telemetry is null. Nested telemetry is authoritative; divergent flight-state
aliases, malformed nested values, and missing last-known enrichment fail closed. Flat
console projection aliases are never substituted as safety evidence, and no safe
defaults are invented.

Accepted state updates are explicit: `arm` contains `armed_update=True`, `select`
contains `selection_update`, and `estop` contains `estop_update=True`. The integrator
must atomically apply those values through #14's control projection before accepting
the next intent. Arm is a global session-authorization operation and does not depend on
a possibly stale aircraft selection. E-stop latches even when no aircraft is currently
eligible and remains true if an adapter call fails; clearing it is outside M1.2 because
Intent v1 has no reset-stop intent. Session arm authorization is separate from the
per-aircraft physical-disarmed proof required for graceful removal.

Call `authorize_graceful_removal()` before applying a signed leave, then
`reconcile_roster_change()` against the resulting atomic relay projection. #14 owns
clearing selection, pending confirmation, and accepted plan and emits one-shot
`invalidated_intent_ids`. #15 reports stale accepted work by `Plan.plan_id` and stale
pending confirmation by the caller's confirmation key; the integration uses the
originating `intent_id` as that key and includes `intent_id` in both relay-owned opaque
projections. Unexpected loss remains visible and returns the configured hold/failsafe
decision as audit metadata. A disconnected aircraft does not wait for that relay
result: its node-local activity clock independently enters hold and then its configured
adapter failsafe; #17/M1.4 owns production runtime wiring.

`AutonomyRelayBridge` is that runtime boundary. Supply it through
`create_app(intent_sink_factory=...)` with an explicit controller and safety-enrichment
provider. The relay records the accepted request, complete autonomy result, authoritative
control projection, and terminal lifecycle in one session log. Simulator and hardware
composition roots supply their own measured planner, arbiter, adapter, watchdog, and enrichment
configuration.

The opt-in altitude path and its floor-reference contract are described in
[Altitude controls](../docs/ALTITUDE_CONTROLS.md). It is disabled by default and
requires explicit deployment configuration plus the existing live acceptance gates.

Later formation, sweep, `map_area`, and route-allocation behavior remains
future scope and must earn a capability before planning.

PRD: sections 5.3 and 5.4 (modes: indoor constrained is the capstone mode).

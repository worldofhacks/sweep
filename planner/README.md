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

## C2 simulator formations

The explicit simulator-only C2 release adds `formation_next`, `formation_set`,
`spacing`, and `sweep` without widening the remote Mini 3 backend. Its formation
contract contains exactly line, column, wedge, and diamond. Line and column accept
2–6 selected aircraft; wedge and diamond accept 4–6. Slots retain the selected
fleet's centroid and a small clearance margin above the reported spacing.

For at most six aircraft, a deterministic Hungarian solver minimizes total 3-D
Euclidean travel. Bounded Murty partitioning visits alternative assignments in cost
and slot-index order until one has non-crossing transitions and a sequential
one-aircraft-at-a-time `goto` order whose complete segments clear every stationary or
already projected ready aircraft. Spacing changes reposition the completed formation;
they refuse instead of reporting metadata-only success when no supported formation or
clear route exists. Sweep similarly stages every assigned lane start before traversing
the lanes. If no safe assignment and order exists, planning refuses before adapter I/O.
The arbiter independently rechecks every segment. This is bounded simulator behavior,
not physical-flight acceptance and not a substitute for mapped route or clearance
evidence.

## Known-map navigation previews

`NavigationPlanner` produces deterministic, inspectable route previews. It does not
add a dispatch capability. Every `NavigationArtifact` and `NavigationPlan` is
non-dispatchable by construction, and otherwise-successful revalidation returns
`artifact_not_dispatchable`. Runtime execution remains future work and requires a
separate accepted-geometry, capability, controller, and adapter contract.

`NavigationArtifact.from_geometry_directory()` starts with an independently accepted
and content-pinned map bundle. It admits only the exact version-1 geometry-report
schema emitted by the offline authoring tool: `offline_authoring`,
`flight_approved=false`, explicit `synthetic` or `surveyed` evidence, meters, `+x/+y`
axes, and binary `0=candidate` / `1=blocked` grids. Those facts never become flight
approval. Report and grid files are confined to one directory, read once into bounded
byte snapshots, hash-checked, and parsed from those same bytes. Zones, geofence, owner
approval, and autonomous graph constraints come from the accepted map; preview-only
arrival and connector overlays receive their own deterministic content pin.

The frozen artifact and plan also carry the reason they cannot dispatch. The current
blocking evidence gaps are independent geometry acceptance, verified camera visibility,
and a runtime dispatch contract; synthetic input additionally carries a
`synthetic_geometry_evidence` gap. The author's tag-distance calculation remains
`candidate_proximity_only` with `visibility_verified=false`: proximity is useful for
planning later measurements, but it is not line-of-sight, field-of-view, occlusion, or
localization-coverage proof and cannot clear the gap.

The grid producer has already inflated occupied, unknown, and out-of-domain geometry
by `hazard_margin_m`. The planner therefore checks that each frozen aircraft envelope
fits that margin and traces its centerline through the conservative free-cell
supercover; it does not dilate the grid a second time. The two envelope dimensions are:

- horizontal radius = aircraft radius + map uncertainty + pose uncertainty + tracking
  allowance + stopping allowance;
- vertical half-height = half the aircraft height + the same four allowances.

Aircraft-to-aircraft clearance sums the two aircraft envelopes exactly once. It is
analytic in XYZ rather than sample-based, keyed by aircraft identity, and independent
of logical floor labels. Route creation refuses initial overlap, blocked intermediate
altitude bands, incomplete geofence containment, and any assignment without a feasible
slot for every selected aircraft. Revalidation freezes and compares map, geometry,
overlay, roster, selection, plan revision, connection epochs, motion allowances, and
permission before checking drift and the remaining 3-D route.

`planner.mapped_formations` builds non-dispatchable line, column, wedge, and diamond
previews for two or four aircraft. Formation permission is independent of arrival
permission, every target slot must fit an explicitly approved formation volume, and
the canonical navigation planner searches all feasible slot assignments before choosing
the minimum-cost deterministic result. No kitchen fallback or formation permission is
inferred from a navigation destination.

These are software-planning foundations for issues #87, #88, #143, and #144. They emit
no command and cannot authorize flight. Public `map_area`, search, mapped route
dispatch, and physical formation execution remain gated on their separate capability,
evidence, confirmation, relay, arbiter, adapter, and physical-acceptance requirements.

Current scope: `docs/mvp-plan.md` M3A–M3D and live issues #87, #88, and #143–#145.

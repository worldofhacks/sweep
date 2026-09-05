# Altitude controls

The opt-in altitude planner moves selected airborne drones vertically, preserves
each planned X/Y coordinate, and commands hold after each movement completes.
Grounded targets are refused. Altitude is absent from a deployment's effective
capability profile unless that deployment supplies explicit grounding.

## Frozen Intent v1 contract

`altitude` accepts exactly `{"delta": number}`. The delta uses configured steps:
with a 0.5 m step, `{"delta": 0.6096}` requests a one-foot ascent. Negative values
descend and zero preserves height. Unknown fields, mixed forms, booleans, strings,
and nonfinite or overflowing numbers are refused before planning.

Intent v1 does not expose an absolute-height argument or reference-frame flag.
Any future operator interface that accepts human units or an absolute target must
resolve that request to a PRD-compliant delta from one authoritative, immutable
snapshot before dispatch. It must not widen the relay or planner schema.

The optional floor reference is safety and provenance metadata in the same frame
as `AircraftState.pose.z` and the arbiter geofence. It is supplied through
`PlanningConfig.altitude_floor_z_m`; it is never inferred from a drone's home or
takeoff pose. A configured floor replaces the initial zero lower bound, including
for signed building coordinates, but it is not an externally addressable target.

## Configuration and capability profile

```python
from dataclasses import replace

configured = replace(
    existing_planning_config,
    altitude_step_m=0.5,
    altitude_floor_z_m=0.0,
    altitude_configuration_id="level-1-survey-v1",
    altitude_completion_tolerance_m=0.05,
)
```

`PlanningConfig.altitude_grounding()` returns either `None` when disabled or an
immutable grounding containing the step, optional floor, configuration identity,
and measured completion tolerance. A positive step requires an explicit nonempty
configuration identity and a finite positive completion tolerance.

`PlanningConfig.effective_capability_profile()` removes altitude when that grounding
is absent and never adds an intent omitted by the requested profile. A composition
root must derive this value once and pass that same immutable profile to the relay
runtime, session, bridge, and planner. This keeps advertised and enforced support
equal without a second altitude-specific flag or duplicated profile source.

The plan serializes grounding for preview. Language integration must bind its
fields into the preview/confirmation digest. Changing the step, floor, identity,
tolerance, or enabled state invalidates dispatch. The dispatcher checks the live
grounding before every adapter call, including continuation after asynchronous
completion. A dispatcher without that provider refuses altitude work; already
affected aircraft receive the existing best-effort safety hold.

A terminal hover acknowledgement is necessary but not sufficient for success.
The dispatcher also requires position telemetry newer than the pre-command
baseline, hovering flight state, and a measured pose within the configured
tolerance of the confirmed target. Missing or contradictory evidence fails closed.

## Safety and sequencing

The planner emits one `GOTO`/`HOVER` pair per selected aircraft. Ascent moves higher
aircraft first; descent moves lower aircraft first. Adapter commands advance only
after terminal completion evidence. No simultaneous-arrival behavior is promised.

The arbiter checks the complete sequence before I/O and each command against live
state. It enforces armed/airborne state, roster and epoch identity, operator and RC
authority, battery, fresh positioning, geofence, ceiling, and separation. Vertical
swept spacing includes stationary, completed, unselected, and newly drifted peers.
Both endpoints of a vertical segment must remain inside the geofence and ceiling.

Exact X/Y preservation is deliberate: live horizontal drift from the planned
column refuses the vertical command instead of dispatching a hidden correction.
Altitude also participates in conflicting-motion and stop handling. Synthetic
tests establish software behavior, not live-flight readiness.

## Verification

`planner/test_altitude_control.py` covers relay validation through planning,
arbitration, synchronous and asynchronous completion, configuration changes,
drift, swept collisions, and final measured boundary violations.
`arbiter/test_altitude_safety.py` independently exercises malformed plans and
command-time safety boundaries. The simulator evaluation verifies that one
effective capability profile is advertised and enforced end to end.

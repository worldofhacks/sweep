# Altitude controls

The opt-in altitude planner moves airborne selected drones vertically, preserves
each planned X/Y, and commands hold after each movement completes. Grounded targets
are refused. The default deployment configuration keeps altitude unavailable.

## Arguments and authoritative reference

`altitude` accepts exactly one of these argument objects:

| Arguments | Meaning |
|---|---|
| `{"delta": 1}` | Add one configured altitude step to each selected aircraft's current Z. Negative values descend; zero preserves height. |
| `{"height_m": 1.524}` | Set every selected aircraft to 1.524 m above the explicitly configured surveyed floor plane. |

`delta` keeps its existing step semantics. Language converts feet to meters, then
divides by the deployment's `step_m`: with a 0.5 m step, a one-foot ascent produces
`delta: 0.6096`. Absolute five-foot height produces `height_m: 1.524`. Units and
reference cannot be overridden by intent arguments. Mixed forms, unknown fields,
booleans, nonfinite values and nonpositive absolute heights are refused.

The authoritative reference is a surveyed horizontal floor plane in the same
coordinate frame as `AircraftState.pose.z` and the arbiter geofence. It is supplied
through `PlanningConfig.altitude_floor_z_m`; it is never inferred from per-drone
home or takeoff poses. Signed building Z is supported: a floor at Z = -2 m and a
height of 1.524 m yield target Z = -0.476 m if the geofence allows it.

Relative moves preserve differing starting heights. Absolute height converges to
the shared floor-relative target, subject to spacing. A target at or below the
configured floor is refused. Without a surveyed floor reference, relative moves
retain the initial positive-Z flight-domain lower bound of zero and absolute
height is unavailable. This first path does not infer terrain, stepped-floor
height or a different floor underneath each aircraft.

## Configuration and preview seam

```python
from dataclasses import replace

configured = replace(
    existing_planning_config,
    altitude_step_m=0.5,
    altitude_floor_z_m=0.0,
    altitude_configuration_id="level-1-survey-v1",
)
```

`PlanningConfig.altitude_grounding()` returns either `None` when disabled or an
immutable `AltitudeGrounding(step_m, floor_z_m, configuration_id)`. A positive
step requires an explicit nonempty configuration identity. A missing floor
reference still permits configured relative motion. The C1 capability projection
must advertise altitude only when grounding exists, and absolute height only
when `floor_z_m` exists.

The plan serializes this grounding for preview. Language integration must bind
all three fields into its preview/confirmation digest. Changing the scale, floor
reference, configuration identity or enabled state invalidates initial dispatch.
The controller binds a live grounding provider to the dispatcher, which checks it
before every subsequent adapter call, including resumption after an asynchronous
completion. A dispatcher without that provider refuses altitude work. Already
affected aircraft receive the existing best-effort safety hold when later work
is refused. A changed configuration requires a new preview.

The relay accepts the expanded shape, but the planner remains the deployment
capability gate. This PR does not enable a live deployment or add spoken phrase
recognition. The coordinated C1 language/profile integration consumes this seam.
The translation policy remains the existing authoritative world versus
aircraft-relative policy from `PlanningConfig`/`TranslationGrounding`.

## Safety and sequencing

The planner emits one `GOTO`/`HOVER` pair per selected aircraft. Relative ascent
moves higher aircraft first; descent moves lower aircraft first. Adapter commands
advance only after a terminal completed acknowledgement. No simultaneous-arrival
or group-formation motion is promised.

The arbiter checks the complete sequence before any I/O and checks each command
again against current state. It enforces armed/airborne state, selection and
roster/epoch identity, operator and RC authority, battery, fresh positioning,
geofence, ceiling and separation. Vertical swept spacing includes intermediate
stationary aircraft and completed projected positions, including unselected ready
aircraft. Stale peer positioning refuses a move. Both endpoints of a vertical
segment must be inside the convex box geofence and below the ceiling.

Exact X/Y preservation is deliberate: if authoritative horizontal position drifts
from the planned column before a vertical command, that command is refused. The
planner does not dispatch a horizontal correction as part of altitude control.
Altitude also participates in the existing conflicting-motion and stop handling.

The current arbiter uses its configured box and ceiling. Scan-derived obstacles,
per-zone clearance and measured stopping envelopes still require the indoor-map
integration gates. Synthetic tests do not establish live flight readiness.

## Verification

`planner/test_altitude_control.py` exercises validated intent payloads through the
controller, planner, arbiter and simulated adapter. It covers relative feet-to-step
examples, absolute heights from different starts, signed floor references,
grounded refusal, sequential column movement, stale state, swept collisions,
configuration changes, horizontal drift and asynchronous completion.
`arbiter/test_altitude_safety.py` independently tests malformed plans and the
command-time safety boundary. Run the full Python package suite with `uv run pytest`.

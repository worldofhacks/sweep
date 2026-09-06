# arbiter

Capability area: Autonomy. Milestone: M1.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Every arbiter or e-stop change names one change owner and requires cross-review.

Pure Python, no I/O. `SafetyArbiter.check_intent()` runs before planning,
`check_plan()` validates the complete frozen plan, and `check_command()` revalidates
against the latest snapshot immediately before I/O. Checks cover session and physical
armed evidence, e-stop, state, risky-intent confirmation, finite geofence and ceiling,
spacing against every ready airborne aircraft (selected or not), return battery reserve,
critical battery, link and positioning quality/freshness, operator activity, network and
physical-RC authority, active task, camera readiness, and capture storage.

Predicted unsafe `GOTO` trajectories pass through a Buffered Voronoi Cell projection
before adapter dispatch. The filter follows [Zhou et al., 2017](https://doi.org/10.1109/LRA.2017.2656241)
and the [Crazyflie firmware implementation](https://github.com/bitcraze/crazyflie-firmware/blob/master/src/modules/src/collision_avoidance.c):
it projects every requested velocity into the buffered cells of all ready airborne
aircraft, the geofence, and the altitude ceiling. The existing GOTO wire command
carries the projected position to the Android Virtual Stick controller. A deflected
command records its actual setpoint in the acknowledgement detail. An airborne peer
without velocity evidence produces a zero-displacement setpoint.
Completion acknowledges that projected setpoint, not the original destination; the
navigation controller must use the next authoritative telemetry sample to replan if the
original destination remains outstanding.

`SafetyConfig` has no deployment defaults. Every threshold—including battery reserve
and critical fractions, battery cost per metre, link/position freshness, operator
timeout, capture storage, motion-conflict window, and position-loss dwell—must come
from measured configuration. The arbiter, rather than the planner, caps accepted
capture pose drift and gimbal evidence error. Its positive future-clock-skew budget is
configured alongside #14's adapter-frame budget; timestamps at or below that bound are
accepted and later values fail closed. Non-finite numbers, booleans in numeric fields,
zero declared image overlap, and unordered geofences are rejected so configuration
cannot silently disable a gate.

Whole-plan validation binds every command to its plan and intent, rejects duplicate
command IDs, out-of-selection normal targets, and operations that do not match the
intent. The authority/telemetry bypass is available only to genuine safety `hold`,
confirmed `land_all`, and `estop` plans with matching `hover`, `land`, and `estop`
commands. An ordinary plan cannot self-label an unsafe command as a safety action.
Every earned intent also has an exact command count, target coverage, operation, and
parameter shape. Camera plans require one immutable anchor, unique ordered headings,
consistent declared tolerances, and source-linked retrieval steps. The whole-plan gate
simulates command order from current poses, so a later transient spacing collision is
refused with zero adapter I/O; the immediate gate repeats this against live state.

Hold plans carry a typed scope. Operator holds must cover the current authoritative
selection, fleet safety holds cover every eligible airborne aircraft, and targeted
internal holds are accepted only through `check_targeted_hold()` with a non-empty
caller-derived exact target set. A plan cannot make an empty hold succeed by labeling
itself as an internal safety action.

Operator-requested motion and camera work requires authoritative session arm
authorization and physical armed evidence where applicable. `hold`, fail-safe land,
and e-stop remain executable for degraded aircraft and during stop conditions. Unsafe
requests produce a typed refusal and zero requested adapter commands. The physical
RC-N1 and safety operator remain the independent pause, RTH, landing, and takeover
path.

Rule: no model in the safety path. Target: every safety rule has a test that tries to
break it.

PRD: sections 4.8, 5.5, 7.3, 8.6.

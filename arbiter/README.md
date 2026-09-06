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

Device classes change which gates apply, never whether one is checked. The geofence
bounds every device in x and y; only an aircraft is bounded in z, and the ceiling does
not apply to a ground vehicle on the floor plane. Within a class, spacing keeps its
existing target-position check. Across classes, motion reserves the configured
horizontal clearance along the full commanded paths, including stationary bodies and
the body pulse displacement envelope. Missing or unusable pose evidence blocks motion;
unbounded movement by another class must stop first. No height waiver is assumed.
These checks require measured shared coordinates and a physically suitable spacing
configuration; they do not establish either. The gates that ask
whether a device is airborne read `mobile` for a ground vehicle, so `hold`, `goto`,
`rotate_to`, and `hover` require one that is `idle`, `moving`, or `stopped`, and `arm`
requires one that is `docked`, `idle`, or `stopped`. `ground_max_speed_m_s` caps the
planned drive speed of a ground `goto` with a `speed_limit` refusal, and a ground
vehicle accepts only `goto`, `rotate_to`, `hover`, and `estop`; every other operation
is refused `unsupported_for_device_class`, as are the aircraft-only intents targeted at
one. Battery, link, telemetry freshness, position quality, operator, authority, and
membership gates are unchanged and apply to both classes.

Rule: no model in the safety path. Target: every safety rule has a test that tries to
break it.

PRD: sections 4.8, 5.5, 7.3, 8.6.

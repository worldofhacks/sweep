# Supervised vertical integration and landing recovery

The aircraft source is selectively integrated from PR #317 at
`f0b528b8ab51ad8cc5e6cc7b7d01da396b23e0ba`, including PR #316's verified
Virtual Stick mode contract. This is software integration, not hardware acceptance.
No aircraft installation or flight is part of the automated checks.

## Explicit deployment policy

`SWEEP_SUPERVISED_VERTICAL_JSON` selects the separate `supervised_vertical`
profile. It cannot be combined with world planning, safety, localization or
navigation configuration. Required fields are:

- `takeoff_altitude_m`: exactly 1.8.
- `maximum_height_m`: at least the target and at most 2.5908.
- `operator_declared_vertical_clearance_m`: at least the target; this is a host
  assertion about the measured available space, not a measurement made by software.
- `min_battery_fraction`, `min_link_quality`: explicit fractions.
- `max_link_age_ms`, `operator_timeout_ms`, `max_future_clock_skew_ms`,
  `motion_conflict_window_ms`: explicit nonnegative millisecond policies.
- `max_local_height_age_ms`: 1–500 milliseconds.

The profile supports selection, arm, fixed-height takeoff, hold, land, land-all,
estop and the implemented ground velocity/survey routes. It does not qualify
world navigation, formations, capture or a home pose. Voice additionally requires
an explicit qualified-intent allowlist. No qualification values are filled in
from historical flights or test fixtures.

Every supervised TAKEOFF carries `z_mm`, `maximum_height_mm`, and
`max_local_height_age_ms` inside its existing signed command arguments. The relay
rounds the lesser of the configured ceiling and declared clearance **down** to
millimetres. The phone requires the paired policy, uses the lesser of its local
limit and the signed limit, and retains it through climb, hover and HOLD. Stale
local height or crossing the effective ceiling ends upward control and invokes
landing; this cannot eliminate physical overshoot or replace measured clearance.
The signed limit never authorizes a climb in an ordinary phone profile. Older
phones reject the additional arguments; a supervised phone rejects policy-less
TAKEOFF. Update both sides before qualification. LAND has no height prerequisite
and never re-enables Virtual Stick merely to descend.

## Narrow landing recovery

DJI documents `KeyStartAutoLanding` as a flight-controller action, separate from
Virtual Stick enable and control parameters. This supports using the existing
hardware landing action after Virtual Stick alone dropped; it does not establish
permission to countermand an RC takeover.
[Flight-controller actions](https://developer.dji.com/api-reference-v5/Components/IKeyManager/Key_FlightController_FlightControllerKey.html),
[Virtual Stick API](https://developer.dji.com/api-reference-v5/android-api/Components/IVirtualStickManager/IVirtualStickManager.html).

The relay admits selected, confirmed LAND after loss of network control only
when fresh signed node status from the current connection epoch reports
`virtual_stick_dropped`, Virtual Stick disabled and a nominal watchdog. Physical
RC/safety-operator presence, current link evidence and operator confirmation
remain required. The phone independently requires the pilot's control grant,
connected aircraft and RC, current relay join, nominal deadman, airborne state
and its own VS-only loss latch. The latch stays set after landing, blocking
subsequent takeoff or translation until the pilot explicitly re-arms authority.

RC sticks, pause, go-home, mode-switch and known foreign authority revoke this
recovery path, including an RC event arriving after the VS-only loss while idle.
Automatic failsafe/estop logic does not use the explicit landing exception to land
under an RC takeover. A landing action acknowledgement is not completion: the
existing controller waits for landed telemetry and reports action errors/timeouts.
DJI may require a separate landing-confirmation action near the ground; this
integration does not automatically confirm it. Hardware acceptance must establish
that behavior on the actual aircraft and firmware.

## Remaining physical acceptance

Qualify the installed build's grounded VS reset/enable/disable evidence, bounded
climb and sustained hover, configured lower ceiling and height freshness, explicit
LAND after VS-only loss, RC takeover refusal, deadman, landing completion and
reconnect identity. Record actual outcomes and limits. Camera/positioning
telemetry remains diagnostic until calibrated sources, clocks, approved map
registration and route clearance are independently qualified. Automated fake-node
and JVM tests supply no physical positioning or flight evidence.

# Coordination verification

The integration was reviewed against production base `54db59ec` and tested through
the real relay HTTP/WebSocket boundaries. Tests use simulated aircraft responses
and a fake ground device; no physical motion or deployment was performed.

## Test evidence

| Check | Result |
| --- | --- |
| Focused relay, navigation, audit, MCAP, and live replay | 214 passed |
| Arbiter suite, including every command's stopped-state classification | 157 passed |
| Ground deployment environment contract | 4 passed |
| Ground HTTP, node runtime, STOP, and MCAP | 5 passed |
| Example environment declarations | 3 passed |
| Console | 1,165 passed; lint and production build passed |
| Bridge JVM | Four modules passed |
| Python/Kotlin interop | Observation and signed heartbeat passed |
| Isolated browser mission | Passed, including geofence and node-watchdog evidence |
| Changed Python files | Ruff lint and formatting passed |

The broad Python run recorded 2,845 passes and 20 failures while fixes were still
being assembled. Fifteen failures were an enum-classification fixture that omitted
the new ground command; the complete arbiter suite passes after adding its explicit
unsafe-while-stopped classification. The other failures covered ground completion,
sequential aircraft publication, aircraft readiness, restart authentication, and
duplicate environment declarations. Targeted reruns pass after the integration
fixes. Restart authentication also failed on the unchanged production base.

A separate spatial/perception/language/evaluation run recorded 771 passes and one
failure. The language transport test expects internal-only `navigate` in the generic
compiler schema; the same failure reproduces on `54db59ec`. Repository-wide Ruff
also reports ten pre-existing line-length errors in mapping files. These remain
separate from the changed-file checks; this report does not claim an all-green
repository run.

## Standards and correctness review

Independent review of the assembled ground integration found cancellation could
invalidate a route without stopping its node when HOLD selected another device.
The actual-node reproducer failed before the fix. The new independent STOP path
preserves cleanup even after the original intent is invalidated. The same path
covers delivery failure after bytes have already reached a moving node.

Review and end-to-end tests also exposed mismatched pose/readiness updates,
navigation/world-store lock inversion, and telemetry ticks invalidating frozen
reviews. Regression tests exercise matching authenticated readiness, concurrent
preview/confirmation callbacks, and real registry state rather than fixture-only
state shapes. No additional standards-only findings remained in the ground review.

## Spec review

Two acceptance gaps remain explicit. Mixed aircraft/ground selection is review-only,
so full mixed-selection execution acceptance remains open. The replay sidecar
records the available production producers; shared live occupancy and its fleet-wide
obstacle veto remain excluded with #245. Physical coordination replay and live-layer
acceptance for #93 remain open. No hardware or issue-completion claim follows from
the simulated tests.

The two-aircraft test confirms a platform route, exchanges signed route and pose
frames, and decodes both devices' records from the normal audit's MCAP export.
Ground tests confirm named routes through HTTP and the real node controller, then
check measured fake-device arrival and confirmed STOP. The checks also cover
qualified-world tracking loss, host approval withdrawal, cross-selection HOLD,
and uncertain delivery. Scan/tag replay fixtures pass through observation admission
before export, and live Foxglove tests use real sockets with a non-reading viewer.

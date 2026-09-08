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

The final broad Python run completed 3,663 tests in 15 minutes 45 seconds:
3,655 passed and eight failed. Five failures came from the local test directory
exceeding Unix socket path limits. One compiler-schema expectation still included
the internal-only `navigate` intent. Two audit-state checks needed to recognize
arrival time as volatile metadata. After those corrections, all eight failures
and their adjacent tests passed in a 112-test run using a shorter temporary path.
Repository-wide Ruff lint and formatting pass.

The ground-confidence and map checks passed 76 tests, including refusal of a new
route at zero confidence and confirmed STOP when confidence disappears during
motion. Replay and session checks passed 71 tests, including delayed membership,
telemetry, and acknowledgements exported through the real audit and MCAP writer.

All six CI jobs passed on the reviewed PR #332 head `105161dd`. Python completed
with 3,653 passed and ten skipped in 11 minutes 26 seconds. The Python job now has
a twenty-minute limit, following a measured timeout at the former ten-minute limit.
PR #332 merged as `09a7b835`.

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

The final review also found that zero-confidence ground poses could admit or
sustain motion. Admission and every execution revalidation now refuse those poses.
An independent STOP ends motion after confidence loss. Delayed source events now
retain a separate relay arrival time in the audit. MCAP metadata declares the
historical source-time fallback explicitly, and source wire timestamps remain
unchanged. Independent review of the timestamp correction found no further issues.

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

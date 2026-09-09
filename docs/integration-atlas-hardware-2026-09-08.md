# Atlas / hardware workflow integration

## Review scope and source heads

This integration carries the Spaces experience and native Atlas work into the
existing hardware checkpoint PR, rather than replacing either implementation.

- [PR #338](https://github.com/worldofhacks/sweep/pull/338), hardware checkpoint:
  `cceef72d6c5d39a7e55401e7dcc3f2d122d85408`.
- [PR #339](https://github.com/worldofhacks/sweep/pull/339), Atlas and application
  design: `deb56b7e73b8cb9bf42eaddab3a88a886dd23119`.
- Base `main`: `ba55cd1685c13b76508d4085bb635d4330f5bfcc`.

Both source heads passed all six GitHub checks before integration. Their green
results do not substitute for checking the combined head. #338 already contains
the head of #334 in its ancestry. #329 is a separate, overlapping change set and
is not incorporated by this integration.

## Reconciliation

- Keep the Spaces landing page, one shared header, sidebar, responsive navigation,
  compact Stop control, confirmation dock, and existing mineral/pine design.
  Atlas, Live, Gesture, the shared header, shell styles and tokens are unchanged
  from #339. Search now uses the same working-pane component as other fleet pages.
  Search, ordered-photo and captured-map controls reuse the shared spacing,
  borders, radii, colors and touch targets without a new component dependency.
- Compose Atlas, semantic speech, search and ordered-photo clients together, both
  at startup and after platform-provider refreshes. Regression tests cover their
  shared runtime and preservation of the exact client instances across refreshes.
- Keep #338's semantic speech compiler, current catalog checks and photo-route
  review path. Do not resurrect the superseded local text-matching fallback.
  Preserve the recording cleanup and staged-plan confirmation regression across
  a Spaces round trip. Shared-shell coverage includes all ten pages.
- Preserve #338's ordered photo retrieval, search/survey completion, cancellation,
  mapped navigation, Android camera readiness and signed observation paths.
  Retain Atlas Android activities, persistence, imports, uploads and metadata-cache
  lifecycle alongside those hardware services.
- Keep the browser-safe `fetch` wrapper and both generated-output ignore entries.
  Five text conflicts were resolved; no branch was replaced wholesale.

Two test portability issues were addressed during verification. The loopback
rehearsal resolves only its newly created temporary directory to a canonical path,
with a regression for a symlinked temporary root. Production artifact checks are
unchanged. A synthetic two-tag fixture now has enough nonplanarity to yield a
unique joint pose across OpenCV builds, while still asserting that each individual
tag is ambiguous. Localization code, thresholds and assertions are unchanged.

## Combined-tree verification

| Check | Result |
| --- | --- |
| Console, `pnpm test --maxWorkers=2` | 102 files, 1,301 tests passed |
| Console lint and production build | Passed |
| M14 real-browser / loopback mission | Passed, including geofence and node watchdog |
| Android fake / probe unit tests | 99 + 119 = 218 passed |
| JVM bridge-core / bridge-node / bridge-publish / bench | 217 + 63 + 35 + 14 = 329 passed |
| Both Android assemblies and lint tasks | Passed |
| Both APK native-library and 16 KB ZIP alignment checks | Passed |
| Signed Python/Kotlin observation and heartbeat interoperability | Passed |
| Loopback rehearsal tests after the temporary-path fix | 6 passed in 143.66 seconds |
| Synthetic localization suite on macOS | 26 passed |
| Linux platform-sensitive test groups, locked dependencies | 140 passed |
| Python lint / formatting | Passed, 438 files formatted |

The loopback rehearsal requires two ordered photos to be retrieved, a survey's
six GOTOs and final HOVER to complete with 32/32 coverage cells, and HOLD to prevent
later photo legs. These are simulator checks, not physical device qualification.

The initial full macOS Python run had 3,925 passes and 21 failures. Four rehearsal
failures and one synthetic localization failure were corrected and their complete
test files rerun. The remaining failures depend on Linux `/proc`, Linux installer
behavior, or a short Unix socket path. The unchanged affected groups, together
with localization, passed in an isolated Linux container using the locked Python
dependencies. This is not a claim that a final complete macOS Python run passed;
the combined-head Linux CI remains the full-suite merge gate.

Production-bundle browser checks covered Search and Captures at 1440 x 900 and
390 x 844, plus the captured-map empty state. Search's mobile document width was
390 px with one workspace header and no page-wide horizontal overflow. The
preview intentionally had no relay or map artifact configured; its unavailable
states are not evidence of connected hardware operation. Screenshots remain in
ignored `output/playwright/`, not in the application bundle. Existing large
map/3D bundle and toolchain/deprecation warnings remain visible.

## Deployment and merge gate

Deploy the updated relay before the new web/native clients. Keep Atlas storage
durable, preserve local originals and outbox rows, and follow the migration and
rollback notes in [the Atlas handoff](atlas-pr-handoff-2026-09-08.md). The original
native/emulator and reconstruction evidence remains linked there; it was not
repeated as physical-hardware acceptance during this integration.

Publish the combined merge history to #338, require its new CI run and an
independent approving review, then merge through the protected PR workflow. Do not
use the two source heads' approvals or test results as approval of a new head.
Keeping the merge ancestry makes #339 and the already-contained production PRs
traceable. Recheck the remaining PRs against the resulting `main` before deciding
whether to merge or close them.

The hardware checkpoint's retained 7 ft soft / 8 ft hard height limits and pending
physical localization, arrival, stopping-distance, multi-device and obstacle
measurements remain unchanged. No physical commands were sent for this review.

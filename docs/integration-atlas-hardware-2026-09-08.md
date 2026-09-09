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
  At the initial merge, Atlas, Live, Gesture, the shared header, shell styles and
  tokens are unchanged from #339. Search uses the same working-pane component as other fleet pages.
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

The initial combined head `a94ae477` subsequently passed all six GitHub checks.
Its complete Linux Python run collected 3,947 cases: 3,937 passed, 10 skipped,
and four warnings. This result belongs to that head, not to later follow-ups.

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

## Map recovery follow-up

The shared desktop/native map now offers **Retry map** after a tile failure.
It reloads only the raster street source, keeping the current camera, selected
space, markers and coverage layers. A pending retry is labeled; a new failure
restores the warning. MapLibre's idle event includes failed tiles, so it ends the
pending state but never clears an error. There is no background retry loop,
new dependency, new map provider, or claim of guaranteed offline imagery.
The existing secondary button has a 44 px target and the notice stays clear of
the zoom controls.

The complete console suite passes 102 files / 1,303 tests; lint and both web/native
asset builds pass. Both Android APKs assemble, and their unit tests and lint pass.
Desktop (1440 x 900) and phone (390 x 844) browser checks deliberately refused
street-tile requests, retained the warning after another failed attempt, then
restored real Austin tiles using Retry map without reloading the page.

The final fakeDebug APK also ran on the isolated AOSP API 35 emulator. With no
active Android network, zooming the saved Austin space produced a tile warning.
After Wi-Fi returned, Retry map restored the street map and cleared its pending
notice, retaining the same selected space, marker and 200 m scale. The workspace
metadata remained correctly labeled cached because its disposable relay was not
running. All 12 private test originals retained identical checksums. No new
capture, location sharing, physical hardware or fleet control was used. Local
screenshots remain ignored under `output/playwright/map-recovery-*.png` and
`output/playwright/android-map-retry-*.png`; this is not handset/network coverage
or general Android system-inset acceptance. New-head CI and independent review
remain required after publishing this follow-up.

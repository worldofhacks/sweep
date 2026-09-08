# Atlas and fleet UI continuity — 2026-09-08

Scope: the existing console and Android app on `codex/atlas-spaces`. This is a local
integration/visual audit, not physical-device acceptance or completion of the Atlas product.

## Design and fixes

- One mineral/pine token source for desktop and bundled Android web UI: shared surfaces,
  typography, action/focus colors, 8 px control / 12 px card radii, and border-led depth.
  Native Compose capture and fleet screens use matching `SweepTheme` values.
- Readable navigation labels and regular phone controls with at least 44 px height. Radio and
  checkbox labels provide the hit area; compact coverage cells remain a distinct data control.
- Short-window content scrolls with its heading. Header details and pending previews have
  bounded scrolling, leaving navigation reachable. Active mobile navigation scrolls into view.
- Device/Only selection pairs wrap as complete groups instead of overlapping adjacent devices.
- The keyboard skip link focuses the working pane. Changing pages preserves pending requests
  and the network-stop control; an empty roster does not hide an armed or active-stop state.
- Failed space reads clear stale live details and show a retry action. Failed native uploads
  retain actionable error styling and original-export controls.

## Browser coverage

Each state below was checked at **1440×1000, 768×1024, 390×844, and 320×568**, in two setups:
the real loopback Atlas preview and a separately built, in-memory populated fleet fixture.
The fixture uses the existing application and fixture clients, displays a conspicuous fixture
banner, and never connects to devices. It is ignored local tooling, not shipped fallback data.

| Page | States checked |
| --- | --- |
| Spaces | Workspace landing |
| Control | Swarm, Navigate, Ground, Capture, Commands, Requests, Fleet |
| Live | Live view |
| Gesture | Camera and readout, Gesture vocabulary |
| Speech | Speak or type, Compiler pipeline |
| Captures | Capture list |
| Worlds | Rooms, Jobs |
| Devices | Registry, Health, Config |
| Map | Live observations, Map authoring |

That is **21 states × 4 sizes × 2 setups = 168 checks**. No document overflow, undersized
visible regular phone controls, or overflowing device-selection pairs remained in this matrix.
Normal short-screen usable content measured at least 209 px in the preview and 234 px in the
populated fixture. Screenshots were also inspected visually; geometry alone missed the initial
selection overlap, so group-bound checks were added.

In a separate fixture workflow, a capture request remained pending while visiting all eight
other pages. Navigation sent no intent. Confirm sent exactly one confirmed capture request to
the in-memory client. At 320×568, the preview left an 81.8 px scrollable working pane and the
navigation ended at 568 px; expanded session details without a preview left 181.1 px. Confirm,
Cancel, navigation, and network stop remained reachable. Keyboard skip navigation passed.

Built Android assets were loaded under the shipped CSP at their actual asset origin in Chromium,
with a simulated native message port and real read-only preview HTTP. Uploading, failed, and saved
rows were explicitly labeled visual fixtures. Native capture/export actions were not simulated.
The actual authenticated Fountain model was also viewed in the shared reconstruction viewer.
This does **not** exercise Android WebView, CameraX, or background scheduling.

## Repeatable regression evidence

- `pnpm --dir console lint`: pass.
- `pnpm --dir console test --run`: **81 files / 1,175 tests pass**.
- `pnpm --dir console build` and `build:android`: pass. Map/3D chunks remain lazy; Vite size
  warnings are visible, not suppressed. No new UI component framework was introduced.
- `pnpm --dir console test:m14-browser`: isolated UI → relay → simulator mission passes,
  covering selection, arm/takeoff, confirmed translation, typed and mocked-recorded speech,
  hold/home, geofence refusal, node-watchdog hold/failsafe, emergency stop, and land-all.
  It now explicitly navigates from Spaces to Control and checks selection layout at four sizes.
  Test processes receive only their own configuration and cannot contact live device endpoints.
- Android fake/probe: **63 / 83 unit tests**, zero failures/errors/skips; both APKs assemble.
  Both lint tasks pass with zero errors and 26 warnings each. API compatibility and notification
  permission regressions are covered. Lint now runs with the existing CI Android test job.
- Both APK native-library and ZIP alignment checks pass at 16 KB (4 / 66 native libraries).

Relevant persistent regressions live in `console/src/shell/shell.test.tsx`,
`console/src/atlas/SpacesModule.test.tsx`, `console/scripts/m14-browser-smoke.mjs`, and Android's
`PlatformCompatibilityTest.kt` / `RawEvidenceExportTest.kt`. Local browser screenshots remain
ignored under `output/playwright/`; temporary fixtures are not included in production bundles.

## Limits and follow-up

No Android handset was available; the charging iPhone is excluded. The new emulator image license
is still awaiting owner approval. Native permission/capture/sensors, force-stop/relaunch,
background uploads, export, large-text behavior, and physical fleet behavior still need actual
device qualification. Camera/microphone/browser gesture hardware was not exercised by the page
matrix. The preview intentionally lacks fleet platform/map/media providers, so their unavailable
responses are expected and were not hidden or replaced with fabricated production success.

Native-library packaging/resource, Android lint, and Vite size warnings remain visible. Remote
CI has not been claimed as run by this local evidence. Detailed dense reconstruction and other
open product requirements remain tracked in [the product ledger](../atlas-product.md).

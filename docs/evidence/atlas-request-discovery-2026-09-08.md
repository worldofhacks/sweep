# Atlas request discovery — 2026-09-08

## Gap and change

Previously, **Needs views** selected every active space below 100% GPS coverage,
including spaces with no request. Surface requests were visible only inside 3D
review. The filter now uses server-derived actionable request counts; both request
kinds are discoverable in the existing shared desktop/Android Spaces interface.

- Directory cards and Overview show open-request counts. A filtered result opens
  its Requests view directly. Empty results explain what qualifies and allow
  returning to Explore independently of local draft-storage availability.
- Requests offer map-cell inspection, exact build/checksum/region inspection,
  source-bound capture, and linked originals. Completed-location, dismissed,
  earlier-build and resolved-space records remain under previous requests without
  soliciting new contributions. Existing 3D owner-review controls remain available.
- Map inspection recenters the requested cell. Model inspection loads the selected
  immutable manifest region, retires its read on navigation, and does not replay an
  old selection on ordinary return to 3D. Polling does not repeatedly reload it.
- Location requests are admitted transactionally, preserve the first note/time on
  retry and refuse new requests on resolved spaces. No database migration, native
  permission, service or package dependency was added in this increment.
- Shared pine/mineral colors, thin borders, 8px spacing and 44px controls continue.
  Five space-level tabs wrap on narrow layouts. Temporary street-map loading now
  has a status and busy state, including when returning from 3D.

## Verification

- Full console: **93 files / 1,257 tests passed** before the final map-loading
  regression. Final Atlas subset: **15 files / 90 tests passed**. ESLint,
  TypeScript, desktop and Android web builds passed. Existing Node and bundle-size
  warnings remain visible.
- Full relay/spatial: **1,206 tests passed**, with two existing Starlette warnings.
  New HTTP tests compare directory/detail counts through empty coverage, request
  retries, imports, qualified camera evidence, rebuilding, dismissal, resolve,
  reopen and restart. Workspace isolation is retained.
- Both Android fake/probe assemble, unit and lint tasks passed with the rebuilt
  assets. This uses the existing 86 / 106 unit-test suites, not a physical phone.
- Actual disposable preview: empty Needs views → Explore → map-cell request →
  Requests → inspect area → composer. Exactly one request was created and no
  capture/device command was sent. Reopening its filtered directory result landed
  in Requests. All three tested sizes (**1440×1000, 390×844, 320×568**) had no
  document/tab overflow and request controls at least 44px high. Phone maps kept
  their 250px and 170.4px areas.
- Built Android assets under the real asset-origin CSP, using simulated native
  IPC: opened the existing real Fountain request and its Region 3 on the verified
  photo-textured model. The manifest and GLB were read through authenticated HTTP.
  Original count stayed 11; the model checksum stayed
  `e1ea6aa6c8f16a47e5ad83f9eaafa6e14b4cc6b632c26d5e879bfe1669b9f9b7`.
- Android layout checks at **390×844 and 320×568** retained map space, fit dialogs,
  and passed the exact surface target to simulated camera and import actions.
  Actual hardware, media picking and capture were not invoked. A loaded street-map
  screenshot was additionally inspected after returning from 3D.
- Ruff and Git whitespace checks passed. A regression caught an empty-state Explore
  button incorrectly coupled to draft loading; that UI bug was fixed, not bypassed
  in the test. The earlier capture test was updated to navigate the new Requests
  view instead of the removed duplicate list in Coverage.

Local screenshots: `output/playwright/discovery-*`. Desktop request cards, 320px
controls, the actual native-layout 3D region, native source chooser and loaded
street map were visually inspected. The disposable fleet-free console still emits
its known `map_unavailable` navigation responses and media-provider warning; these
are not counted as qualified fleet/provider behavior.

## Limits

This is in-app polling, not background push delivery. Native offline metadata is
still explicitly marked cached. A request is not a safe route, a proof of missing
surfaces or capture GPS; imports never acquire coordinates from the requested area.
The original 3D reconstruction is not geographically registered or metrically
qualified. Hardware acceptance, full reconstruction qualification and licensing
remain outstanding in the unchanged broader Atlas objective. The refreshed saved
preview retains its credential/data; only the disposable test space was mutated.

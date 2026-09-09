# Sweep Atlas

Product objective: make the existing desktop console and Android application beautiful and
intuitive, and add collaborative, GPS-anchored incident spaces. People create a space, contribute
phone photos, videos or a guided 360 capture, see contributors who choose to share location,
explore a reconstructed 3D world, and request captures in missing areas.

Current checkpoint and merge guidance: [PR handoff — 2026-09-08](atlas-pr-handoff-2026-09-08.md).
The entries below are chronological implementation evidence, not blanket production acceptance.

## Experience

The primary destination is **Spaces**: a geographic map alongside a concise incident feed.
Opening a space exposes its story, capture coverage, source media, contributors, and 3D atlas.
The dominant phone action is **Add a capture**. Operational fleet controls remain available
in the same app. A reported incident is not automatically verified.

Design direction: calm field atlas. Neutral mineral surfaces, deep pine accent, crisp borders,
8 px spacing, 12–20 px surface radii, large readable headings, restrained 150 ms transitions.
Desktop uses a persistent navigation rail; phone layouts prioritize the map and a bottom action.

## Evidence required for completion

- Desktop and Android rendered and exercised at real viewport sizes; navigation, focus,
  empty/loading/error states and large text remain usable.
- Spaces persist and are shared through the existing relay service. Create, discover, open,
  resolve, and reopen a GPS-located incident/community space.
- Real photos/videos are uploaded durably with capture-time location, accuracy, heading,
  source, timestamps and checksums. Imported files never inherit a fabricated capture location.
- Android camera/location permissions, actual capture, interrupted uploads and retry work.
- Opt-in contributor location appears with accuracy and expires when stale; stopping sharing
  removes it. Space contributors have a scoped credential, not fleet control authority.
- Capture coverage and missing-cell requests are based on accepted sensor evidence. Coverage
  distinguishes camera positions from reconstructed surface completeness.
- An actual reconstruction worker processes real overlapping captures, preserves provenance,
  reports progress/failure, and produces a viewable 3D asset. A geographic camera layout or
  generated illustration does not satisfy this requirement.
- The demo can be understood and repeated from a fresh checkout. Example content is explicitly
  labeled, isolated from real reports, and never presented as current incidents.

## Implementation ledger

Baseline: `ba55cd1` on `main`, 2026-09-08. Worktree: `sweep-atlas`, branch `codex/atlas-spaces`.
The original saved scaffold is unrelated dirty work and is preserved.

The existing World Builder UI uses an unavailable catalog client; it is not an operational
reconstruction provider. Atlas will use the existing FastAPI application and authentication,
with a durable SQLite/media store. Heavy map rendering is loaded only when Spaces is opened.

## Verified progress — 2026-09-08

The first implementation uses the existing console, relay authentication and FastAPI service.
It adds no second web application or motion-control authority. MapLibre is loaded on demand;
the Three.js viewer is loaded only for a completed 3D model. COLMAP is a pinned optional
Python extra, not a dependency of the core relay installation.

- Real browser checks found and fixed an incorrectly bound native `fetch`, a collapsed map
  panel, a MapLibre CSS positioning conflict, and the Spaces skip-link target. Desktop
  (1440×1000) and phone (390×844) screens render. Space creation was exercised through the
  phone layout against the actual local API.
- Space creation, scoped invitations and revocation, media checksums and persistence across
  restart, coverage requests, rejected stale/inaccurate GPS, expiring presence, and owner-only
  reconstruction admission are covered by HTTP/store tests. Capture retries retain the exact
  file and metadata while the dialog stays open; they also offer a download. This is **not**
  durable offline capture yet.
- Real reconstruction acceptance: the 11 original PNGs from the official COLMAP example's
  Strecha Fountain archive were uploaded through HTTP, processed by the worker, and viewed in
  the console. All 11 views registered into one component; 14,991 points passed the track and
  reprojection filters. Mean reprojection error was 0.2261 px. The 240,696-byte GLB was served
  through authenticated HTTP and its SHA-256 verified. No ground-truth camera poses or geometry
  were fed into the reconstruction. Imported example images correctly produced 0 GPS coverage.
- Console: all 79 test files / 1,164 tests pass; production TypeScript/Vite build passes.
  Relay suite: 1,146 tests passed in the restricted environment; 15 socket-based tests could
  not bind localhost there. The three affected files were rerun with local socket access:
  all 21 tests passed (including those 15). Two additional GLB encoding tests pass. Targeted
  lint passes. Existing Vite large-chunk and Starlette deprecation warnings remain visible.

The isolated preview data and browser screenshots are local, ignored artifacts, not public
incident reports. The example fixture is documented in the
[official COLMAP example](https://github.com/colmap/pycolmap/blob/master/example.py).
Numeric acceptance evidence is recorded in `docs/evidence/atlas-sparse-2026-09-08.json`.

### Android implementation milestone

The existing Android app now bundles the shared Atlas interface with native CameraX capture,
capture-time sensors, a private SQLite/WorkManager outbox, encrypted Atlas access separate from
fleet controls, original export, and credential-isolated offline space metadata. Both APK variants
build; fake passes 63 and probe passes 83 local Android tests. The console now passes 1,175 tests and full lint.
The built Android UI was visually checked at phone size using a simulated native bridge and real
preview HTTP. That milestone did not verify an Android runtime; the final handoff adds
actual AOSP emulator photo/upload and subsequent video/scan/offline-recovery checks.
See [`atlas-android.md`](atlas-android.md)
for architecture, reproduction, evidence limits, and remaining Android lint warnings. Both Android lint variants now
pass with zero errors; lint is included in CI. The full goal remains active.

### Cross-page continuity

Spaces and the existing Control, Live, Gesture, Speech, Captures, Worlds, Devices, and Map pages
now use shared mineral/pine tokens, readable navigation, consistent form controls, and bounded
phone layouts. Native Compose screens use the corresponding `SweepTheme`. The audit fixed
overlapping device-selection pairs, inaccessible short-screen content, preview/footer overflow,
keyboard skip-link focus, and stale space details after access failure. Safety state remains
visible when an armed fleet empties; navigation does not send a pending request.

The [2026-09-08 continuity evidence](evidence/atlas-ui-continuity-2026-09-08.md) records 168
page/subtab/viewport checks, confirmation continuity, and the passing isolated simulator browser
mission. These results cover tested local UI and simulated-device workflows, not every physical
camera, robot, network, or accessibility configuration.

### Existing functionality is an acceptance requirement

Atlas is additive. Typed and recorded speech, gesture tracking and confirmation, the dynamic
all-device camera wall, and every existing fleet page remain required. New work must preserve
source-bound confirmation, network stop, capability/refusal states, and the shared UI design.
An unavailable provider or untested hardware path must not be presented as working.

Three additional page-round-trip regressions exercise microphone cleanup and restarted speech,
explicitly re-enabled gesture confirmation, and cameras joining while the operator is in Spaces.
The console now passes **1,178 tests**. Leaving an input page intentionally releases its local
microphone/tracker; returning does not silently resume recording or gesture commands. Live
playback releases hidden sessions and reopens the current roster on return. These lifecycle
rules preserve functionality without keeping unused capture or playback resources alive.
Physical-device acceptance and unfinished Atlas capabilities below remain outstanding.

### Experimental photo-textured surfaces

The optional local worker can now continue calibrated camera reconstruction through dense depth,
triangle meshing, and photographic texturing. A fresh 11-photo Fountain build produced 539,046
dense points and a **98,895-triangle / 61,025-render-vertex** mesh. Its two 2048×2048 texture
atlases are embedded in one **9,793,456-byte GLB**, below the existing 16 MiB web/native limit.
Authenticated HTTP verification checks the geometry, artifact and texture checksums, and original
source provenance. No reference mesh, reference camera poses, or synthetic scene is used.

The shared viewer supports both the existing sparse format and bounded embedded-PNG meshes.
It checks geometry/resource references before loading, treats missing texture decodes as failure,
releases textures and decoded bitmaps on navigation, and frames both axes on tablet layouts.
The Android CSP permits local texture-blob reads without allowing arbitrary network origins.
The UI distinguishes surface triangles from sparse points and camera fit from surface accuracy.

Visual inspection rejected the first integrated build: default seam-leveling color adjustments
produced black/neon patches in this macOS engine. Disabling both global and local seam leveling
restored photographic colors; a new complete build verified the correction. Hole filling,
artificial tower points, depth-gap interpolation, mesh smoothing, and texture sharpening remain
disabled. Visible gaps are retained rather than filled for presentation. See the
[dense evidence](evidence/atlas-dense-2026-09-08.json) for the accepted local build and limits.

**Deployment gate:** OpenMVS 2.4.0 is operator-provided and experimental, not bundled, downloaded,
or enabled automatically. Its [copyright notices](https://raw.githubusercontent.com/cdcseacave/openMVS/v2.4.0/COPYRIGHT.md)
include AGPL and a research-only IBFS component; the pinned
[mesh source enables IBFS](https://github.com/cdcseacave/openMVS/blob/v2.4.0/libs/MVS/SceneReconstruct.cpp#L55-L57).
Production use requires a reviewed engine/build and license compliance. This local experiment
does not resolve that gate or qualify phone performance, geometric accuracy, or geographic scale.

Current local regressions: **84 console files / 1,201 tests**, **1,179 relay/spatial tests**, full
ESLint and targeted Ruff, both web builds, both Android assemble/unit/lint variants (63 / 83 unit
tests), and the isolated M14 control/speech browser mission pass. Both APKs pass 16 KiB alignment.
The model was visually inspected at desktop/tablet/phone sizes and under the Android asset CSP;
selecting 3D reveals it on the short phone layout. Actual Android hardware is still unverified.

### Mesh-derived surface review

Dense builds now generate review candidates from actual open triangle edges, with exact-position
welding across texture seams and duplicate-face removal. A bounded 4×4×4 partition excludes tiny
groups and ranks the six displayed regions by boundary length, not urgency. Open edges may be
natural object boundaries, scan extents, or reconstruction gaps: this is **not** complete missing-
surface detection, a safe viewpoint, a GPS transform, or a metric coverage score.

Selecting a region in **3D atlas → Surface review** focuses the model and overlays sampled actual
edges, including occluded ones. Owners can share a request tied to the exact build, region ID,
and artifact checksum. Invited contributors inspect the same target. Requests persist, retries
do not duplicate them, and uploads/rebuilds never auto-resolve them. Earlier-build requests cannot
highlight new geometry. Owner dismissal is not an assertion of reconstructed completeness.

**Contribute this view** now binds a photo, video, or scan view to an existing location or
current-build surface request. The web composer and Android camera/import chooser show the
requested context. Request cards link to the contributed originals, including older captures
beyond the initial 24-item page. Dismissed surface requests retain their linked originals.

The upload metadata may contain `response_to`, either `{kind: "location", cell_id}` or
`{kind: "surface", job_id, artifact_sha256, region_id}`. The relay validates this target against
the authorized space and commits its relation with the capture in one SQLite transaction.
Original-byte deduplication is unchanged: another request adds a relation, not another file or
rewritten original metadata. Membership is limited to eight requests per original; retries of
an existing membership remain idempotent. Detail responses expose each request's `capture_ids`.
Clients require the exact `response_to` acknowledgment before reporting a linked upload saved.
Delayed native uploads keep their old build binding after rebuilding or dismissal, but a resolved
space must still be reopened before any upload. Uploads neither auto-dismiss surface requests nor
turn imported media into qualified GPS coverage. Push notifications remain unimplemented.

### Discovering requested views

The directory's **Needs views** filter now uses an authoritative `open_request_count`,
not GPS coverage below 100%. Counts include unobserved requested location cells and
open regions from the current ready model with the exact artifact checksum. Resolved
spaces, earlier builds, dismissed regions, and locations already captured do not
solicit contributions. Empty coverage without a request is not a call for people to
go there. The count is workspace-scoped and available in both list and detail responses.

Opening a Needs views result goes directly to **Requests** inside the space. Overview
also offers an open-request shortcut. The shared desktop/Android list combines map
and model requests with inspection, source-bound contribution, and linked-original
actions. **Show requested area** centers the actual requested cell; **Inspect in 3D**
loads the exact immutable model region. Leaving retires that read, and ordinary
navigation back to 3D shows the whole model. Previous requests remain inspectable
without inviting contributions to stale geometry. Location-request retries preserve
the original note/time, and resolved spaces refuse new requests.

This is in-app discovery using existing polling, not background push delivery.
Android's existing offline banner continues to identify cached space information.
The [request discovery evidence](evidence/atlas-request-discovery-2026-09-08.md)
records software and rendered-layout verification; full hardware and reconstruction
qualification remains outstanding.

Polling includes only a small summary; sampled edges load from the authenticated immutable
manifest on selection. Navigation aborts pending reads, removes old highlights and disposes line
resources. Android's narrowly scoped allowlist includes these routes without exposing worker
internals or fleet APIs. No new framework or geometry library was added.

A fresh complete 11-photo Fountain build (`544db565-bec9-4bf9-aae9-5ef89a696d0d`) produced 98,951
triangles and 540,143 dense points. Its 4,557 open edges produced 16 eligible groups; six are
displayed. HTTP verification recomputed guidance from the downloaded mesh and matched the manifest
and summary; GPS coverage remains zero. A browser owner published one explicitly labeled local
demo request, and an invited contributor inspected it under the Android asset CSP. This used
simulated native IPC, not a physical Android runtime. See the
[surface-review evidence](evidence/atlas-surface-review-2026-09-08.json).

### Persistent Spaces-style shell

The owner's updated design requirement makes the Spaces brand header and sidebar persistent
across Control, Live, Gesture, Speech, Captures, Worlds, Devices, and Map. The branded header is
one shared component. Following the owner's screenshot feedback, there is no second network-stop
header or permanent development-fixture band. A compact stop action and on-demand session panel
live inside the main header; demo status is explicitly labeled there. Active fleet warnings and
pending confirmations remain accessible without opening the panel. Unconfigured fleet services
do not add alarm bands to a quiet, fleet-free Spaces workspace.

The operational pages share border-led cards, pine selections/actions, 44px controls, mineral
surfaces and the Spaces heading hierarchy. Control separates device selection, fleet actions,
and movement; Live keeps the complete dynamic camera wall; Speech/Gesture use grouped input and
review panels; Worlds uses the available page width for room/job cards rather than a half-empty
two-column wrapper. Existing event handlers, source-bound confirmation and media lifecycles are
retained. No UI framework or new dependency was added.

All five local preview examples are anchored in Austin, Texas, and new preview seeds also use
Austin. These are demo scenarios, not incident reports. The imported Fountain photos explicitly
remain non-geolocated: the Austin pin is demonstration placement, not capture metadata. All 11
original capture records are unchanged.

Survey, community and hazard views retain a dedicated map area beside the desktop details panel,
or above independently scrolling details on phones. Map zoom is bounded to 0–22, with the minimum
raised on tall viewports to avoid empty polar bands and no-op zoom-out clicks. Real OSM raster
tiles stop at z19 and are overscaled above that level instead of requesting nonexistent tiles.
Polling does not reset the operator's zoom; explicit recentering is available. Map imagery still
requires WebGL and a reachable tile provider, and failures are reported rather than fabricating
geography.

### Existing-media contribution on Android

The shared Android interface now offers camera capture or import from the system
file picker. Selected photos/videos are durably copied and checksummed on-device
before entering the existing upload pipeline. Scope remains bound to the original
workspace and space. Partial copies, temporary provider failures, quota limits,
read-permission cleanup and an in-place outbox schema migration have regression
coverage. New imports on both web and Android retain unknown capture time as null;
file modification dates and today's GPS are not substituted for capture evidence.
The original bytes remain unchanged. Full Android runtime acceptance is still
required. See the [native import evidence](evidence/atlas-native-import-2026-09-08.md).

### Private new-space drafts

Creating a space now starts one private, automatically saved draft per exact workspace
credential. Text, category, partially entered coordinates and radius survive navigation
and reopening. Desktop stores bounded JSON under a SHA-256-scoped key; Android uses
the existing encrypted native vault rather than WebView storage. A contribution-only
invitation cannot create or read an owner draft. These are local drafts, not shared
reports, and clearing browser/app storage removes them.

**Publish space** is explicit. Before sending anything, the app durably saves a frozen
submission and a random draft ID. The relay transaction admits that ID once per workspace;
a lost reply, restart, or simultaneous retry returns the same space. Conflicting content
is refused and retries do not rotate invitations or reopen resolved incidents. Until
confirmation, the form is locked and **Check publication** retries the exact saved report.
There is no automatic background incident publication or non-idempotent fallback.

Browser writes use a cross-tab lock and revision comparison; stale editors cannot overwrite
a newer draft. The saved indicator follows storage acknowledgment, not just a field change.
**Keep for later** waits for that acknowledgment; **Discard draft** requires confirmation
and only removes local text/location. Creation notices stay inside the form instead of
covering its phone controls. The map remains visible while editing and retrying.

See the [draft acceptance evidence](evidence/atlas-drafts-2026-09-08.md). Bundled Android UI
and native JVM storage tests pass, but actual handset cold-start/storage-failure acceptance
remains open. Desktop recovery needs the app assets to load; this does not add a service
worker or promise a cold offline website launch. Browser drafts require HTTPS/localhost
and Web Locks support. Updated clients require the new draft-publication relay endpoint.

## Repeat locally

From the repository root:

```sh
uv sync --extra atlas
pnpm --dir console install --frozen-lockfile
pnpm --dir console build
.venv/bin/python -m tools.atlas_preview --port 8177 --examples
```

Open `http://127.0.0.1:8177`. This preview binds loopback only, uses isolated data in
`.sweep/atlas-preview`, and generates an ephemeral local workspace credential. It is not a
LAN deployment or a live robot session. The fleet navigation/media providers are unconfigured
in this preview; their existing unavailable responses do not indicate Atlas upload failures.

In a second terminal, start the optional worker:

```sh
.venv/bin/python -m tools.atlas_worker --data-dir .sweep/atlas-preview/atlas
```

For the explicit **local dense experiment**, supply an existing OpenMVS 2.4.0 directory containing
`InterfaceCOLMAP`, `DensifyPointCloud`, `ReconstructMesh`, and `TextureMesh`:

```sh
.venv/bin/python -m tools.atlas_worker --data-dir .sweep/atlas-preview/atlas --openmvs-bin /absolute/path/to/openmvs --once
```

Review the deployment gate above before using this dependency. The worker verifies its version,
records individual binary hashes and processing settings, and uses its own supervised process
group so timeout/cancellation cannot leave an engine child running. A dense failure is reported
as failure, never silently passed off as a successful sparse result. With no dense option, the
existing sparse worker remains available. A completed build cleans its large temporary files.

Create a space, upload overlapping photos or a walking video, and select **3D atlas → Build
3D atlas**. The UI reports queue, feature extraction, matching, mapping, completion or failure.
Workers process one job at a time with four CPU threads and a 20-minute job timeout. Jobs
snapshot source IDs and checksums, cap prepared views at 120, preserve the actual COLMAP
camera/track solution, and publish only checked image-derived points. A vanished heartbeat
fails a job; it does not silently restart beside a potentially live process. Rebuilds create
a new job. A contribution invitation can view results but cannot enqueue expensive builds.

For a repeatable real-photo check, download the image archive linked by the official example
and extract only `Fountain/images/*.png` into a temporary directory. Do not import the provided
ground-truth geometry. Then run `tools.atlas_reconstruction_smoke --images PATH` via
`.venv/bin/python -m`; it prints the created demo space ID. After processing, run the same tool
with `--verify-space SPACE_ID`. The tool targets only the local preview.

## Remaining requirements — goal is not complete

The [native cache lifecycle follow-up](evidence/atlas-cache-lifecycle-2026-09-08.md)
now caches successful detail reads natively and invalidates observed access refusals
without touching contributor originals or drafts. Its regressions cover restart
persistence and older in-flight observations; the actual APK's ordinary offline
and cold-start cache flow also passes. This does not promise remote erasure or
knowledge of invitation changes while the device has no connection.

The [two-client runtime checkpoint](evidence/atlas-contributor-runtime-2026-09-08.md)
now verifies a desktop owner requesting a map view and an actual Android emulator
contributor capturing offline, recovering automatically, and opening the exact
linked original on both clients. Online invitation replacement also removes the
contributor's remote view while preserving private originals. These are software
runtime results, not physical multi-handset, GPS, HTTPS or complete revocation-
lifecycle qualification.

1. Verify the implemented Android-native camera/location/sensor integration and durable upload
   recovery on the actual Android runtime; native permission, lifecycle, storage and real device tests.
   No Android handset was detected by ADB on this host; the charging iPhone is excluded.
2. Qualify the experimental detailed reconstruction for production: license-reviewed engine,
   diverse real phone photo/video sets, surface accuracy, resource quotas/failed-job cleanup,
   and actual Android performance. The Fountain build is now a real photo-textured mesh, but
   its scale is relative and it is not georeferenced. Geographic alignment requires sufficient
   measured evidence; a space's map pin is not proof of a 3D transform.
3. Extend open-edge review into qualified missing-surface and targeted-viewpoint guidance using
   camera support, occlusion and geographic evidence. Boundaries alone do not establish what is
   missing; small and undetected areas remain unclassified. Gray cells still represent camera
   positions, not surface coverage. Proactive notifications and verified post-capture resolution
   are not implemented.
4. Full multi-contributor / second-device invitation testing, reliable identity/session boundaries,
   actual-device qualification of the implemented private drafts, physical library-import verification, thumbnail/storage lifecycle policy,
   and cross-device HTTPS deployment. Android's native-capture outbox and cached space metadata
   exist, but are not proof that every offline workflow is complete.
5. Further mobile navigation, type/contrast, focus, large-text and capture-preview refinements.
   Benchmark map and viewer startup on an actual Android device; do not hide bundle-size warnings
   as a substitute for performance work.

Completion still requires the full product objective above, not just passing a reconstruction
milestone. Existing fleet control and the original dirty scaffold must remain intact.

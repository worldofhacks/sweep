# Sweep Atlas

Product objective: make the existing desktop console and Android application beautiful and
intuitive, and add collaborative, GPS-anchored incident spaces. People create a space, contribute
phone photos, videos or a guided 360 capture, see contributors who choose to share location,
explore a reconstructed 3D world, and request captures in missing areas.

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
the Three.js point viewer is loaded only for a completed 3D model. COLMAP is a pinned optional
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
preview HTTP. It is not yet verified on an Android device or emulator. See
[`atlas-android.md`](atlas-android.md) for architecture, reproduction, evidence limits, the emulator
license approval dependency, and remaining Android lint warnings. Both Android lint variants now
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

1. Verify the implemented Android-native camera/location/sensor integration and durable upload
   recovery on the actual Android runtime; native permission, lifecycle, storage and real device tests.
   No Android handset was detected by ADB on this host; the charging iPhone is excluded.
2. Dense, detailed reconstruction. The delivered model is an actual **sparse point cloud**,
   not a textured mesh, Gaussian scene, or dense surface model. Its scale is relative and it
   is not georeferenced. Geographic alignment must be supported by sufficient measured evidence;
   a space's map pin is not proof of a 3D transform.
3. Reconstruction-aware surface gaps and targeted capture guidance. Current gray cells represent
   qualified camera positions, not which surfaces have been reconstructed. Viewpoint requests
   are visible in the shared space; proactive contributor notifications are not implemented.
4. Full multi-contributor / second-device invitation testing, reliable identity/session boundaries,
   durable offline new-space drafts, native library imports, thumbnail/storage lifecycle policy,
   and cross-device HTTPS deployment. Android's native-capture outbox and cached space metadata
   exist, but are not proof that every offline workflow is complete.
5. Further mobile navigation, type/contrast, focus, large-text and capture-preview refinements.
   Benchmark map and viewer startup on an actual Android device; do not hide bundle-size warnings
   as a substitute for performance work.

Completion still requires the full product objective above, not just passing this sparse-model
milestone. Existing fleet control and the original dirty scaffold must remain intact.

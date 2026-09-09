# Atlas and application design checkpoint

## Scope

This checkpoint packages the work on `codex/atlas-spaces` against `main` at
`ba55cd1`. It preserves the existing fleet application and adds Atlas within the
same desktop console, Android application, relay and authentication model.
It is a software integration checkpoint, not a declaration that the full product
or physical-hardware qualification is finished.

- Shared Spaces-style header, sidebar, mineral/pine tokens, forms and responsive
  layouts across Control, Live, Gestures, Speech, Captures, Worlds, Devices and
  Map, including fleet and connection/status views. The duplicate operational
  header and development-fixture band are removed. A compact Stop action and
  on-demand session details retain operational access.
- Austin-centered example spaces and persistent map layouts, including close
  zoom/overzoom and narrow screens. Demo locations are labeled; sample Fountain
  photography is not claimed to have Austin capture GPS.
- Durable GPS-anchored spaces, scoped invitations, opt-in expiring presence,
  capture uploads, checksum validation and sensor-backed coverage.
- Shared desktop/native capture requests and discovery, exact requested-view
  acknowledgments, previous-request history, and stale-build rejection.
- Private desktop/native space drafts with idempotent explicit publication.
- Native CameraX photos, silent video and guided scan views; encrypted access,
  durable private originals/outbox, WorkManager uploads, document import/export,
  credential-bound retries and offline space metadata.
- Real optional COLMAP reconstruction and an explicitly opt-in experimental
  photo-textured OpenMVS path. Mesh-edge review candidates are not certified
  missing surfaces, safe routes, or georeferenced geometry.

## Verification

Final checks are recorded here and in the PR description. Detailed evidence:

- [Cross-page UI cohesion](evidence/atlas-ui-cohesion-2026-09-08.md)
- [Speech, gesture and multi-camera continuity](evidence/atlas-ui-continuity-2026-09-08.md)
- [Private drafts](evidence/atlas-drafts-2026-09-08.md)
- [Native import](evidence/atlas-native-import-2026-09-08.md)
- [Requested-view capture binding](evidence/atlas-request-captures-2026-09-08.md)
- [Request discovery](evidence/atlas-request-discovery-2026-09-08.md)
- [Real textured reconstruction](evidence/atlas-dense-2026-09-08.json)
- [Android runtime smoke and limitations](atlas-android.md#actual-android-emulator-smoke--final-handoff)

The final console run passes 93 files / 1,258 tests, ESLint and production build.
Both Android variants assemble and pass lint: fake has 86 unit tests and probe has
106, all passing. The four JVM bridge suites pass 297 tests. The complete Python
suite passes 3,343 tests with four deprecation warnings. Python lint and formatting
pass. The isolated M14 browser mission passes, including geofence and node-watchdog
evidence. GitHub Actions results are separate from these local results.

The actual AOSP API 35 emulator check used the shipped debug APK and actual
WebView, CameraX, Keystore-backed access, private outbox and relay HTTP, without a
simulated native bridge. One generated-camera JPEG was saved locally and remotely
with matching SHA-256. Location stayed denied and coverage stayed zero. An app
force-stop/update retained the workspace connection. The check found and fixed
unwanted camera/location permission coupling. This is not a handset/video/scan,
location-sharing lifecycle, network-loss or background scheduling acceptance.

Warnings remain visible: large lazy map/3D bundles, existing Android lint warnings,
and toolchain/deprecation notices. No passing result is claimed for an unrun check.

## Deployment and review order

Deploy the updated relay before new web/native clients: null capture timestamps
for imports, draft publication and exact request acknowledgments need the new
server contracts. Keep the existing Atlas data directory durable. Outbox schema 2
migrates existing rows without rewriting originals. Back up application data
before deployment; rollback across new persisted data needs explicit review.

The branch was fetched against current `main` before publication and required no
base update. It does not incorporate or approve other open hardware/console PRs.
PRs #329, #334 and #338 overlap portions of console, CI or Android integration
(10, 6 and 16 shared paths respectively at this checkpoint; overlap is not itself
proof of a text conflict).
Merge one reviewed change set at a time, then refresh and rerun checks for the
remaining branches. Preserve Atlas launcher/assets, the shared workspace shell,
speech/gesture confirmations, camera selection, and existing flight/control
safety contracts when resolving overlaps. Do not blindly replace files wholesale.

Review the PR in layers: relay/storage contracts; Android capture and persistence;
shared Atlas experience; fleet-shell continuity; optional reconstruction; evidence.
Local preview databases, credentials, emulator images, APKs, screenshots, downloaded
sample originals and reconstruction binaries remain ignored and are not committed.

## Explicit follow-ups

- Physical Android camera/video/scan, picker/provider, GPS/heading, revoked
  permissions, process death and network-loss/background-worker qualification.
- Production licensing/build review for OpenMVS and its dependencies; geographic
  alignment and genuine missing-surface qualification. The dense engine remains
  opt-in and is not installed or redistributed by the default app.
- Contributor push notifications, HTTPS deployment, broader multi-device access
  lifecycle tests and large-text/tablet refinements.
- Review and reconcile the other open PRs independently. This checkpoint does not
  close hardware issues, authorize physical motion, or claim those branches pass.

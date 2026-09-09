# Atlas in the Android app

Atlas is the portrait launcher in the **existing** Android application. **Fleet** opens the
existing landscape pilot activity. The probe variant still handles the USB accessory intent
in that pilot activity. Merely opening Atlas or running its upload worker does not instantiate
an aircraft session, relay control node, or video publisher.

## One interface, native capture

The native entry bundles the same React spaces, MapLibre map and Three.js reconstruction viewer
as the desktop console. It does not load a remote website. Its entry excludes the inactive fleet
clients and their connection polling. Gradle builds those assets from `console/src/atlas` through
the supported Android generated-assets API; CI installs the locked JavaScript toolchain first.
Desktop and native web views share the mineral/pine tokens. CameraX capture and the existing
Compose fleet screen use `SweepTheme`, with matching semantic colors and control radii.

CameraX supplies photos, silent videos (60 seconds / 60 MB), and guided overlapping 360 scan
views. Location is optional; it can be enabled separately after camera permission. The app records
fresh, capture-time coordinates and accuracy, plus an absolute rear-camera heading when reliable
orientation and a location-derived magnetic declination are available. GPS travel bearing is not
used as camera heading. Missing, invalid, or stale fixes remain absent, not guessed.

Private original files and a SQLite outbox persist independently of the camera activity.
Finalization syncs and hashes the file before admitting it to WorkManager. Uploads require
connectivity, retain their original credential/destination, and retry temporary network/server
failures. The server's matching SHA-256 is required for **Saved**. An interrupted unfinalized
capture becomes an actionable failure on cold startup, retaining its original. Local copies can
be exported through Android's document picker or removed with native confirmation; successful
uploads are not silently deleted. The queue is bounded at 100 items and approximately 1 GB.

Atlas keys use separate Android Keystore-backed encrypted preferences, never pilot setup keys.
The web message port is restricted to the bundled origin and main frame. JSON calls are restricted
to Atlas endpoints; media streams use a same-origin, bounded native interceptor. The interface
does not receive the stored key. External navigation, frames, executable remote content, redirects,
and mixed-content web requests are blocked. Workspace transport must be HTTPS except for explicit
local development addresses. No TLS verification bypass is provided. Backup/device transfer of
private app files, databases, and preferences is excluded.

Opened space metadata is cached for offline capture into known spaces (20 bounded entries,
isolated by credential). Cached live people are stripped. The interface labels the offline state;
it does not imply that map tiles, source media, or reconstructed geometry are cached for offline use.
Revoked invitations do not fall back to cached access. A network failure can use previously cached
space metadata; a server authorization refusal is shown as a refusal.

Mesh-derived surface review uses the same native-scoped JSON transport. Geometry annotations load
on demand from the authenticated build manifest, not in every cached/polled space detail. The
endpoint allowlist admits only the required manifest, request and dismissal paths; tests retain
cross-space/fleet refusal. An invited contributor inspected a real shared request at 390×844 and
320×568 under the bundled asset CSP with no new page/texture errors. Native IPC was simulated;
this is not WebView, camera, sensor or background-work hardware acceptance.

## Import existing photos and videos

**Add a capture** now offers **Use your camera** or **Import from device**. The
second option opens Android's document picker for up to ten JPEG, PNG, WebP, MP4
or WebM files, each at most 64 MiB. It requests only persisted read access to the
selected documents, not camera, location or broad library permission. HEIC/MOV
are not yet supported; they are rejected with a format message, not transcoded.

The picker target snapshots the original credential, space and contributor before
launch, and survives activity recreation. A separate persistent import job copies
the selected bytes into private storage, syncs/hashes the completed file, then
hands it to the existing upload worker. A restarted copy truncates its incomplete
local file and starts from the selected source. Partial files cannot acquire a
completed-original or saved badge. Moving/revoking a source gives an actionable
failure. Local-source copies can run offline; cloud document providers may need a
connection. Interrupted copies retry with backoff, then expose a manual retry.

Outbox schema 2 migrates existing rows without moving or rewriting their files.
In-flight copies reserve their bounded capacity within the 1 GiB local quota.
The source URI remains private and its persisted grant is released after the
last dependent import is complete or removed. Importing never retargets a capture
to a newly selected workspace. No new Android dependency or manifest permission
was added. This follows Android's
[persisted document access](https://developer.android.com/training/data-storage/shared/documents-files)
and [persistent work scheduling](https://developer.android.com/develop/background-work/background-tasks/persistent)
model.

New imports use `source=import`, `position=null`, and `captured_at=null`. Neither
the current phone fix, import time, nor a file's modification date is capture-time
evidence. Source bytes (including embedded metadata) stay unchanged. The relay
continues to require a timestamp for new camera captures. **Deploy the updated
relay before the new web/native clients**: an older relay rejects null timestamps;
the native original remains in the outbox with an actionable refusal, not a lost upload.

The import implementation is tested with Robolectric, real SQLite/private files,
ContentResolver streams and native HTTP; actual Android picker/provider behavior
remains a device qualification. See the
[import evidence](evidence/atlas-native-import-2026-09-08.md).

## Private space drafts

With a saved owner workspace connection, **Create a space** automatically saves a local
draft, including unfinished text and coordinates. This uses `AtlasVault`'s existing
encrypted preferences through `AtlasDrafts`, not WebView local storage or the upload queue.
One draft belongs to each immutable credential ID. A contribution-only invitation cannot
create a new space. Switching credentials does not expose or move a different draft.

**Keep for later** waits for a successful native disk commit. A failed commit is shown as
unsaved, and an identical save can be safely retried if its bridge reply was lost. Revision
checks refuse stale edits. The Kotlin commit-result check is intentional: the KTX edit
helper drops this result, so the narrow `UseKtx` lint suggestion is suppressed with that
reason. Actual storage failure on a handset still needs qualification.

Publishing is always explicit. A frozen payload and UUID are saved first; the relay's
`POST /atlas/spaces/drafts/{draft_id}/publish` transaction creates at most one space for that
workspace/draft. Ambiguous replies leave **Check publication** available after reopening.
No network worker publishes incidents automatically. Install the updated relay before
these clients; an older relay lacks this endpoint and the draft remains local.

Discarding requires confirmation and removes only the local draft. Previously shared
spaces/captures are untouched. Clearing app storage or uninstalling removes local drafts.


Use the repository's Node and pnpm versions, JDK 17 or newer, and Android SDK 35. Ensure Node and
pnpm are on the Gradle process's PATH (including when launching Android Studio).

## Build

From the repository root:

```sh
pnpm --dir console install --frozen-lockfile
pnpm --dir console build:android
```

Open `adapters/dji_mini3/pilot-app` in Android Studio, or run from that directory with the SDK
configured through `ANDROID_HOME` or `local.properties`:

```sh
./gradlew :app:assembleFakeDebug :app:assembleProbeDebug
./gradlew :app:testFakeDebugUnitTest :app:testProbeDebugUnitTest
./gradlew :app:lintFakeDebug :app:lintProbeDebug
python3 tools/check_apk_alignment.py app/build/outputs/apk/fake/debug/app-fake-debug.apk
python3 tools/check_apk_alignment.py app/build/outputs/apk/probe/debug/app-probe-debug.apk
```

Use **fakeDebug** for emulator/product testing; it does not include the DJI aircraft runtime.

Requested-view contributions carry an immutable request context through the source chooser,
native camera intent, document picker recreation, and durable upload metadata. The Uploads page
distinguishes a pending request link from an acknowledged saved link. A checksum-only response
is insufficient for a requested view: the exact location-cell or build/checksum/region binding
must also be acknowledged. Missing/mismatched acknowledgments retain the original for retry.
Imports still have unknown capture time and location; selecting a requested area never supplies
GPS evidence. No new Android permission or outbox schema migration is needed for this binding.

Do not substitute an emulator result for physical phone/camera or aircraft qualification.
In the app, choose **Connect**, then paste a scoped space invitation or enter your workspace
connection. A laptop's `127.0.0.1` is not reachable as that address from an ordinary handset;
use an explicitly configured development connection or the deployed HTTPS relay.

## Verified on 2026-09-08

- Both APK variants assemble with the new native capture and shared UI code. Fake passes 63 and
  probe passes 83 Android unit tests, each including 19 new Atlas tests. Those use Robolectric's Android APIs and
  native SQLite, actual private test files, and a loopback HTTP server—not real camera hardware.
- Covered: scoped endpoint admission; immutable credential binding; exact file/Unicode metadata
  upload; server checksum confirmation; temporary failure and byte-identical retry; revoked
  invitation; redirect refusal; corrupt local file; preserved originals; cold SQLite reopening;
  incomplete capture recovery; credential-isolated cache; stale/invalid GPS; provider ordering.
- Native-library ELF alignment and ZIP alignment pass at 16 KB: four libraries in fakeDebug,
  66 in probeDebug. Existing DJI resource/native-packaging warnings remain visible.
- Console: 81 files / 1,175 tests and full ESLint pass. Desktop and native production builds pass.
  Native initial JavaScript is approximately 242 KB uncompressed / 76 KB gzip; map (~1,029 KB)
  and 3D (~586 KB) are separate lazy chunks. Bundle-size warnings are not suppressed.
- Browser visual smoke at 390×844 loads the **built Android assets** under their shipped CSP,
  using a simulated native message port and real read-only local preview HTTP. It found and fixed
  content overflowing the bottom navigation. Document and navigation bottom both measure 844 px;
  the model canvas is 380×380. This is **not** an Android WebView/device acceptance test.
- Both Android lint variants pass with zero errors and 26 warnings each. Notification updates
  handle denied/revoked permission; Wi-Fi labels guard the API-29 transport-info accessor;
  raw-evidence export uses API-24-compatible path checks with symlink regression coverage.
  The probe USB activity explicitly preserves its existing exported setting. Backup exclusion
  is explicit for both older Android and Android 12+. No lint baseline or suppression was added.
- The cross-page [UI continuity audit](evidence/atlas-ui-continuity-2026-09-08.md) covers the
  existing fleet workspace as well as Spaces. Its in-memory upload states are visual fixtures,
  not evidence that a handset completed an upload.
- The preview's already-queued second Fountain job was processed after confirming no worker was
  live. Its actual model registered 11/11 views and 14,992 checked points. Authenticated artifact
  verification passed: 240,708 bytes, SHA-256
  `693794a56b6a805dcd4d9e7150d8374efbfe6123bc7362c828031a8bacd6c528`.
  It remains a sparse, relative-scale point cloud, not a dense or geographic reconstruction.

Local visual artifacts are ignored under `output/playwright/`. The read-only
visual fixture is local test setup, not a shipped fallback or simulated upload implementation.

### Shared dense-viewer follow-up

The bundled viewer now supports validated photo-textured meshes as well as sparse points, with
the same 16 MiB media-interceptor limit. A real 9.8 MB image-derived Fountain mesh renders under
the shipped Android asset origin/CSP in Chromium. Embedded textures require `blob:` in
`connect-src` as well as `img-src`; a regression fixes that and rejects a scene whose texture
decoding failed. Geometry, materials, textures, and shared decoded bitmaps are released on exit.
Phone-sized browser checks exercise keyboard view controls and 390×844 / 320×568 layouts.
The native message port is simulated; these checks do not establish Android runtime acceptance.
The engine runs off-device as an explicit local experiment and is not bundled into either APK.
See [the product ledger](atlas-product.md#experimental-photo-textured-surfaces) for its evidence
and unresolved licensing/production qualification.

### Actual Android emulator smoke — final handoff

The final `fakeDebug` APK ran on an isolated AOSP API 35 ARM64 emulator with
Android WebView 124. The bundled interface, Austin street map, encrypted workspace
connection, CameraX photo, private outbox and native HTTP upload were exercised.
After force-stop and an APK update, the saved workspace connection was retained.
Camera permission is now separate from optional location permission; granting the
camera reaches capture without a second GPS prompt.

A software-generated camera photo (41,576 bytes) was retained on the emulator and
saved by a disposable relay workspace. The two originals have the same SHA-256:
`f4daba271a37cef3ede88ed69757ce4891be4614f6f40ba861ba1bffe4b748ad`.
The Uploads page showed **Saved**, `source=camera`, `position=null`, and 0% GPS
coverage. No host webcam, microphone, physical phone or aircraft was used.
The SDK installed the AOSP default image without a new license prompt; the existing
license file was unchanged. The separately considered ARM ATD image was not used.

This is a narrow emulator result, not physical-device or complete background-work
qualification. See the [PR handoff](atlas-pr-handoff-2026-09-08.md).

## Still required

- Actual Android permission, CameraX photo/video/scan, Android Keystore, location-sharing lifecycle,
  document export, background WorkManager scheduling, force-stop/relaunch, and network-loss tests.
  No Android handset was connected. The charging iPhone is excluded. The emulator photo
  smoke above does not cover video, scan sequences, interrupted uploads, process death during
  capture, or physical sensors. JVM/browser tests do not satisfy these device requirements.
- Physical picker/provider import verification, durable new-space drafts, large-text/tablet/rotation refinements,
  credential-retention cleanup, and full multi-device invitation/revocation tests.
- Remaining lint warnings include locked orientation, target/dependency updates, existing
  wake-lock handling, and the deliberate JavaScript-enabled bundled WebView. A passing lint
  task is not proof that these follow-ups or physical-device qualification are complete.
- Production-qualified dense reconstruction, evidence-backed geographic alignment, reconstructed-surface gaps,
  targeted capture guidance, contributor notifications, and HTTPS deployment remain on the full
  [product completion ledger](atlas-product.md). This Android milestone does not complete the goal.

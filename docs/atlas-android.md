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

## Build

Use the repository's Node and pnpm versions, JDK 17 or newer, and Android SDK 35. Ensure Node and
pnpm are on the Gradle process's PATH (including when launching Android Studio).

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

## Still required

- Actual Android permission, CameraX photo/video/scan, Android Keystore, location-sharing lifecycle,
  document export, background WorkManager scheduling, force-stop/relaunch, and network-loss tests.
  No Android handset was connected. The charging iPhone is excluded. The host's Android emulator
  image requires a new Google ARM system-image license; it has not been accepted pending owner
  approval. JVM/browser tests do not satisfy these device requirements.
- Native photo-library import, durable new-space drafts, large-text/tablet/rotation refinements,
  credential-retention cleanup, and full multi-device invitation/revocation tests.
- Remaining lint warnings include locked orientation, target/dependency updates, existing
  wake-lock handling, and the deliberate JavaScript-enabled bundled WebView. A passing lint
  task is not proof that these follow-ups or physical-device qualification are complete.
- Production-qualified dense reconstruction, evidence-backed geographic alignment, reconstructed-surface gaps,
  targeted capture guidance, contributor notifications, and HTTPS deployment remain on the full
  [product completion ledger](atlas-product.md). This Android milestone does not complete the goal.

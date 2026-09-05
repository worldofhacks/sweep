# DJI Mini 3 bridge

Capability area: Autonomy, with Platform support. Issue #43 (M1.9), Phases B3 and B4.

One Android phone per DJI Mini 3 and RC-N1 pair runs the pilot app under `pilot-app/`. The
app registers with the DJI Mobile SDK, proves the aircraft identity, and (from Phase C on)
keeps one authenticated WebSocket to the relay as a foreground service. Everything that does
not need the SDK lives in pure Kotlin/JVM modules so it runs in CI without an Android SDK.

Seeded from the connection example at techmexdev/drone-maps: the MSDK Gradle and manifest
wiring, the `fake` versus `probe` flavor pattern, `Helper.install`, the `SDKManagerCallback`
skeleton, and the `KeyProductType` / `KeyRcFirmwareInfo` identity check, each attributed in
the file header. Its indoor-capture domain, Room database, placeholder camera, WorkManager
dependency, and dangling `@xml/accessory_filter` reference were not carried over.

`hardware-profile.json` pins the one node this bridge is proven on; `aircraft_firmware` and
`rc_firmware` stay `null` until the probe report reads them from the connected product.

## Modules

| Module | Kind | Contents |
|---|---|---|
| `bridge-core` | Kotlin/JVM | Frame models mirroring `relay/contracts.py` plus the Phase A `command`, `capabilities`, `capture_readiness`, and `node_status` frames; canonical JSON that byte-matches `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`; HMAC-SHA256 signing; command admission (signature, drone, epoch, roster, monotonic `seq`, `issued_at + ttl_ms` against a measured clock offset); the watchdog state machine (`armed`, `hold`, `failsafe`); the H.264/H.265 SPS parser for codec evidence |
| `bench` | Kotlin/JVM | JSONL recorder for command round-trip time, jitter, drops, stick send rate, telemetry rate, and video frame stats, plus the report writer |
| `app` | Android | `SdkSession` (probe) and `FakeAircraftSession` (fake) behind one `AircraftSession` interface, the Compose registration and identity screen, the probe-report export, the foreground `BridgeService` declaration |

The JVM tests read fixtures under `bridge-core/src/test/resources/vectors/` that
`adapters/dji_mini3/vectors.py` generates from the relay code itself; `test_vectors.py`
fails the Python suite whenever those files drift. Refresh them with
`uv run python -m adapters.dji_mini3.vectors`.

## Build

Requirements on the build machine: Android Studio 2026.1 (its bundled JDK is what the
Gradle wrapper runs on), the Android SDK with platform 35 or newer, build tools 36, and
network access for the Gradle distribution, AndroidX, and the DJI artifacts on Maven Central.

```sh
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
cd adapters/dji_mini3/pilot-app
printf 'sdk.dir=%s/Library/Android/sdk\n' "$HOME" > local.properties   # gitignored
./gradlew :bridge-core:test :bench:test        # what CI runs; no Android SDK needed
./gradlew :app:assembleFakeDebug               # any phone, no DJI dependency
./gradlew :app:assembleProbeDebug              # DJI MSDK 5.18.0, arm64-v8a only
```

`settings.gradle.kts` only includes `:app` when an SDK location resolves from
`local.properties`, `ANDROID_HOME`, or `ANDROID_SDK_ROOT`, so the JVM tasks work on a bare JDK.

### DJI key

Registration needs an app key from the DJI developer portal whose package name is exactly
`org.worldofhacks.sweep.bridge` (the `applicationId`; DJI rejects registration when the two
differ). Put it in `~/.gradle/gradle.properties`, never in the repo:

```
DJI_API_KEY=your-key
```

The build resolves the key from that Gradle property, then from the `DJI_APP_KEY`
environment variable, else it stays empty so the probe flavor still assembles without one;
registration then fails on the phone with a DJI error naming the missing key.

### Flavors

- `fake`: no DJI dependency. The launcher shows the same registration and identity screen
  driven by a simulated session with buttons for register (success or failure), connect,
  disconnect, and a late callback that must be dropped by the generation fence.
- `probe`: `dji-sdk-v5-aircraft` (implementation), `dji-sdk-v5-aircraft-provided`
  (compileOnly), `dji-sdk-v5-networkImp` (runtimeOnly). `Helper.install` runs in
  `Application.attachBaseContext`, `SDKManager.init` starts in `SdkSession`, and
  `registerApp()` is called on `INITIALIZE_COMPLETE`. Plugging in the RC-N1 launches the app
  through the USB accessory filter in `app/src/main/res/xml/accessory_filter.xml`.

## Phase B4 exit on the phone

Install `app-probe-debug.apk` on the pinned phone, then:

1. With network, the screen shows `Registration: REGISTERED`; a failure shows `FAILED` with
   the DJI error text.
2. Kill and relaunch the app with network off: registration still reaches `REGISTERED` from
   the SDK's cached result.
3. Plug the RC-N1 into the phone: the app launches by itself (USB attach), `Product:
   CONNECTED` appears with the product id, and the connection generation increments.
4. Power the aircraft: the identity card shows `DJI_MINI_3` confirmed, the aircraft firmware
   string, the RC firmware profile (`DJI_MINI_3`) and its version strings.
5. Unplug and replug: the generation increments each time, the identity clears and returns,
   and `dropped late callbacks` stays at 0 unless a callback really did arrive late.
6. Tap `Export probe report`; the file lands under the app's `filesDir/probe-reports/`. Copy
   it with `adb` and record the firmware strings into `hardware-profile.json`.

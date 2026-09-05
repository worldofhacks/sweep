# DJI Mini 3 bridge

Capability area: Autonomy, with Platform support. Issue #43 (M1.9), Phases B3, B4, and C.

One Android phone per DJI Mini 3 and RC-N1 pair runs the pilot app under `pilot-app/`. The
app registers with the DJI Mobile SDK, proves the aircraft identity, and keeps one
authenticated WebSocket to the relay as a foreground service, speaking the node protocol in
`relay/README.md`. Everything that does not need the SDK lives in pure Kotlin/JVM modules so
it runs in CI without an Android SDK.

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
| `bridge-core` | Kotlin/JVM | Frame models mirroring `relay/contracts.py`: signed membership, telemetry, acknowledgement, the relay-signed `command` (integer-only `args`), `capabilities`, `capture_readiness`, `node_status`, `auth.accepted` with its `node` thresholds, and the relay-authored `membership`, `state`, and `refusal` events; canonical JSON that byte-matches `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`; HMAC-SHA256 signing; command admission (signature, drone, epoch, roster, monotonic `seq`, `issued_at + ttl_ms` against a measured clock offset); the watchdog state machine (`armed`, `hold`, `failsafe`; `nominal` on the wire); the H.264/H.265 SPS parser for codec evidence |
| `bridge-node` | Kotlin/JVM | `RelayLink`, the node's OkHttp WebSocket client: auth, join, readiness, telemetry at 10 Hz, capabilities, node_status, command admission and acknowledgement, reconnect with bounded backoff, the watchdog under relay silence; `FakeAircraft`, the kinematic fixture with the command semantics of `fake_node.py`; tested against a stub relay on MockWebServer |
| `bench` | Kotlin/JVM | JSONL recorder for command round-trip time, jitter, drops, stick send rate, telemetry rate, and video frame stats, plus the report writer |
| `app` | Android | `SdkSession` (probe, with `ProbeAircraft` reading `KeyManager` telemetry and measuring per-key rates) and `FakeAircraftSession` (fake) behind one `AircraftSession` interface; the foreground `BridgeService` that owns the link; `BridgeSetupStore` on `EncryptedSharedPreferences`; the Compose page with Setup, Connectivity, Readiness, node status, the command log, and the Phase B4 registration and identity cards |

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
./gradlew :bridge-core:test :bench:test :bridge-node:test   # what CI runs; no Android SDK needed
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

- `fake`: no DJI dependency. The launcher shows the same page driven by a simulated session
  with buttons for register (success or failure), connect, disconnect, and a late callback
  that must be dropped by the generation fence. Its aircraft is `FakeAircraft`, a kinematic
  fixture with the command semantics of `fake_node.py`; Connect and Disconnect also stand in
  for the aircraft and RC link, so the disconnect semantics below can be shown without
  hardware. Setup values may arrive as launch extras (fake flavor only, see below).
- `probe`: `dji-sdk-v5-aircraft` (implementation), `dji-sdk-v5-aircraft-provided`
  (compileOnly), `dji-sdk-v5-networkImp` (runtimeOnly). `Helper.install` runs in
  `Application.attachBaseContext`, `SDKManager.init` starts in `SdkSession`, and
  `registerApp()` is called on `INITIALIZE_COMPLETE`. Plugging in the RC-N1 launches the app
  through the USB accessory filter in `app/src/main/res/xml/accessory_filter.xml`.

## Phase C bring-up: the node in the relay

Phase C makes the phone visible in the relay as a live aircraft node. The relay side is PR
#108 (`relay/README.md`, "Node protocol"); the node mirrors `adapters/dji_mini3/fake_node.py`
frame for frame: `auth`, then the signed `join`, then, once the relay answers with the node's
connection epoch, one telemetry frame, the signed `readiness`, `capabilities`, and
`node_status`, then telemetry at 10 Hz. Commands are verified against the node key and
admitted (epoch, roster, monotonic `seq`, `issued_at + ttl_ms` on the relay clock);
`accepted` is acknowledged on admission, then `executing` and `completed` (fake flavor) or
`failed` with `control_loop_unavailable` (probe flavor, until Phase E). Rejections use
`stale_command` or `out_of_order_command`; a frame whose signature does not verify is dropped
without a reply.

### Relay

Run the relay from a checkout of PR #108 with the remote adapter backend and a per-drone key
(32 characters or more; `openssl rand -hex 32`). The per-drone key is both the node's auth
token and its HMAC signing key:

```sh
export SWEEP_RELAY_TOKEN=<console token, 32+ characters>
export SWEEP_ADAPTER_KEYS_JSON='{"1":"<drone 1 key>","2":"<drone 2 key>"}'
export SWEEP_ADAPTER_BACKEND=remote
export SWEEP_SESSION_LOG_DIR=.sweep/session-logs
uv run uvicorn relay.app:app --host 127.0.0.1 --port 8000
```

The relay's thresholds (`SWEEP_COMMAND_TTL_MS`, `SWEEP_VIRTUAL_STICK_HZ`,
`SWEEP_NODE_WATCHDOG_HOLD_MS`, `SWEEP_NODE_WATCHDOG_FAILSAFE_MS`) reach the phone in
`auth.accepted`; the node configures its watchdog and command admission from them and never
invents its own. Dispatching a command to the node needs the relay's autonomy composition
(`relay.bridge.build_dispatcher` on the `remote` backend, as
`relay/tests/test_bridge_roundtrip.py` does in-process); `just fake-node drone_id=2`
connects the Python fake node beside the phone for a side-by-side comparison in the same
audit log.

### Phone over USB

No Wi-Fi is needed: `adb reverse` makes the Mac's relay reachable from the phone at
`127.0.0.1:8000`.

```sh
adb reverse tcp:8000 tcp:8000
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
cd adapters/dji_mini3/pilot-app
./gradlew :app:installFakeDebug
adb shell am start -n org.worldofhacks.sweep.bridge/.MainActivity
```

On the Setup card enter the relay URL (`ws://127.0.0.1:8000` over the tunnel, or the Mac's
LAN address), the session id, the aircraft number (1 to 4), and the node token once; the
token is stored in `EncryptedSharedPreferences`, never logged, never placed in a URL, and
never shown again in full (Replace token overwrites it). Save and connect starts the
foreground service, which owns the socket. A stored setup reconnects by itself on the next
launch. The fake flavor also accepts the values as launch extras so a bench run can be
scripted; the values go straight into the encrypted store:

```sh
adb shell am start -n org.worldofhacks.sweep.bridge/.MainActivity \
  --es relay_url ws://127.0.0.1:8000 --es session bench --ei drone_id 1 --es token "$DRONE1_KEY"
```

### What to look for

On the phone:

- Connectivity: `Relay: connected, authenticated`, the relay thresholds, the measured clock
  offset, `Membership: registered` then `ready` with the connection epoch, frames in and out,
  and the telemetry rate near 10.0 Hz once the fake aircraft is connected (Connect button) or
  the real aircraft is powered.
- Readiness: the three toggles. Each sends a signed readiness frame with the current epoch;
  the card shows the relay's answer and its `readiness_reasons` until all gates pass.
- Node status: the watchdog word (`nominal`, `hold`, `failsafe`), the last `node_status`
  body, and the aircraft and RC connection. Disconnect (fake) or unplugging the RC (probe)
  sends readiness with `control_authority=false` plus a `node_status` while the socket stays
  up; Connect or a replug recovers without an epoch change.
- Commands: every command this epoch with its outcome and reason.

In the relay's audit JSONL (`SWEEP_SESSION_LOG_DIR/<sha256 of the session id>.jsonl`):
`membership` with `action: "join"` and `connection_epoch: 1`, `telemetry` at 10 Hz,
`membership` with `action: "readiness"` and `membership: "ready"`, `capabilities` with the
hardware profile, `node_status`, then `command` records (without signatures) followed by the
node's `acknowledgement` records `accepted`, `executing`, `completed`. Killing the node's
socket (Disconnect on the Setup card, `adb shell am force-stop`, or a USB unplug) logs an
`unexpected_loss` with `provenance: "relay_transport_attestation"`; the rejoin logs
`authenticated_rejoin` with `connection_epoch: 2`.

A relay process restart closes the session id: the phone shows `Auth refused: session_closed`
on the Connectivity card and stops reconnecting until a new session id is saved, which the
relay then serves from epoch 1. Relay silence with the socket up (or the socket down) drives
the watchdog to `hold` after `watchdog_hold_ms` and `failsafe` after `watchdog_failsafe_ms`,
visible in `node_status` and on the phone; the flight action for those states is Phase E
(neutral sticks and hover at hold, land indoors at failsafe, never return to home).

### Probe flavor

`./gradlew :app:installProbeDebug` builds with the DJI MSDK, but registration needs a DJI
developer key for the package `org.worldofhacks.sweep.bridge` in `~/.gradle/gradle.properties`
as `DJI_API_KEY`; without it the build assembles and the app reports the DJI registration
failure. Once registered, `ProbeAircraft` listens to `KeyAircraftLocation3D`,
`KeyAircraftVelocity`, `KeyAircraftAttitude`, `KeyAltitude`, `KeyUltrasonicHeight`,
`KeyFlightMode`, `KeyAreMotorsOn`, `KeyIsFlying`, `KeyConnection` (aircraft and RC),
`KeyChargeRemainingInPercent`, and `KeySignalQuality`, assembles Telemetry v1 from the latest
values, and shows each key's measured update rate on the node status card. The `x`, `y`,
`pos_quality`, and flight-state mappings are provisional until measured with the aircraft;
the probe flavor acknowledges every command `failed` with `control_loop_unavailable` until
the Phase E Virtual Stick loop lands.

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

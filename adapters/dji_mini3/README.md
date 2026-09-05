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

## Phase D bring-up: local FPV and codec evidence

Phase D puts the aircraft's live feed on the phone under the `visual_advisory` overlay and
records what the stream really is: mime type, picture size, nominal and measured frame
rate, and keyframe cadence from `ICameraStreamManager.addReceiveStreamListener`, plus the
profile and level parsed from the SPS with `bridge-core`'s `SpsParser`. The evidence goes to
the screen and the bench log only; `node_status` is unchanged and carries no codec fields.
#51 reads the codec and profile from here before choosing its publish path.

Where the code lives: `bridge-core` `core/video/` (`StreamMonitor`, `KeyframeCadence`,
`FlightOverlay`; pure Kotlin, run by the CI job), `bench` (`stream_info` records and their
report lines), and the app package `org.worldofhacks.sweep.bridge.video` (`FpvSession` and
`FpvSessionHost` hooks, `FpvSurface`, `FpvOverlay`, `FlightDisplayScreen`,
`StreamEvidenceCard`, `StreamEvidenceTracker`; `FakeFpv` in the fake flavor, `DjiFpv` in
the probe flavor). `SdkSession` and `FakeAircraftSession` expose it through
`FpvSessionHost`; `SessionScreen` routes to the display.

### On the phone

Open the Flight display from the button at the top of the session page or on the Camera
stream card. The relay link keeps running in the foreground service whether or not the
display is open; leaving the display (Session button or back) releases the Surface.

- Flight display with the aircraft powered (probe) or after Connect (fake):
  - the live picture fills the screen (fake: a synthetic scene whose wall markers scroll
    with a slow yaw sweep, a frame counter, and an amber square on every keyframe);
  - the center reticle; around it the coverage compass drawn heading-up: eight 45° sectors
    while `measured_hfov_deg` is null (the label says "field of view unmeasured"; the
    published 82.1° lens value is never used as a horizontal field of view), all hollow
    (unseen) until Phase G marks them dashed (weak) or solid (accepted);
  - the amber next-heading marker on the ring and the `yaw +n°` / `yaw −n°` label under
    the reticle; the first heading is the sector the aircraft already looks at, so the
    delta starts small and follows the yaw;
  - bottom scrim: the capture pill `Ready`, `visual_advisory`, `operator_approved`,
    `clearance: pilot approved`, `next n°`, the sector rule, and the stream line
    (`1280×720 29.9 Hz Main 3.1`) once the SPS has been read;
  - top scrim: `Authority Sweep` or `Authority RC (reason)`, `Video live`, and
    `Physical RC remains primary`; the fake flavor adds its banner.
- Disconnect (fake) or power the aircraft off / unplug the RC (probe): the pill reads
  `Disconnected`, the top scrim lists `Aircraft disconnected`, `RC disconnected`, and after
  a second `No video for n s`; the SDK events (probe) show `Camera stream · surface
  released: aircraft disconnected`. Reconnect: the picture and `Ready` return, the evidence
  counters restart from zero, and the yaw follows `KeyAircraftAttitude` again.
- Stop the relay: `Relay disconnected` joins the top scrim and, past the relay thresholds,
  `Watchdog hold: neutral sticks and hover` then `Watchdog failsafe: land indoors, never
  return home`; the pill stays `Ready` because the aircraft is still there.
- Session page, Camera stream card: the same evidence as text with the SPS profile, level,
  and tier, the keyframe cadence in milliseconds and frames, the yaw, and the bench log
  path. This is the codec evidence to quote on #51.

`Capturing`, `Downloading`, and `Needs retake` are driven by `CaptureProgressSource`, which
Phase G fills; Phase D ships the idle source, so the fake flavor shows `Ready` and
`Disconnected`. Arrows on the overlay mean yaw or gimbal only; nothing suggests a
translation in `visual_advisory`.

### What the bench log records

Attaching the Surface opens `filesDir/bench/stream-<t_ms>.jsonl` (the path is on the
Camera stream card; copy it with `adb shell run-as org.worldofhacks.sweep.bridge cat
files/bench/<name> > <name>`). Detaching closes it. Records:

- `video_frame`, one per received encoded frame: `size_bytes` and `keyframe`; `decode_ms`
  stays `null` and `dropped` `false` because the Surface path decodes inside the SDK and
  exposes neither.
- `stream_info`, once per second and on every descriptor or SPS change: `mime_type`,
  `codec`, `width`, `height`, `nominal_frame_rate_hz`, `measured_frame_rate_hz` (over the
  last 5 s of arrivals), `frames`, `keyframes`, `keyframe_interval_ms` with its `_min_` and
  `_max_` over the last eight groups, `keyframe_interval_frames` (the GOP length),
  `profile`, `profile_idc`, `level`, `level_idc`, `tier`, `sps_error`, `bytes`,
  `phone_battery_percent`, `phone_thermal_state`.
- `note` at open and close and on aircraft connect or disconnect.

`BenchAnalysis` folds a log into the report's `video` block (frames, keyframes, and the
rate from `video_frame` timestamps) and `video.stream` (the last `stream_info` with the
sample count); `ReportWriter.text` prints them as `stream_*` lines. The last line of
evidence is also just `grep stream_info <log> | tail -1`.

### Reading the codec evidence

- `mime_type` `video/avc` is H.264, `video/hevc` is H.265. `profile` and `level` come from
  the SPS, not from the SDK: Constrained Baseline, Main, and High are what browser WebRTC
  decodes; High 10 or H.265 means #51 needs the SDK's other stream mode or a ground-side
  transcode before WHIP.
- `nominal_frame_rate_hz` is what the SDK claims; `measured_frame_rate_hz` is what arrived.
  Report both; a persistent gap is a downlink or phone problem, not a codec property.
- `keyframe_interval_ms` and `keyframe_interval_frames` bound how long a WHEP viewer waits
  to start after joining; 1000 ms / 30 frames is the usual shape at 720p30.
- `sps_error` set while `profile` is null means no parameter set was found in the first
  16 KiB of a keyframe; quote the message in the PR, the search bound is then the fix.
- `phone_thermal_state` next to `measured_frame_rate_hz` over a five-minute run is the
  thermal evidence Phase D's exit asks for, and the headroom #51 has for publishing.

### Phase D exit checklist (aircraft powered)

1. Probe flavor registered, aircraft and RC connected, Flight display open: the picture is
   live, the top scrim says `Video live`, `Authority` matches the Readiness toggle, and the
   yaw on the Camera stream card follows the aircraft when it is turned by hand.
2. Five minutes on the bench with the display open; Connectivity keeps `telemetry` near
   10.0 Hz and the relay `connected` throughout.
3. Power the aircraft off and on: `Disconnected` then `Ready`; `surface released` and
   `surface attached` appear in the SDK events, and `dropped late callbacks` stays at 0.
4. Leave the display (Session) and reopen it: a new bench log opens and the old one ends
   with its closing `note`.
5. Pull the log and record in the PR the last `stream_info`: mime type, size, nominal and
   measured frame rate, keyframe interval, profile and level, and the thermal state at the
   end of the run.

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

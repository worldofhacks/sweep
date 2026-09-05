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
./gradlew :bridge-core:test :bench:test :bridge-node:test :bridge-publish:test   # what CI runs; no Android SDK needed
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

## Phase F: the aircraft's video into MediaMTX over WHIP

Phase F publishes the live feed from the phone into the ground station's MediaMTX over WHIP,
where the console plays it over WHEP at `http://<ground-station>:8889/drone{id}/whep`
(`console/src/media/playback.ts` derives the name `drone{droneId}`). The direction is the
prior-art directive on issue #51: WHIP first, the publisher vendored from WildBridge rather
than written fresh, no RTMP or RTSP path.

The pure-JVM half is the `bridge-publish` module: `WhipClient` (POST the SDP offer, 201 with
`Location`, DELETE on stop), `SdpMunger`, `CodecGate`, `PublishStateMachine`,
`PublishMetricsAggregator`, and `WhipEndpoint`, tested against MockWebServer and run by CI
with the other JVM modules. The libwebrtc side, the frame sources, and the screen are the
app's `publish` package.

### What is vendored and what changed

From WildDrone/WildBridge (MIT, `android-sdk-v5-sample/.../webrtc/`), each file keeping its
MIT header plus a `Vendored from WildDrone/WildBridge (MIT)` line, under
`org.worldofhacks.sweep.bridge.publish.webrtc` (or `bridge-publish` when pure Kotlin):

| File | Where | Changes |
|---|---|---|
| `WhipPublisher.kt` | app main | HTTP POST/DELETE moved to `WhipClient` (OkHttp bound to the Wi-Fi network); retry delay and the decision to retry come from the publish state machine; no STUN server (LAN host candidates; an unreachable STUN on an internet-less AP stalls ICE gathering); the first-frame wait wraps the `CapturerObserver` so any capturer works; the source's encoder factory builds the peer connection factory; ICE `FAILED`, `DISCONNECTED` past a grace period, and a connect timeout end the attempt with a reason; sender bitrate floor, `DegradationPreference.DISABLED`, and `setBitrate` for passthrough; the resolution and frame-rate switching is not carried over |
| `WebRTCPeerFactory.kt` | app main | one global `initialize` with `WebRTC-H264HighProfile/Enabled/WebRTC-FrameDropper/Disabled/`; one factory per session with the source's encoder factory; the encoder list is logged |
| `SdpUtils.kt` | `bridge-publish` as `SdpMunger` | Android logging replaced by an injectable sink; `videoCodecs` and `negotiatedVideoCodec` added |
| `WebRTCStreamMetrics.kt` | `bridge-publish` | package only |
| `WebRTCMediaOptions.kt` | app main | default 1280x720 at 30 fps and 4 Mbps (the Mini 3 live view); the fleet bitrate ceilings removed |
| `SimpleSdpObserver.kt` | app main | `onSetSuccess` also reports success so one observer covers create and set |
| `SharedDJIFrameSource.kt` | app probe | the explicit re-encode source only; telemetry metadata and the Matrice 400 payload-port logic removed; native resolution by default; a log sink added |
| `SharedVideoCapturerHandle.kt` | app probe | metadata listener removed |

Not vendored: `DJIV5VideoCapturer.kt` (the shared source covers it), `MockMp4VideoCapturer.kt`
(the fake flavor generates a test pattern instead), `WebRTCStreamer.kt`, `TelemetryProvider.kt`,
`FrameMetadata.kt`.

### Sources and the codec gate

WildBridge publishes the SDK's *decoded* NV21 frames re-encoded by the phone. Sweep's default
is the SDK's *encoded* access units (`ICameraStreamManager.addReceiveStreamListener`, MSDK
5.8.0+) handed to WebRTC's H.264 packetizer unchanged, which is the low-latency path: no
decode, no encode, no thermal load. libwebrtc's Android API has no injection point for
pre-encoded frames, so `Passthrough.kt` pushes one placeholder I420 frame per access unit
whose buffer carries the unit, and a passthrough `VideoEncoder` emits that unit as the
`EncodedImage`. Keyframes cannot be requested from the aircraft; delta units are skipped
until the next SDK keyframe after a start or a PLI, and the skips count as dropped frames.

The gate decides once per stream from the first keyframes: the SPS gives profile and level
(`bridge-core`'s `SpsParser`), one GOP of delta frames is scanned for B slices, and the
keyframe cadence is measured. H.264 baseline (incl. constrained), main, or high without B
slices passes; H.265, High 10, 4:2:2, or B slices fail with `codec_unsupported` and the
profile in the log and on screen. Nothing is transcoded silently: `Re-encode on the phone`
is a separate source the pilot selects on the Connectivity card, labelled as adding latency.

Before publish can work with the aircraft, Phase D's codec evidence must show: mime type
`H264`; profile Baseline, Main, or High (`profile_idc` 66, 77, or 100); no B slices; and the
keyframe interval (the console freezes for one interval after every viewer join or packet
loss). An H.265 live view means either selecting H.264 in the aircraft's video settings, if
this firmware offers it, or the explicit re-encode source.

### Ground station

MediaMTX 1.20.1 (`media/mediamtx.yml`, `webrtc: yes` on 8889, no authentication on
`all_others`) accepts the four paths `drone1` to `drone4` without per-path configuration.
The one change is `docker-compose.yml` passing `MTX_WEBRTCADDITIONALHOSTS` from
`SWEEP_MEDIA_HOST`: inside Docker MediaMTX advertises its container address as its ICE
candidate, which a phone cannot reach, so the ground station's LAN IP has to be advertised
too. Set it and recreate the container:

```sh
export SWEEP_MEDIA_HOST=10.10.1.60      # this Mac on the flight-room LAN; or put it in .env
docker compose up -d mediamtx
docker compose logs mediamtx | grep WebRTC
# [WebRTC] started with listeners on :8889 (TCP/HTTP), :8189 (UDP/ICE)
```

### On the phone

Build and install with the commands under Build. The fake flavor proves the WHIP path
without an aircraft or a relay; the probe flavor publishes the aircraft.

1. Setup card: the ground-station host (blank means the relay host) and WebRTC port beside
   the relay URL; the card shows the derived `http://<host>:8889/drone<n>/whip`. Leave
   `Publish video automatically` on for the aircraft; the relay link must be joined and the
   aircraft connected for the automatic start, and either loss stops the session.
2. Fake flavor: on the Connectivity card press `Start publish` (no relay needed). The
   publish line walks `connecting` then `publishing` with the negotiated codec: `H264
   (phone encoder)` when libwebrtc offers the phone's hardware H.264 encoder (the pinned
   Seeker offers `H264 (42e01f)`), otherwise `VP8 (no H.264 encoder on this phone)`, which
   the console decodes too. Then the one-second metrics line: bitrate, frame rate, 1280x720,
   dropped frames, RTT, ICE state (the Seeker against this Mac: 30 fps, RTT 5 to 11 ms, about
   9 ms of processing per frame, no drops). Scripted, with the screen off: `adb shell am start -n
   org.worldofhacks.sweep.bridge/.MainActivity --es publish_host 10.10.1.60 --ei publish_port
   8889 --es publish start` (`--es publish stop` ends the session, `auto` returns to the
   automatic policy); `adb logcat -s SweepPublish WhipPublisher` shows the same lines as the card.
3. `docker compose logs -f mediamtx` on the ground station shows, in order:
   `[WebRTC] [session ...] created by <phone ip>:<port>` (Docker Desktop on a Mac shows its
   gateway, `192.168.65.1`, instead of the phone), `peer connection established, local
   candidate: host/udp/..., remote candidate: prflx/udp/...`, `[path drone1] stream is
   available and online, 1 track (H264)` (or VP8), `[WebRTC] [session ...] is publishing to
   path 'drone1'`. A session that is created and then `closed: deadline exceeded while
   waiting connection` means the phone could not reach the advertised candidates: the phone
   logs `publish failed: ice_failed (no ICE connection within 10 s ...)` and retries after 1,
   2, 4, 8, 16, then 30 s; set `SWEEP_MEDIA_HOST`, recreate the container, and the next
   attempt connects. The `DELETE 404` on those failed attempts is MediaMTX declining to
   delete a session that never connected.
4. First look: `http://<ground-station>:8889/drone1` in any browser is MediaMTX's built-in
   WHEP page; the test pattern's clock and sweeping block make a frozen or late feed obvious
   (the clock against the browser machine's clock is the glass-to-glass estimate). Then the
   console's Live module, which reads `<webrtcOrigin>/drone1/whep`.
5. Probe flavor with the aircraft powered: the publish log shows `listening to the
   LEFT_OR_MAIN encoded stream`, then `codec gate: H264 High 4.0 1280x720 30fps
   keyframe/<n>ms` (the Phase D evidence, also in the bench log), then `publishing`. A
   `Publish failed: codec_unsupported (...)` line names the profile; it does not retry until
   the source or the aircraft changes.
6. `Stop publish` logs `WHIP resource released (DELETE 200)` and MediaMTX logs the session
   closed and `[path drone1] destroyed`. A relay drop, ICE loss, or HTTP error shows
   `failed` with the reason and `Next publish attempt in <n> s` (1 s doubling to 30 s).
7. Node status: `Last node_status: ... video publishing` mirrors the relay's
   `video_publish_state`; the relay's audit JSONL carries the same word.
8. Bench log: the card names `files/bench/publish-drone<n>-<stamp>.jsonl`; copy it with
   `adb shell run-as org.worldofhacks.sweep.bridge cat files/bench/<name> > publish.jsonl`.
   One `video_publish` record per second: `bitrate_kbps`, `fps`, `frames_sent`,
   `dropped_frames`, `ice_state`, `rtt_ms` (the LAN leg, from the selected ICE pair),
   `processing_ms` (the Android leg: queueing for passthrough, NV21 scale plus encode for
   re-encode), `codec`, `width`, `height`, `keyframe_interval_ms`. The aircraft-to-controller
   leg is not observable on the phone; the glass-to-glass measurement (a clock in front of
   the camera against the console) minus the two logged legs gives it.

The console's Live player mounts only once the relay reports per-drone `video` state, which
is PR #68's relay-side work; until that lands, MediaMTX's own page is the viewer.

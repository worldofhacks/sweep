# DJI Mini 3 bridge

Capability area: Autonomy, with Platform support. Issue #43 (M1.9), Phases B3, B4, C, and E.

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
| `bridge-core` | Kotlin/JVM | Frame models mirroring `relay/contracts.py`: signed membership, telemetry, acknowledgement, the relay-signed `command` (integer-only `args`), `capabilities`, `capture_readiness`, `node_status`, `auth.accepted` with its `node` thresholds, and the relay-authored `membership`, `state`, and `refusal` events; canonical JSON that byte-matches `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`; HMAC-SHA256 signing; command admission (signature, drone, epoch, roster, monotonic `seq`, `issued_at + ttl_ms` against a measured clock offset); the watchdog state machine (`armed`, `hold`, `failsafe`; `nominal` on the wire); the H.264/H.265 SPS parser for codec evidence; `core/flight`, the Phase E Virtual Stick loop as a pure tick-driven state machine (axis mapping, time-boxed steps from millimetre arguments, deadman actions, estop and takeover latches), the kinematic `FakeFlightModel`, and the #85 axis-probe classifier |
| `bridge-node` | Kotlin/JVM | `RelayLink`, the node's OkHttp WebSocket client: auth, join, readiness, telemetry at 10 Hz, capabilities, node_status, command admission and acknowledgement, reconnect with bounded backoff, the watchdog under relay silence; `FakeAircraft`, the kinematic fixture with the command semantics of `fake_node.py`; `flight/FlightExecutor`, the loop's coroutine ticker and command executor, and `FakeFlightAircraft`, the fixture flown by the loop; tested against a stub relay on MockWebServer |
| `bench` | Kotlin/JVM | JSONL recorder for command round-trip time, jitter, drops, stick send rate, telemetry rate, video frame stats, and the #85 `probe` entries, plus the report writer |
| `app` | Android | `SdkSession` (probe, with `ProbeAircraft` reading `KeyManager` telemetry and measuring per-key rates, and `DjiFlightPort` on `IVirtualStickManager`) and `FakeAircraftSession` (fake) behind one `AircraftSession` interface; the foreground `BridgeService` that owns the link; `BridgeSetupStore` on `EncryptedSharedPreferences`; the Compose page with Setup, Connectivity, Readiness, node status, the Flight and First-flight probes cards, the command log, and the Phase B4 registration and identity cards |

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
`accepted` is acknowledged on admission, then `executing` and `completed` or `failed` from
the Phase E flight loop (both flavors; see below). Rejections use `stale_command` or
`out_of_order_command`; a frame whose signature does not verify is dropped without a reply.

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
visible in `node_status` and on the phone; the flight loop acts on the same clock (neutral
sticks at hold, land indoors at failsafe, never return to home; see Phase E).

### Probe flavor

`./gradlew :app:installProbeDebug` builds with the DJI MSDK, but registration needs a DJI
developer key for the package `org.worldofhacks.sweep.bridge` in `~/.gradle/gradle.properties`
as `DJI_API_KEY`; without it the build assembles and the app reports the DJI registration
failure. Once registered, `ProbeAircraft` listens to `KeyAircraftLocation3D`,
`KeyAircraftVelocity`, `KeyAircraftAttitude`, `KeyAltitude`, `KeyUltrasonicHeight`,
`KeyFlightMode`, `KeyAreMotorsOn`, `KeyIsFlying`, `KeyConnection` (aircraft and RC),
`KeyChargeRemainingInPercent`, and `KeySignalQuality`, assembles Telemetry v1 from the latest
values, and shows each key's measured update rate on the node status card. The `x`, `y`,
`pos_quality`, and flight-state mappings are provisional until measured with the aircraft.
`KeyAircraftVelocity` is N-E-D, so its `y` is the planner's `vx` (east) and its `x` the
planner's `vy` (north); `KeyAircraftAttitude.yaw` (degrees, 0 north, clockwise) is the
heading the flight loop rotates body-frame steps with. Flight commands run in the Phase E
loop below; the camera and media commands acknowledge `failed` with `unsupported` until
Phase G.

The listeners are registered when the SDK registers, before the RC and aircraft are
connected, and `isKeySupported` is not allowed to skip a key: its answer is per connected
product and is usually `false` at that moment, so it is recorded, asked again on every
product connect, and shown on the Node status card as `Telemetry keys` (`yes` or `no` at
registration and on connect, then the age of the key's first value) beside the measured
key rates. The same evidence is in the SDK events (`Telemetry keys`, `Telemetry key`), so
in the exported probe report, and in `filesDir/bench/telemetry-keys-<stamp>.jsonl` as
`telemetry_key` records (`event` is `attached`, `product_connected`, or `first_value`;
pull it like the Phase D logs; `BenchAnalysis` lists the first values under `notes`). An
aircraft power cycle asks support again without registering a second listener: every
listener shares one holder object and is cancelled by it.

## Phase E: Virtual Stick loop, deadman, and RC takeover

Phase E connects admitted commands to the aircraft. The loop lives in
`bridge-core/.../core/flight` as a pure state machine (`FlightController`) driven by
`bridge-node/.../flight/FlightExecutor`, a dedicated single-threaded coroutine ticker at the
relay's `virtual_stick_hz` (clamped to DJI's 5 to 25 Hz, drift-free; it survives any failure
of one tick, and if it is ever stopped it sends neutral sticks and disables Virtual Stick on
its way out). The probe flavor's
port is `DjiFlightPort` on `IVirtualStickManager` (advanced mode, `VirtualStickFlightControlParam`
in velocity mode with the BODY coordinate system, yaw angle mode for `rotate_to`) plus the
`KeyStartTakeoff` and `KeyStartAutoLanding` actions; the fake flavor's port is
`FakeFlightAircraft`, simple kinematics that hover when frames stop and drop Virtual Stick on
landing, so the whole path runs on any phone.

Virtual Stick is enabled only while a command that needs it is active and disabled the
moment it completes: an idle aircraft is always under the flight controller and the RC.

### Commands and acknowledgements

`accepted` is sent on admission by the link; the loop sends `executing` when the first stick
frame goes out (or the takeoff or landing action is accepted), repeats `executing` with a
progress detail about once a second for long operations (the MAVLink `IN_PROGRESS` shape;
the relay audits each one and the remote adapter keeps waiting), then `completed` or
`failed` with a snake_case reason. Every failure detail ends in `[retryable]` or
`[terminal]`, the refusal class the wire has no field for.

| Operation | What the loop does | Completes on |
|---|---|---|
| `takeoff {z_mm}` | `KeyStartTakeoff`, wait for the reported flight state to settle in a hover, then a vertical velocity step to `z_mm` if it differs by more than 0.2 m | hover at the requested altitude |
| `goto {x_mm, y_mm, z_mm, speed_mm_s}` | one time-boxed body-frame velocity step: the displacement from the position the node reports (0,0 indoors until M3 localization) rotated into the body frame at the current heading, held for exactly `distance / speed`; speed is clamped to the node limit (0.5 m/s horizontal, 0.3 m/s vertical, PRD 5.4) and the acknowledgement says when it was slowed | the time box plus a 500 ms neutral settle |
| `rotate_to {yaw_mdeg, speed_mdeg_s}` | yaw angle mode to the compass heading, shortest way, rate clamped to 30 deg/s | heading within 5 deg for 500 ms; `yaw_not_reached` past the deadline |
| `hover` | neutral sticks (velocity zero) | 500 ms settle |
| `land` | neutral sticks, Virtual Stick off, `KeyStartAutoLanding` | the reported `landed` state |
| `estop` | neutral sticks and hover at once, plus the network-stop latch below | 500 ms settle |

Reasons the loop returns, besides the contract's `authority_lost`, `watchdog_hold`, and
`watchdog_failsafe`: `watchdog_disarmed`, `estop_asserted`, `not_airborne`, `already_airborne`,
`aircraft_unavailable`, `virtual_stick_unavailable`, `takeoff_failed`, `takeoff_timeout`,
`landing_failed`, `landing_timeout`, `landing_in_progress`, `yaw_not_reached`, `node_busy`,
`superseded` (a `hover`, `land`, or `estop` preempted the active motion), and
`unsupported`. `FlightReason` in `core/flight/FlightPort.kt` is the list with each one's class.

### Axis mapping (issue #85)

DJI documents the velocity-mode fields as "the roll property represents the X direction
velocity; the pitch property represents the Y direction velocity" (MSDK 5.18.0 docs; the v4
API says the same: `setRoll` is velocity along the x-axis, `setPitch` along the y-axis). In
the BODY coordinate system X is the nose axis and Y the starboard axis, so the loop's default
is `roll = forward`, `pitch = right`, positive up on `verticalThrottle`, yaw clockwise
positive. That is exactly the "transpose" lis-epfl measured in GROUND frame (`pitch` drove
east, `roll` drove north). `AxisMapping` in `core/flight/Axes.kt` is the one place the
mapping lives; `AxesTest` writes the signs down. The #85 axis probe below confirms it on the
exact Mini 3 and MSDK 5.18.0 pair; if the aircraft moves the other way, the Axis transpose
switch on the Flight card flips the mapping in the bridge, never downstream, and the
constant in `FlightConfig.mapping` is then changed in code.

### Deadman (local and mandatory)

The flight controller has no link-loss failsafe under Virtual Stick: it hovers forever when
stick frames stop (prior-art notes on #43). The loop therefore keeps its own `Watchdog` on
the relay-distributed `watchdog_hold_ms` and `watchdog_failsafe_ms`, fed by the relay frames
it sees through the link state and by admitted commands, independently of the link object,
so tearing the link down cannot stop the protection. With no relay activity:

- at `hold`: the active command fails with `watchdog_hold` (retryable) and the stream decays
  to neutral sticks; the frames keep flowing while Virtual Stick is enabled, so the stream
  never stops silently. Relay activity releases the hold and disables Virtual Stick.
- at `failsafe`: the active command fails with `watchdog_failsafe` (terminal), the stream
  sends one neutral frame, Virtual Stick is disabled, and `KeyStartAutoLanding` is issued
  (retried up to five times if the flight controller refuses). Indoors the failsafe is land,
  never return to home. New commands are refused with `watchdog_failsafe` until the next
  join re-arms the deadman.
- a landing already in progress is never interrupted by the deadman.
- the node lands only what it was flying. Failsafe commands the landing when the loop was
  active at that moment: Virtual Stick on, a command in flight, or any phase other than
  idle (hold included, since hold keeps Virtual Stick on), and when the hold itself
  interrupted a node takeoff or a Virtual Stick enable still pending (that leaves the loop
  idle with Virtual Stick off while the flight controller finishes the takeoff; the loop
  remembers it was flying until relay activity re-arms the deadman). With the loop idle and
  Virtual Stick off otherwise, the aircraft is already under the flight controller and the
  RC operator and the node commands nothing: an aircraft the RC operator took off by hand,
  or one hovering after a completed command, stays in the RC operator's hands when the relay
  goes silent (the Flight card shows `failsafe` and the last event says so). After an RC
  takeover the node never commands a landing, whatever the loop was doing: the RC has the
  aircraft until Re-arm control authority, and the deadman only logs `the RC has the
  aircraft`.
- nothing streams without the deadman: a Virtual Stick enable or a bench hold is refused
  with `watchdog_disarmed` until the relay's thresholds have arrived and the node has joined
  (a bench takeoff without a relay still works and hovers under the flight controller), and
  with `watchdog_failsafe` after a failsafe until the next join re-arms it.

The flight controller's own failsafe setting is read on every product connection
(`KeyFailsafeAction`, one of `HOVER`, `LANDING`, `GOHOME`) and shown on the Flight card and
in the bench log for the record. The node never changes it: the RC link is the physical RC
path and its setting stays whatever the pilot configured in DJI Fly.

### RC takeover and control authority

Physical RC input drops the aircraft out of Virtual Stick: any stick past 30 % of full
deflection (`KeyStickLeft/RightHorizontal/Vertical`, the WildBridge latch pattern), the
pause or RTH button, and every `FlightControlAuthorityChangeReason` other than the node's
own `MSDK_REQUEST` (`RC_LOST`, `RC_NOT_P_MODE`, `RC_SWITCH`, `RC_PAUSE_STOP`,
`RC_ONE_KEY_GO_HOME`, the battery reasons, `NEAR_BOUNDARY`), and the Virtual Stick state
listener reporting Virtual Stick off or owned by someone else while the loop believes it is
on. The loop fails the active command with `authority_lost` (terminal), disables Virtual
Stick, and latches: readiness reports `control_authority=false` with the reason in
`node_status.authority_change_reason` (`rc_takeover`, `rc_pause`, `rc_lost`,
`rc_mode_switch`, `virtual_stick_dropped`, ...), the relay shows `control_authority_missing`,
and every command is refused with `authority_lost` until the pilot presses Re-arm control
authority on the Flight card. Every stick event past the threshold and every button press
is forwarded to the loop; the loop, on its own thread and in order with the commands it
admits, is the only judge of whether there is anything to cancel, so a takeover works every
time: after a re-arm, and for a stick moved in the window between a command's admission and
its first tick. Stick input while the loop is idle is the pilot flying and latches nothing
(the loop notes it once per idle stretch); input while the latch is already set changes
nothing. Losing the aircraft or the RC link cancels the loop with `authority_lost` too,
without a latch, and disables Virtual Stick on the aircraft (best effort while the SDK is
down): readiness recovers when the link does, as in Phase C. If the SDK later reports
Virtual Stick still enabled for the node while the loop is idle (a link blip or an app
restart mid-command), the loop disables it at once, so the flight controller is never left
waiting for frames nobody sends.

### Network stop

The relay's authoritative `estop` flag in every `state` frame is level-triggered: on every
stick tick while it is asserted, any running motion is cut to neutral sticks
(`estop_asserted`, retryable), including a motion admitted before the flag arrived or whose
Virtual Stick enable answered after it, and the flag latches: `takeoff`, `goto`, and
`rotate_to` are refused while it is asserted (admission reads the live flag, so a command
that arrives between the flag and the next tick is refused too), `hover`, `land`, and
`estop` are accepted. If the stop stays asserted for 5 s while airborne, the node lands on
its own (PRD 5.5: hold, then land if held), unless the RC has the aircraft after a takeover:
then the node commands nothing, logs it, and the RC operator lands. Releasing the flag
clears the latch. The `estop` command is the same hover with the same latch.

### Fake flavor end to end

Install `app-fake-debug.apk`, connect to a relay as in Phase C, press Connect on the Fake
aircraft controls, then dispatch commands through the relay's remote adapter (or use Takeoff
1.2 m and Land on the probes card): the telemetry moves kinematically, the Flight card shows
the phase, the stick rate and the last frame, and `node_status.virtual_stick_enabled` flips
in the audit log. Stick 45 %, Pause, and FC drops VS on the Flight card stand in for the RC;
killing the relay drives hold, failsafe, and a landing on the fixture. The same JVM tests run
this path without a phone.

### Guarded-hover checklist (first flight)

Space and people:

- An empty room with at least 4 m by 4 m of clear floor, a patterned floor for vision
  positioning, no people or props inside the flight volume, 2.5 m or more of ceiling. GPS
  is not expected; note the flight mode the RC shows (P or ATTI).
- One RC operator on the RC-N1 with thumbs on the sticks for the whole session; one node
  operator on the phone and laptop. The RC operator calls every step and can end it at any
  time by moving a stick (takeover), pressing pause, or landing; that is the primary path.
- The RC operator is briefed on what the node does and does not do on its own: a stick past
  a third of its travel or the pause button ends any node motion and keeps the aircraft
  until Re-arm control authority is pressed; after a takeover the node lands nothing, not
  on deadman failsafe and not on a held network stop, the RC operator lands. With the loop
  idle (`virtual stick off`) the aircraft is the RC operator's and relay loss lands nothing,
  except an aircraft whose node takeoff the relay loss interrupted: the failsafe lands that
  one.
  A network stop held for 5 s lands an airborne aircraft the node is still authorized to fly,
  idle or not; a stick or pause ends that too.
- Battery above 50 %, propellers checked, the aircraft on its takeoff spot with the nose
  toward the room's `north`, the Mini 3 and RC-N1 firmware and MSDK 5.18.0 recorded in
  `hardware-profile.json` beforehand (the probe log repeats them in every entry).

Order of operations:

1. Relay up with the `remote` backend and a session id, no other node bound to this drone id.
2. Phone: probe flavor registered, RC plugged in, aircraft powered, identity card confirmed
   `DJI_MINI_3`. Save and connect on the Setup card; Connectivity shows `connected,
   authenticated`, the relay thresholds (`stick 10 Hz`, `hold 2000 ms`, `failsafe 10000 ms`
   for the demo values), and `Membership: ready` once the three readiness toggles are on.
3. Flight card: `Phase: idle`, `virtual stick off`, `Loop deadman: armed` with the same
   thresholds (the probes and every Virtual Stick enable are refused with
   `watchdog_disarmed` until it is), `Control authority: armed`, the failsafe setting line
   present, the axis transpose switch off.
4. Takeoff: the RC operator takes off manually to about 1.2 m and hovers, or the node
   operator presses Takeoff 1.2 m on the probes card with the RC operator ready. Wait for the
   node status card to show `state hovering`.
5. Axis-transpose probe: Pure pitch, then Pure roll. Each holds 0.3 m/s for 1.5 s. The RC
   operator says out loud which way the aircraft went (nose direction is `forward`, starboard
   is `right`); the result line says which axis the telemetry saw and whether it agrees with
   the mapping. Sign each one off with the operator's name and the observed motion. If the
   probe says `TRANSPOSED`, flip the Axis transpose switch and repeat both.
6. Hover drill, Deadman: press Deadman, wait for `virtual stick ENABLED` and the stick rate
   near the relay's value, then stop the relay process (or kill the LAN for Relay kill). The
   Flight card shows `watchdog hold` after the hold window with neutral sticks, then
   `failsafe` and `Landing: watchdog_failsafe` after the failsafe window; the aircraft lands.
   The drill's transitions list the times and the measured stick rate; sign it off. Restart
   the relay with a new session id before the next drill.
7. Hover drill, RC takeover: press RC takeover, wait for `virtual stick ENABLED`, then the RC
   operator moves one stick past a third of its travel. The card shows `Control authority
   LOST: rc_takeover` within a stick update, the aircraft answers the RC, and readiness
   reports `control_authority=false`. Land with the RC, press Re-arm control authority, sign
   it off. Repeat once with the pause button, and once more with a stick after that re-arm:
   the second and third takeovers must show `Control authority LOST` as fast as the first.
8. Command path through the relay: with the aircraft hovering, dispatch `hover`,
   `rotate_to` 90 deg, `goto` 0.5 m ahead, `estop`, and `land` through the remote adapter
   (`relay/tests/test_bridge_roundtrip.py` shows the in-process dispatcher); the Commands
   card and the audit JSONL show `accepted`, `executing` with progress, `completed`.

Abort criteria (the RC operator takes over, lands, and the session ends until the cause is
understood): the aircraft moves on an axis the probe did not command; the stick rate on the
Flight card reads below 5 Hz or `Phase` and the aircraft disagree for more than a second;
`watchdog hold` appears while the relay is running; a takeover does not show
`Control authority LOST` within a second; any drift toward a wall past 1 m of clearance; the
RC flight-mode switch leaves P mode; battery below 30 %.

What each screen shows during the run: Connectivity (relay link, thresholds, telemetry
rate), Readiness (the three toggles and the relay's answer, `control_authority_missing`
after a takeover), Node status (watchdog word, aircraft and RC connection, measured key
rates, `virtual stick` in the last `node_status`), Flight (phase, deadman, sticks sent and
rate, last frame, authority latch and Re-arm, axis transpose, last event), First-flight
probes (procedures, result, transitions, sign-off, log path), Commands (every command with
its outcome and detail). The probe log is `filesDir/bench/first-flight-<time>.jsonl`; copy
it with `adb` and attach it to #85 with the videos.

### Tests that prove the safety behaviours

`bridge-core`: `AxesTest` (BODY-frame signs and the transpose), `LimitsTest` (5 to 25 Hz
clamp, drift-free cadence, node limits), `MotionPlannerTest` (time-boxed steps from
millimetre arguments, heading rotation, speed clamps, shortest yaw), `FakeFlightModelTest`,
`AxisProbeTest`, and `FlightControllerTest` (hover, goto with progress, deadman hold then
failsafe with the contract reasons, a landing never interrupted, hold release, estop cut,
hold, land-if-held and release, RC takeover latch and re-arm, idle stick input, flight
controller dropping Virtual Stick, aircraft or RC loss, takeoff completion on flight state
and climb, land completion, rotate_to in angle mode, preemption and busy, enable refusal,
bench holds, a stop asserted while Virtual Stick is enabling cut before the first frame, a
motion posted after the stop flag refused before the next tick, no landing underneath the
pilot on failsafe or on a held stop after a takeover, the deadman landing only what the
node flew, Virtual Stick released on link loss and a stale enable cleared while idle, every
stick event reaching the loop and takeovers cancelling again after each re-arm, bench holds
and Virtual Stick refused while the deadman is disarmed or in failsafe, a hold-interrupted
takeoff landed at failsafe unless the relay came back).
`bridge-node`: `FlightExecutorTest` runs the loop behind `RelayLink` against the stub relay
(acknowledgement sequences on the wire with progress detail, the measured stick rate at
`virtual_stick_hz`, relay silence to hold and failsafe landing, the RC takeover as readiness
and `node_status` report it, twice across a re-arm) and checks that closing the executor
mid-hold releases Virtual Stick.

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

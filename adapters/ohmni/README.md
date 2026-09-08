# Ohmni ground node

Implements the ground-device software interface through `nodekit`: the same relay command, readiness,
telemetry and sensor paths used by aircraft. This package runs directly on the measured
Ohmni Android 7.1.2 UP-CHT01 robots. There is no Docker on these robots. MediaMTX alone
runs in the ground station's Docker compose service.

**Deployment and physical motion verification are still pending.** No robot was installed,
restarted, driven, or calibrated by this packaging change. Install only after a spotter
is beside the robot and the launch pose and lidar mounting calibration have been checked.
The installer does not start the node or enable wheels. Runtime admission requires explicit
home confirmation and a local spotter; missing LiDAR or other local evidence keeps drive disabled.

## Hardware behavior preserved

- Independent bot-shell sockets for drive, 10 Hz encoder reads, battery, and lidar setup.
  Socket: `/data/data/com.ohmnilabs.telebot_rtc/files/bot_shell.sock`. Replies are parsed by
  content; battery waits never block the encoder or drive loop.
- `manual_move +250 -250` is forward (~0.18 m/s); two positive goals turn clockwise.
  `manual_move 0 0` clears the override. `pre_drive` and `pre_rot` are never used.
- Differential-drive wheel odometry uses 16384 ticks/motor revolution, gear ratio 30/11,
  150.5 mm wheels and 332 mm base. Encoders unwrap at 10 Hz into a configured launch pose.
  A sampling gap beyond 350 ms withdraws position quality permanently because missed
  wraps cannot be reconstructed. Reposition at a marked launch pose before restarting.
- Motion has a local 0.18 m/s cap, 2 m target-distance cap, 8 cm position tolerance and
  6° heading tolerance. Requested slower speeds are scaled down; these are measured
  bring-up parameters, not a precision velocity controller. A drive thread reissues goals
  every 100 ms and stops when node motion polling is silent for 350 ms. This deadman runs
  independently of the asyncio loop and its relay heartbeat watchdog.
- A discovered CP2102 USB device with ID **10c4:ea60** is the only eligible lidar port.
  `/dev/ttyUSB0` may be the FT230X wheel bus and is never assumed to be lidar. Reader sends
  `lidar_stop` / `lidar_release`, verifies A2M8 device info and good health, uses 115200
  8N1, clears DTR and requests PWM 660. Cleanup independently attempts STOP/PWM 0 and
  sets DTR. The vendor detector is disabled while this node owns the kit. A process/serial
  owner lock prevents competing readers. Startup runs vendor init before serial handoff;
  relay re-enable never resets an active reader. Missing USB at boot keeps retrying discovery.
- One-degree scan bins use the closest valid return in a bin. Both mounting offset and
  angular handedness are required for map scans: 0 forward, increasing counter-clockwise.
  Uncalibrated raw ranges remain available in console diagnostics and the full-circle safety
  guard. They are never mislabeled as robot/world-frame map scans.
- Zero to two explicitly configured V4L2 inputs publish independent software H.264 feeds
  over **TCP RTSP**. [Camera configuration](CAMERAS.md) declares each actual input, capture
  mode, stream and publisher account; missing configuration starts no publisher. Units
  1–64 are supported without inventing MediaMTX permissions. Per-feed freshness comes from
  advancing ffmpeg output, while relay MediaMTX monitoring independently determines stream
  availability. Neither publication nor this diagnostic advertises capture or tilt controls.
- Battery reports five measured cell voltages and the installed vendor's coarse minimum-cell
  display band (20/50/80/100%), explicitly labeled estimated. Unknown or stale charge is null
  in custom telemetry and conservative 0 in planner telemetry. Wheel-only position quality
  is 0.6; unavailable is 0. Platform model, Android release and installed telebot version are
  read once from the actual host; missing identity fields remain unreported.

The kit anchors time to `auth.accepted.t` and advances it by monotonic elapsed time;
Android clock drift does not poison TTL or telemetry. Frame timestamps strictly increase
with a bounded 500 ms lead. ACKs are built and immediately enqueued on the node event loop,
so a later telemetry frame cannot overtake an earlier ACK.

## Guard coverage and local controls

Every movement, including turns, requires a fresh full-circle LiDAR scan (at most 500 ms
old), at least 30 valid angular bins, a return in every 30° sector, and no return within
45 cm anywhere around the robot. Unknown sectors, stale/disconnected sensing, lost
odometry or a missing spotter refuse or stop motion. Failed motion never resumes simply
because scans recover. Raw full-circle clearance needs no mounting assumption; map scans
still require measured mounting calibration and fresh odometry. Single-plane LiDAR cannot
see obstacles outside its scan plane, so a watching spotter remains required.

LiDAR is mandatory. A robot with no kit can join and show telemetry/video while movement
remains blocked. `SWEEP_ALLOW_NO_LIDAR=1` and the old supervised-bypass config now fail
explicitly. The owner keeps retrying USB discovery so a powered kit attached later can
recover sensing. `lidar` advertises implemented reader support; `lidar.present`, scan age,
coverage and health report the actual sensor state.

Custom `node_status.device_telemetry` includes identity, position/yaw and velocity, battery
cells/voltage/charge age and docking, odometry quality/loss/encoder samples, raw LiDAR ranges
and coverage, sensor identity/health/port, calibration/map availability, safety reasons,
local authority/spotter state, and supported controls. Legacy `link=1` means an authenticated
transport frame, not measured RF quality; custom radio quality remains unreported. Requested motor PWM is labeled as a
command; only fresh complete scans establish working rotation.

Battery band provenance: the preserved measured bot-shell response contains five cells. The installed `telebot_node.js` `battery_new` handler selects the
minimum cell and uses `<3150 mV → 20%`, `<3250 → 50%`, `<3380 → 80%`, otherwise `100%`,
then publishes that level to Android. This node reproduces those exact coarse bands; it
does not apply a four-cell lithium-ion voltage curve to Ohmni's LiFePO4 pack.

Open `http://127.0.0.1:8765/` on the robot's browser. It shows link, authority, lidar/guard,
video and last refusal, plus a large STOP, `Spotter present`, and `Re-enable and rejoin`.
The HTTP server binds only loopback. STOP immediately latches motion off before hardware
I/O, attempts STOP and disable independently, and reports an unconfirmed hardware write
as an error. The drive thread keeps retrying failed STOP/SLEEP writes even with no motion.
A new local STOP invalidates older queued re-enable requests. A relay estop executes at
admission, so a following goto cannot supersede it. Failsafe survives automatic reconnect;
only a fresh local re-enable with a spotter requests a new connection epoch.

## Build and install (host Python 3.12)

The four required artifacts are:

1. python-build-standalone **3.12 x86_64 unknown-linux-musl**, install-only archive;
2. Alpine x86_64 musl APK containing `lib/ld-musl-x86_64.so.1`;
3. static x86_64 ffmpeg with v4l2 input and libx264 (measured build 7.0.2);
4. a **pure Python** `websockets` wheel (`py3-none-any`; no glibc `.so`).

Use a private JSON manifest with exactly `python`, `musl`, `ffmpeg`, `websockets` keys.
Each entry contains a reviewed immutable HTTPS `url` and a verified `sha256`. No script
selects an unverified latest executable. Preserve the actual upstream URLs and hashes
with the deployment evidence.

```sh
python3 adapters/ohmni/tools/fetch_artifacts.py /private/artifacts.json /private/artifacts
python3 adapters/ohmni/tools/build_payload.py /private/artifacts /private/ohmni-runtime.tar
# Stop the existing node through its own launcher first; install never kills it.
adapters/ohmni/install.sh "$ADB_SERIAL" /private/ohmni-runtime.tar
```

The builder also accepts already preserved archives named `python`, `musl`, `ffmpeg`,
`websockets` with a `manifest.json` providing each SHA256. On 2026-09-06 the preserved
Python, musl 1.2.6-r2, ffmpeg 7.0.2 and pure websockets archives were hashed and successfully
built into a ~162 MB plain tar. The local provenance manifest is outside git; its original
upstream URLs were not preserved, so this is offline reuse evidence, not a reconstructed
upstream lockfile. The host builder omits unused `python/share/terminfo` aliases because
case-distinct symlinks collide on macOS. Payload inspection confirmed the loader, Python,
ffmpeg, kit and local page and excluded credentials, relay source and git data.

The installer checks for an existing package PID and a legacy `ohmni_node.py` running
under this deployment's exact musl loader. It uses `adb push` and `toybox tar xf` into
`/data/local/sweep`; no pip executes on Android. Its launcher validates a saved PID's
command line before signaling it, so a reused PID cannot stop an unrelated process.

Create a private, shell-quoted `node.env` with mode 600 on the robot. Keys are entered
locally or pushed separately from a private file; never commit them. Example non-secret
configuration (replace the relay/session/id/unit with their configured values):

```sh
SWEEP_RELAY_URL=ws://relay-host:8010
SWEEP_SESSION_ID=sweep-session
SWEEP_DEVICE_ID=11
SWEEP_DEVICE_UNIT=1
SWEEP_NODE_KEY_FILE=/data/local/sweep/node.key
SWEEP_MEDIA_HOST=media-host
SWEEP_SPOTTER=0
SWEEP_HOME_CONFIRMED=0
SWEEP_HOME_X=0
SWEEP_HOME_Y=0
SWEEP_HOME_YAW_DEG=0
# Set both only after measuring the mounting; there is no assumed calibration:
# SWEEP_LIDAR_OFFSET_DEG=<measured raw-zero heading in robot CCW degrees>
# SWEEP_LIDAR_ANGLE_SIGN=<1 or -1, measured handedness>
```

After confirming the marked launch pose, set `SWEEP_HOME_CONFIRMED=1`. Start with
`adb -s "$ADB_SERIAL" shell /data/local/sweep/run.sh start`. The spotter checks the local
page, claims presence, and presses re-enable. Stop with `run.sh stop`; it waits for orderly
STOP/disable and publisher/lidar cleanup and never force-kills an unresponsive node.
A failed hardware STOP cannot be proven safe by software alone; its local page reports
that it is unconfirmed.

Telemetry v1 has no yaw field. Custom telemetry reports heading and explicitly labels the
pose `launch_wheel_odometry`; this is not a measured world registration. Calibrated sensor
frames include heading, but qualified shared-map/world navigation remains a separate integration.
The relay's command TTL is also the node's motion deadline; configure a suitable bounded
window for a 90° turn at 45°/s and retain the short independent heartbeat/drive watchdogs.

## Preserved bring-up tools and evidence

`tools/console_client.py`, `tools/calibrate_drive.py`, and `tools/lidar_offset.py` preserve
the handoff's console intent and lidar ground-truth procedures. They read process env
(e.g. `uv run --env-file /private/live.env python ...`), contain no machine-specific paths
or keys, and the motion probes require `--spotter-confirmed`, cap wheel goals at 250 and
bursts at 2 s, and send STOP in `finally`. The lidar offset output is a hypothesis to check
with a visible target and the robot camera, not an automatically accepted calibration.
Use raw probe recordings for initial calibration; published runtime scans are already
rotated into the robot frame. Capture raw JSONL from the spike lidar probe and console
ACK/telemetry/sensor JSONL during the supervised tests.

The operator console remains the single canonical instance on port **5173**. The host
launcher is `tools/ground_runtime.py` at the repository root, with explicit private
configuration and process ownership. The old `tools/bringup.sh` / `tools/stack.py`
entry points now print that referral and exit without starting or stopping any service
or robot. They cannot create another console or replace a relay on an occupied port.

Device count follows the configured identity inventory; it is not fixed to the historical
three-robot handoff. Each additional unit needs its actual adapter credential, two camera
mappings, LiDAR and launch/calibration evidence. Missing hardware remains unavailable.

Outstanding S3 evidence must be recorded before calling the physical system verified:

- [ ] Lidar offset and angle sign measured per installed kit, with a person watching.
- [ ] Local STOP, spotter withdrawal, stale lidar, lost odometry and link-loss stop verified.
- [ ] One robot on a marked launch line: one-foot forward, then 90° rotation, JSONL + camera evidence.
- [ ] Investigate no-motion bursts/stalls and objects outside the lidar plane.
- [ ] Only then additional commissioned robots, individual selection and qualified coordinated commands.
- [ ] Confirm every installed sensor's live scan and occupancy map plus all live video paths.

Offline checks cover measured signs, unwrap/gap behavior, hardware socket separation,
calibration and guard rejection, persistent safety writes, clock drift/order, watchdog
latches, STOP priority, and existing DJI/ground relay roundtrips. These do not replace the
physical checklist above.

The original S0 probe instructions are preserved in [the spike runbook](spike/README.md).


## Bounded console peripherals

`robot_peripheral_v1` plus `neck`, `speech`, `lights` and `screen` advertise the implemented
peripheral route. The console submits an exact confirmed single-device request through the
normal signed command, TTL, sequence and connection-epoch checks. Stationary screen, lights
and speech are independent of wheel authority; they do not arm, wake or move the robot.
Neck requests additionally require the local robot enabled, undocked and a spotter present,
and retain the runtime watchdog and network-stop guards. All peripheral requests require a
current nominal control lease; screen, lights and speech can work with wheel authority disabled.

The installed telebot 4.1.4.4 handlers are `neck_angle POSITION` (300–650, 512 forward),
`light_color 20 H S V` (each 0–255), and `say TEXT` (one printable line, 240 characters).
`neck_angle` uses the vendor model and requires an already-awake neck; no auto-wake or torque
command is sent. Screen text updates only the local Sweep safety page, via textContent,
with STOP and spotter controls retained. Empty screen text clears it.

The bot shell provides no measured completion/readback for these APIs. A successful ACK
means the bounded request was submitted; `peripherals.requested_*` and
`readback_verified: false` make that distinction visible in the console. Hardware output
still needs supervised verification on each robot's installed vendor stack.

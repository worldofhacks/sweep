# Ohmni ground node

Implements MVP F.2 / issue #239 through `nodekit`: the same relay command, readiness,
telemetry and sensor paths used by aircraft. This package runs directly on the measured
Ohmni Android 7.1.2 UP-CHT01 robots. There is no Docker on these robots. MediaMTX alone
runs in the ground station's Docker compose service.

**Deployment and physical motion verification are still pending.** No robot was installed,
restarted, driven, or calibrated by this packaging change. Install only after a spotter
is beside the robot and the launch pose and lidar mounting calibration have been checked.
The installer does not start the node or enable wheels. On an initial start with the
configuration below, the local screen and STOP work while wheels remain disabled.

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
  `lidar_stop` / `lidar_release`, uses 115200 8N1, clears DTR, sets PWM 660; cleanup sends
  STOP/PWM 0 and sets DTR. The vendor detector is disabled while this node owns the kit.
- One-degree scan bins use the closest valid return in a bin. Both mounting offset and
  angular handedness are required. The published scan and obstacle guard use robot-frame
  angles: 0 forward, increasing counter-clockwise. An uncalibrated scan is not published.
- UVC `/dev/video0`, UYVY 640×480@30 capture, software H.264@15 and **TCP RTSP** publish to
  `ground{unit}`. Credentials are derived from the device key with the media HMAC domain;
  they are never logged. Process liveness is a node diagnostic; relay MediaMTX monitoring
  determines whether the console has a live stream.
- Battery comes from five measured cell voltages, not a default safe charge. Unknown or
  stale battery readings report 0. Wheel-only position quality is 0.6; unavailable is 0.

The kit anchors time to `auth.accepted.t` and advances it by monotonic elapsed time;
Android clock drift does not poison TTL or telemetry. Frame timestamps strictly increase
with a bounded 500 ms lead. ACKs are built and immediately enqueued on the node event loop,
so a later telemetry frame cannot overtake an earlier ACK.

## Guard coverage and local controls

The forward guard checks ±20° and 45 cm. A stale scan (>500 ms), no forward coverage,
missing calibration, or lost odometry refuses/stops motion. An installed kit never silently
falls back when it stops producing scans. Single-plane lidar misses obstacles above and
below its height; it is not complete collision detection and does not prove rotational
clearance. A watching spotter is required throughout motion.

A robot without a kit can join and show telemetry/video, but motion is blocked by default.
`SWEEP_ALLOW_NO_LIDAR=1` permits an explicitly supervised exception only when **no kit is
installed** and a spotter is claimed. It does not create sensing. G-03 has no kit in the
handoff; install one for lidar functionality. Aircraft have no invented lidar capability:
only devices whose hardware actually supplies scans can populate the map.

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
SWEEP_ALLOW_NO_LIDAR=0
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

Telemetry v1 has no yaw field. Use room-relative translation (`translation_frame: world`)
until the contract has a reviewed heading field. Lidar pose includes heading for maps.
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

`tools/bringup.sh [nodes|stop]` is the durable stack launcher. It requires private
`SWEEP_RELAY_ENV_FILE` and `SWEEP_ROBOTS_FILE` (JSON rows containing `serial`, `env_file`).
No robot addresses, keys, launch positions or spotter claims are invented. Default start
creates a fresh session and starts relay :8010, console :5174, repository MediaMTX, and
already-installed robot nodes. `nodes` restarts only those nodes; `stop` stops them and
only host process groups recorded by this launcher. Logs/PIDs live in ignored
`.sweep-ohmni/` with mode 700. Port 8000 and unrelated sessions are never stopped.

Outstanding S3 evidence must be recorded before calling the physical system verified:

- [ ] Lidar offset and angle sign measured per installed kit, with a person watching.
- [ ] Local STOP, spotter withdrawal, stale lidar, lost odometry and link-loss stop verified.
- [ ] One robot on a marked launch line: one-foot forward, then 90° rotation, JSONL + camera evidence.
- [ ] Investigate no-motion bursts/stalls and objects outside the lidar plane.
- [ ] Only then three robots, floor-plane line formation, individual selection and coordinated commands.
- [ ] Confirm every installed sensor's live scan and occupancy map plus all live video paths.

Offline checks cover measured signs, unwrap/gap behavior, hardware socket separation,
calibration and guard rejection, persistent safety writes, clock drift/order, watchdog
latches, STOP priority, and existing DJI/ground relay roundtrips. These do not replace the
physical checklist above.

The original S0 probe instructions are preserved in [the spike runbook](spike/README.md).

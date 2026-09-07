# Ohmni Android runtime

This package runs the ground adapter on the measured Ohmni Android 7.1 image. It ships a musl Python 3.12 runtime, a pure-Python `websockets` wheel, the Ohmni adapter, and the small relay/planner contract set it imports. The robot does not run Docker, `pip`, or a host Python interpreter.

`run.sh` starts `./lib/ld-musl-x86_64.so.1 ./python/bin/python3.12 -m adapters.ohmni`. It reads a mode-600 `node.env`, records a PID, verifies that PID before stopping it, and gives the runtime time to issue its local stop writes. `camera.sh` is separate: it starts only the camera publisher from `camera.env`, verifies its own PID before stopping it, and never creates a ground device or sends a control command. `install.sh` stages a plain tar through ADB, then extracts it with Toybox through the Ohmni's `su 0` shell. It leaves the adapter stopped. Set `ADB` to the platform-tools executable when it is outside your PATH.

A normal runtime joins with motion disabled until the local spotter, calibrated lidar, current pose, and signed relay heartbeat qualify it. The local device deadman continues to run independently of the relay event loop. Deployment and physical motion are separate supervised activities.

## Build and install

The host needs Python 3.12 and four reviewed files, each named in a private `manifest.json` with its SHA-256:

1. a Python 3.12 x86_64 musl standalone archive containing `python/bin/python3.12`;
2. an x86_64 musl archive containing `lib/ld-musl-x86_64.so.1`;
3. a static x86_64 `ffmpeg` archive if the camera publisher is enabled;
4. a `py3-none-any` `websockets` wheel.

The URLs and hashes are deployment evidence. The fetcher accepts only HTTPS URLs and exact SHA-256 values; the builder verifies the downloaded archive before extracting it.

```sh
python3 adapters/ohmni/tools/fetch_artifacts.py /private/ohmni-artifacts.json /private/ohmni-artifacts
python3 adapters/ohmni/tools/build_payload.py /private/ohmni-artifacts /private/ohmni-runtime.tar
adapters/ohmni/install.sh "$ADB_SERIAL" /private/ohmni-runtime.tar
```

The builder produces an uncompressed tar because the measured robot’s Toybox extraction path is reliable for that format. It omits tests, tools, Git data, caches, logs, and environment files. The payload contains no keys, map artifacts, or approval records.

Create `/data/local/sweep/node.env` locally with mode 600. The example names the configuration values but contains no real endpoint or credential:

```sh
SWEEP_RELAY_URL=ws://relay-host:8010
SWEEP_SESSION=replace-with-current-session
SWEEP_DEVICE_UNIT=9
SWEEP_NODE_KEY=replace-with-device-key
SWEEP_ADAPTER_ID=ohmni-9
SWEEP_RELAY_CONNECT_HOST=192.0.2.10
SWEEP_RELAY_CLOCK_OFFSET_MS=0
SWEEP_ODOM_ORIGIN_ID=measured-odom-origin
# Set all five only when this node is approved to publish video.
SWEEP_MEDIA_HOST=media-host:8554
SWEEP_CAMERA_DEVICE=/dev/video1
SWEEP_CAMERA_INPUT_FORMAT=mjpeg
SWEEP_CAMERA_INPUT_FPS=native
SWEEP_CAMERA_WIDTH_PX=640
SWEEP_CAMERA_HEIGHT_PX=480
SWEEP_LIDAR_MOUNT_X_M=0.00
SWEEP_LIDAR_MOUNT_Y_M=0.00
SWEEP_LIDAR_MOUNT_Z_M=0.25
SWEEP_LIDAR_MOUNT_YAW_DEG=0.00
```

Keep `SWEEP_RELAY_URL` at the verified `wss://` hostname. When the robot must dial a numeric address, set `SWEEP_RELAY_CONNECT_HOST` to that IPv4 or IPv6 address. The TCP connection uses the numeric address while TLS and the HTTP Host header use the hostname in `SWEEP_RELAY_URL`.

`SWEEP_RELAY_CLOCK_OFFSET_MS` is a measured relay wall-clock correction, bounded to five minutes. It applies to signed relay envelopes and lease deadlines. It does not alter sensor receipt times or establish a capture-clock mapping.

Measure the lidar center relative to the midpoint between the drive wheels: X forward, Y left, and Z up from the floor, in metres. Do not copy the example XYZ values. `SWEEP_LIDAR_OFFSET_DEG` and `SWEEP_LIDAR_ANGLE_SIGN` convert raw scan angles into body axes and require a stationary target check. The published scan keeps the lidar center as its origin and uses body-aligned axes, so `SWEEP_LIDAR_MOUNT_YAW_DEG` must be zero. A nonzero yaw is refused because it would rotate an already normalized scan again.

Camera publishing is disabled unless `SWEEP_MEDIA_HOST` and every `SWEEP_CAMERA_*` source value are present. The source is an approved V4L node, its exact input format, its native rate (`native`) or a measured integer FPS, and its dimensions. The relay device ID derives the canonical MediaMTX path `drone{id}`; it is never renumbered to a ground-unit path. Current measured configurations are unit 11: `/dev/video1`, `mjpeg`, `native`, 640×480; unit 12: `/dev/video0`, `uyvy422`, `30`, 640×480. These identify a usable image stream only. They do not establish tag identity, camera calibration, pose, or timing.

The publisher reports `publishing` only after ffmpeg reports a decoded frame and reverts to `failed` when progress goes stale. The field media server keeps RTSP on VPS loopback. Each camera uses `adb reverse tcp:8554 tcp:18554`, and its `camera.env` sets `SWEEP_MEDIA_HOST=127.0.0.1:8554`; the media-only publisher credential then stays inside the authenticated ADB tunnel instead of crossing the public network. `camera.env` contains only the media host and the five camera-source settings; `camera.sh` takes the device ID and node key from `node.env` and refuses a changed device ID. Keep `camera.env` mode 600 and use the separate camera process only after the payload that contains it is installed:

```sh
adb -s "$ADB_SERIAL" reverse tcp:8554 tcp:18554
adb -s "$ADB_SERIAL" shell su 0 /data/local/sweep/adapters/ohmni/camera.sh start
adb -s "$ADB_SERIAL" shell su 0 /data/local/sweep/adapters/ohmni/camera.sh stop
```

Start the ground runtime only after the qualification checks below:


```sh
adb -s "$ADB_SERIAL" shell su 0 /data/local/sweep/run.sh start
adb -s "$ADB_SERIAL" shell su 0 /data/local/sweep/run.sh stop
```

## Owner encoder sampler

The vendor Node owns paired drive-encoder reads and publishes bounded records through `sweep_encoder.sock`. No BotShell or adapter reader may query those registers while the sampler is installed. The ownership gate starts with the Node; polling begins only when the native model starts after servo initialization. A later native initialization withdraws pose through an unavailable event until a newly qualified sampler produces pairs. The patch installer only stages verified source and does not restart the vendor owner. A stock owner restart reconnects the serial bus, reinitializes the servos, enables wheel torque, and initializes the neck. A restart therefore requires a separately reviewed operator procedure after physical motion is permitted.

## Approved ground return

A confirmed `come_home` for a selected ground node is eligible only when the relay has `SWEEP_GROUND_RETURN_ID` and the node has a matching approved return artifact. The command carries only that ID. It does not carry a route, a destination, or a way to alter the route.

The node reads `SWEEP_RETURN_APPROVAL_FILE` and verifies it using the separate `SWEEP_RETURN_APPROVAL_KEY_FILE`. The record is signed by the external approval authority and contains the SHA-256 of the raw measured geometry bytes carried in base64, the session, device ID, connection epoch, odometry-origin ID, source-registration ID, source/frame binding, and measured `world_to_odom` transform. Its geometry is a `measured_corridor_v1` object with one fixed start and an ordered set of fixed segment targets and footprint polygons. The source-registration ID must equal the transform registration ID. The configured `SWEEP_ODOM_ORIGIN_ID` must equal the approved origin ID.

At command admission, the controller checks the approved session, device ID, connection epoch, odometry-origin ID, pose source, and odometry frame against the live node. A confirmed start begins at the fixed start; a confirmed resume begins only inside one of the same pinned segment footprints. Before every turn and every forward pulse it requires a current external relay grant, a qualified pose in that epoch, and a current 360-degree scan aligned with that pose. Every scan bin must clear the robot footprint, stopping distance, and one forward pulse. Each approved polygon is simple, and its fixed segment must maintain that same clearance from every boundary. Before each pulse, the controller verifies the current pose and remaining direct segment preserve the clearance. It never asks the planner for a new route and it never commands negative linear velocity. A lost grant, stale pose, changed epoch, changed source/frame binding, stale scan, incomplete footprint clearance, missing record, hash mismatch, or departure from the footprint stops and refuses the return.

The adapter acknowledges completion after the final pose is inside the approved arrival tolerance. `accepted` and `executing` acknowledgements do not complete a return. A local stop remains in effect after arrival; no return outcome clears an estop latch or reenables a stopped robot.

## Qualification record

Before enabling ground motion, record the following alongside the device/session evidence:

- review the exact artifact manifest and verify the produced payload contains the musl loader, Python, runtime modules, and no credentials;
- verify the local STOP, relay HOLD, relay ESTOP, link loss, heartbeat expiry, lost odometry, stale lidar, and spotter withdrawal all stop the robot;
- measure and record the lidar mounting transform and angle convention for the installed kit;
- verify the return approval signature, raw geometry SHA-256, session, device ID, connection epoch, odometry-origin ID, source-registration ID, `world_to_odom` measurement, pose source, and frame against the approved map record;
- verify a supervised return on the marked corridor with raw pose, scan, command, acknowledgement, and camera evidence;
- confirm that the final completion follows a measured arrival pose and that no completion is emitted when the artifact, grant, pose, scan, or footprint check is unavailable.

Installation and runtime startup are separate commands. Runtime startup leaves motion subject to the local safety checks above.

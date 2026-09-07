# Ohmni runtime

The Ohmni adapter runs on the robot's Android deployment payload. `run.sh` launches the ground runtime; it records and verifies its own PID before stopping it. Shutdown sends `SIGTERM` and waits for the runtime to write its local stop before the launcher removes the PID record.

`node.env` is private configuration and must have mode 600:

```sh
<<<<<<< HEAD
SWEEP_RELAY_URL=wss://relay.example/field
SWEEP_SESSION=current-session
SWEEP_DEVICE_UNIT=11
SWEEP_NODE_KEY=replace-with-node-key
SWEEP_ADAPTER_ID=ohmni-11
SWEEP_RELAY_CONNECT_HOST=192.0.2.10
SWEEP_RELAY_CLOCK_OFFSET_MS=0
=======
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
SWEEP_ODOM_ORIGIN_ID=measured-odom-origin
>>>>>>> 648b9661 (fix: harden approved Ohmni return safety)
SWEEP_LIDAR_MOUNT_X_M=0.00
SWEEP_LIDAR_MOUNT_Y_M=0.00
SWEEP_LIDAR_MOUNT_Z_M=0.25
SWEEP_LIDAR_MOUNT_YAW_DEG=0.00
```

Keep `SWEEP_RELAY_URL` at the verified `wss://` hostname. If the robot must dial a numeric address, set `SWEEP_RELAY_CONNECT_HOST` to that IPv4 or IPv6 address. The TCP connection uses the numeric address while TLS and the HTTP Host header retain the hostname from `SWEEP_RELAY_URL`.

`SWEEP_RELAY_CLOCK_OFFSET_MS` is a measured relay wall-clock correction, bounded to five minutes. It applies to signed relay envelopes and lease deadlines. It does not alter sensor receipt times or establish a capture-clock mapping.

## Camera-only publisher

`camera.sh` runs the camera publisher without starting the ground-control runtime. It reads `node.env` plus a mode-600 `camera.env`, and rejects a `camera.env` that changes the node device ID.

```sh
SWEEP_MEDIA_HOST=127.0.0.1:8554
SWEEP_CAMERA_DEVICE=/dev/video1
SWEEP_CAMERA_INPUT_FORMAT=mjpeg
SWEEP_CAMERA_INPUT_FPS=native
SWEEP_CAMERA_WIDTH_PX=640
SWEEP_CAMERA_HEIGHT_PX=480
```

<<<<<<< HEAD
The source must be an approved V4L node, `mjpeg` or `uyvy422`, its native rate or a measured integer rate, and measured dimensions. The publisher uses the node ID for the MediaMTX path `drone{id}`. It reports `publishing` only after ffmpeg reports increasing decoded-frame counts and changes to `failed` when that progress is stale.

## Approved ground return

A confirmed `come_home` for a selected ground node can dispatch an approved return only when the relay is configured with `SWEEP_GROUND_RETURN_ID`. The node requires `SWEEP_RETURN_APPROVAL_FILE`, `SWEEP_RETURN_APPROVAL_KEY_FILE`, and `SWEEP_ODOM_ORIGIN_ID`; it refuses the command when any is absent.

The separate external approval binds the session, device ID, connection epoch, odometry origin, source registration, pose source, frame, exact measured geometry bytes, and world-to-odometry transform. Its fixed corridor footprints must be simple and provide clearance for the full robot, stopping distance, and one forward pulse. Each turn and forward pulse rechecks the active external grant, qualified current pose, current full scan, clearance, and remaining approved chord. The controller never replans, reverses, or treats an acknowledgement as arrival. It completes only after a measured final pose satisfies the approved tolerance.

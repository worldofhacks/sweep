# Ohmni runtime

The Ohmni adapter runs on the robot's Android deployment payload. `run.sh` launches the ground runtime; it records and verifies its own PID before stopping it. Shutdown sends `SIGTERM` and waits for the runtime to write its local stop before the launcher removes the PID record.

`node.env` is private configuration and must have mode 600:

```sh
SWEEP_RELAY_URL=wss://relay.example/field
SWEEP_SESSION=current-session
SWEEP_DEVICE_UNIT=11
SWEEP_NODE_KEY=replace-with-node-key
SWEEP_ADAPTER_ID=ohmni-11
SWEEP_RELAY_CONNECT_HOST=192.0.2.10
SWEEP_RELAY_CLOCK_OFFSET_MS=0
SWEEP_LIDAR_MOUNT_X_M=0.00
SWEEP_LIDAR_MOUNT_Y_M=0.00
SWEEP_LIDAR_MOUNT_Z_M=0.25
SWEEP_LIDAR_MOUNT_YAW_DEG=0.00
```

Keep `SWEEP_RELAY_URL` at the verified `wss://` hostname. If the robot must dial a numeric address, set `SWEEP_RELAY_CONNECT_HOST` to that IPv4 or IPv6 address. The TCP connection uses the numeric address while TLS and the HTTP Host header retain the hostname from `SWEEP_RELAY_URL`.

`SWEEP_RELAY_CLOCK_OFFSET_MS` is a measured relay wall-clock correction, bounded to five minutes. It applies to signed relay envelopes and lease deadlines. It does not alter sensor receipt times or establish a capture-clock mapping.

Measure the lidar center relative to the midpoint between the drive wheels: X forward, Y left, and Z up from the floor, in metres. Do not copy the example XYZ values. `SWEEP_LIDAR_OFFSET_DEG` and `SWEEP_LIDAR_ANGLE_SIGN` convert raw scan angles into body axes and require a stationary target check. The published scan keeps the lidar center as its origin and uses body-aligned axes, so `SWEEP_LIDAR_MOUNT_YAW_DEG` must be zero. A nonzero yaw is refused because it would rotate an already normalized scan again.

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

The source must be an approved V4L node, `mjpeg` or `uyvy422`, its native rate or a measured integer rate, and measured dimensions. The publisher uses the node ID for the MediaMTX path `drone{id}`. It reports `publishing` only after ffmpeg reports increasing decoded-frame counts and changes to `failed` when that progress is stale.

## Approved ground return

A confirmed `come_home` for a selected ground node can dispatch an approved return only when the relay is configured with `SWEEP_GROUND_RETURN_ID`. The node requires `SWEEP_RETURN_APPROVAL_FILE`, `SWEEP_RETURN_APPROVAL_KEY_FILE`, and `SWEEP_ODOM_ORIGIN_ID`; it refuses the command when any is absent.

The separate external approval binds the session, device ID, connection epoch, odometry origin, source registration, pose source, frame, exact measured geometry bytes, and world-to-odometry transform. Its fixed corridor footprints must be simple and provide clearance for the full robot, stopping distance, and one forward pulse. Each turn and forward pulse rechecks the active external grant, qualified current pose, current full scan, clearance, and remaining approved chord. The controller never replans, reverses, or treats an acknowledgement as arrival. It completes only after a measured final pose satisfies the approved tolerance.

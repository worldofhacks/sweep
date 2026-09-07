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
SWEEP_SPOTTER=0
```

Keep `SWEEP_RELAY_URL` at the verified `wss://` hostname. If the robot must dial a numeric address, set `SWEEP_RELAY_CONNECT_HOST` to that IPv4 or IPv6 address. The TCP connection uses the numeric address while TLS and the HTTP Host header retain the hostname from `SWEEP_RELAY_URL`.

`SWEEP_RELAY_CLOCK_OFFSET_MS` is a measured relay wall-clock correction, bounded to five minutes. It applies to signed relay envelopes and lease deadlines. It does not alter sensor receipt times or establish a capture-clock mapping.

Measure the lidar center relative to the midpoint between the drive wheels: X forward, Y left, and Z up from the floor, in metres. Provide those measured values as `SWEEP_LIDAR_MOUNT_X_M`, `SWEEP_LIDAR_MOUNT_Y_M`, and `SWEEP_LIDAR_MOUNT_Z_M`; no numeric deployment defaults are supplied. `SWEEP_LIDAR_OFFSET_DEG` and `SWEEP_LIDAR_ANGLE_SIGN` convert raw scan angles into body axes and require a stationary target check. The published scan keeps the lidar center as its origin and uses body-aligned axes, so `SWEEP_LIDAR_MOUNT_YAW_DEG` must be zero. A nonzero yaw is refused because it would rotate an already normalized scan again.

## Mandatory ground avoidance and drive qualification

Ground motion requires the configured LiDAR and a continuously valid paired-encoder pose before vendor drive initialization. `SWEEP_ALLOW_NO_LIDAR` is rejected when enabled. A spotter declaration alone does not qualify motion. Devices may join the single existing console at port 5173 and publish available observations while wheel control remains unavailable. The relay wire ID (`SWEEP_DEVICE_UNIT`) and optional host console unit mapping are separate identities; configure each additional robot explicitly with its own credentials, source bindings and sensor measurements.

In addition to the measured mount and angular calibration above, wheel control requires `SWEEP_GROUND_FOOTPRINT_RADIUS_M` (a circle enclosing the complete robot), `SWEEP_GROUND_STOPPING_DISTANCE_M` (measured at the allowed speed), and `SWEEP_GROUND_CLEARANCE_MARGIN_M`. Missing or invalid measurements refuse drive. Clearance about the LiDAR origin includes the measured body radius, horizontal sensor offset, stopping distance, margin, and bounded travel during scan age, owner timeout and one drive-loop tick. All 360 one-degree bins must contain fresh positive measured returns outside that clearance. Unknown bins, stale/future scans and obstacles at the sides or rear block both forward and yaw pulses; the runtime does not infer empty space from missing returns. This conservative rule can require a better mounting position or coverage qualification before the robot can move.

`ground_velocity` supports one confirmed selected ground node: a forward pulse up to 180 mm/s or yaw up to 785 mrad/s, lasting at most 500 ms. It does not support reverse or simultaneous linear/angular motion. Each hardware tick rechecks pose, all-around clearance and the local owner deadman. The relay also revalidates current accepted pose, authority and emergency-stop state atomically when signing the command. HOLD and ESTOP remain available after pose or authority loss. Completion requires successful local STOP writes; failure is reported as failed and disable is independently attempted. Successful software writes do not establish measured physical stopping distance.

The production pose consumer reads the vendor owner's `sweep_encoder.sock`; it never falls back to competing direct `apos` requests. `install_owner_encoder_plugin.sh` stages the coherent PR315 loader/sampler/trace bundle without changing the reviewed vendor source or restarting it. The installer refuses occupied plugin paths and verifies exact bytes; updating an installed bundle requires preserving its matching rollback evidence first. See [HANDBACK.md](HANDBACK.md). A retained `missing_encoder_reply` fault still keeps pose invalid. PR315's recoverable normal-query timeout and a successful qualification on another robot do not qualify this robot. Vendor lifecycle changes and measured continuous encoder/STOP/clearance acceptance are separate deployment steps.

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

The source must be an approved V4L node, `mjpeg` or `uyvy422`, its native rate or a measured integer rate, and measured dimensions. By default the publisher uses the global node ID for the MediaMTX path `drone{id}`. An optional `SWEEP_CAMERA_STREAM=ground1` explicitly selects an already provisioned local stream; it does not change the node ID. The account name and HMAC password domain both follow that exact stream. Names must be flat, begin with an ASCII letter or digit, contain only letters, digits, `_` or `-`, and fit within 64 characters. Existing publisher and reader permissions must authorize the selected stream; this option changes no MediaMTX service or credentials.

`sh /data/local/sweep/adapters/ohmni/camera.sh probe` performs one foreground publication attempt using the same private configuration. It waits at most ten seconds for two advancing ffmpeg frame-count samples, then terminates only its own ffmpeg process. Cleanup can take another five seconds. It prints only a JSON result with a bounded reason, observed frame/progress counts and `cleanup_confirmed`; `exit 0` means local producer output was observed, while `exit 1` means failure. It does not retry, write a background PID record, start ground control or stop vendor processes. It refuses an existing live camera PID record. A vendor-open V4L input may still reject capture; use the result instead of inferring availability from process liveness. Independently verify increasing MediaMTX inbound bytes and decoded browser frames before calling the feed live.

The long-running `start` operation reports `publishing` internally only after increasing ffmpeg frame counts and changes to `failed` when progress is stale. Native input rate remains supported. Neither mode implements photo capture or camera tilt controls.

## Approved ground return

A confirmed `come_home` for a selected ground node can dispatch an approved return only when the relay is configured with `SWEEP_GROUND_RETURN_ID`. The node requires `SWEEP_RETURN_APPROVAL_FILE`, `SWEEP_RETURN_APPROVAL_KEY_FILE`, and `SWEEP_ODOM_ORIGIN_ID`; it refuses the command when any is absent.

The separate external approval binds the session, device ID, connection epoch, odometry origin, source registration, pose source, frame, exact measured geometry bytes, and world-to-odometry transform. Its fixed corridor footprints must be simple and provide clearance for the full robot, stopping distance, and one forward pulse. Each turn and forward pulse rechecks the active external grant, qualified current pose, current full scan, clearance, and remaining approved chord. The controller never replans, reverses, or treats an acknowledgement as arrival. It completes only after a measured final pose satisfies the approved tolerance.

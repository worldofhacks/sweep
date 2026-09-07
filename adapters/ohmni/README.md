# Ohmni Android runtime

This package runs the ground adapter on the measured Ohmni Android 7.1 image. It ships a musl Python 3.12 runtime, a pure-Python `websockets` wheel, the Ohmni adapter, and the small relay/planner contract set it imports. The robot does not run Docker, `pip`, or a host Python interpreter.

`run.sh` starts `./lib/ld-musl-x86_64.so.1 ./python/bin/python3.12 -m adapters.ohmni`. It reads a mode-600 `node.env`, records a PID, verifies that PID before stopping it, and gives the runtime time to issue its local stop writes. `install.sh` transfers a plain tar with `adb` and extracts it with Toybox. It does not start the adapter or enable motion.

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
SWEEP_LIDAR_MOUNT_X_M=0.00
SWEEP_LIDAR_MOUNT_Y_M=0.00
SWEEP_LIDAR_MOUNT_Z_M=0.25
SWEEP_LIDAR_MOUNT_YAW_DEG=0.00
```

The lidar transform values are measurements. They must not be copied from this example. Start only after the qualification checks below:

```sh
adb -s "$ADB_SERIAL" shell /data/local/sweep/run.sh start
adb -s "$ADB_SERIAL" shell /data/local/sweep/run.sh stop
```

## Approved ground return

A confirmed `come_home` for a selected ground node is eligible only when the relay has `SWEEP_GROUND_RETURN_ID` and the node has a matching approved return artifact. The command carries only that ID. It does not carry a route, a destination, or a way to alter the route.

The node reads `SWEEP_RETURN_APPROVAL_FILE` and verifies it using the separate `SWEEP_RETURN_APPROVAL_KEY_FILE`. The record is signed by the external approval authority and contains the SHA-256 of the exact base64-encoded measured geometry bytes, the source-registration ID, source/frame binding, and measured `world_to_odom` transform. Its geometry is a `measured_corridor_v1` object with one fixed start and an ordered set of fixed segment targets and footprint polygons. The source-registration ID must equal the transform registration ID.

At command admission, the controller binds the current connection epoch and checks the configured pose source and odometry frame. A confirmed start begins at the fixed start; a confirmed resume begins only inside one of the same pinned segment footprints. Before every turn and every forward pulse it requires a current external relay grant, a qualified pose in that epoch, and a current 360-degree scan aligned with that pose. Every scan bin must clear the configured robot footprint radius. It checks the current position against the segment footprint, turns in place, then drives forward. It never asks the planner for a new route and it never commands negative linear velocity. A lost grant, stale pose, changed epoch, changed source/frame binding, stale scan, incomplete footprint clearance, missing record, hash mismatch, or departure from the footprint stops and refuses the return.

The adapter acknowledges completion after the final pose is inside the approved arrival tolerance. `accepted` and `executing` acknowledgements do not complete a return. A local stop remains in effect after arrival; no return outcome clears an estop latch or reenables a stopped robot.

## Qualification record

Before any hardware deployment or motion, record the following alongside the device/session evidence:

- review the exact artifact manifest and verify the produced payload contains the musl loader, Python, runtime modules, and no credentials;
- verify the local STOP, relay HOLD, relay ESTOP, link loss, heartbeat expiry, lost odometry, stale lidar, and spotter withdrawal all stop the robot;
- measure and record the lidar mounting transform and angle convention for the installed kit;
- verify the return approval signature, raw geometry SHA-256, source-registration ID, `world_to_odom` measurement, pose source, and frame against the approved map record;
- verify a supervised return on the marked corridor with raw pose, scan, command, acknowledgement, and camera evidence;
- confirm that the final completion follows a measured arrival pose and that no completion is emitted when the artifact, grant, pose, scan, or footprint check is unavailable.

No command in this package deploys to a robot or moves it.

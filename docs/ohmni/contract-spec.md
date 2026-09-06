# Device classes, sensor frames, mapping, and the node kit: the contract for issue #239

This is the pinned design every track implements. Names here are final; if a track finds a name impossible, it keeps the semantics and reports the rename in its result. Read `relay-map.md` and `console-map.md` beside this file for exact file and line anchors on main (cf73068).

## 0. Principles

- Wire compatibility with the Android bridge: no existing frame gains or loses a required key. New information rides in existing string lists or in new frame types. Fixtures under `adapters/dji_mini3/pilot-app/bridge-core/src/test/resources/vectors/` change only by adding vectors.
- Device ids stay positive integers in one shared id space. Ground robots use ids that do not collide with aircraft (the demo uses 1..4 for aircraft, 11..13 for ground vehicles).
- Everything a ground vehicle does goes through the same relay, planner, arbiter, and console paths. No input or model calls an adapter directly.
- Lidar is a display feed in this issue. The arbiter does not consume scans.

## 1. Device class

`planner/models.py` gains:

```python
class DeviceClass(StrEnum):
    AIRCRAFT = "aircraft"
    GROUND_VEHICLE = "ground_vehicle"
```

The relay learns a device's class from configuration and from its join:

- `SWEEP_DEVICE_CLASSES_JSON` (new env, optional, default `{}`): object keyed by canonical device id string → class name. Ids absent from it are `aircraft`. Every id present must also be present in `SWEEP_ADAPTER_KEYS_JSON` (settings validation, `device_class_without_key`). Add it to `.env.example` and `tests/test_env_example.py`.
- The signed `join` carries the class as a capability string `class:ground_vehicle` (or `class:aircraft`; absent means aircraft). More than one `class:` entry, or a class that differs from the configured one, refuses the join with reason `device_class_mismatch`.
- `unit`: the 1-based ordinal of the device among the configured ids of its class, sorted ascending (aircraft with no configuration: unit = position among the ids in `SWEEP_ADAPTER_KEYS_JSON` of class aircraft, sorted; with no keys at all in `sim` backend, unit = id). `unit` is stable across reconnects and is what labels and media paths use.

Projection: every per-device state object (`_aircraft_state` in `relay/state.py`) gains `device_class` and `unit`. The audit projector `_DRONE_STATE_KEYS` includes both (material, not volatile).

Labels: aircraft `D-{unit:02d}`, ground vehicles `G-{unit:02d}`. `language/relay_compiler.py _label` and the console's `formatDeviceId` produce these; `formatDroneId(id)` stays as an alias that formats aircraft only.

Capacity: `MAX_PHYSICAL_AIRCRAFT` becomes `MAX_PHYSICAL_DEVICES: dict[DeviceClass, int] = {AIRCRAFT: 4, GROUND_VEHICLE: 4}` enforced per class in `apply_join` (`fleet_capacity` reason unchanged). The audit bound in `_material_drones_projection` uses the sum.

## 2. Capability strings per class (the `capabilities` list in `join`)

- aircraft: `flight` (required for readiness), `pano_360`, `reconstruct_8` (bare or `camera:` namespaced as today).
- ground_vehicle: `class:ground_vehicle`, `ground_drive` (required for readiness; missing → `drive_capability_missing`), optional `lidar`, `camera`, `neck`, `speech`, `lights`, `screen`.

Readiness reasons per class (`_readiness_reasons`): the `flight_capability_missing` gate applies to aircraft only; `drive_capability_missing` to ground vehicles only. All other gates stay (identity, capabilities present, telemetry present and fresh, home pose, control authority, `rc_safety_operator_missing`). For ground vehicles `rc_safety_operator_present` means a spotter is beside the robot with the screen STOP in reach; `control_authority` means the wheels are enabled and no local override is active. The README states this.

Home pose: captured from telemetry on `readiness` with `home_pose_confirmed` for both classes. `_is_grounded` is class-aware: aircraft as today; ground vehicles are grounded when `state in {"docked","idle","stopped"}`.

## 3. Telemetry for ground vehicles

Telemetry v1 keeps its exact keys. Ground vehicles send `z = 0.0`, `vz = 0.0`, and `state` from the drive vocabulary:

```
docked | idle | moving | stopped | fault
```

`planner/models.py` gains `DriveState(StrEnum)` with those values. `AircraftState` (kept name) gains `device_class: DeviceClass` and `drive_state: DriveState | None`; `flight_state` is `None` for ground vehicles. `relay/autonomy.py relay_snapshot` maps by class instead of silently excluding non-FlightState states. New property `AircraftState.mobile`: aircraft → `airborne`; ground vehicles → `drive_state in {idle, moving, stopped}` (docked and fault are not mobile).

Position source for ground vehicles is wheel odometry in a frame anchored at the robot's launch spot with `pos_quality` from the node (0.0 when odometry is absent, 0.6 when only wheel odometry is available, higher when corrected by an external observer later).

## 4. Sensor frame (node → relay, unsigned like telemetry)

New node frame type `sensor`, added to `NODE_FRAME_TYPES`, parsed by `parse_sensor` in `relay/contracts.py` (`SensorFrame` dataclass), retained per device by `FleetRegistry.apply_sensor`, fanned out to console subscribers as event type `sensor`.

```json
{"v":1,"t":1720000000000,"type":"sensor","event_id":"...","session":"sweep-6",
 "drone_id":11,"connection_epoch":3,"kind":"lidar_scan",
 "pose":{"x":1.20,"y":-0.40,"yaw_deg":87.5},
 "angle_min_deg":0.0,"angle_increment_deg":1.0,
 "range_min_m":0.15,"range_max_m":12.0,
 "ranges_cm":[0,152,151,0,...]}
```

Rules:
- `kind` is exactly `lidar_scan` in this issue (bounded enum for later kinds).
- `pose` is the device's pose at scan time in the same frame as its telemetry; `yaw_deg` in [0, 360), counter-clockwise from +x, and the scan's angle 0 points along the device's forward axis; angles increase counter-clockwise.
- `ranges_cm`: integers 0..65535, 0 = no return; length = 360 / `angle_increment_deg` exactly; `angle_increment_deg` ∈ {0.5, 1.0, 2.0}; maximum 720 entries; canonical JSON ≤ 8192 bytes.
- Rate: the relay accepts at most 5 frames per second per device; faster frames are dropped silently (a counter in `/metrics`, `sensor_frames_dropped_total`), not refused.
- Audit: the sensor frame is NOT appended in full. `process_node_frame` appends a digest `{"type":"sensor_digest","drone_id","connection_epoch","t","kind","count","valid","min_cm","max_cm"}` at most once per `SWEEP_AUDIT_STATE_INTERVAL_MS` per device; the state projection is not touched (no state audit).
- Fan-out event to consoles: the frame as received plus nothing else (type `sensor`). Nodes never receive sensor events.
- The device's projected state gains `sensor: {"kind":"lidar_scan","last_scan_at": int|null}` (last_scan_at volatile in the audit projector, kind material), mirroring `video`.

## 5. Occupancy map (relay-side)

`relay/mapping.py`: `OccupancyGrid` per session, resolution 0.05 m, extent from the safety geofence `min_x..max_x, min_y..max_y` expanded by 2.0 m on each side (fallback ±10 m when no geofence), log-odds cells (int8), Bresenham ray casting from `pose` through each valid range; hits within `range_max_m` mark occupied, the ray before them free; zero ranges are skipped. Update on every accepted sensor frame; O(points × cells along ray) with numpy.

Endpoint: `GET /api/sessions/{session_id}/map` (bearer token like `/runtime-config.json`), returns `image/png` 8-bit grayscale (0 occupied, 255 free, 128 unknown) with headers `X-Sweep-Map-Resolution-M`, `X-Sweep-Map-Origin-X`, `X-Sweep-Map-Origin-Y` (world coordinates of the bottom-left cell corner), `X-Sweep-Map-Width`, `X-Sweep-Map-Height`, `X-Sweep-Map-Updated-At` (ms). The image row 0 is the TOP (max y); the console flips accordingly. Encode with `cv2.imencode` (opencv is already a dependency). `POST /api/sessions/{session_id}/map/reset` clears the grid (console button, audited as `map_reset`). The grid is in-memory only.

## 6. Planner and arbiter per class

- Formations, `translate`, `come_home`, `hold`, `estop`, `spacing`, `select`, `arm`, `disarm` work for ground vehicles on the floor plane (targets have z = 0; `translation_frame = aircraft_relative` uses the ground vehicle's heading).
- `takeoff`, `land`, `land_all`, `altitude`, `sweep`, `capture_room`, `survey_area`, `map_area` targeted at a ground vehicle are refused with the new `RefusalReason.UNSUPPORTED_FOR_DEVICE_CLASS` (`unsupported_for_device_class`), with `detail` naming the class. `land_all` skips ground vehicles rather than refusing when the selection is mixed, and refuses only if the roster has no aircraft.
- Arbiter: ceiling and geofence z checks skip ground vehicles; the x/y geofence applies. Spacing applies within a class only (aircraft to aircraft, ground to ground); cross-class spacing is not checked. Airborne gates use `mobile` for ground vehicles. Battery, link, telemetry freshness, position quality gates apply unchanged.
- `PlanningConfig` gains `drive_speed_m_s: float = 0.3` (ground `goto` speed) and `drive_rotate_speed_deg_s: float = 45.0`; add both to the demo `.env` planning JSON docs. `SafetyConfig` gains `ground_max_speed_m_s: float = 0.5` (arbiter refuses a ground goto faster than this with `SPEED_LIMIT`).
- Commands to ground nodes reuse the existing operations: `goto {x_mm, y_mm, z_mm: 0, speed_mm_s}`, `rotate_to {yaw_mdeg, speed_mdeg_s}`, `hover {}` (stop and hold position), `estop {}`. No new operation.
- Capability profile: session-wide `c1_basic_control` stays; the console derives per-device availability from `device_class`. No new profile in this issue.

## 7. Media paths for ground vehicles

- `relay/media.py stream_name` takes `(device_class, unit)`: aircraft `drone{unit}`, ground `ground{unit}`. The monitor polls every configured device (from settings), not a fixed tuple.
- `media/mediamtx.yml`: accounts `ground1..ground3` (publish only their path), reader regex `~^(drone[1-4]|ground[1-3])$`. `docker-compose.yml`: `SWEEP_MEDIA_GROUND1..3_PASSWORD` mapped to the next `MTX_AUTHINTERNALUSERS_n` indices after the existing six (keep positional order consistent with the yml). `media/README.md` documents derivation `sweep-media-publish-v1:ground1` with the device's adapter key.
- `.env.example` and `tests/test_env_example.py` gain the three variables.

## 8. Node kit (`nodekit/`, Python ≥ 3.9, depends only on `websockets`)

Package layout: `nodekit/__init__.py`, `nodekit/protocol.py` (frame builders and parsers with signing; pure functions), `nodekit/node.py` (`Node` runtime), `nodekit/device.py` (`Device` protocol and `DeviceStatus`, `Scan`), `nodekit/fake.py` (in-memory device used by the rebuilt fake node), `nodekit/cli.py`.

```python
@dataclass
class DeviceStatus:
    x: float; y: float; z: float; yaw_deg: float
    vx: float; vy: float; vz: float
    battery: float; link: float; pos_quality: float
    state: str                      # FlightState or DriveState value
    control_authority: bool
    extras: dict[str, object] = field(default_factory=dict)   # battery_voltage, docked, ...

@dataclass
class Scan:
    t_ms: int; pose: tuple[float, float, float]   # x, y, yaw_deg
    angle_min_deg: float; angle_increment_deg: float
    range_min_m: float; range_max_m: float
    ranges_cm: list[int]

class Device(Protocol):
    device_class: str               # "aircraft" | "ground_vehicle"
    capabilities: list[str]         # without the class: entry; the kit adds it
    def status(self) -> DeviceStatus: ...
    def stop(self) -> None: ...                       # hold in place, keep enabled
    def disable(self) -> None: ...                    # failsafe: wheels off / motors safe
    def enable(self) -> bool: ...                     # returns control authority
    def move_to(self, x_m: float, y_m: float, z_m: float, speed_m_s: float) -> str: ...   # returns a motion id
    def rotate_to(self, yaw_deg: float, speed_deg_s: float) -> str: ...
    def motion_done(self, motion_id: str) -> bool | None: ...   # True done, False running, None failed
    def latest_scan(self) -> Scan | None: ...         # optional, return None when no lidar
    def hardware_profile(self) -> dict[str, object]: ...   # fills the capabilities frame fields
```

`Node(config, device)` behaviour:
- Connect `ws(s)://relay/ws/{session}`, send `auth` with the device key from `SWEEP_NODE_KEY` (env) or a file path; on `auth.accepted` read `node` settings (ttl, watchdog hold and failsafe).
- Send signed `join` with `adapter_id`, `capabilities` = device capabilities + `class:<device_class>`; on the relay's join echo set `connection_epoch`, then telemetry at `telemetry_hz` (10), signed `readiness` (claims from config: `home_pose_confirmed`, `control_authority` from `device.enable()`, `rc_safety_operator_present` from config or a runtime toggle), `capabilities` frame, `node_status`, and sensor frames at ≤ 5 Hz when `latest_scan()` returns new scans.
- Command admission exactly as the fake node: signature with the device key, session and id, epoch, roster version, ttl, strictly increasing seq; refusals as acknowledgements with `NodeAcknowledgementReason`. Execution: `goto` → `move_to`, `rotate_to` → `rotate_to`, `hover` → `stop` (completed immediately), `estop` → `stop` then `disable` (completed), `takeoff`/`land`/camera ops → `failed` with reason `unsupported_operation` (add to `NodeAcknowledgementReason`; the relay accepts it as a snake_case reason already). `executing` acknowledgement first, then `completed`/`failed` when `motion_done` resolves or after `command_ttl_ms`.
- Watchdog: no `control_heartbeat` for `watchdog_hold_ms` → `stop()` and `watchdog_state=hold`; for `watchdog_failsafe_ms` → `disable()` and `failsafe`; a fresh heartbeat after hold returns to nominal; after failsafe the node stays failsafe until the operator re-enables (config flag or local action) and rejoins.
- Reconnect with exponential backoff (0.5 s → 8 s) on socket loss; `disable()` immediately on loss beyond hold.
- `node_status` frame: `virtual_stick_enabled=false` for ground vehicles, `phone_battery_percent` = device battery ×100, `phone_thermal_state="none"`, `video_publish_state` from the camera publisher.
- Vectors: `nodekit/vectors.py` generates `nodekit/tests/vectors/*.json` from relay code (reusing `adapters/dji_mini3/vectors.py` helpers) and a test asserts they are current; `adapters/dji_mini3/fake_node.py` becomes a thin wrapper over `Node` + `nodekit.fake.FakeAircraft` with the same CLI and `FakeNodeConfig` (existing tests keep passing).

## 9. Ohmni node (`adapters/ohmni/`)

- `adapters/ohmni/device.py OhmniDevice(Device)`: botshell client (`/app/bot_shell.sock`), drive through `manual_move` with a 250 ms re-issue loop and hard stop on silence, `pre_drive`/`pre_rot` for `move_to`/`rotate_to` when odometry is absent, wheel odometry from the ROS bridge when available (`adapters/ohmni/ros_bridge.py` subscribes `/tb_control/wheel_odom` over rosbridge or a tiny TCP feed from a rospy script in the ROS container; pick the path the spike proved), `battery` polling for `battery`, `docked` → `state`, `start_collision_detection` on enable, `sleep` on disable.
- `adapters/ohmni/lidar.py`: RPLIDAR A2M8 standard scan reader (from the spike's `rplidar_protocol.py`), 1° bins (360 entries), latest scan with the pose at scan time.
- `adapters/ohmni/camera.py`: `/dev/libcamera_stream` reader (from the spike's `ohmnicam.py`) piped into `ffmpeg` publishing RTSP to `rtsp://<media host>:8554/ground{unit}` with the derived password; `video_publish_state` from the process state. WHIP from the standalone page is the alternative if the spike proves it.
- `adapters/ohmni/screen/index.html`: standalone WebAPI page served by the node on `http://127.0.0.1:8765/` (device id, link, readiness, last refusal, full-screen STOP that hits the node's `/stop` which disables the wheels regardless of the relay; a `Spotter present` toggle that feeds `rc_safety_operator_present`).
- `adapters/ohmni/Dockerfile` (python:3.11-slim, ffmpeg, the kit) and `adapters/ohmni/run.sh` (`docker-ohmnirun` flags: `--network host --privileged -v /dev:/dev -v /data/data/com.ohmnilabs.telebot_rtc/files:/app`); env: `SWEEP_RELAY_URL`, `SWEEP_SESSION_ID`, `SWEEP_DEVICE_ID`, `SWEEP_NODE_KEY` (entered by a person on the robot), `SWEEP_MEDIA_HOST`.
- README: install on three robots, keys, media passwords, the S3 checklist.

## 10. Console

- `RelayAircraftState` gains `device_class: 'aircraft' | 'ground_vehicle'` and `unit: number` (validated; absent → aircraft, unit = drone_id for old relays) and `sensor?: { kind: 'lidar_scan'; last_scan_at: number | null }`.
- New `RelaySensorEvent` (the frame as in §4 with `type: 'sensor'`) in `RelayServerEvent`; `parseRelayServerEvent` validates lengths and bounds; scans live in a `useSensorStore` (latest scan per device, a ring of the last 20 for trails) outside the control reducer.
- `formatDeviceId(device)` → `D-01` / `G-01`; `deviceNoun(class)` → `aircraft` / `robot`; sentences and control copy use them; `formatDroneId` kept as alias.
- Control module: per selected device class the buttons for takeoff, land, land all, altitude, sweep, capture room show `unsupported` with note "Not available for robots." when every selected device is a ground vehicle; mixed selections keep them enabled (relay decides).
- Live module: a `Ground` pane with tiles for ground vehicles (`ground{unit}` streams) beside the existing panes; `streamName(device)` by class and unit; `createPlaybackDescriptor` accepts any positive unit.
- Map: `Reference › Map` tab shows `FleetMap` (one canvas: occupancy raster from `GET /api/sessions/{id}/map` polled at 1 Hz while visible, geofence box, each device's pose as a triangle with heading, each ground vehicle's latest scan as dots in its own colour, trails), with a `Reset map` button. A `LidarPolar` component (small canvas) on each ground vehicle's registry card and in the Devices module.
- Devices module (`devices`): expected devices from configuration (via a new relay event? no: from `state.drones` plus `departed`), connected devices with class, unit, capabilities, link, readiness reasons, video and sensor state, last refusal; a copyable node configuration block (relay URL, session, device id) with the key never displayed; instructions to enter the key on the device.
- Fixtures: `fixture-relay-client.ts` gains a `mixed` scenario with 2 aircraft + 3 ground vehicles, synthetic scans (a room-shaped scan), and a map PNG stub.

## 11. Evidence and tests

- Relay: unit tests for `parse_sensor` bounds, rate limiting, digest audit, device class from config and join, per-class readiness, `unit` computation, map endpoint (PNG decodes, headers), planner refusals per class, arbiter class-aware geofence/spacing/speed, mixed-session snapshot.
- Kit: vectors current; `FakeNode` roundtrip tests still pass; a ground fake device roundtrip through the in-process relay (join, ready, goto, rotate, hover, estop, sensor fan-out, watchdog hold and failsafe).
- Console: contract parse tests, labels, control gating per class, ground tiles, map canvas smoke test (jsdom: assert draw calls), Devices module render, shell rail count.
- Ohmni: parser tests from the spike; the hardware checklist in the README records the S3 results.

## 12. S0 spike results (measured on the three robots; binding for the Ohmni node)

- Robots: UP-CHT01 boards (Intel Atom x5-Z8350, 4 cores, 1.4 GB RAM, ~11 GB free on /data), Ohmni OS = Android 7.1.2 userdebug (`ohmni_up`), telebot app 4.1.4.4. Root: `adb root` works (adbd restarts as root) and `su` works. IPs on the lab Wi-Fi: 10.10.1.110, 10.10.0.74, 10.10.3.160. SELinux permissive.
- There is NO Docker on these robots (`docker`, `dockerenv` absent). The node runs directly on Android as root from `/data/local/sweep/`: `python/` = python-build-standalone 3.12 x86_64 musl `install_only_stripped` (dynamically linked against musl) started through the Alpine musl loader placed at `/data/local/sweep/lib/ld-musl-x86_64.so.1`: run as `/data/local/sweep/lib/ld-musl-x86_64.so.1 /data/local/sweep/python/bin/python3.12 <script>`. `ffmpeg` = johnvansickle static build 7.0.2 at `/data/local/sweep/ffmpeg` (the robot's own /system/bin/ffmpeg is from 2017 and has no v4l2 input). The Ohmni node's install script must push these three artifacts (Python tar, loader, ffmpeg) with `adb push` and extract with `toybox tar xf` (busybox's gzip path crashes); no Dockerfile.
- Bot shell: UNIX stream socket `/data/data/com.ohmnilabs.telebot_rtc/files/bot_shell.sock` (root or system uid). Newline-terminated commands; replies are newline-terminated text lines that can interleave between commands, so parse by content (`apos 0 = 16080`, `Last battery: [ 3269, 3282, 3305, 3285, 3259 ]`, `Last docked: 0`). First-byte latency 6 to 20 ms, `apos` median 14 ms, max 97 ms. From the laptop the socket can be reached with `adb forward tcp:<port> localfilesystem:<socket path>` after `adb root`.
- Drive commands (from the vendor JavaScript): `pre_drive <mm> <speed>` (positive forward), `pre_rot <deg> <speed>` (positive = turn right), `manual_move <l> <r>` sets the two wheel goals directly and does NOT stop by itself (`manual_move 0 0` clears it); wheel goal sign: forward = left negative, right positive (`add_ptarg(0, -ticks)`, `add_ptarg(1, +ticks)`); `init` wakes wheels, `sleep` rests them. Geometry: wheel diameter 150.5 mm (150.21 in the control model), base diameter 330 to 332 mm, gear train 30/11, 16384 ticks per motor revolution, so one wheel revolution = 44683 ticks and 1 mm = 94.5 ticks. `apos <sid>` (sid 0 left, 1 right) returns the 14-bit motor position (wraps every 16384 ticks = 174 mm of wheel travel), so odometry from `apos` needs polling at 10 Hz or more with unwrapping; commanded `pre_drive`/`pre_rot` distances are the fallback dead reckoning. The vendor `speed` argument scale for pre_drive/pre_rot and the `manual_move` goal units are still unmeasured (the drive test measures them; start at speed 10 and goals of ±200).
- Battery: `battery` returns five cell voltages in mV (pack ≈ 16.3 V full); `voltage 0` returns the full battery record. `Last docked: 0|1`.
- Lidar: present only on the robot at 10.10.0.74 (the RPLIDAR A2M8 on a CP2102 USB-serial at `/dev/ttyUSB0`, alias `/dev/usb/tty1-7.1`; on the other two robots `/dev/ttyUSB0` is the FT230X wheel bus, so the node must identify the port by USB vendor id 10c4:ea60 under /sys, never by name). 115200 8N1. Send `lidar_stop` and `lidar_release` over the bot shell before opening the port. The motor runs only while DTR is CLEARED (ioctl TIOCMBIC TIOCM_DTR) plus SET_MOTOR_PWM 660; set DTR and PWM 0 on exit. GET_INFO: model 0x28, firmware 1.25, hardware 5; health good. Standard scan (A5 20) yields about 200 points per revolution at roughly 7 Hz; 60% valid returns indoors. After RESET the device prints a text boot banner and needs about 2 s before commands.
- Camera: `/dev/video0` = See3CAM_CU135 UVC (UYVY 640x480 at 30/60 fps, 1280x720 at 16 fps; MJPG 1280x720 at 60 fps); `/dev/video1` = a second "HD USB Camera" (the down camera). The telebot process keeps /dev/video0 open but ffmpeg can still capture. Static ffmpeg `-f v4l2 -input_format uyvy422 -video_size 640x480 -framerate 30 -i /dev/video0 -r 15 -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -b:v 800k` runs at a steady 15 fps using 24% of one core (utime 2.25 s per 10 s). Publish with `-f rtsp rtsp://ground<unit>:<password>@<media host>:8554/ground<unit>`. `/dev/dri/renderD128` exists but VAAPI is unverified; do not depend on it.
- The vendor runtime holds the wheel bus; our node talks only through the bot shell, never to `/dev/ttyUSB*` of the FT230X.

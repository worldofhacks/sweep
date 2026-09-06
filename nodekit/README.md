# nodekit

Capability area: Platform. Plan: F.2 (extend vehicle portability).

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to the node protocol name one change owner and require cross-review, because the Android bridge and every deployed node speak it.

The reference node: everything a vehicle needs to become a device on a relay session, so a new device class costs one file instead of a protocol change. `adapters/dji_mini3/fake_node.py` is this kit plus a Mini 3's operations; `adapters/ohmni/` packages this kit with the measured Android robot runtime.

The package depends only on `websockets` and stays inside Python 3.9 syntax (no `match`, no `StrEnum`, no `slots=True` dataclasses), so it can be copied onto a robot on its own and run there. `nodekit/tests/test_protocol.py` parses every module at that language level and fails when something newer slips in.

| Module | What it owns |
|---|---|
| `protocol.py` | Canonical JSON, HMAC signing, frame builders and parsers. Pure functions, no I/O |
| `device.py` | `Device`, `DeviceStatus`, and `Scan`: what a vehicle implements |
| `node.py` | `Node`: the socket, the join, telemetry, scans, command admission, the deadman, reconnection |
| `fake.py` | `FakeAircraft` and `FakeGroundVehicle`, the in-memory devices tests and wiring checks run against |
| `cli.py` | `python -m nodekit.cli`: run one device against a relay |
| `vectors.py` | Regenerates `tests/vectors/` from the relay code (`uv run python -m nodekit.vectors`). A development tool: it imports the relay and never runs on the device |

## What a device implements

```python
class Device(Protocol):
    device_class: str          # "aircraft" or "ground_vehicle"
    capabilities: Sequence[str]  # without the class: entry; the kit adds it
    def status(self) -> DeviceStatus: ...
    def stop(self) -> None: ...      # hold in place, stay enabled
    def disable(self) -> None: ...   # failsafe: wheels off, motors safe
    def enable(self) -> bool: ...    # returns whether control authority was granted
    def move_to(self, x_m, y_m, z_m, speed_m_s) -> str: ...   # returns a motion id
    def rotate_to(self, yaw_deg, speed_deg_s) -> str: ...
    def motion_done(self, motion_id) -> bool | None: ...  # True done, False running, None failed
    def latest_scan(self) -> Scan | None: ...      # None when there is no scanning sensor
    def hardware_profile(self) -> dict[str, object]: ...   # capabilities-frame fields
```

Two members are optional and read with `getattr`: `video_publish_state()` returns `stopped`, `connecting`, `publishing`, or `failed` for the camera publisher, and `close()` releases hardware when the node stops. `status()` is called at the telemetry rate and must not block.

`capabilities` is the device's own claims. A ground vehicle must claim `ground_drive` and an aircraft `flight` before the relay calls it ready; `lidar`, `camera`, `neck`, `speech`, `lights`, and `screen` are optional and drive what the console offers. The kit appends exactly one `class:<device_class>` entry, which is how the join declares its class without any frame gaining a key (`relay/README.md`).

## What the node does

- **Clock and source order**: anchor to `auth.accepted.t` plus monotonic elapsed time; strictly increase outbound timestamps with at most 500 ms synthetic lead. Build and queue frames together on the event loop, including ACKs and local-screen updates.
- **Authenticate** as an adapter with the device's own key, then read the relay's `node` settings from `auth.accepted` (command TTL, watchdog hold and failsafe). The relay's thresholds always replace the configured ones; a node never invents its own.
- **Join** with a signed membership frame carrying `adapter_id` and the capability list. On the relay's join echo it takes the connection epoch, then sends telemetry (so the relay can capture a home pose), signed readiness (`home_pose_confirmed` and the safety-operator claim from configuration, `control_authority` from `device.enable()`), a `capabilities` frame from the hardware profile, and `node_status`. `Node.set_safety_operator_present` re-signs and re-sends that claim while the node runs, which is what a robot's screen toggle calls when the spotter steps away; it is safe to call from another thread.
- **Stream** telemetry at `telemetry_hz` (10 by default) and scans at `sensor_hz` (at most 5; the relay drops faster frames). A scan goes out only when `latest_scan()` returns a new one, and a scan whose bin count does not match its increment is dropped locally rather than refused on the wire.
- **Admit commands** by signature, session, device, epoch, roster version, TTL, and a strictly increasing sequence. A command for another session or device, or one whose signature does not verify, is dropped without an acknowledgement; everything else is refused as `stale_command` or `out_of_order_command`. An admitted command is acknowledged `accepted`, then `executing`, then `completed` or `failed`.
- **Execute** `goto` through `move_to`, `rotate_to` through `rotate_to`, `hover` through `stop()`, and `estop` through `stop()` then `disable()`. Every other operation fails with `unsupported_operation` naming the class, unless the node overrides `supported_operations`. A motion that outlives the command TTL stops the device and fails with `motion_timeout`; a motion the device abandons fails with `motion_failed`.
- **Keep a deadman**. Only a verified, current control heartbeat is liveness evidence. `watchdog_hold_ms` without one stops the device and reports `hold`; new motion is refused until a fresh heartbeat recovers the lease. `watchdog_failsafe_ms` stops and disables the device and latches `failsafe` across reconnect. `Node.request_reenable()` requires a local operator and requests a fresh connection epoch; a later local STOP cancels an older queued re-enable. Hover and estop remain available under both watchdog states. They execute before another inbound command can supersede them. STOP and disable are attempted independently even if hardware I/O fails.
- **Reconnect** with exponential backoff from 0.5 s to 8 s, except after an auth refusal that means "do not come back" (`session_closed`, `authentication_failed`, `invalid_auth`, `unknown_source`). The deadman keeps counting across the gap.
- **Stop cleanly**: cancel in-flight commands, independently stop and disable the device, and close. A graceful-leave frame is off by default, because an unannounced disappearance is the honest report of a node that was killed.

## Extending it

A node that speaks more of the command set than a generic vehicle does overrides `supported_operations` and `start_operation`, adds join-time frames with `extra_join_frames`, and (for fixtures) swallows or delays acknowledgements with `drop_command` and `completion_delay_s`. `adapters/dji_mini3/fake_node.py` is the worked example: it adds takeoff, land, the gimbal, capture, and media retrieval, and leaves the rest to the kit.

## Running one

```
just node                                  # a fixture ground vehicle against a local relay
just node 11 ground demo ws://127.0.0.1:8000
uv run python -m nodekit.cli --device-id 11 --device mypackage.device:build
```

`--device` is `ground`, `aircraft`, or a `module:factory` path that is imported and called with no arguments and must return a `Device`. The key comes from `SWEEP_NODE_KEY` or `--key-file`; it is entered on the device by a person and is never printed or logged. `SWEEP_RELAY_URL`, `SWEEP_SESSION_ID`, and `SWEEP_DEVICE_ID` fill in the rest. The fixture devices are for wiring checks and tests; a demo runs the real device.

## Why the vectors exist

The kit may not import `relay` at runtime, so it carries a second implementation of the canonical JSON, the HMAC, and every frame shape. `nodekit/vectors.py` renders `tests/vectors/*.json` **from the relay's own contracts and signer**, reusing `adapters/dji_mini3/vectors.py` wherever the shape is shared, and `tests/test_vectors.py` holds the kit to those bytes in both directions: the kit's builders reproduce the relay's frames, and the relay's parsers accept the kit's. Refresh them with `uv run python -m nodekit.vectors` whenever a contract changes; a stale file fails the suite.

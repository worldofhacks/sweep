from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from planner.models import CommandOperation
from relay.auth import sign_event, verify_event_signature
from relay.contracts import CommandFrame, ContractError, parse_command
from relay.observations import ObservationSubmission

from .models import GroundStatus, RangeScan

_LOGGER = logging.getLogger(__name__)


class GroundDevice(Protocol):
    capabilities: object

    def enable(self) -> bool: ...
    def stop(self) -> None: ...
    def disable(self) -> None: ...
    def drive_velocity(
        self, velocity_m_s: float, yaw_rate_deg_s: float, duration_s: float
    ) -> str: ...
    def motion_done(self, identity: str) -> bool | None: ...
    def status(self) -> GroundStatus: ...
    def latest_scan(self) -> RangeScan | None: ...
    def video_publish_state(self) -> str: ...


@dataclass(frozen=True, slots=True)
class GroundRuntimeConfig:
    relay_url: str
    session: str
    device_id: int
    token: str
    adapter_id: str
    source_clock_id: str = "ohmni-monotonic"
    pose_source_id: str = "ohmni-pose"
    telemetry_source_id: str = "ohmni-telemetry"
    status_source_id: str = "ohmni-status"
    odom_frame: str = "odom"
    body_frame: str = "body"
    lidar_source_id: str = "ohmni-lidar"
    lidar_frame: str = "lidar"
    camera_source_id: str = "ohmni-camera"
    camera_frame: str = "camera"
    camera_calibration_id: str = "unconfigured"
    camera_width_px: int = 640
    camera_height_px: int = 480
    heartbeat_hold_ms: int = 2_000
    heartbeat_failsafe_ms: int = 10_000
    telemetry_hz: float = 5.0
    monotonic: Callable[[], float] = time.monotonic
    event_ids: Callable[[], str] = lambda: str(uuid.uuid4())

    def __post_init__(self) -> None:
        if self.device_id <= 0 or not self.token or not self.session or not self.adapter_id:
            raise ValueError("ground runtime requires a session, device identity, and key")
        if not 0 < self.telemetry_hz <= 10:
            raise ValueError("ground telemetry rate must be between 0 and 10 Hz")
        if not 0 < self.heartbeat_hold_ms < self.heartbeat_failsafe_ms:
            raise ValueError("ground heartbeat windows are invalid")
        if not 1 <= self.camera_width_px <= 16_384 or not 1 <= self.camera_height_px <= 16_384:
            raise ValueError("ground camera dimensions are invalid")


class OhmniRuntime:
    def __init__(self, config: GroundRuntimeConfig, device: GroundDevice) -> None:
        self.config = config
        self.device = device
        self._key = config.token.encode()
        self._epoch: int | None = None
        self._roster_version = 0
        self._last_command_seq = 0
        self._last_heartbeat_seq = 0
        self._last_heartbeat_at: float | None = None
        self._watchdog_state = "failsafe"
        self._ready = False
        self._pose_event_id: str | None = None
        self._outbound: asyncio.Queue[dict[str, object]] | None = None
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._failure: BaseException | None = None

    @property
    def connection_epoch(self) -> int | None:
        return self._epoch

    @property
    def watchdog_state(self) -> str:
        return self._watchdog_state

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._outbound = asyncio.Queue()
        try:
            async with connect(
                f"{self.config.relay_url.rstrip('/')}/ws/{self.config.session}"
            ) as socket:
                await socket.send(
                    json.dumps(
                        {
                            "v": 1,
                            "type": "auth",
                            "source": "adapter",
                            "drone_id": self.config.device_id,
                            "token": self.config.token,
                        }
                    )
                )
                accepted = json.loads(await socket.recv())
                if accepted.get("type") != "auth.accepted":
                    raise RuntimeError(
                        f"relay refused adapter authentication: {accepted.get('reason')}"
                    )
                initial = json.loads(await socket.recv())
                if initial.get("type") == "state":
                    self._roster_version = int(initial["roster_version"])
                self._enqueue(
                    self._membership(
                        "join",
                        adapter_id=self.config.adapter_id,
                        capabilities=list(self.device.capabilities),
                        node_type="ground",
                    )
                )
                self._started.set()
                tasks = [
                    asyncio.create_task(self._send(socket)),
                    asyncio.create_task(self._receive(socket)),
                    asyncio.create_task(self._watchdog()),
                    asyncio.create_task(self._stop.wait()),
                ]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if not self._stop.is_set():
                    for task in done:
                        if (error := task.exception()) is not None:
                            raise error
                    raise RuntimeError("relay closed the ground adapter socket")
        except (OSError, WebSocketException):
            self._local_stop("watchdog_failsafe", disable=True)
            raise
        finally:
            self._local_stop("watchdog_failsafe", disable=True)
            self._started.set()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("ground runtime is already running")

        def runner() -> None:
            try:
                asyncio.run(self.run())
            except BaseException as error:
                self._failure = error
                self._started.set()

        self._thread = threading.Thread(target=runner, name="ohmni-runtime", daemon=True)
        self._thread.start()
        if not self._started.wait(10):
            raise RuntimeError("ground runtime did not authenticate")
        if self._failure is not None:
            raise RuntimeError("ground runtime failed to start") from self._failure

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=10)

    async def _send(self, socket: object) -> None:
        assert self._outbound is not None
        while True:
            await socket.send(json.dumps(await self._outbound.get()))  # type: ignore[attr-defined]

    async def _receive(self, socket: object) -> None:
        async for raw in socket:  # type: ignore[attr-defined]
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            if frame.get("type") == "membership" and frame.get("drone_id") == self.config.device_id:
                self._on_membership(frame)
            elif frame.get("type") == "state":
                value = frame.get("roster_version")
                if isinstance(value, int):
                    self._roster_version = value
            elif frame.get("type") == "control_heartbeat":
                self._on_heartbeat(frame)
            elif frame.get("type") == "command":
                self._on_command(frame)

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            last = self._last_heartbeat_at
            if last is None:
                continue
            elapsed_ms = int((self.config.monotonic() - last) * 1_000)
            if (
                elapsed_ms >= self.config.heartbeat_failsafe_ms
                and self._watchdog_state != "failsafe"
            ):
                self._local_stop("watchdog_failsafe", disable=True)
            elif elapsed_ms >= self.config.heartbeat_hold_ms and self._watchdog_state == "nominal":
                self._local_stop("watchdog_hold", disable=False)

    def _on_membership(self, frame: Mapping[str, object]) -> None:
        if frame.get("action") != "join" or not isinstance(frame.get("connection_epoch"), int):
            return
        self._epoch = frame["connection_epoch"]
        self._roster_version = int(frame.get("roster_version", self._roster_version))
        self._last_command_seq = self._last_heartbeat_seq = 0
        self._last_heartbeat_at = None
        self._ready = False
        self._watchdog_state = "failsafe"
        self._local_stop("rejoin_requires_heartbeat", disable=True)
        self._publish_observations()
        self._publish_readiness()
        self._publish_status("rejoin_requires_heartbeat")

    def _on_heartbeat(self, frame: Mapping[str, object]) -> None:
        unsigned = {key: value for key, value in frame.items() if key != "signature"}
        if (
            frame.get("session") != self.config.session
            or frame.get("source") != "relay"
            or frame.get("drone_id") != self.config.device_id
            or frame.get("connection_epoch") != self._epoch
            or frame.get("roster_version") != self._roster_version
            or not isinstance(frame.get("seq"), int)
            or frame["seq"] <= self._last_heartbeat_seq
            or not verify_event_signature(unsigned, frame.get("signature"), self._key)
        ):
            return
        self._last_heartbeat_seq = frame["seq"]
        self._last_heartbeat_at = self.config.monotonic()
        if self._watchdog_state != "nominal":
            self._watchdog_state = "nominal"
            self._publish_observations()
            self._ready = self.device.enable() and self._pose_event_id is not None
            self._publish_readiness()
            self._publish_status(None)

    def _on_command(self, raw: Mapping[str, object]) -> None:
        try:
            command = parse_command(raw)
        except ContractError:
            return
        if command.session != self.config.session or command.drone_id != self.config.device_id:
            return
        if not verify_event_signature(command.unsigned_event(), command.signature, self._key):
            return
        failure = self._command_failure(command)
        if failure is not None:
            if (
                command.connection_epoch == self._epoch
                and command.roster_version == self._roster_version
                and command.seq > self._last_command_seq
            ):
                self._last_command_seq = command.seq
            self._enqueue(self._ack(command, "failed", *failure))
            return
        self._last_command_seq = command.seq
        self._enqueue(self._ack(command, "accepted"))
        if command.operation is CommandOperation.GROUND_VELOCITY:
            args = command.args
            try:
                motion = self.device.drive_velocity(
                    int(args["velocity_mm_s"]) / 1_000,
                    math.degrees(int(args["yaw_mrad_s"]) / 1_000),
                    int(args["duration_ms"]) / 1_000,
                )
            except (RuntimeError, ValueError) as error:
                self._enqueue(self._ack(command, "failed", "local_guard_refused", str(error)))
                return
            self._enqueue(self._ack(command, "executing"))
            assert self._loop is not None
            self._loop.create_task(self._complete_motion(command, motion))
            return
        if command.operation is CommandOperation.HOVER:
            self.device.stop()
            self._enqueue(self._ack(command, "executing"))
            self._enqueue(self._ack(command, "completed"))
            return
        if command.operation is CommandOperation.ESTOP:
            self._local_stop("local_estop", disable=True)
            self._enqueue(self._ack(command, "executing"))
            self._enqueue(self._ack(command, "completed"))
            return

    async def _complete_motion(self, command: CommandFrame, motion: str) -> None:
        while True:
            await asyncio.sleep(0.02)
            completed = self.device.motion_done(motion)
            if completed is False:
                continue
            if completed is True:
                self._enqueue(self._ack(command, "completed"))
            else:
                self._enqueue(
                    self._ack(command, "failed", "motion_failed", "ground motion stopped")
                )
            return

    def _command_failure(self, command: CommandFrame) -> tuple[str, str] | None:
        now_ms = int(time.time_ns() // 1_000_000)
        if (
            command.connection_epoch != self._epoch
            or command.roster_version != self._roster_version
        ):
            return "stale_command", "command identity is no longer current"
        if command.issued_at + command.ttl_ms < now_ms or command.seq <= self._last_command_seq:
            return "stale_command", "command is stale or replayed"
        if command.operation not in {
            CommandOperation.GROUND_VELOCITY,
            CommandOperation.HOVER,
            CommandOperation.ESTOP,
        }:
            return "unsupported_operation", "ground route is not qualified"
        if self._watchdog_state == "failsafe":
            return "watchdog_failsafe", "fresh verified heartbeat and readiness are required"
        if self._watchdog_state == "hold":
            return "watchdog_hold", "local deadman is holding the robot"
        if not self._ready:
            return "authority_lost", "ground readiness is not current"
        return None

    def _publish_observations(self) -> None:
        if self._epoch is None:
            return
        status = self.device.status()
        receipt = {
            "clock_id": self.config.source_clock_id,
            "unit": "ns",
            "value": time.monotonic_ns(),
        }
        pose_event = self._observation(
            self.config.pose_source_id,
            self.config.odom_frame,
            receipt,
            {
                "kind": "pose",
                "pose": {
                    "parent_frame": self.config.odom_frame,
                    "child_frame": self.config.body_frame,
                    "x_m": status.x,
                    "y_m": status.y,
                    "z_m": 0.0,
                    "qx": 0.0,
                    "qy": 0.0,
                    "qz": math.sin(math.radians(status.yaw_deg) / 2),
                    "qw": math.cos(math.radians(status.yaw_deg) / 2),
                },
            },
        )
        self._pose_event_id = pose_event["event_id"]
        self._enqueue(pose_event)
        self._enqueue(
            self._observation(
                self.config.telemetry_source_id,
                self.config.odom_frame,
                receipt,
                {
                    "kind": "telemetry",
                    "position": {
                        "frame": self.config.odom_frame,
                        "x_m": status.x,
                        "y_m": status.y,
                        "z_m": 0.0,
                    },
                    "velocity": {
                        "frame": self.config.odom_frame,
                        "x_m_s": status.vx,
                        "y_m_s": status.vy,
                        "z_m_s": 0.0,
                    },
                    "battery": status.battery,
                    "link": status.link,
                    "pos_quality": status.pos_quality,
                    "state": status.state,
                },
            )
        )
        self._enqueue(
            self._observation(
                self.config.status_source_id,
                self.config.odom_frame,
                receipt,
                {
                    "kind": "status",
                    "code": "ground_runtime",
                    "detail": status.state,
                    "capabilities": list(self.device.capabilities),
                },
            )
        )
        scan = self.device.latest_scan()
        if scan is not None:
            self._enqueue(
                self._observation(
                    self.config.lidar_source_id,
                    self.config.lidar_frame,
                    receipt,
                    {
                        "kind": "range_scan",
                        "sensor_pose": {
                            "parent_frame": self.config.odom_frame,
                            "child_frame": self.config.lidar_frame,
                            "x_m": scan.pose[0],
                            "y_m": scan.pose[1],
                            "z_m": 0.0,
                            "qx": 0.0,
                            "qy": 0.0,
                            "qz": math.sin(math.radians(scan.pose[2]) / 2),
                            "qw": math.cos(math.radians(scan.pose[2]) / 2),
                        },
                        "angle_min_rad": math.radians(scan.angle_min_deg),
                        "angle_increment_rad": math.radians(scan.angle_increment_deg),
                        "range_min_m": scan.range_min_m,
                        "range_max_m": scan.range_max_m,
                        "ranges_m": [None if item == 0 else item / 100 for item in scan.ranges_cm],
                        "mount_id": "ohmni-rplidar",
                    },
                )
            )
        if "camera" in self.device.capabilities:
            self._enqueue(
                self._observation(
                    self.config.camera_source_id,
                    self.config.camera_frame,
                    receipt,
                    {
                        "kind": "camera_frame",
                        "image_id": f"metadata-{self._epoch}-{self._pose_event_id}",
                        "sha256": "0" * 64,
                        "width_px": self.config.camera_width_px,
                        "height_px": self.config.camera_height_px,
                        "calibration_id": self.config.camera_calibration_id,
                    },
                )
            )

    def _observation(
        self,
        source_id: str,
        frame: str,
        receipt: Mapping[str, object],
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        assert self._epoch is not None
        raw = {
            "v": 1,
            "type": "observation",
            "event_id": self.config.event_ids(),
            "session": self.config.session,
            "device_id": self.config.device_id,
            "connection_epoch": self._epoch,
            "source_id": source_id,
            "node_type": "ground",
            "frame": frame,
            "confidence": 1.0,
            "t_capture": None,
            "t_source_receipt": dict(receipt),
            "clock_mapping_id": None,
            "payload": dict(payload),
        }
        return ObservationSubmission.parse(raw).to_mapping()

    def _publish_readiness(self) -> None:
        if self._epoch is None or self._pose_event_id is None:
            return
        status = self.device.status()
        self._enqueue(
            self._membership(
                "readiness",
                connection_epoch=self._epoch,
                drive_authority=self._ready and status.drive_authority,
                safety_operator_present=bool(status.extras.get("spotter_present", False)),
                local_stop_ready=True,
                heartbeat_ready=self._watchdog_state == "nominal",
                pose_identity={
                    "event_id": self._pose_event_id,
                    "session": self.config.session,
                    "connection_epoch": self._epoch,
                    "frame": self.config.odom_frame,
                },
            )
        )

    def _publish_status(self, reason: str | None) -> None:
        if self._epoch is None:
            return
        status = self.device.status()
        self._enqueue(
            {
                **self._envelope("node_status"),
                "drone_id": self.config.device_id,
                "connection_epoch": self._epoch,
                "virtual_stick_enabled": False,
                "control_authority": status.drive_authority,
                "authority_change_reason": reason,
                "watchdog_state": self._watchdog_state,
                "video_publish_state": self.device.video_publish_state(),
                "phone_battery_percent": 0,
                "phone_thermal_state": "none",
            }
        )

    def _local_stop(self, reason: str, *, disable: bool) -> None:
        try:
            self.device.stop()
            if disable:
                self.device.disable()
        except OSError:
            _LOGGER.warning("local ground stop failed")
        self._ready = False
        self._watchdog_state = "failsafe" if disable else "hold"
        if self._epoch is not None:
            self._publish_readiness()
            self._publish_status(reason)

    def _membership(self, action: str, **fields: object) -> dict[str, object]:
        frame = {
            **self._envelope("membership"),
            "drone_id": self.config.device_id,
            "action": action,
            **fields,
        }
        frame["signature"] = sign_event(frame, self._key)
        return frame

    def _ack(
        self,
        command: CommandFrame,
        status: str,
        reason: str | None = None,
        detail: str | None = None,
    ) -> dict[str, object]:
        return {
            **self._envelope("acknowledgement"),
            "intent_id": command.intent_id,
            "command_id": command.command_id,
            "status": status,
            "drone_id": self.config.device_id,
            "connection_epoch": command.connection_epoch,
            "roster_version": command.roster_version,
            "reason": reason,
            "detail": detail,
        }

    def _envelope(self, frame_type: str) -> dict[str, object]:
        return {
            "v": 1,
            "t": int(time.time_ns() // 1_000_000),
            "type": frame_type,
            "event_id": self.config.event_ids(),
            "session": self.config.session,
        }

    def _enqueue(self, frame: dict[str, object]) -> None:
        if self._outbound is not None:
            self._outbound.put_nowait(frame)

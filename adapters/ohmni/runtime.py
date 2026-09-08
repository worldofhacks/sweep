from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import math
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from planner.models import CommandOperation
from relay.auth import sign_event, verify_event_signature
from relay.contracts import CommandFrame, ContractError, parse_command
from relay.observations import ObservationSubmission

from .models import EncoderPoseSample, GroundStatus, RangeScan
from .paired_encoder import MAX_PAIR_SKEW_NS
from .return_controller import ApprovedReturnRoute, ReturnController, read_approval_key

if TYPE_CHECKING:
    from planner.ground_navigation import GroundNavigationAdmission, GroundNavigationDeployment

_LOGGER = logging.getLogger(__name__)


def _kernel_boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value if value else None


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
    def encoder_pose_sample(self) -> EncoderPoseSample | None: ...
    def latest_scan(self) -> RangeScan | None: ...
    def video_publish_state(self) -> str: ...
    def pre_enable_refusal(self) -> str | None: ...
    def stop_confirmed(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class GroundRuntimeConfig:
    relay_url: str
    session: str
    device_id: int
    token: str
    adapter_id: str
    source_clock_id: str = "ohmni-monotonic"
    pose_source_id: str = "ohmni-pose"
    capture_pose_source_id: str | None = None
    capture_pose_boot_id: str | None = None
    capture_pose_clock_mapping_id: str | None = None
    telemetry_source_id: str = "ohmni-telemetry"
    status_source_id: str = "ohmni-status"
    odom_frame: str = "odom"
    odom_origin_id: str | None = None
    body_frame: str = "body"
    lidar_source_id: str = "ohmni-lidar"
    lidar_frame: str = "lidar"
    lidar_mount_x_m: float | None = None
    lidar_mount_y_m: float | None = None
    lidar_mount_z_m: float | None = None
    lidar_mount_yaw_deg: float | None = None
    camera_source_id: str = "ohmni-camera"
    camera_frame: str = "camera"
    camera_calibration_id: str = "unconfigured"
    camera_width_px: int = 640
    camera_height_px: int = 480
    heartbeat_hold_ms: int = 2_000
    heartbeat_failsafe_ms: int = 10_000
    telemetry_hz: float = 5.0
    outbound_queue_limit: int = 256
    reconnect_initial_delay_s: float = 0.25
    reconnect_max_delay_s: float = 5.0
    return_approval: ApprovedReturnRoute | None = None
    navigation: GroundNavigationDeployment | None = None
    monotonic: Callable[[], float] = time.monotonic
    event_ids: Callable[[], str] = lambda: str(uuid.uuid4())
    relay_connect_host: str | None = None
    # Measured additive wall-clock correction for relay messages; sensor clocks stay native.
    relay_clock_offset_ms: int = 0

    def __post_init__(self) -> None:
        if self.relay_connect_host is not None:
            ipaddress.ip_address(self.relay_connect_host)
        if type(self.relay_clock_offset_ms) is not int or abs(self.relay_clock_offset_ms) > 300_000:
            raise ValueError("relay clock correction must be bounded to five minutes")
        if self.device_id <= 0 or not self.token or not self.session or not self.adapter_id:
            raise ValueError("ground runtime requires a session, device identity, and key")
        capture_values = (
            self.capture_pose_source_id,
            self.capture_pose_boot_id,
            self.capture_pose_clock_mapping_id,
        )
        if any(value is not None for value in capture_values):
            if not all(
                isinstance(value, str) and value and value == value.strip() and value.isprintable()
                for value in capture_values
            ):
                raise ValueError("capture pose requires source, boot, and clock mapping identities")
            if self.capture_pose_source_id == self.pose_source_id:
                raise ValueError(
                    "capture pose source must be distinct from the control pose source"
                )
            if (
                not isinstance(self.source_clock_id, str)
                or not self.source_clock_id
                or self.source_clock_id != self.source_clock_id.strip()
                or not self.source_clock_id.isprintable()
            ):
                raise ValueError("capture pose requires a bounded source clock identity")
        if not 0 < self.telemetry_hz <= 10:
            raise ValueError("ground telemetry rate must be between 0 and 10 Hz")
        if not 0 < self.heartbeat_hold_ms < self.heartbeat_failsafe_ms:
            raise ValueError("ground heartbeat windows are invalid")
        if not 1 <= self.camera_width_px <= 16_384 or not 1 <= self.camera_height_px <= 16_384:
            raise ValueError("ground camera dimensions are invalid")
        if self.outbound_queue_limit < 8:
            raise ValueError("ground outbound queue must retain at least eight safety frames")
        if (
            not isinstance(self.reconnect_initial_delay_s, int | float)
            or isinstance(self.reconnect_initial_delay_s, bool)
            or not math.isfinite(self.reconnect_initial_delay_s)
            or not isinstance(self.reconnect_max_delay_s, int | float)
            or isinstance(self.reconnect_max_delay_s, bool)
            or not math.isfinite(self.reconnect_max_delay_s)
            or not 0.05 <= self.reconnect_initial_delay_s <= self.reconnect_max_delay_s <= 30
        ):
            raise ValueError("ground reconnect delays must be finite and bounded")
        if self.odom_origin_id is not None and (
            not self.odom_origin_id
            or len(self.odom_origin_id) > 128
            or self.odom_origin_id != self.odom_origin_id.strip()
            or not self.odom_origin_id.isprintable()
        ):
            raise ValueError("odometry origin ID must be bounded non-empty text")
        if self.return_approval is not None and not self.odom_origin_id:
            raise ValueError("approved return requires an odometry origin ID")
        if self.navigation is not None:
            device = self.navigation.device(self.device_id)
            if (device.odom_origin_id, device.pose_source_id, device.odom_frame) != (
                self.odom_origin_id,
                self.pose_source_id,
                self.odom_frame,
            ):
                raise ValueError("navigation node source or odometry origin differs from approval")
            if device.identity_source_id != self.status_source_id:
                raise ValueError("navigation identity source differs from the node status source")
        mount = (
            self.lidar_mount_x_m,
            self.lidar_mount_y_m,
            self.lidar_mount_z_m,
            self.lidar_mount_yaw_deg,
        )
        if any(value is not None for value in mount) and not all(
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
            for value in mount
        ):
            raise ValueError("lidar mount requires a complete finite transform")
        if self.lidar_mount_yaw_deg not in (None, 0.0):
            raise ValueError("lidar scan axes are body-aligned; mount yaw must be zero")


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
        self._last_heartbeat_expires_at: int | None = None
        self._watchdog_state = "failsafe"
        self._ready = False
        self._local_stop_ready = False
        self._estop_latched = False
        self._operator_rearm_required = False
        self._pose_event_id: str | None = None
        self._last_capture_pose_poll_id: int | None = None
        self._last_capture_pose_receipt_ns: int | None = None
        self._last_scan_t_ms: int | None = None
        self._outbound: asyncio.Queue[dict[str, object]] | None = None
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._failure: BaseException | None = None
        self._connection_authenticated = False
        self._transport_stopped = True
        self._command_tasks: set[asyncio.Task[None]] = set()
        self._navigation_generation = 0
        self._navigation_active = False
        self._spent_navigation: dict[str, int] = {}
        self._return_controller = (
            None
            if config.return_approval is None
            else ReturnController(
                config.return_approval,
                status=device.status,
                scan=device.latest_scan,
                drive_velocity=device.drive_velocity,
                motion_done=device.motion_done,
                stop=device.stop,
                grant_active=self._return_grant_active,
                epoch=lambda: self._epoch,
                session=config.session,
                device_id=config.device_id,
                odom_origin_id=config.odom_origin_id or "",
                pose_source_id=config.pose_source_id,
                odom_frame=config.odom_frame,
                monotonic=config.monotonic,
            )
        )

    @property
    def connection_epoch(self) -> int | None:
        return self._epoch

    @property
    def watchdog_state(self) -> str:
        return self._watchdog_state

    def rearm_after_operator_confirmation(self) -> None:
        """Clear a local estop latch only after a physically present operator confirms it."""
        if not self._operator_rearm_required:
            return
        self._estop_latched = False
        self._operator_rearm_required = False
        self._last_heartbeat_at = None
        self._last_heartbeat_expires_at = None
        self._local_stop("operator_rearm_pending_heartbeat", disable=True)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        connection_options = (
            {}
            if self.config.relay_connect_host is None
            else {"host": self.config.relay_connect_host, "proxy": None}
        )
        delay = self.config.reconnect_initial_delay_s
        try:
            while not self._stop.is_set():
                self._outbound = asyncio.Queue(maxsize=self.config.outbound_queue_limit)
                self._connection_authenticated = False
                self._transport_stopped = False
                try:
                    await self._run_connection(connection_options)
                except (OSError, WebSocketException) as error:
                    _LOGGER.warning("ground relay connection lost: %s", error)
                finally:
                    authenticated = self._connection_authenticated
                    if not self._transport_stopped:
                        self._local_stop("watchdog_failsafe", disable=True, publish=False)
                    self._discard_transport_state()
                if self._stop.is_set():
                    break
                if authenticated:
                    delay = self.config.reconnect_initial_delay_s
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    delay = min(delay * 2, self.config.reconnect_max_delay_s)
        finally:
            self._local_stop("watchdog_failsafe", disable=True)
            self._discard_transport_state()
            self._started.set()

    async def _run_connection(self, connection_options: Mapping[str, object]) -> None:
        tasks: list[asyncio.Task[object]] = []
        async with connect(
            f"{self.config.relay_url.rstrip('/')}/ws/{self.config.session}", **connection_options
        ) as socket:
            try:
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
                self._connection_authenticated = True
                initial = json.loads(await socket.recv())
                if initial.get("type") == "state":
                    self._roster_version = int(initial["roster_version"])
                self._enqueue(
                    self._membership(
                        "join",
                        adapter_id=self.config.adapter_id,
                        capabilities=[
                            *self.device.capabilities,
                            *(["navigate"] if self.config.navigation is not None else []),
                        ],
                        node_type="ground",
                    )
                )
                self._started.set()
                tasks = [
                    asyncio.create_task(self._send(socket)),
                    asyncio.create_task(self._receive(socket)),
                    asyncio.create_task(self._telemetry()),
                    asyncio.create_task(self._watchdog()),
                    asyncio.create_task(self._stop.wait()),
                ]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if not self._stop.is_set():
                    for task in done:
                        if (error := task.exception()) is not None:
                            raise error
                    raise WebSocketException("relay closed the ground adapter socket")
            finally:
                # Stop the base before leaving the socket context, which may block on close.
                self._local_stop("watchdog_failsafe", disable=True, publish=False)
                self._transport_stopped = True
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                command_tasks = tuple(self._command_tasks)
                for task in command_tasks:
                    task.cancel()
                await asyncio.gather(*command_tasks, return_exceptions=True)

    def _discard_transport_state(self) -> None:
        self._outbound = None
        self._epoch = None
        self._roster_version = 0
        self._last_command_seq = 0
        self._last_heartbeat_seq = 0
        self._last_heartbeat_at = None
        self._last_heartbeat_expires_at = None
        self._ready = False
        self._watchdog_state = "failsafe"
        self._pose_event_id = None
        self._last_scan_t_ms = None

    def _track_command_task(self, task: asyncio.Task[None]) -> None:
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)

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
                self._lease_expired() or elapsed_ms >= self.config.heartbeat_failsafe_ms
            ) and self._watchdog_state != "failsafe":
                self._local_stop("watchdog_failsafe", disable=True)
            elif elapsed_ms >= self.config.heartbeat_hold_ms and self._watchdog_state == "nominal":
                self._local_stop("watchdog_hold", disable=False)

    async def _telemetry(self) -> None:
        while True:
            await asyncio.sleep(1 / self.config.telemetry_hz)
            self._publish_observations()
            self._publish_readiness()
            self._publish_status(None)

    def _on_membership(self, frame: Mapping[str, object]) -> None:
        if frame.get("action") != "join" or not isinstance(frame.get("connection_epoch"), int):
            return
        self._epoch = frame["connection_epoch"]
        self._roster_version = int(frame.get("roster_version", self._roster_version))
        self._last_command_seq = self._last_heartbeat_seq = 0
        self._last_heartbeat_at = None
        self._last_heartbeat_expires_at = None
        self._ready = False
        self._watchdog_state = "failsafe"
        self._local_stop("rejoin_requires_heartbeat", disable=True)
        self._publish_observations()
        self._publish_readiness()
        self._publish_status("rejoin_requires_heartbeat")

    def _on_heartbeat(self, frame: Mapping[str, object]) -> None:
        unsigned = {key: value for key, value in frame.items() if key != "signature"}
        now_ms = self._relay_now_ms()
        issued_at = frame.get("issued_at")
        expires_at = frame.get("expires_at")
        if (
            frame.get("session") != self.config.session
            or frame.get("source") != "relay"
            or frame.get("drone_id") != self.config.device_id
            or frame.get("connection_epoch") != self._epoch
            or frame.get("roster_version") != self._roster_version
            or not isinstance(frame.get("seq"), int)
            or frame["seq"] <= self._last_heartbeat_seq
            or not isinstance(issued_at, int)
            or isinstance(issued_at, bool)
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or issued_at > now_ms
            or expires_at <= now_ms
            or expires_at - issued_at > self.config.heartbeat_failsafe_ms
            or frame.get("hold_after_ms") != self.config.heartbeat_hold_ms
            or frame.get("failsafe_after_ms") != self.config.heartbeat_failsafe_ms
            or not verify_event_signature(unsigned, frame.get("signature"), self._key)
        ):
            return
        self._last_heartbeat_seq = frame["seq"]
        self._last_heartbeat_at = self.config.monotonic()
        self._last_heartbeat_expires_at = expires_at
        if self._operator_rearm_required:
            self._publish_status("operator_rearm_required")
            return
        if self._watchdog_state != "nominal":
            self._watchdog_state = "nominal"
            self._publish_observations()
            refusal = self._pre_enable_refusal()
            self._ready = (
                refusal is None
                and self._watchdog_state == "nominal"
                and self._pose_event_id is not None
                and self.device.enable()
            )
            if self._ready and self.config.navigation is not None:
                try:
                    self.device.stop()
                    self._ready = self.device.stop_confirmed()
                except Exception:
                    self._ready = False
            if not self._ready:
                self._local_stop(refusal or "ground_guard_not_ready", disable=True)
            else:
                self._local_stop_ready = self.device.status().state != "moving"
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
                    int(args["linear_mm_s"]) / 1_000,
                    math.degrees(int(args["angular_mrad_s"]) / 1_000),
                    int(args["duration_ms"]) / 1_000,
                )
            except (OSError, RuntimeError, ValueError) as error:
                self._local_stop("ground_drive_io_failure", disable=True)
                self._enqueue(self._ack(command, "failed", "local_guard_refused", str(error)))
                return
            self._enqueue(self._ack(command, "executing"))
            assert self._loop is not None
            self._track_command_task(self._loop.create_task(self._complete_motion(command, motion)))
            return
        if command.operation is CommandOperation.GROUND_RETURN:
            route = self.config.return_approval
            if (
                self._return_controller is None
                or route is None
                or command.args["return_id"] != route.return_id
            ):
                self._enqueue(
                    self._ack(
                        command, "failed", "return_route_unavailable", "no matching approved route"
                    )
                )
                return
            self._enqueue(self._ack(command, "executing"))
            assert self._loop is not None
            self._track_command_task(self._loop.create_task(self._complete_return(command)))
            return
        if command.operation is CommandOperation.GROUND_NAVIGATE:
            navigation = self.config.navigation
            assert navigation is not None
            now = self._relay_now_ms()
            self._spent_navigation = {
                identity: expiry
                for identity, expiry in self._spent_navigation.items()
                if expiry > now
            }
            route_id = command.args["route_id"]
            try:
                admission = navigation.admit_route(
                    command.args["navigation_route"],
                    route_id=route_id,
                    session=self.config.session,
                    device_id=self.config.device_id,
                    connection_epoch=command.connection_epoch,
                    roster_version=command.roster_version,
                    now_ms=now,
                )
                if route_id in self._spent_navigation or len(self._spent_navigation) >= 256:
                    raise ValueError("navigation route was consumed or route capacity is exhausted")
            except (ValueError, OSError) as error:
                self._enqueue(self._ack(command, "failed", "navigation_refused", str(error)))
                return
            self._spent_navigation[route_id] = admission.expires_at
            self._navigation_active = True
            self._enqueue(self._ack(command, "executing"))
            assert self._loop is not None
            self._track_command_task(
                self._loop.create_task(
                    self._complete_navigation(command, admission, self._navigation_generation)
                )
            )
            return
        if command.operation is CommandOperation.HOVER:
            self._enqueue(self._ack(command, "executing"))
            stopped = self._local_stop("remote_hold", disable=False)
            self._enqueue(
                self._ack(command, "completed")
                if stopped
                else self._ack(
                    command,
                    "failed",
                    "local_stop_unconfirmed",
                    "local STOP writes did not complete",
                )
            )
            return
        if command.operation is CommandOperation.ESTOP:
            self._estop_latched = True
            self._operator_rearm_required = True
            self._enqueue(self._ack(command, "executing"))
            stopped = self._local_stop("local_estop", disable=True)
            self._enqueue(
                self._ack(command, "completed")
                if stopped
                else self._ack(
                    command,
                    "failed",
                    "local_stop_unconfirmed",
                    "local STOP/disable did not complete",
                )
            )
            return

    async def _complete_return(self, command: CommandFrame) -> None:
        assert self._return_controller is not None
        outcome = await self._return_controller.run()
        if outcome.completed:
            stopped = self._local_stop("return_arrived", disable=False)
            self._enqueue(
                self._ack(command, "completed")
                if stopped
                else self._ack(
                    command, "failed", "local_stop_unconfirmed", "return STOP did not complete"
                )
            )
            return
        self._local_stop(outcome.reason or "return_failed", disable=True)
        self._enqueue(
            self._ack(command, "failed", outcome.reason or "return_failed", outcome.detail)
        )

    async def _complete_navigation(
        self, command: CommandFrame, admission: GroundNavigationAdmission, generation: int
    ) -> None:
        navigation = self.config.navigation
        assert navigation is not None
        controller = ReturnController(
            admission.route,
            status=self.device.status,
            scan=self.device.latest_scan,
            drive_velocity=self.device.drive_velocity,
            motion_done=self.device.motion_done,
            stop=self.device.stop,
            grant_active=lambda: (
                generation == self._navigation_generation
                and command.roster_version == self._roster_version
                and self._return_grant_active()
                and navigation.check_active(admission, now_ms=self._relay_now_ms())
            ),
            epoch=lambda: self._epoch,
            session=self.config.session,
            device_id=self.config.device_id,
            odom_origin_id=self.config.odom_origin_id or "",
            pose_source_id=self.config.pose_source_id,
            odom_frame=self.config.odom_frame,
            monotonic=self.config.monotonic,
            require_fresh_arrival=True,
            allow_resume=False,
        )
        try:
            outcome = await controller.run()
            stopped = False
            if outcome.completed:
                self.device.stop()
                status = self.device.status()
                stopped = (
                    self.device.stop_confirmed()
                    and status.state in {"idle", "stopped"}
                    and type(status.t_ms) is int
                    and 0
                    <= int(self.config.monotonic() * 1000) - status.t_ms
                    <= admission.route.pose_max_age_ms
                )
            if not outcome.completed or not stopped:
                self._local_stop(outcome.reason or "local_stop_unconfirmed", disable=True)
            if outcome.completed and stopped:
                self._enqueue(self._ack(command, "completed"))
            else:
                self._enqueue(
                    self._ack(
                        command,
                        "failed",
                        outcome.reason or "local_stop_unconfirmed",
                        outcome.detail or "navigation STOP did not complete",
                    )
                )
        except asyncio.CancelledError:
            self._local_stop("navigation_cancelled", disable=True)
            raise
        except (OSError, ValueError, RuntimeError) as error:
            self._local_stop("navigation_failed", disable=True)
            self._enqueue(self._ack(command, "failed", "navigation_failed", str(error)))
        finally:
            self._navigation_active = False

    async def _complete_motion(self, command: CommandFrame, motion: str) -> None:
        while True:
            await asyncio.sleep(0.02)
            try:
                completed = self.device.motion_done(motion)
            except OSError as error:
                self._local_stop("ground_motion_io_failure", disable=True)
                self._enqueue(self._ack(command, "failed", "motion_failed", str(error)))
                return
            if completed is False:
                continue
            if completed is True:
                try:
                    stopped = self.device.stop_confirmed() and self.device.status().state in {
                        "idle",
                        "stopped",
                    }
                except OSError:
                    stopped = False
                if not stopped:
                    self._local_stop("local_stop_unconfirmed", disable=True)
                self._enqueue(
                    self._ack(command, "completed")
                    if stopped
                    else self._ack(
                        command, "failed", "local_stop_unconfirmed", "motion STOP did not complete"
                    )
                )
            else:
                self._enqueue(
                    self._ack(command, "failed", "motion_failed", "ground motion stopped")
                )
            return

    def _command_failure(self, command: CommandFrame) -> tuple[str, str] | None:
        now_ms = self._relay_now_ms()
        if (
            command.connection_epoch != self._epoch
            or command.roster_version != self._roster_version
        ):
            return "stale_command", "command identity is no longer current"
        if command.issued_at + command.ttl_ms < now_ms or command.seq <= self._last_command_seq:
            return "stale_command", "command is stale or replayed"
        if command.operation not in {
            CommandOperation.GROUND_VELOCITY,
            CommandOperation.GROUND_RETURN,
            CommandOperation.GROUND_NAVIGATE,
            CommandOperation.HOVER,
            CommandOperation.ESTOP,
        }:
            return "unsupported_operation", "ground route is not qualified"
        if command.operation in {CommandOperation.HOVER, CommandOperation.ESTOP}:
            return None
        if command.operation is CommandOperation.GROUND_RETURN and self._return_controller is None:
            return "return_route_unavailable", "no externally approved return route is configured"
        if command.operation is CommandOperation.GROUND_NAVIGATE and self.config.navigation is None:
            return (
                "navigation_unavailable",
                "no approved ground navigation deployment is configured",
            )
        if self._navigation_active or self._command_tasks:
            return "ground_motion_active", "another ground movement has not finished"
        if self._operator_rearm_required:
            return "operator_rearm_required", "physical operator rearm is required after estop"
        if self._lease_expired():
            self._local_stop("watchdog_failsafe", disable=True)
            return "watchdog_failsafe", "the signed control lease has expired"
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
        confidence = _confidence(status.pos_quality)
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
            confidence=confidence,
        )
        self._pose_event_id = pose_event["event_id"]
        self._enqueue(pose_event)
        self._publish_encoder_pose()
        if confidence <= 0:
            self._local_stop("pose_unusable", disable=True)
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
                confidence=confidence,
            )
        )
        if self.config.navigation is not None:
            deployment = self.config.navigation
            device = deployment.device(self.config.device_id)
            self._enqueue(
                self._observation(
                    self.config.status_source_id,
                    self.config.odom_frame,
                    receipt,
                    {
                        "kind": "status",
                        "code": "ground_navigation_identity",
                        "detail": json.dumps(
                            {
                                "odom_origin_id": self.config.odom_origin_id,
                                "pose_source_id": self.config.pose_source_id,
                                "registration_id": device.registration_id,
                                "configuration_sha256": deployment.configuration_sha256,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "capabilities": ["ground_drive", "navigate"],
                    },
                    confidence=confidence,
                )
            )
        else:
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
                    confidence=confidence,
                )
            )
        scan = self.device.latest_scan()
        if scan is not None and self._lidar_mount_configured and scan.t_ms != self._last_scan_t_ms:
            self._last_scan_t_ms = scan.t_ms
            sensor_x, sensor_y, sensor_yaw = self._lidar_sensor_pose(scan)
            self._enqueue(
                self._observation(
                    self.config.lidar_source_id,
                    self.config.lidar_frame,
                    {
                        "clock_id": self.config.source_clock_id,
                        "unit": "ms",
                        "value": scan.t_ms,
                    },
                    {
                        "kind": "range_scan",
                        "sensor_pose": {
                            "parent_frame": self.config.odom_frame,
                            "child_frame": self.config.lidar_frame,
                            "x_m": sensor_x,
                            "y_m": sensor_y,
                            "z_m": self.config.lidar_mount_z_m,
                            "qx": 0.0,
                            "qy": 0.0,
                            "qz": math.sin(math.radians(sensor_yaw) / 2),
                            "qw": math.cos(math.radians(sensor_yaw) / 2),
                        },
                        "angle_min_rad": math.radians(scan.angle_min_deg),
                        "angle_increment_rad": math.radians(scan.angle_increment_deg),
                        "range_min_m": scan.range_min_m,
                        "range_max_m": scan.range_max_m,
                        "ranges_m": [None if item == 0 else item / 100 for item in scan.ranges_cm],
                        "mount_id": "ohmni-rplidar",
                    },
                    confidence=confidence,
                )
            )

    def _publish_encoder_pose(self) -> None:
        if self.config.capture_pose_source_id is None:
            return
        if _kernel_boot_id() != self.config.capture_pose_boot_id:
            return
        sample = self.device.encoder_pose_sample()
        if sample is None or sample.quality <= 0:
            return
        if (
            sample.poll_id <= 0
            or sample.left_receipt_ns < 0
            or sample.right_receipt_ns < sample.left_receipt_ns
            or sample.right_receipt_ns - sample.left_receipt_ns > MAX_PAIR_SKEW_NS
            or (
                self._last_capture_pose_poll_id is not None
                and sample.poll_id <= self._last_capture_pose_poll_id
            )
            or (
                self._last_capture_pose_receipt_ns is not None
                and sample.right_receipt_ns <= self._last_capture_pose_receipt_ns
            )
        ):
            return
        clock = {
            "clock_id": self.config.source_clock_id,
            "unit": "ns",
            "value": sample.right_receipt_ns,
        }
        self._enqueue(
            self._observation(
                self.config.capture_pose_source_id,
                self.config.odom_frame,
                clock,
                {
                    "kind": "pose",
                    "pose": {
                        "parent_frame": self.config.odom_frame,
                        "child_frame": self.config.body_frame,
                        "x_m": sample.x,
                        "y_m": sample.y,
                        "z_m": 0.0,
                        "qx": 0.0,
                        "qy": 0.0,
                        "qz": math.sin(math.radians(sample.yaw_deg) / 2),
                        "qw": math.cos(math.radians(sample.yaw_deg) / 2),
                    },
                    "encoder_timing": {
                        "v": 1,
                        "time_basis": "encoder_reply_receipt",
                        "boot_id": self.config.capture_pose_boot_id,
                        "poll_id": sample.poll_id,
                        "left_receipt": {
                            "clock_id": self.config.source_clock_id,
                            "unit": "ns",
                            "value": sample.left_receipt_ns,
                        },
                        "right_receipt": clock,
                        "pair_skew_ns": sample.right_receipt_ns - sample.left_receipt_ns,
                    },
                },
                confidence=_confidence(sample.quality),
                capture=clock,
                clock_mapping_id=self.config.capture_pose_clock_mapping_id,
            )
        )
        self._last_capture_pose_poll_id = sample.poll_id
        self._last_capture_pose_receipt_ns = sample.right_receipt_ns

    def _observation(
        self,
        source_id: str,
        frame: str,
        receipt: Mapping[str, object],
        payload: Mapping[str, object],
        *,
        confidence: float,
        capture: Mapping[str, object] | None = None,
        clock_mapping_id: str | None = None,
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
            "confidence": confidence,
            "t_capture": None if capture is None else dict(capture),
            "t_source_receipt": dict(receipt),
            "clock_mapping_id": clock_mapping_id,
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
                local_stop_ready=self._local_stop_ready,
                heartbeat_ready=self._watchdog_state == "nominal",
                pose_identity={
                    "event_id": self._pose_event_id,
                    "session": self.config.session,
                    "connection_epoch": self._epoch,
                    "source_id": self.config.pose_source_id,
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
                "control_authority": self._ready and status.drive_authority,
                "authority_change_reason": reason,
                "watchdog_state": self._watchdog_state,
                "video_publish_state": self.device.video_publish_state(),
                "phone_battery_percent": 0,
                "phone_thermal_state": "none",
            }
        )

    def _pre_enable_refusal(self) -> str | None:
        if not self._lidar_mount_configured:
            return "lidar_mount_unconfigured"
        status = self.device.status()
        now_ms = int(self.config.monotonic() * 1_000)
        if (
            type(status.t_ms) is not int
            or not 0 <= now_ms - status.t_ms <= 500
            or status.pos_quality <= 0
            or not all(
                math.isfinite(value)
                for value in (status.pos_quality, status.x, status.y, status.yaw_deg)
            )
        ):
            return "pose_unusable"
        guard = getattr(self.device, "pre_enable_refusal", None)
        return "ground_safety_unconfigured" if guard is None else guard()

    def _local_stop(self, reason: str, *, disable: bool, publish: bool = True) -> bool:
        self._navigation_generation += 1
        self._ready = False
        self._watchdog_state = "failsafe" if disable else "hold"
        self._local_stop_ready = False
        failed = False
        operations = (self.device.stop, self.device.disable) if disable else (self.device.stop,)
        for operation in operations:
            try:
                operation()
            except OSError:
                _LOGGER.warning("local ground stop/disable failed")
                failed = True
        try:
            status = self.device.status()
            confirmed = getattr(self.device, "stop_confirmed", lambda: False)
            self._local_stop_ready = (
                not failed
                and confirmed()
                and status.state in {"idle", "stopped"}
                and (not disable or not status.drive_authority)
            )
        except OSError:
            _LOGGER.warning("local ground stop status unavailable")
        if publish and self._epoch is not None:
            self._publish_readiness()
            self._publish_status(reason)
        return self._local_stop_ready

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
            "t": self._relay_now_ms(),
            "type": frame_type,
            "event_id": self.config.event_ids(),
            "session": self.config.session,
        }

    def _enqueue(self, frame: dict[str, object]) -> None:
        if self._outbound is not None:
            try:
                self._outbound.put_nowait(frame)
            except asyncio.QueueFull:
                _LOGGER.error("ground outbound queue overflow; stopping local drive")
                self._local_stop("outbound_queue_overflow", disable=True, publish=False)
                if self._stop is not None:
                    self._stop.set()

    @property
    def _lidar_mount_configured(self) -> bool:
        return all(
            value is not None
            for value in (
                self.config.lidar_mount_x_m,
                self.config.lidar_mount_y_m,
                self.config.lidar_mount_z_m,
                self.config.lidar_mount_yaw_deg,
            )
        )

    def _lidar_sensor_pose(self, scan: RangeScan) -> tuple[float, float, float]:
        assert self.config.lidar_mount_x_m is not None
        assert self.config.lidar_mount_y_m is not None
        heading = math.radians(scan.pose[2])
        return (
            scan.pose[0]
            + math.cos(heading) * self.config.lidar_mount_x_m
            - math.sin(heading) * self.config.lidar_mount_y_m,
            scan.pose[1]
            + math.sin(heading) * self.config.lidar_mount_x_m
            + math.cos(heading) * self.config.lidar_mount_y_m,
            scan.pose[2] % 360,
        )

    def _return_grant_active(self) -> bool:
        return (
            not self._operator_rearm_required
            and self._ready
            and self._watchdog_state == "nominal"
            and not self._lease_expired()
        )

    def _lease_expired(self) -> bool:
        expires_at = self._last_heartbeat_expires_at
        return expires_at is None or self._relay_now_ms() >= expires_at

    def _relay_now_ms(self) -> int:
        return int(time.time_ns() // 1_000_000) + self.config.relay_clock_offset_ms


def _confidence(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return min(1.0, max(0.0, float(value)))
    return 0.0


def parse_args(argv: Sequence[str] | None = None) -> GroundRuntimeConfig:
    parser = argparse.ArgumentParser(prog="python -m adapters.ohmni.runtime")
    parser.add_argument("--relay", default=os.environ.get("SWEEP_RELAY_URL"))
    parser.add_argument("--session", default=os.environ.get("SWEEP_SESSION"))
    parser.add_argument("--device-id", type=int, default=os.environ.get("SWEEP_DEVICE_UNIT"))
    parser.add_argument("--token", default=os.environ.get("SWEEP_NODE_KEY"))
    parser.add_argument("--adapter-id", default=os.environ.get("SWEEP_ADAPTER_ID"))
    parser.add_argument("--source-clock-id", default=os.environ.get("SWEEP_SOURCE_CLOCK_ID"))
    parser.add_argument(
        "--capture-pose-source-id", default=os.environ.get("SWEEP_CAPTURE_POSE_SOURCE_ID")
    )
    parser.add_argument(
        "--capture-pose-boot-id", default=os.environ.get("SWEEP_CAPTURE_POSE_BOOT_ID")
    )
    parser.add_argument(
        "--capture-pose-clock-mapping-id",
        default=os.environ.get("SWEEP_CAPTURE_POSE_CLOCK_MAPPING_ID"),
    )
    parser.add_argument("--relay-connect-host", default=os.environ.get("SWEEP_RELAY_CONNECT_HOST"))
    parser.add_argument(
        "--relay-clock-offset-ms",
        type=int,
        default=os.environ.get("SWEEP_RELAY_CLOCK_OFFSET_MS", "0"),
    )
    parser.add_argument("--odom-origin-id", default=os.environ.get("SWEEP_ODOM_ORIGIN_ID"))
    parser.add_argument("--telemetry-hz", type=float, default=5.0)
    parser.add_argument(
        "--return-approval-file", default=os.environ.get("SWEEP_RETURN_APPROVAL_FILE")
    )
    parser.add_argument(
        "--return-approval-key-file", default=os.environ.get("SWEEP_RETURN_APPROVAL_KEY_FILE")
    )
    parser.add_argument(
        "--navigation-config", default=os.environ.get("SWEEP_GROUND_NAVIGATION_CONFIG")
    )
    parser.add_argument(
        "--navigation-key-file", default=os.environ.get("SWEEP_GROUND_NAVIGATION_KEY_FILE")
    )
    parser.add_argument(
        "--lidar-mount-x-m", type=float, default=os.environ.get("SWEEP_LIDAR_MOUNT_X_M")
    )
    parser.add_argument(
        "--lidar-mount-y-m", type=float, default=os.environ.get("SWEEP_LIDAR_MOUNT_Y_M")
    )
    parser.add_argument(
        "--lidar-mount-z-m", type=float, default=os.environ.get("SWEEP_LIDAR_MOUNT_Z_M")
    )
    parser.add_argument(
        "--lidar-mount-yaw-deg", type=float, default=os.environ.get("SWEEP_LIDAR_MOUNT_YAW_DEG")
    )
    args = parser.parse_args(argv)
    if bool(args.return_approval_file) != bool(args.return_approval_key_file):
        parser.error("return approval and approval key files must be supplied together")
    if args.return_approval_file and not args.odom_origin_id:
        parser.error("return approval requires an odometry origin ID")
    if bool(args.navigation_config) != bool(args.navigation_key_file):
        parser.error("ground navigation configuration and key files must be supplied together")
    if args.navigation_config and not args.odom_origin_id:
        parser.error("ground navigation requires an odometry origin ID")
    if not args.relay or not args.session or not args.token or args.device_id is None:
        parser.error("relay, session, device ID, and adapter token are required")
    try:
        navigation = None
        if args.navigation_config:
            from planner.ground_navigation import GroundNavigationDeployment

            navigation = GroundNavigationDeployment.load(
                Path(args.navigation_config), read_approval_key(Path(args.navigation_key_file))
            )
        approval = (
            None
            if not args.return_approval_file
            else ApprovedReturnRoute.load(
                Path(args.return_approval_file),
                read_approval_key(Path(args.return_approval_key_file)),
            )
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return GroundRuntimeConfig(
        relay_url=args.relay.rstrip("/"),
        session=args.session,
        device_id=args.device_id,
        token=args.token,
        adapter_id=args.adapter_id or f"ohmni-{args.device_id}",
        source_clock_id=args.source_clock_id or "ohmni-monotonic",
        capture_pose_source_id=args.capture_pose_source_id,
        capture_pose_boot_id=args.capture_pose_boot_id,
        capture_pose_clock_mapping_id=args.capture_pose_clock_mapping_id,
        relay_connect_host=args.relay_connect_host,
        relay_clock_offset_ms=args.relay_clock_offset_ms,
        telemetry_hz=args.telemetry_hz,
        return_approval=approval,
        navigation=navigation,
        odom_origin_id=args.odom_origin_id,
        lidar_mount_x_m=args.lidar_mount_x_m,
        lidar_mount_y_m=args.lidar_mount_y_m,
        lidar_mount_z_m=args.lidar_mount_z_m,
        lidar_mount_yaw_deg=args.lidar_mount_yaw_deg,
    )


def _install_sigterm_stop(node: OhmniRuntime) -> Callable[[], None]:
    previous = signal.getsignal(signal.SIGTERM)

    def stop(_signal: int, _frame: object) -> None:
        node.stop()

    signal.signal(signal.SIGTERM, stop)

    def restore() -> None:
        signal.signal(signal.SIGTERM, previous)

    return restore


def main(argv: Sequence[str] | None = None) -> int:
    from .device import build

    config = parse_args(argv)
    device = build()
    node = OhmniRuntime(config, device)
    restore_sigterm = _install_sigterm_stop(node)
    try:
        asyncio.run(node.run())
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, WebSocketException) as error:
        _LOGGER.error("%s", error)
        return 1
    finally:
        restore_sigterm()
        device.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

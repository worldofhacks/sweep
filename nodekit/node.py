"""The node runtime: one authenticated socket, one device, and the whole node protocol.

``Node(config, device)`` authenticates as an adapter, sends the signed join carrying the
device's class, claims readiness, streams telemetry and scans, admits and executes
commands, keeps the relay's control lease as a deadman, and reconnects with backoff. A
device integration implements ``nodekit.device.Device`` and nothing else.

Three seams are meant to be overridden by a node that speaks more of the command set than
a generic vehicle does (``adapters/dji_mini3/fake_node.py`` is the worked example):
``supported_operations``, ``start_operation``, and ``extra_join_frames``, plus
``drop_command`` and ``completion_delay_s`` for fixtures that need to swallow or delay an
acknowledgement.

Python 3.9 is the floor: no ``match``, no ``StrEnum``, no ``slots=True`` dataclasses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from nodekit import protocol
from nodekit.device import Device, scan_is_valid

_LOGGER = logging.getLogger(__name__)

_STARTUP_TIMEOUT_S = 10.0
_MOTION_POLL_S = 0.02

# Auth refusals a node must not retry: coming back only burns the relay's log.
HALT_REASONS = frozenset(
    {"session_closed", "authentication_failed", "invalid_auth", "unknown_source"}
)

# What a generic vehicle executes. Everything else in the command set is refused with
# ``unsupported_operation``; a node that flies overrides ``supported_operations``.
KIT_OPERATIONS = frozenset({"goto", "rotate_to", "hover", "estop"})

# Node-local watchdog states. ``disarmed`` and ``armed`` are both ``nominal`` on the wire.
DISARMED = "disarmed"
ARMED = "armed"
WATCHDOG_HOLD = "hold"
WATCHDOG_FAILSAFE = "failsafe"


class NodeError(RuntimeError):
    """The node cannot continue on this socket; ``run`` reconnects unless it must halt."""


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000


def _uuid4() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class NodeConfig:
    """Everything the node needs that the device does not provide.

    ``token`` is the device's own relay key: it is entered on the device by a person and
    is never displayed or logged. The watchdog and TTL values here are only a floor; the
    relay's ``auth.accepted`` settings replace them on every connection.
    """

    relay_url: str
    session: str
    device_id: int
    token: str
    adapter_id: str
    telemetry_hz: float = 10.0
    sensor_hz: float = 5.0
    home_pose_confirmed: bool = True
    # A ground vehicle reads this as "a spotter is beside the robot with the screen STOP
    # in reach"; an aircraft as "an RC safety pilot holds the controller".
    safety_operator_present: bool = True
    reconnect: bool = True
    backoff_initial_s: float = 0.5
    backoff_max_s: float = 8.0
    watchdog_poll_s: float = 0.05
    command_ttl_ms: int = 2_000
    watchdog_hold_ms: int = 2_000
    watchdog_failsafe_ms: int = 10_000
    # Off by default: an unannounced disappearance is the honest report of a node that was
    # killed, and the relay authorizes a graceful leave only for a stopped device anyway.
    graceful_leave_on_stop: bool = False
    monotonic: Callable[[], float] = time.monotonic
    clock_ms: Callable[[], int] = epoch_ms
    event_ids: Callable[[], str] = _uuid4

    def __post_init__(self) -> None:
        if self.device_id <= 0:
            raise ValueError("device_id must be a positive integer")
        if not self.token:
            raise ValueError("token must be a non-empty string")
        if not 0 < self.telemetry_hz <= 50:
            raise ValueError("telemetry_hz must be between 0 and 50")
        if not 0 < self.sensor_hz <= 5:
            raise ValueError("sensor_hz must be between 0 and 5: the relay drops faster scans")
        if not 0 <= self.watchdog_hold_ms < self.watchdog_failsafe_ms:
            raise ValueError("watchdog thresholds must satisfy 0 <= hold < failsafe")
        if self.command_ttl_ms <= 0:
            raise ValueError("command_ttl_ms must be positive")
        if not 0 < self.backoff_initial_s <= self.backoff_max_s:
            raise ValueError("backoff must satisfy 0 < initial <= max")


@dataclass(frozen=True)
class WatchdogTransition:
    from_state: str
    to_state: str
    reason: str | None
    elapsed_ms: int


class Watchdog:
    """The node-local link deadman.

    Only a verified, current control heartbeat is activity: commands, state, membership,
    and fan-out echoes are deliberately not liveness evidence. ``poll`` moves to ``hold``
    once ``hold_ms`` pass without one and to ``failsafe`` at ``failsafe_ms``; a long
    silence jumps straight to failsafe. Activity during hold recovers; failsafe is
    terminal until the node rejoins and arms again. This mirrors the Android bridge's
    ``bridge-core`` Watchdog, which is the reference implementation.
    """

    def __init__(self, hold_ms: int, failsafe_ms: int, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self.state = DISARMED
        self._last_activity: float | None = None
        self._queued: WatchdogTransition | None = None
        self.hold_ms = 0
        self.failsafe_ms = 1
        self.configure(hold_ms, failsafe_ms)

    def configure(self, hold_ms: int, failsafe_ms: int) -> None:
        if not 0 <= hold_ms < failsafe_ms:
            raise ValueError("watchdog thresholds must satisfy 0 <= hold < failsafe")
        self.hold_ms = hold_ms
        self.failsafe_ms = failsafe_ms

    def arm(self) -> None:
        self.state = ARMED
        self._last_activity = self._monotonic()
        self._queued = None

    def disarm(self) -> None:
        self.state = DISARMED
        self._last_activity = None
        self._queued = None

    def heartbeat(self) -> None:
        if self.state == ARMED:
            self._last_activity = self._monotonic()
        elif self.state == WATCHDOG_HOLD:
            self._last_activity = self._monotonic()
            self.state = ARMED
            self._queued = WatchdogTransition(WATCHDOG_HOLD, ARMED, None, 0)

    def trip_failsafe(self) -> WatchdogTransition | None:
        """Force failsafe now: the link is gone and no lease can arrive to clear a hold."""
        if self.state in (DISARMED, WATCHDOG_FAILSAFE):
            return None
        transition = WatchdogTransition(
            self.state, WATCHDOG_FAILSAFE, protocol.WATCHDOG_FAILSAFE, self.elapsed_ms()
        )
        self.state = WATCHDOG_FAILSAFE
        self._queued = None
        return transition

    def elapsed_ms(self) -> int:
        if self._last_activity is None:
            return 0
        return int((self._monotonic() - self._last_activity) * 1000)

    def poll(self) -> WatchdogTransition | None:
        """The transition that just happened, if any; call it from the control tick."""
        if self._queued is not None:
            transition = self._queued
            self._queued = None
            return transition
        if self.state in (DISARMED, WATCHDOG_FAILSAFE) or self._last_activity is None:
            return None
        elapsed = self.elapsed_ms()
        if elapsed >= self.failsafe_ms:
            target = WATCHDOG_FAILSAFE
        elif elapsed >= self.hold_ms:
            target = WATCHDOG_HOLD
        else:
            target = ARMED
        if target == self.state:
            return None
        reason = None
        if target == WATCHDOG_HOLD:
            reason = protocol.WATCHDOG_HOLD
        elif target == WATCHDOG_FAILSAFE:
            reason = protocol.WATCHDOG_FAILSAFE
        transition = WatchdogTransition(self.state, target, reason, elapsed)
        self.state = target
        return transition

    def wire_state(self) -> str:
        if self.state == WATCHDOG_HOLD:
            return protocol.HOLD
        if self.state == WATCHDOG_FAILSAFE:
            return protocol.FAILSAFE
        return protocol.NOMINAL


@dataclass(frozen=True)
class Admission:
    """Whether a command runs, and whether its refusal is worth an acknowledgement."""

    admitted: bool
    acknowledge: bool = False
    reason: str | None = None
    detail: str | None = None


ADMITTED = Admission(True)


def admit_command(
    command: protocol.Command,
    *,
    session: str,
    device_id: int,
    key: bytes | str,
    connection_epoch: int | None,
    roster_version: int,
    last_seq: int,
    now_ms: int,
) -> Admission:
    """Decide one command, in the order the relay documents.

    A command for another session or device, or one whose signature does not verify, is
    dropped without an acknowledgement: it is not evidence about this node. Everything
    else is refused with a machine-readable reason the relay already understands.
    """
    if command.session != session or command.drone_id != device_id:
        return Admission(False, reason="misaddressed", detail="command is not for this device")
    if not command.verifies(key):
        return Admission(
            False, reason="invalid_signature", detail="command signature did not verify"
        )
    if connection_epoch is None or command.connection_epoch != connection_epoch:
        return Admission(
            False,
            acknowledge=connection_epoch is not None,
            reason=protocol.STALE_COMMAND,
            detail=f"command epoch {command.connection_epoch} is not the node epoch",
        )
    if command.roster_version != roster_version:
        return Admission(
            False,
            acknowledge=True,
            reason=protocol.STALE_COMMAND,
            detail=(
                f"command roster {command.roster_version} differs from the last state "
                f"{roster_version}"
            ),
        )
    if command.issued_at + command.ttl_ms < now_ms:
        return Admission(
            False,
            acknowledge=True,
            reason=protocol.STALE_COMMAND,
            detail="command is older than its ttl",
        )
    if command.seq <= last_seq:
        return Admission(
            False,
            acknowledge=True,
            reason=protocol.OUT_OF_ORDER_COMMAND,
            detail=f"seq {command.seq} after seq {last_seq}",
        )
    return ADMITTED


@dataclass(frozen=True)
class Execution:
    """What one operation started: a motion to poll, or a result already known."""

    motion_id: str | None = None
    status: str | None = None
    reason: str | None = None
    detail: str | None = None

    @classmethod
    def motion(cls, motion_id: str) -> Execution:
        return cls(motion_id=motion_id)

    @classmethod
    def completed(cls) -> Execution:
        return cls(status=protocol.COMPLETED)

    @classmethod
    def failed(cls, reason: str, detail: str) -> Execution:
        return cls(status=protocol.FAILED, reason=reason, detail=detail)


class Node:
    """One device on one relay session."""

    def __init__(self, config: NodeConfig, device: Device) -> None:
        self.config = config
        self.device = device
        self.node_settings: dict[str, Any] | None = None
        self._key = config.token.encode()
        self._watchdog = Watchdog(
            config.watchdog_hold_ms, config.watchdog_failsafe_ms, config.monotonic
        )
        self._command_ttl_ms = config.command_ttl_ms
        self._safety_operator_present = config.safety_operator_present
        self._connection_epoch: int | None = None
        self._roster_version = 0
        self._last_seq = 0
        self._last_heartbeat_seq = 0
        self._last_t = 0
        self._last_scan_t: int | None = None
        self._last_node_status: dict[str, Any] | None = None
        self._authority_change_reason: str | None = None
        self._outbound: asyncio.Queue[dict[str, Any]] | None = None
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._authenticated = threading.Event()
        self._failure: BaseException | None = None
        self._halt = False
        self._commands: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ inspection

    @property
    def connection_epoch(self) -> int | None:
        return self._connection_epoch

    @property
    def roster_version(self) -> int:
        return self._roster_version

    @property
    def watchdog_state(self) -> str:
        """The wire value: ``nominal``, ``hold``, or ``failsafe``."""
        return self._watchdog.wire_state()

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        """Connect, join, and serve until ``stop()``, reconnecting with backoff."""
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        watchdog = asyncio.create_task(self._watchdog_loop())
        backoff = self.config.backoff_initial_s
        try:
            while not self._stop.is_set():
                try:
                    await self._serve()
                except asyncio.CancelledError:
                    raise
                except (NodeError, OSError, WebSocketException) as error:
                    self._on_link_lost()
                    if self._halt or not self.config.reconnect or self._stop.is_set():
                        raise NodeError(str(error)) from error
                    _LOGGER.warning("relay link lost (%s); reconnecting in %.1fs", error, backoff)
                    await self._sleep_until_stop(backoff)
                    backoff = min(self.config.backoff_max_s, backoff * 2)
                else:
                    self._on_link_lost()
                    backoff = self.config.backoff_initial_s
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            await self._cancel_commands()
            self._shutdown_device()
            self._authenticated.set()

    def start(self) -> None:
        """Run the node on its own thread and loop; return once it has authenticated."""
        if self._thread is not None:
            raise NodeError("node is already running")

        def runner() -> None:
            try:
                asyncio.run(self.run())
            except BaseException as error:  # surfaced to the starting thread
                self._failure = error
                self._authenticated.set()

        self._thread = threading.Thread(target=runner, name="sweep-node", daemon=True)
        self._thread.start()
        if not self._authenticated.wait(_STARTUP_TIMEOUT_S):
            raise NodeError("node did not authenticate in time")
        if self._failure is not None:
            raise NodeError(f"node failed to start: {self._failure}") from self._failure

    def stop(self) -> None:
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(stop.set)
        if self._thread is not None:
            self._thread.join(timeout=_STARTUP_TIMEOUT_S)

    # ------------------------------------------------------------------ extension seams

    def supported_operations(self) -> frozenset[str]:
        """The operations this node executes; the rest fail with ``unsupported_operation``."""
        return KIT_OPERATIONS

    def start_operation(self, command: protocol.Command) -> Execution:
        """Map one admitted command onto the device.

        ``goto`` and ``rotate_to`` start a motion the runtime then polls; ``hover`` holds
        in place and ``estop`` stops and then disables, both finishing at once.
        """
        args = command.args
        operation = command.operation
        if operation == "goto":
            return Execution.motion(
                self.device.move_to(
                    int(args["x_mm"]) / 1000,
                    int(args["y_mm"]) / 1000,
                    int(args["z_mm"]) / 1000,
                    int(args["speed_mm_s"]) / 1000,
                )
            )
        if operation == "rotate_to":
            return Execution.motion(
                self.device.rotate_to(
                    int(args["yaw_mdeg"]) / 1000, int(args["speed_mdeg_s"]) / 1000
                )
            )
        if operation == "hover":
            self.device.stop()
            return Execution.completed()
        if operation == "estop":
            self.device.stop()
            self.device.disable()
            return Execution.completed()
        return self._unsupported(operation)

    def extra_join_frames(self) -> list[dict[str, Any]]:
        """Frames to send after readiness, capabilities, and node_status on every join."""
        return []

    def drop_command(self, command: protocol.Command) -> bool:
        """Whether to swallow a command entirely: no admission, no acknowledgement."""
        return False

    def completion_delay_s(self, command: protocol.Command) -> float:
        """Seconds to wait between ``executing`` and running the operation."""
        return 0.0

    # ------------------------------------------------------------------ frames

    def emit(self, frame: dict[str, Any]) -> None:
        """Queue one node-authored frame; dropped when the socket is down."""
        outbound = self._outbound
        if outbound is not None:
            outbound.put_nowait(frame)

    def envelope(self, frame_type: str) -> dict[str, Any]:
        """A frame envelope with a fresh event id and a non-decreasing timestamp."""
        return protocol.envelope(
            frame_type,
            t=self.now_t(),
            event_id=self.config.event_ids(),
            session=self.config.session,
        )

    def now_t(self) -> int:
        """The frame clock: wall time, never allowed to go backwards inside one node."""
        self._last_t = max(self._last_t, self.config.clock_ms())
        return self._last_t

    def _envelope_fields(self, frame_type: str) -> dict[str, Any]:
        envelope = self.envelope(frame_type)
        return {
            "t": envelope["t"],
            "event_id": envelope["event_id"],
            "session": envelope["session"],
        }

    def telemetry_frame(self) -> dict[str, Any]:
        status = self.device.status()
        assert self._connection_epoch is not None
        return protocol.telemetry_frame(
            device_id=self.config.device_id,
            connection_epoch=self._connection_epoch,
            x=status.x,
            y=status.y,
            z=status.z,
            vx=status.vx,
            vy=status.vy,
            vz=status.vz,
            battery=status.battery,
            state=status.state,
            link=status.link,
            pos_quality=status.pos_quality,
            **self._envelope_fields("telemetry"),
        )

    def capabilities_frame(self) -> dict[str, Any]:
        assert self._connection_epoch is not None
        profile = self.device.hardware_profile()
        return protocol.capabilities_frame(
            device_id=self.config.device_id,
            connection_epoch=self._connection_epoch,
            profile=profile,
            **self._envelope_fields("capabilities"),
        )

    def node_status_body(self) -> dict[str, Any]:
        status = self.device.status()
        publisher = getattr(self.device, "video_publish_state", None)
        video = publisher() if callable(publisher) else "stopped"
        return {
            # Virtual sticks are a DJI flight-control surface; nothing else has them.
            "virtual_stick_enabled": False,
            "control_authority": status.control_authority,
            "authority_change_reason": self._authority_change_reason,
            "watchdog_state": self._watchdog.wire_state(),
            "video_publish_state": video,
            "phone_battery_percent": int(round(max(0.0, min(1.0, status.battery)) * 100)),
            "phone_thermal_state": "none",
        }

    def node_status_frame(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._connection_epoch is not None
        return protocol.node_status_frame(
            device_id=self.config.device_id,
            connection_epoch=self._connection_epoch,
            **self._envelope_fields("node_status"),
            **(self.node_status_body() if body is None else body),
        )

    # ------------------------------------------------------------------ the socket

    async def _serve(self) -> None:
        url = f"{self.config.relay_url}/ws/{self.config.session}"
        async with connect(url) as socket:
            await socket.send(
                json.dumps(
                    protocol.auth_frame(device_id=self.config.device_id, token=self.config.token)
                )
            )
            accepted = json.loads(await socket.recv())
            if not isinstance(accepted, dict) or accepted.get("type") != "auth.accepted":
                reason = accepted.get("reason") if isinstance(accepted, dict) else None
                if reason in HALT_REASONS:
                    self._halt = True
                raise NodeError(f"relay refused authentication: {reason}")
            settings = accepted.get("node")
            if isinstance(settings, dict):
                self.node_settings = settings
                self._apply_node_settings(settings)
            initial = json.loads(await socket.recv())
            if isinstance(initial, dict) and initial.get("type") == "state":
                self._roster_version = int(initial["roster_version"])
            self._outbound = asyncio.Queue()
            self.emit(self._join_frame())
            self._authenticated.set()
            tasks = [
                asyncio.create_task(self._send_loop(socket)),
                asyncio.create_task(self._receive_loop(socket)),
                asyncio.create_task(self._telemetry_loop()),
                asyncio.create_task(self._sensor_loop()),
                asyncio.create_task(self._stop.wait()),  # type: ignore[union-attr]
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert self._stop is not None
            if self._stop.is_set():
                await self._leave(socket)
                return
            for task in done:
                error = task.exception()
                if error is not None:
                    raise NodeError(f"node stopped: {error}") from error
            raise NodeError("relay closed the node socket")

    async def _leave(self, socket: Any) -> None:
        if not self.config.graceful_leave_on_stop or self._connection_epoch is None:
            return
        frame = protocol.graceful_leave_frame(
            device_id=self.config.device_id,
            connection_epoch=self._connection_epoch,
            key=self._key,
            **self._envelope_fields("membership"),
        )
        try:
            await socket.send(json.dumps(frame))
        except (OSError, WebSocketException) as error:  # the socket may already be gone
            _LOGGER.warning("graceful leave was not delivered: %s", error)

    async def _send_loop(self, socket: Any) -> None:
        assert self._outbound is not None
        while True:
            frame = await self._outbound.get()
            await socket.send(json.dumps(frame))

    async def _receive_loop(self, socket: Any) -> None:
        async for raw in socket:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            frame_type = frame.get("type")
            if frame_type == "command":
                self._handle_command(frame)
            elif frame_type == "control_heartbeat":
                self._handle_heartbeat(frame)
            elif frame_type == "membership" and frame.get("drone_id") == self.config.device_id:
                self._handle_membership(frame)
            elif frame_type == "state":
                roster = frame.get("roster_version")
                if isinstance(roster, int):
                    self._roster_version = roster
            elif frame_type == "auth.refused":
                if frame.get("reason") in HALT_REASONS:
                    self._halt = True
                raise NodeError("relay refused the connection: {}".format(frame.get("reason")))
            elif (
                frame_type == "refusal"
                and frame.get("source") == "relay"
                and frame.get("drone_id") == self.config.device_id
            ):
                # Autonomy refusals also name a device; only relay protocol refusals mean
                # this node's own frame was rejected.
                _LOGGER.warning(
                    "relay refused a node frame: %s (%s)", frame.get("reason"), frame.get("detail")
                )

    async def _telemetry_loop(self) -> None:
        interval = 1.0 / self.config.telemetry_hz
        while True:
            await asyncio.sleep(interval)
            if self._connection_epoch is not None:
                self.emit(self.telemetry_frame())

    async def _sensor_loop(self) -> None:
        reader = getattr(self.device, "latest_scan", None)
        if not callable(reader):
            return
        interval = 1.0 / self.config.sensor_hz
        while True:
            await asyncio.sleep(interval)
            if self._connection_epoch is None:
                continue
            scan = reader()
            if scan is None or scan.t_ms == self._last_scan_t:
                continue
            if not scan_is_valid(scan):
                _LOGGER.warning("dropping a scan whose bin count does not match its increment")
                continue
            self._last_scan_t = scan.t_ms
            try:
                frame = protocol.sensor_frame(
                    device_id=self.config.device_id,
                    connection_epoch=self._connection_epoch,
                    kind=protocol.LIDAR_SCAN,
                    pose=scan.pose,
                    angle_min_deg=scan.angle_min_deg,
                    angle_increment_deg=scan.angle_increment_deg,
                    range_min_m=scan.range_min_m,
                    range_max_m=scan.range_max_m,
                    ranges_cm=scan.ranges_cm,
                    **self._envelope_fields("sensor"),
                )
            except protocol.ProtocolError as error:
                _LOGGER.warning("dropping an unsendable scan: %s", error.detail)
                continue
            self.emit(frame)

    async def _watchdog_loop(self) -> None:
        """The control tick: watchdog transitions and a node_status when something moves.

        It outlives one socket on purpose, so the deadman keeps counting while the node
        is reconnecting.
        """
        while True:
            await asyncio.sleep(self.config.watchdog_poll_s)
            transition = self._watchdog.poll()
            if transition is not None:
                self._apply_watchdog(transition)
            self._emit_node_status_if_changed()

    async def _sleep_until_stop(self, seconds: float) -> None:
        assert self._stop is not None
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        # Python 3.9 and 3.10 raise asyncio.TimeoutError here, which is not the builtin.
        except asyncio.TimeoutError:  # noqa: UP041
            return

    # ------------------------------------------------------------------ membership

    def _join_frame(self) -> dict[str, Any]:
        return protocol.join_frame(
            device_id=self.config.device_id,
            adapter_id=self.config.adapter_id,
            device_class=self.device.device_class,
            capabilities=list(self.device.capabilities),
            key=self._key,
            **self._envelope_fields("membership"),
        )

    def _handle_membership(self, frame: dict[str, Any]) -> None:
        roster = frame.get("roster_version")
        if isinstance(roster, int):
            self._roster_version = roster
        epoch = frame.get("connection_epoch")
        if frame.get("action") != "join" or not isinstance(epoch, int):
            return
        self._connection_epoch = epoch
        self._last_seq = 0
        self._last_heartbeat_seq = 0
        self._last_scan_t = None
        self._last_node_status = None
        self._authority_change_reason = None
        self._watchdog.arm()
        control_authority = bool(self.device.enable())
        # Telemetry first: the relay captures the home pose from it when readiness
        # confirms one.
        self.emit(self.telemetry_frame())
        self.emit(self._readiness_frame(epoch, control_authority))
        self.emit(self.capabilities_frame())
        body = self.node_status_body()
        self._last_node_status = body
        self.emit(self.node_status_frame(body))
        for extra in self.extra_join_frames():
            self.emit(extra)

    def _readiness_frame(self, epoch: int, control_authority: bool) -> dict[str, Any]:
        return protocol.readiness_frame(
            device_id=self.config.device_id,
            connection_epoch=epoch,
            home_pose_confirmed=self.config.home_pose_confirmed,
            control_authority=control_authority,
            rc_safety_operator_present=self._safety_operator_present,
            key=self._key,
            **self._envelope_fields("membership"),
        )

    def set_safety_operator_present(self, present: bool) -> None:
        """Claim, or withdraw, the spotter beside the device.

        A ground vehicle's screen and an aircraft's RC pilot both change this while the
        node is running, so the claim is re-signed and re-sent at once and the relay's
        readiness gate follows within one state event. Safe to call from any thread.
        """
        self._safety_operator_present = bool(present)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._reclaim_readiness)

    def _reclaim_readiness(self) -> None:
        epoch = self._connection_epoch
        if epoch is None:
            return
        self.emit(self._readiness_frame(epoch, self.device.status().control_authority))

    # ------------------------------------------------------------------ heartbeats

    def _handle_heartbeat(self, raw: dict[str, Any]) -> None:
        try:
            heartbeat = protocol.parse_control_heartbeat(raw)
        except protocol.ProtocolError as error:
            _LOGGER.warning("dropping a malformed control_heartbeat: %s", error.detail)
            return
        if (
            heartbeat.session != self.config.session
            or heartbeat.drone_id != self.config.device_id
            or self._connection_epoch is None
            or heartbeat.connection_epoch != self._connection_epoch
            or heartbeat.roster_version != self._roster_version
        ):
            _LOGGER.debug("dropping a control_heartbeat with a stale connection identity")
            return
        if not heartbeat.verifies(self._key):
            _LOGGER.warning("dropping a control_heartbeat whose signature did not verify")
            return
        if heartbeat.seq <= self._last_heartbeat_seq:
            _LOGGER.warning("dropping a replayed control_heartbeat sequence %s", heartbeat.seq)
            return
        self._last_heartbeat_seq = heartbeat.seq
        self._watchdog.heartbeat()

    def _apply_watchdog(self, transition: WatchdogTransition) -> None:
        if transition.to_state == WATCHDOG_HOLD:
            self._authority_change_reason = protocol.WATCHDOG_HOLD
            self.device.stop()
            _LOGGER.warning(
                "watchdog hold after %s ms without an authorized control heartbeat; holding",
                transition.elapsed_ms,
            )
        elif transition.to_state == WATCHDOG_FAILSAFE:
            self._authority_change_reason = protocol.WATCHDOG_FAILSAFE
            self.device.disable()
            _LOGGER.error(
                "watchdog failsafe after %s ms without an authorized control heartbeat; "
                "the device is disabled until it rejoins",
                transition.elapsed_ms,
            )
        elif transition.to_state == ARMED:
            self._authority_change_reason = None
            _LOGGER.info("control heartbeats resumed; the watchdog is nominal")

    def _emit_node_status_if_changed(self) -> None:
        if self._connection_epoch is None or self._outbound is None:
            return
        body = self.node_status_body()
        if body == self._last_node_status:
            return
        self._last_node_status = body
        self.emit(self.node_status_frame(body))

    # ------------------------------------------------------------------ commands

    def _handle_command(self, raw: dict[str, Any]) -> None:
        try:
            command = protocol.parse_command(raw)
        except protocol.ProtocolError as error:
            _LOGGER.warning("dropping a malformed command: %s", error.detail)
            return
        if command.session != self.config.session or command.drone_id != self.config.device_id:
            return
        if self.drop_command(command):
            return
        admission = admit_command(
            command,
            session=self.config.session,
            device_id=self.config.device_id,
            key=self._key,
            connection_epoch=self._connection_epoch,
            roster_version=self._roster_version,
            last_seq=self._last_seq,
            now_ms=self.config.clock_ms(),
        )
        if not admission.admitted:
            if admission.acknowledge:
                self.emit(
                    self._acknowledgement(
                        command, protocol.FAILED, admission.reason, admission.detail
                    )
                )
            else:
                _LOGGER.warning(
                    "dropped command %s without an acknowledgement: %s (%s)",
                    command.command_id,
                    admission.reason,
                    admission.detail,
                )
            return
        self._last_seq = command.seq
        # Failsafe refuses; a hold does not. The next lease may arrive at any moment and
        # the device has already stopped, so a held node still runs what the relay sends.
        if self._watchdog.state == WATCHDOG_FAILSAFE:
            self.emit(
                self._acknowledgement(
                    command,
                    protocol.FAILED,
                    protocol.WATCHDOG_FAILSAFE,
                    "watchdog is in failsafe after relay silence; it re-arms on the next join",
                )
            )
            return
        if command.operation in protocol.MOTION_OPERATIONS:
            if not self.device.status().control_authority:
                self.emit(
                    self._acknowledgement(
                        command,
                        protocol.FAILED,
                        protocol.AUTHORITY_LOST,
                        "the device has not granted control authority; "
                        f"{command.operation} refused",
                    )
                )
                return
        self.emit(self._acknowledgement(command, protocol.ACCEPTED))
        task = asyncio.ensure_future(self._run_command(command))
        self._commands.add(task)
        task.add_done_callback(self._commands.discard)

    async def _run_command(self, command: protocol.Command) -> None:
        if command.operation not in self.supported_operations():
            unsupported = self._unsupported(command.operation)
            self.emit(
                self._acknowledgement(
                    command, protocol.FAILED, unsupported.reason, unsupported.detail
                )
            )
            return
        self.emit(self._acknowledgement(command, protocol.EXECUTING))
        delay = self.completion_delay_s(command)
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            execution = self.start_operation(command)
        except Exception as error:  # a device fault is a failed command, not a dead node
            _LOGGER.exception("device failed while running %s", command.operation)
            self.emit(self._acknowledgement(command, protocol.FAILED, "device_failure", f"{error}"))
            return
        if execution.motion_id is None:
            self.emit(
                self._acknowledgement(
                    command,
                    execution.status or protocol.COMPLETED,
                    execution.reason,
                    execution.detail,
                )
            )
            return
        await self._await_motion(command, execution.motion_id)

    async def _await_motion(self, command: protocol.Command, motion_id: str) -> None:
        deadline = self.config.monotonic() + self._command_ttl_ms / 1000
        while True:
            done = self.device.motion_done(motion_id)
            if done is True:
                self.emit(self._acknowledgement(command, protocol.COMPLETED))
                return
            if done is None:
                self.emit(
                    self._acknowledgement(
                        command,
                        protocol.FAILED,
                        "motion_failed",
                        f"the device abandoned motion {motion_id}",
                    )
                )
                return
            if self.config.monotonic() >= deadline:
                self.device.stop()
                self.emit(
                    self._acknowledgement(
                        command,
                        protocol.FAILED,
                        "motion_timeout",
                        f"motion {motion_id} did not finish within "
                        f"{self._command_ttl_ms} ms; the device is holding",
                    )
                )
                return
            await asyncio.sleep(_MOTION_POLL_S)

    def _acknowledgement(
        self,
        command: protocol.Command,
        status: str,
        reason: str | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        return protocol.acknowledgement_frame(
            device_id=self.config.device_id,
            command=command,
            status=status,
            reason=reason,
            detail=detail,
            **self._envelope_fields("acknowledgement"),
        )

    def _unsupported(self, operation: str) -> Execution:
        return Execution.failed(
            protocol.UNSUPPORTED_OPERATION,
            f"{operation} is not available for a {self.device.device_class}",
        )

    # ------------------------------------------------------------------ teardown

    def _apply_node_settings(self, settings: dict[str, Any]) -> None:
        ttl = settings.get("command_ttl_ms")
        if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl > 0:
            self._command_ttl_ms = ttl
        hold = settings.get("watchdog_hold_ms")
        failsafe = settings.get("watchdog_failsafe_ms")
        if isinstance(hold, int) and isinstance(failsafe, int):
            try:
                self._watchdog.configure(hold, failsafe)
            except ValueError:
                _LOGGER.warning(
                    "relay sent watchdog thresholds hold=%s failsafe=%s; keeping the configured "
                    "%s and %s",
                    hold,
                    failsafe,
                    self._watchdog.hold_ms,
                    self._watchdog.failsafe_ms,
                )

    def _on_link_lost(self) -> None:
        self._connection_epoch = None
        self._outbound = None
        self._last_node_status = None
        # Beyond hold there is no way back: no lease can arrive on a socket that is gone.
        if self._watchdog.state == WATCHDOG_HOLD:
            transition = self._watchdog.trip_failsafe()
            if transition is not None:
                self._apply_watchdog(transition)

    async def _cancel_commands(self) -> None:
        tasks = tuple(self._commands)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._commands.clear()

    def _shutdown_device(self) -> None:
        try:
            self.device.stop()
        except Exception:  # a clean stop must not mask why the node is stopping
            _LOGGER.exception("the device raised while stopping")
        close = getattr(self.device, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                _LOGGER.exception("the device raised while closing")

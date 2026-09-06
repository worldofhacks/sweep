"""Fake bridge node: a WebSocket client that behaves like the phone app on the wire.

Run it against a relay from the repo root:

    uv run python -m adapters.dji_mini3.fake_node --drone-id 1

The node authenticates as an adapter, sends a signed join and readiness, streams
telemetry, publishes capabilities, node_status, and capture_readiness, verifies every
relay-signed command against its own key, and acknowledges accepted, executing, then
completed or failed. Its aircraft is a kinematic fixture, not a flight model, and every
hardware profile field says so. ``FakeNodeConfig.silent_operations`` and
``slow_operations`` make it swallow or delay acknowledgements for tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256

from websockets.asyncio.client import connect

from planner.models import CommandOperation
from relay.auth import sign_event, verify_event_signature
from relay.contracts import (
    CommandFrame,
    ContractError,
    NodeAcknowledgementReason,
    parse_command,
)

_LOGGER = logging.getLogger(__name__)
_STARTUP_TIMEOUT_S = 10.0


class FakeNodeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FakeNodeConfig:
    relay_url: str
    session: str
    drone_id: int
    token: str
    adapter_id: str
    telemetry_hz: float = 10.0
    capabilities: tuple[str, ...] = ("flight", "pano_360", "reconstruct_8")
    home: tuple[float, float, float] = (0.0, 0.0, 0.0)
    horizontal_fov_deg: float = 66.0
    gimbal_pitch_min_deg: float = -90.0
    gimbal_pitch_max_deg: float = 30.0
    photo_width_px: int = 1_920
    photo_height_px: int = 1_080
    panorama_width_px: int = 4_096
    storage_remaining_bytes: int = 50_000_000
    silent_operations: tuple[str, ...] = ()
    slow_operations: tuple[str, ...] = ()
    slow_ack_delay_s: float = 0.0

    def __post_init__(self) -> None:
        if self.drone_id <= 0:
            raise ValueError("drone_id must be a positive integer")
        if not self.token:
            raise ValueError("token must be a non-empty string")
        if not 0 < self.telemetry_hz <= 50:
            raise ValueError("telemetry_hz must be between 0 and 50")
        for name in (*self.silent_operations, *self.slow_operations):
            CommandOperation(name)
        if not 0 <= self.slow_ack_delay_s <= 60:
            raise ValueError("slow_ack_delay_s must be between 0 and 60 seconds")


@dataclass(slots=True)
class _Aircraft:
    x: float
    y: float
    z: float
    yaw_deg: float = 0.0
    gimbal_pitch_deg: float = 0.0
    state: str = "landed"
    battery: float = 0.8
    link: float = 0.9
    pos_quality: float = 0.95


class FakeNode:
    def __init__(self, config: FakeNodeConfig) -> None:
        self.config = config
        self.node_settings: dict[str, object] | None = None
        self._key = config.token.encode()
        self._aircraft = _Aircraft(*config.home)
        self._connection_epoch: int | None = None
        self._roster_version = 0
        self._last_seq = 0
        self._last_t = 0
        self._media_t = 0
        self._frame_counts: dict[str, int] = {}
        self._media: dict[str, dict[str, object]] = {}
        self._outbound: asyncio.Queue[dict[str, object]] | None = None
        self._telemetry_pending = False
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._authenticated = threading.Event()
        self._failure: BaseException | None = None

    @property
    def connection_epoch(self) -> int | None:
        return self._connection_epoch

    async def run(self) -> None:
        """Connect, join, and serve until ``stop()`` is called or the socket closes."""
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._outbound = asyncio.Queue()
        try:
            async with connect(f"{self.config.relay_url}/ws/{self.config.session}") as socket:
                await socket.send(
                    json.dumps(
                        {
                            "v": 1,
                            "type": "auth",
                            "source": "adapter",
                            "drone_id": self.config.drone_id,
                            "token": self.config.token,
                        }
                    )
                )
                accepted = json.loads(await socket.recv())
                if accepted.get("type") != "auth.accepted":
                    raise FakeNodeError(f"relay refused authentication: {accepted.get('reason')}")
                node_settings = accepted.get("node")
                self.node_settings = node_settings if isinstance(node_settings, dict) else None
                initial = json.loads(await socket.recv())
                if initial.get("type") == "state":
                    self._roster_version = int(initial["roster_version"])
                self._enqueue(
                    self._signed_membership(
                        "join",
                        adapter_id=self.config.adapter_id,
                        capabilities=list(self.config.capabilities),
                    )
                )
                self._authenticated.set()
                tasks = [
                    asyncio.create_task(self._send_loop(socket)),
                    asyncio.create_task(self._receive_loop(socket)),
                    asyncio.create_task(self._telemetry_loop()),
                    asyncio.create_task(self._stop.wait()),
                ]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if not self._stop.is_set():
                    for task in done:
                        error = task.exception()
                        if error is not None:
                            raise FakeNodeError(f"node stopped: {error}") from error
                    raise FakeNodeError("relay closed the node socket")
        finally:
            self._authenticated.set()

    def start(self) -> None:
        """Run the node on its own thread and loop; return once it has authenticated."""
        if self._thread is not None:
            raise FakeNodeError("node is already running")

        def runner() -> None:
            try:
                asyncio.run(self.run())
            except BaseException as error:  # surfaced to the starting thread
                self._failure = error
                self._authenticated.set()

        self._thread = threading.Thread(target=runner, name="fake-node", daemon=True)
        self._thread.start()
        if not self._authenticated.wait(_STARTUP_TIMEOUT_S):
            raise FakeNodeError("node did not authenticate in time")
        if self._failure is not None:
            raise FakeNodeError(f"node failed to start: {self._failure}") from self._failure

    def stop(self) -> None:
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(stop.set)
        if self._thread is not None:
            self._thread.join(timeout=_STARTUP_TIMEOUT_S)

    async def _send_loop(self, socket: object) -> None:
        assert self._outbound is not None
        while True:
            frame = await self._outbound.get()
            if frame["type"] == "telemetry":
                self._telemetry_pending = False
            await socket.send(json.dumps(frame))  # type: ignore[attr-defined]

    async def _receive_loop(self, socket: object) -> None:
        async for raw in socket:  # type: ignore[attr-defined]
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            frame_type = frame.get("type")
            if frame_type == "command":
                self._handle_command(frame)
            elif frame_type == "membership" and frame.get("drone_id") == self.config.drone_id:
                self._handle_membership(frame)
            elif frame_type == "state":
                self._roster_version = int(frame.get("roster_version", self._roster_version))
            elif (
                frame_type == "refusal"
                and frame.get("source") == "relay"
                and frame.get("drone_id") == self.config.drone_id
            ):
                # Autonomy refusals also name an aircraft; only relay protocol refusals
                # mean this node's own frame was rejected.
                _LOGGER.warning(
                    "relay refused a node frame: %s (%s)", frame.get("reason"), frame.get("detail")
                )

    async def _telemetry_loop(self) -> None:
        interval = 1.0 / self.config.telemetry_hz
        while True:
            await asyncio.sleep(interval)
            if self._connection_epoch is not None:
                self._enqueue_telemetry()

    def _handle_membership(self, frame: dict[str, object]) -> None:
        epoch = frame.get("connection_epoch")
        roster_version = frame.get("roster_version")
        if isinstance(roster_version, int):
            self._roster_version = roster_version
        if frame.get("action") != "join" or not isinstance(epoch, int):
            return
        self._connection_epoch = epoch
        self._last_seq = 0
        self._enqueue_telemetry()
        self._enqueue(
            self._signed_membership(
                "readiness",
                connection_epoch=epoch,
                home_pose_confirmed=True,
                control_authority=True,
                rc_safety_operator_present=True,
            )
        )
        self._enqueue(self._capabilities_frame())
        self._enqueue(self._node_status_frame())
        self._enqueue(self._capture_readiness_frame())

    def _handle_command(self, raw: dict[str, object]) -> None:
        try:
            frame = parse_command(raw)
        except ContractError as error:
            _LOGGER.warning("dropping a malformed command: %s", error.detail)
            return
        if frame.session != self.config.session or frame.drone_id != self.config.drone_id:
            return
        if not verify_event_signature(frame.unsigned_event(), frame.signature, self._key):
            _LOGGER.warning("dropping command %s with an invalid signature", frame.command_id)
            return
        if frame.operation.value in self.config.silent_operations:
            return  # a silent node: no admission, no acknowledgement, no state change
        refusal = self._admission_refusal(frame)
        if refusal is not None:
            reason, detail = refusal
            self._enqueue(
                self._acknowledgement(frame, "failed", reason=reason.value, detail=detail)
            )
            return
        self._last_seq = frame.seq
        self._enqueue(self._acknowledgement(frame, "accepted"))
        self._enqueue(self._acknowledgement(frame, "executing"))
        if frame.operation.value in self.config.slow_operations and self.config.slow_ack_delay_s:
            assert self._loop is not None
            self._loop.call_later(self.config.slow_ack_delay_s, self._finish_command, frame)
            return
        self._finish_command(frame)

    def _finish_command(self, frame: CommandFrame) -> None:
        status, reason, detail = self._execute(frame)
        if status == "completed" and self._connection_epoch is not None:
            self._enqueue_telemetry()
        self._enqueue(self._acknowledgement(frame, status, reason=reason, detail=detail))

    def _admission_refusal(
        self, frame: CommandFrame
    ) -> tuple[NodeAcknowledgementReason, str] | None:
        now = _epoch_ms()
        if frame.connection_epoch != self._connection_epoch:
            return (
                NodeAcknowledgementReason.STALE_COMMAND,
                f"command epoch {frame.connection_epoch} is not the node epoch",
            )
        if frame.roster_version != self._roster_version:
            return (
                NodeAcknowledgementReason.STALE_COMMAND,
                f"command roster {frame.roster_version} differs from the last state "
                f"{self._roster_version}",
            )
        if frame.issued_at + frame.ttl_ms < now:
            return NodeAcknowledgementReason.STALE_COMMAND, "command is older than its ttl"
        if frame.seq <= self._last_seq:
            return (
                NodeAcknowledgementReason.OUT_OF_ORDER_COMMAND,
                f"seq {frame.seq} after seq {self._last_seq}",
            )
        return None

    def _execute(self, frame: CommandFrame) -> tuple[str, str | None, str | None]:
        aircraft = self._aircraft
        args = frame.args
        operation = frame.operation
        if operation is CommandOperation.TAKEOFF:
            aircraft.z = int(args["z_mm"]) / 1000
            aircraft.state = "hovering"
        elif operation is CommandOperation.GOTO:
            aircraft.x = int(args["x_mm"]) / 1000
            aircraft.y = int(args["y_mm"]) / 1000
            aircraft.z = int(args["z_mm"]) / 1000
            aircraft.state = "hovering"
        elif operation is CommandOperation.ROTATE_TO:
            aircraft.yaw_deg = int(args["yaw_mdeg"]) / 1000
        elif operation is CommandOperation.HOVER:
            if aircraft.state != "landed":
                aircraft.state = "hovering"
        elif operation is CommandOperation.LAND:
            aircraft.z = self.config.home[2]
            aircraft.state = "landed"
        elif operation is CommandOperation.ESTOP:
            if aircraft.state != "landed":
                aircraft.state = "hovering"
        elif operation is CommandOperation.CAMERA_CAPABILITIES:
            self._enqueue(self._capabilities_frame())
        elif operation is CommandOperation.SET_GIMBAL_PITCH:
            pitch = int(args["pitch_mdeg"]) / 1000
            if not self.config.gimbal_pitch_min_deg <= pitch <= self.config.gimbal_pitch_max_deg:
                return "failed", "camera_failure", "gimbal pitch is outside the fixture range"
            aircraft.gimbal_pitch_deg = pitch
        elif operation is CommandOperation.CAMERA_READY:
            pass
        elif operation is CommandOperation.CAPTURE_PANORAMA:
            capture_id = str(args["capture_id"])
            self._enqueue(
                self._media_file_frame(
                    self._media_record(
                        capture_id,
                        f"{capture_id}-pano-360",
                        width=self.config.panorama_width_px,
                        height=self.config.panorama_width_px // 2,
                        horizontal_fov_deg=360.0,
                        projection="equirectangular",
                    )
                )
            )
        elif operation is CommandOperation.CAPTURE_PHOTO:
            capture_id = str(args["capture_id"])
            frame_number = self._frame_counts.get(capture_id, 0) + 1
            self._frame_counts[capture_id] = frame_number
            self._enqueue(
                self._media_file_frame(
                    self._media_record(
                        capture_id,
                        f"{capture_id}-frame-{frame_number:02d}",
                        width=self.config.photo_width_px,
                        height=self.config.photo_height_px,
                        horizontal_fov_deg=self.config.horizontal_fov_deg,
                        projection="rectilinear",
                    )
                )
            )
        elif operation is CommandOperation.RETRIEVE_MEDIA:
            record = self._media.get(str(args["file_id"]))
            if record is None:
                return "failed", "download_failure", "the node has no such file"
            self._enqueue(self._media_file_frame(record))
        return "completed", None, None

    def _enqueue(self, frame: dict[str, object]) -> None:
        assert self._outbound is not None
        self._outbound.put_nowait(frame)

    def _enqueue_telemetry(self) -> None:
        if self._telemetry_pending:
            return
        assert self._outbound is not None
        self._telemetry_pending = True
        self._outbound.put_nowait(self._telemetry_frame())

    def _next_t(self) -> int:
        self._last_t = max(self._last_t, _epoch_ms())
        return self._last_t

    def _envelope(self, frame_type: str) -> dict[str, object]:
        return {
            "v": 1,
            "t": self._next_t(),
            "type": frame_type,
            "event_id": str(uuid.uuid4()),
            "session": self.config.session,
        }

    def _signed_membership(self, action: str, **fields: object) -> dict[str, object]:
        frame = {
            **self._envelope("membership"),
            "drone_id": self.config.drone_id,
            "action": action,
            **fields,
        }
        frame["signature"] = sign_event(frame, self._key)
        return frame

    def _acknowledgement(
        self,
        frame: CommandFrame,
        status: str,
        *,
        reason: str | None = None,
        detail: str | None = None,
    ) -> dict[str, object]:
        return {
            **self._envelope("acknowledgement"),
            "intent_id": frame.intent_id,
            "command_id": frame.command_id,
            "status": status,
            "drone_id": self.config.drone_id,
            "connection_epoch": frame.connection_epoch,
            "roster_version": frame.roster_version,
            "reason": reason,
            "detail": detail,
        }

    def _telemetry_frame(self) -> dict[str, object]:
        aircraft = self._aircraft
        return {
            **self._envelope("telemetry"),
            "drone": self.config.drone_id,
            "connection_epoch": self._connection_epoch,
            "x": aircraft.x,
            "y": aircraft.y,
            "z": aircraft.z,
            "vx": 0.0,
            "vy": 0.0,
            "vz": 0.0,
            "battery": aircraft.battery,
            "state": aircraft.state,
            "link": aircraft.link,
            "pos_quality": aircraft.pos_quality,
        }

    def _capabilities_frame(self) -> dict[str, object]:
        return {
            **self._envelope("capabilities"),
            "drone_id": self.config.drone_id,
            "connection_epoch": self._connection_epoch,
            "native_panorama_modes": (
                ["pano_360"] if "pano_360" in self.config.capabilities else []
            ),
            "photo_capture": True,
            "gimbal_pitch_min_deg": self.config.gimbal_pitch_min_deg,
            "gimbal_pitch_max_deg": self.config.gimbal_pitch_max_deg,
            "horizontal_fov_deg": self.config.horizontal_fov_deg,
            "storage_remaining_bytes": self.config.storage_remaining_bytes,
            "media_retrieval": True,
            "aircraft_model": "fake-mini3",
            "aircraft_firmware": "fake",
            "rc_firmware": "fake",
            "phone_model": "fake-node",
            "android_version": "fake",
            "sdk_version": "fake",
            "measured_hfov_deg": None,
        }

    def _node_status_frame(self) -> dict[str, object]:
        return {
            **self._envelope("node_status"),
            "drone_id": self.config.drone_id,
            "connection_epoch": self._connection_epoch,
            "virtual_stick_enabled": False,
            "control_authority": True,
            "authority_change_reason": None,
            "watchdog_state": "nominal",
            "video_publish_state": "stopped",
            "phone_battery_percent": 81,
            "phone_thermal_state": "none",
        }

    def _capture_readiness_frame(self) -> dict[str, object]:
        return {
            **self._envelope("capture_readiness"),
            "drone_id": self.config.drone_id,
            "connection_epoch": self._connection_epoch,
            "room_id": None,
            "capture_id": None,
            "guidance_mode": "visual_advisory",
            "pose_source": "operator_approved",
            "pose_ok": True,
            "clearance_ok": True,
            "camera_ok": True,
            "storage_ok": True,
            "motion_ok": True,
            "image_quality_ok": True,
            "coverage_missing": [],
            "next_heading_deg": None,
            "suggested_delta": None,
        }

    def _media_record(
        self,
        capture_id: str,
        file_id: str,
        *,
        width: int,
        height: int,
        horizontal_fov_deg: float,
        projection: str,
    ) -> dict[str, object]:
        aircraft = self._aircraft
        self._media_t = max(self._media_t + 1, self._next_t())
        payload = (
            f"{self.config.drone_id}|{self._connection_epoch}|{capture_id}|{file_id}|"
            f"{aircraft.x}|{aircraft.y}|{aircraft.z}|{aircraft.yaw_deg}|"
            f"{aircraft.gimbal_pitch_deg}|{width}|{height}|{projection}"
        ).encode()
        record: dict[str, object] = {
            "capture_id": capture_id,
            "file_id": file_id,
            "timestamp_ms": self._media_t,
            "drone_id": self.config.drone_id,
            "connection_epoch": self._connection_epoch,
            "pose": {"x": aircraft.x, "y": aircraft.y, "z": aircraft.z},
            "actual_yaw_deg": aircraft.yaw_deg,
            "gimbal_pitch_deg": aircraft.gimbal_pitch_deg,
            "intrinsics": {
                "width_px": width,
                "height_px": height,
                "horizontal_fov_deg": horizontal_fov_deg,
                "projection": projection,
            },
            "checksum_sha256": sha256(payload).hexdigest(),
            "storage_ref": f"fake-node://media/{self.config.drone_id}/{file_id}",
            "retrieval_status": "completed",
        }
        self._media[file_id] = record
        return record

    def _media_file_frame(self, record: dict[str, object]) -> dict[str, object]:
        return {**self._envelope("media_file"), **record}


def _epoch_ms() -> int:
    return time.time_ns() // 1_000_000


def _token_from_environment(drone_id: int) -> str:
    raw_keys = os.environ.get("SWEEP_ADAPTER_KEYS_JSON", "{}")
    try:
        keys = json.loads(raw_keys)
    except json.JSONDecodeError:
        keys = {}
    key = keys.get(str(drone_id)) if isinstance(keys, dict) else None
    if isinstance(key, str) and key:
        return key
    shared = os.environ.get("SWEEP_RELAY_TOKEN", "")
    if shared:
        return shared
    raise SystemExit(
        "no credential: pass --token, or set SWEEP_ADAPTER_KEYS_JSON for this drone, "
        "or SWEEP_RELAY_TOKEN with SWEEP_ALLOW_SHARED_ADAPTER_TOKEN=true on the relay"
    )


def parse_args(argv: Sequence[str] | None = None) -> FakeNodeConfig:
    parser = argparse.ArgumentParser(
        prog="python -m adapters.dji_mini3.fake_node",
        description="Run a fake bridge node against a relay so the console shows a real "
        "registry entry before any hardware exists.",
    )
    parser.add_argument("--relay", default="ws://127.0.0.1:8000", help="relay WebSocket origin")
    parser.add_argument("--session", default="demo", help="relay session ID")
    parser.add_argument("--drone-id", type=int, required=True, help="stable positive drone ID")
    parser.add_argument(
        "--token",
        default=None,
        help="adapter credential; defaults to SWEEP_ADAPTER_KEYS_JSON or SWEEP_RELAY_TOKEN",
    )
    parser.add_argument("--adapter-id", default=None, help="adapter_id sent in the signed join")
    parser.add_argument("--telemetry-hz", type=float, default=10.0, help="telemetry rate")
    args = parser.parse_args(argv)
    return FakeNodeConfig(
        relay_url=args.relay.rstrip("/"),
        session=args.session,
        drone_id=args.drone_id,
        token=args.token or _token_from_environment(args.drone_id),
        adapter_id=args.adapter_id or f"fake-node-{args.drone_id}",
        telemetry_hz=args.telemetry_hz,
    )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = parse_args(argv)
    node = FakeNode(config)
    _LOGGER.info(
        "connecting drone %s to %s/ws/%s", config.drone_id, config.relay_url, config.session
    )
    try:
        asyncio.run(node.run())
    except KeyboardInterrupt:
        return 0
    except (FakeNodeError, OSError) as error:
        _LOGGER.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

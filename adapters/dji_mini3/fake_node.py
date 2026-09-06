"""Fake bridge node: a WebSocket client that behaves like the phone app on the wire.

Run it against a relay from the repo root:

    uv run python -m adapters.dji_mini3.fake_node --drone-id 1

It is the reference node kit (``nodekit/``) bound to a fixture aircraft, plus the parts of
the command set only a DJI Mini 3 has: takeoff and land, the gimbal, panorama and photo
capture, and media retrieval. The kit owns authentication, the signed join and readiness,
telemetry, command admission, the control-heartbeat deadman, and reconnection; this module
owns the aircraft's behaviour. Its aircraft is a kinematic fixture, not a flight model, and
every hardware profile field says so. ``FakeNodeConfig.silent_operations`` and
``slow_operations`` make it swallow or delay acknowledgements for tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from nodekit import protocol
from nodekit.fake import FakeAircraft
from nodekit.node import Execution, Node, NodeConfig, NodeError
from planner.models import CommandOperation

_LOGGER = logging.getLogger(__name__)

# The kit raises ``NodeError``; the name stays so existing callers keep working.
FakeNodeError = NodeError


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


class FakeNode(Node):
    """The kit node with a DJI Mini 3's operations behind it."""

    def __init__(self, config: FakeNodeConfig) -> None:
        self.fixture = config
        self.aircraft = FakeAircraft(
            home=config.home,
            capabilities=config.capabilities,
            profile={
                "native_panorama_modes": (
                    ["pano_360"] if "pano_360" in config.capabilities else []
                ),
                "photo_capture": True,
                "gimbal_pitch_min_deg": config.gimbal_pitch_min_deg,
                "gimbal_pitch_max_deg": config.gimbal_pitch_max_deg,
                "horizontal_fov_deg": config.horizontal_fov_deg,
                "storage_remaining_bytes": config.storage_remaining_bytes,
                "media_retrieval": True,
                "aircraft_model": "fake-mini3",
                "aircraft_firmware": "fake",
                "rc_firmware": "fake",
                "phone_model": "fake-node",
                "android_version": "fake",
                "sdk_version": "fake",
                "measured_hfov_deg": None,
            },
        )
        super().__init__(
            NodeConfig(
                relay_url=config.relay_url,
                session=config.session,
                device_id=config.drone_id,
                token=config.token,
                adapter_id=config.adapter_id,
                telemetry_hz=config.telemetry_hz,
            ),
            self.aircraft,
        )
        self._media: dict[str, dict[str, object]] = {}
        self._frame_counts: dict[str, int] = {}
        self._media_t = 0

    # ------------------------------------------------------------------ kit seams

    def supported_operations(self) -> frozenset[str]:
        """A Mini 3 node speaks the whole command set."""
        return protocol.COMMAND_OPERATIONS

    def drop_command(self, command: protocol.Command) -> bool:
        # A silent node: no admission, no acknowledgement, no state change.
        return command.operation in self.fixture.silent_operations

    def completion_delay_s(self, command: protocol.Command) -> float:
        if command.operation in self.fixture.slow_operations:
            return self.fixture.slow_ack_delay_s
        return 0.0

    def extra_join_frames(self) -> list[dict[str, Any]]:
        return [self._capture_readiness_frame()]

    def start_operation(self, command: protocol.Command) -> Execution:
        args = command.args
        operation = command.operation
        if operation == CommandOperation.TAKEOFF.value:
            self.aircraft.takeoff(int(args["z_mm"]) / 1000)
        elif operation == CommandOperation.LAND.value:
            self.aircraft.land()
        elif operation == CommandOperation.CAMERA_CAPABILITIES.value:
            self.emit(self.capabilities_frame())
        elif operation == CommandOperation.SET_GIMBAL_PITCH.value:
            pitch = int(args["pitch_mdeg"]) / 1000
            if not (
                self.fixture.gimbal_pitch_min_deg <= pitch <= self.fixture.gimbal_pitch_max_deg
            ):
                return Execution.failed(
                    "camera_failure", "gimbal pitch is outside the fixture range"
                )
            self.aircraft.set_gimbal_pitch(pitch)
        elif operation == CommandOperation.CAMERA_READY.value:
            pass
        elif operation == CommandOperation.CAPTURE_PANORAMA.value:
            capture_id = str(args["capture_id"])
            self.emit(
                self._media_file_frame(
                    self._media_record(
                        capture_id,
                        f"{capture_id}-pano-360",
                        width=self.fixture.panorama_width_px,
                        height=self.fixture.panorama_width_px // 2,
                        horizontal_fov_deg=360.0,
                        projection="equirectangular",
                    )
                )
            )
        elif operation == CommandOperation.CAPTURE_PHOTO.value:
            capture_id = str(args["capture_id"])
            frame_number = self._frame_counts.get(capture_id, 0) + 1
            self._frame_counts[capture_id] = frame_number
            self.emit(
                self._media_file_frame(
                    self._media_record(
                        capture_id,
                        f"{capture_id}-frame-{frame_number:02d}",
                        width=self.fixture.photo_width_px,
                        height=self.fixture.photo_height_px,
                        horizontal_fov_deg=self.fixture.horizontal_fov_deg,
                        projection="rectilinear",
                    )
                )
            )
        elif operation == CommandOperation.RETRIEVE_MEDIA.value:
            record = self._media.get(str(args["file_id"]))
            if record is None:
                return Execution.failed("download_failure", "the node has no such file")
            self.emit(self._media_file_frame(record))
        else:
            # goto, rotate_to, hover, and estop are the kit's own mapping.
            return super().start_operation(command)
        return Execution.completed()

    # ------------------------------------------------------------------ node frames

    def _capture_readiness_frame(self) -> dict[str, Any]:
        return {
            **self.envelope("capture_readiness"),
            "drone_id": self.fixture.drone_id,
            "connection_epoch": self.connection_epoch,
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
        aircraft = self.aircraft
        self._media_t = max(self._media_t + 1, self.now_t())
        payload = (
            f"{self.fixture.drone_id}|{self.connection_epoch}|{capture_id}|{file_id}|"
            f"{aircraft.x}|{aircraft.y}|{aircraft.z}|{aircraft.yaw_deg}|"
            f"{aircraft.gimbal_pitch_deg}|{width}|{height}|{projection}"
        ).encode()
        record: dict[str, object] = {
            "capture_id": capture_id,
            "file_id": file_id,
            "timestamp_ms": self._media_t,
            "drone_id": self.fixture.drone_id,
            "connection_epoch": self.connection_epoch,
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
            "storage_ref": f"fake-node://media/{self.fixture.drone_id}/{file_id}",
            "retrieval_status": "completed",
        }
        self._media[file_id] = record
        return record

    def _media_file_frame(self, record: dict[str, object]) -> dict[str, Any]:
        return {**self.envelope("media_file"), **record}


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

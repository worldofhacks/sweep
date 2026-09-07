from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from relay.auth import sign_event

from .models import GroundStatus, RangeScan
from .return_controller import ApprovedReturnRoute, ReturnController, read_approval_key

APPROVAL_KEY = b"return-approval-key-that-is-at-least-32-bytes"


def _record(tmp_path: Path) -> Path:
    geometry = {
        "type": "measured_corridor_v1",
        "start": {"x_m": 0.0, "y_m": 0.0},
        "segments": [
            {
                "target": {"x_m": 0.0, "y_m": 0.1},
                "footprint": [
                    {"x_m": -0.5, "y_m": -0.5},
                    {"x_m": 0.5, "y_m": -0.5},
                    {"x_m": 0.5, "y_m": 0.5},
                    {"x_m": -0.5, "y_m": 0.5},
                ],
            }
        ],
    }
    geometry_bytes = json.dumps(geometry, separators=(",", ":")).encode()
    unsigned = {
        "v": 1,
        "return_id": "room-a-return",
        "approval_id": "approval-17",
        "approval_signer": "map-operator",
        "session": "session-a",
        "device_id": 9,
        "connection_epoch": 4,
        "odom_origin_id": "origin-7",
        "source_registration_id": "registration-9",
        "pose_source_id": "ohmni-pose",
        "odom_frame": "odom",
        "world_to_odom": {
            "x_m": 0.0,
            "y_m": 0.0,
            "yaw_deg": 0.0,
            "registration_id": "registration-9",
        },
        "geometry_sha256": hashlib.sha256(geometry_bytes).hexdigest(),
        "geometry_bytes_b64": base64.b64encode(geometry_bytes).decode(),
        "footprint_radius_m": 0.25,
        "arrival_tolerance_m": 0.03,
    }
    path = tmp_path / "approved-return.json"
    path.write_text(json.dumps({**unsigned, "signature": sign_event(unsigned, APPROVAL_KEY)}))
    return path


class _Device:
    def __init__(self) -> None:
        self.x = self.y = self.yaw = 0.0
        self.drives: list[tuple[float, float, float]] = []
        self.stopped = False

    def status(self) -> GroundStatus:
        return GroundStatus(
            self.x,
            self.y,
            self.yaw,
            0.0,
            0.0,
            1.0,
            1.0,
            1.0,
            "idle",
            True,
            0,
        )

    def scan(self) -> RangeScan:
        return RangeScan(0, (self.x, self.y, self.yaw), 0.0, 1.0, 0.15, 12.0, [400] * 360)

    def drive_velocity(self, linear: float, yaw: float, duration: float) -> str:
        self.drives.append((linear, yaw, duration))
        if linear:
            import math

            self.x += linear * duration * math.cos(math.radians(self.yaw))
            self.y += linear * duration * math.sin(math.radians(self.yaw))
        else:
            self.yaw += yaw * duration
        return str(len(self.drives))

    def motion_done(self, _motion: str) -> bool:
        return True

    def stop(self) -> None:
        self.stopped = True


async def _fast_sleep(_seconds: float) -> None:
    return None


def _controller(
    route: ApprovedReturnRoute, device: _Device, *, grant: bool = True
) -> ReturnController:
    return ReturnController(
        route,
        status=device.status,
        scan=device.scan,
        drive_velocity=device.drive_velocity,
        motion_done=device.motion_done,
        stop=device.stop,
        grant_active=lambda: grant,
        epoch=lambda: 4,
        session="session-a",
        device_id=9,
        odom_origin_id="origin-7",
        pose_source_id="ohmni-pose",
        odom_frame="odom",
        monotonic=lambda: 0.0,
        sleep=_fast_sleep,
    )


def test_approved_return_uses_hashed_measured_geometry_and_measured_arrival(tmp_path: Path) -> None:
    route = ApprovedReturnRoute.load(_record(tmp_path), APPROVAL_KEY)
    device = _Device()

    outcome = asyncio.run(_controller(route, device).run())

    assert outcome.completed
    assert device.stopped
    assert device.y >= 0.07
    assert all(linear >= 0 for linear, _yaw, _duration in device.drives)
    assert any(yaw > 0 for _linear, yaw, _duration in device.drives)
    assert any(linear > 0 for linear, _yaw, _duration in device.drives)


def test_return_refuses_without_current_external_grant(tmp_path: Path) -> None:
    route = ApprovedReturnRoute.load(_record(tmp_path), APPROVAL_KEY)
    device = _Device()

    outcome = asyncio.run(_controller(route, device, grant=False).run())

    assert outcome.reason == "return_authority_lost"
    assert device.drives == []
    assert device.stopped


def test_return_refuses_a_geometry_hash_that_does_not_match_the_signed_record(
    tmp_path: Path,
) -> None:
    path = _record(tmp_path)
    record = json.loads(path.read_text())
    record["geometry_sha256"] = "0" * 64
    unsigned = {key: value for key, value in record.items() if key != "signature"}
    record["signature"] = sign_event(unsigned, APPROVAL_KEY)
    path.write_text(json.dumps(record))

    try:
        ApprovedReturnRoute.load(path, APPROVAL_KEY)
    except ValueError as error:
        assert str(error) == "return geometry hash does not match approved bytes"
    else:
        raise AssertionError("modified geometry hash was accepted")


def test_confirmed_resume_stays_on_the_pinned_corridor(tmp_path: Path) -> None:
    route = ApprovedReturnRoute.load(_record(tmp_path), APPROVAL_KEY)
    device = _Device()
    device.y = 0.04
    device.yaw = 90.0

    outcome = asyncio.run(_controller(route, device).run())

    assert outcome.completed
    assert all(linear >= 0 for linear, _yaw, _duration in device.drives)


def _rewrite_record(path: Path, change: Callable[[dict[str, object]], None]) -> None:
    record = json.loads(path.read_text())
    change(record)
    unsigned = {key: value for key, value in record.items() if key != "signature"}
    record["signature"] = sign_event(unsigned, APPROVAL_KEY)
    path.write_text(json.dumps(record))


def test_return_rejects_a_route_bound_to_another_session(tmp_path: Path) -> None:
    path = _record(tmp_path)
    _rewrite_record(path, lambda record: record.__setitem__("session", "other-session"))
    route = ApprovedReturnRoute.load(path, APPROVAL_KEY)

    outcome = asyncio.run(_controller(route, _Device()).run())

    assert outcome.reason == "return_pose_binding_mismatch"


def test_return_rejects_stale_pose_and_future_scan(tmp_path: Path) -> None:
    route = ApprovedReturnRoute.load(_record(tmp_path), APPROVAL_KEY)
    device = _Device()
    device.status = lambda: GroundStatus(  # type: ignore[method-assign]
        device.x, device.y, device.yaw, 0.0, 0.0, 1.0, 1.0, 1.0, "idle", True, -1_000
    )

    stale = asyncio.run(_controller(route, device).run())

    assert stale.reason == "return_pose_stale"

    device = _Device()
    device.scan = lambda: RangeScan(  # type: ignore[method-assign]
        1, (device.x, device.y, device.yaw), 0.0, 1.0, 0.15, 12.0, [400] * 360
    )
    future = asyncio.run(_controller(route, device).run())

    assert future.reason == "return_lidar_stale"
    assert device.stopped


def test_return_rejects_concave_footprint_that_cuts_the_fixed_segment(tmp_path: Path) -> None:
    path = _record(tmp_path)

    def change(record: dict[str, object]) -> None:
        geometry = {
            "type": "measured_corridor_v1",
            "start": {"x_m": 0.0, "y_m": 0.0},
            "segments": [
                {
                    "target": {"x_m": 2.0, "y_m": 2.0},
                    "footprint": [
                        {"x_m": -1.0, "y_m": -1.0},
                        {"x_m": 3.0, "y_m": -1.0},
                        {"x_m": 3.0, "y_m": 3.0},
                        {"x_m": 1.0, "y_m": 3.0},
                        {"x_m": 1.0, "y_m": 1.0},
                        {"x_m": -1.0, "y_m": 1.0},
                    ],
                }
            ],
        }
        raw = json.dumps(geometry, separators=(",", ":")).encode()
        record["geometry_bytes_b64"] = base64.b64encode(raw).decode()
        record["geometry_sha256"] = hashlib.sha256(raw).hexdigest()

    _rewrite_record(path, change)

    try:
        ApprovedReturnRoute.load(path, APPROVAL_KEY)
    except ValueError as error:
        assert (
            str(error)
            == "return footprint cannot clear the fixed segment body and stopping distance"
        )
    else:
        raise AssertionError("a concave footprint with an unsafe fixed chord was accepted")


def test_return_refuses_an_oversized_approval_key_file(tmp_path: Path) -> None:
    key_path = tmp_path / "return-key"
    key_path.write_bytes(b"x" * 4_097)

    try:
        read_approval_key(key_path)
    except ValueError as error:
        assert str(error) == "return approval key exceeds the safety limit"
    else:
        raise AssertionError("an oversized approval key was accepted")

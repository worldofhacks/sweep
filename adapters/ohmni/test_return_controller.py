from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path

from relay.auth import sign_event

from .models import GroundStatus, RangeScan
from .return_controller import ApprovedReturnRoute, ReturnController

APPROVAL_KEY = b"return-approval-key-that-is-at-least-32-bytes"


def _record(tmp_path: Path) -> Path:
    geometry = {
        "type": "measured_corridor_v1",
        "start": {"x_m": 0.0, "y_m": 0.0},
        "segments": [
            {
                "target": {"x_m": 0.0, "y_m": 0.1},
                "footprint": [
                    {"x_m": -0.1, "y_m": -0.2},
                    {"x_m": 0.2, "y_m": -0.2},
                    {"x_m": 0.2, "y_m": 0.2},
                    {"x_m": -0.1, "y_m": 0.2},
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

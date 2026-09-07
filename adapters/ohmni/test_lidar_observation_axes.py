from __future__ import annotations

import asyncio
import math

import pytest

from .botshell import BotShell
from .fake import FakeGroundDevice
from .lidar import Lidar
from .odometry import Pose
from .runtime import GroundRuntimeConfig, OhmniRuntime
from .spike.rplidar_protocol import Measurement


def configuration(yaw: float) -> GroundRuntimeConfig:
    return GroundRuntimeConfig(
        relay_url="ws://127.0.0.1:8000",
        session="test-lidar-axes",
        device_id=11,
        token="test-key-with-at-least-thirty-two-bytes",
        adapter_id="ohmni-11",
        lidar_mount_x_m=0.2,
        lidar_mount_y_m=0.1,
        lidar_mount_z_m=0.3,
        lidar_mount_yaw_deg=yaw,
    )


@pytest.mark.parametrize("yaw", [-90.0, 5.0, 90.0, 180.0, 360.0])
def test_physical_mount_yaw_cannot_rotate_a_body_normalized_scan_again(yaw: float) -> None:
    with pytest.raises(ValueError, match="body-aligned"):
        configuration(yaw)


def test_rotated_raw_lidar_point_reaches_odom_with_one_rotation(monkeypatch) -> None:
    lidar = Lidar(
        BotShell(),
        "/unused-lidar",
        lambda: Pose(1.0, 2.0, 90.0, quality=1.0),
        offset_deg=180.0,
        angle_sign=-1,
    )
    lidar.publish([Measurement(True, 15, 180.0, 1000.0)], now=10.0)
    device = FakeGroundDevice()
    monkeypatch.setattr(device, "latest_scan", lambda: lidar.scan)
    runtime = OhmniRuntime(configuration(0.0), device)
    runtime._epoch = 1
    runtime._outbound = asyncio.Queue()
    runtime._publish_observations()
    events = []
    while not runtime._outbound.empty():
        events.append(runtime._outbound.get_nowait())
    payload = next(event["payload"] for event in events if event.get("source_id") == "ohmni-lidar")
    pose = payload["sensor_pose"]
    yaw = 2 * math.atan2(pose["qz"], pose["qw"])
    distance = payload["ranges_m"][0]
    assert distance == 1.0
    assert pose["z_m"] == 0.3
    assert (
        pose["x_m"] + math.cos(yaw) * distance,
        pose["y_m"] + math.sin(yaw) * distance,
    ) == pytest.approx((0.9, 3.2))

from __future__ import annotations

import json
import math
import random

import pytest

from adapters.ohmni.calibration import CalibrationConfig, CalibrationRunner
from adapters.ohmni.lidar import RawRevolution
from adapters.ohmni.spike.rplidar_protocol import Measurement
from adapters.ohmni.test_calibration import RunnerSimulation
from tools.ohmni_lidar_self_calibration import run

_MOUNT_X_M = -0.3951312693270427
_MOUNT_Y_M = 0.3951312693270427
_REFLECTOR_RADIUS_M = 0.002


def _room_reflectors() -> tuple[tuple[float, float], ...]:
    generator = random.Random(9)
    return (
        tuple((generator.uniform(-3.0, 3.0), generator.uniform(2.0, 3.0)) for _ in range(75))
        + tuple((generator.uniform(-4.0, -3.0), generator.uniform(-2.0, 3.0)) for _ in range(60))
        + tuple((generator.uniform(1.0, 4.0), generator.uniform(-3.0, -2.0)) for _ in range(55))
    )


_REFLECTORS = _room_reflectors()


def _raycast_distance(origin: tuple[float, float], direction: tuple[float, float]) -> float:
    distances = []
    for center in _REFLECTORS:
        delta = (center[0] - origin[0], center[1] - origin[1])
        along = delta[0] * direction[0] + delta[1] * direction[1]
        perpendicular_squared = delta[0] ** 2 + delta[1] ** 2 - along**2
        if along > 0 and perpendicular_squared <= _REFLECTOR_RADIUS_M**2:
            distances.append(along - math.sqrt(_REFLECTOR_RADIUS_M**2 - perpendicular_squared))
    assert distances
    return min(distances)


def _raycast_revolution(
    simulation: RunnerSimulation, *, angle_sign: int, offset_deg: float
) -> RawRevolution:
    pose = simulation.device.odometry.snapshot(simulation.clock())
    yaw = math.radians(pose.yaw_deg)
    sensor = (
        pose.x + math.cos(yaw) * _MOUNT_X_M - math.sin(yaw) * _MOUNT_Y_M,
        pose.y + math.sin(yaw) * _MOUNT_X_M + math.cos(yaw) * _MOUNT_Y_M,
    )
    points = []
    for center in _REFLECTORS:
        bearing = math.atan2(center[1] - sensor[1], center[0] - sensor[0])
        angle_deg = math.degrees((bearing - yaw - math.radians(offset_deg)) / angle_sign) % 360
        direction = (math.cos(bearing), math.sin(bearing))
        points.append(
            Measurement(False, 15, angle_deg, _raycast_distance(sensor, direction) * 1_000.0)
        )
    points.sort(key=lambda point: point.angle_deg)
    points[0] = Measurement(True, 15, points[0].angle_deg, points[0].distance_mm)
    return RawRevolution(tuple(points), simulation.last_scan)


@pytest.mark.parametrize(
    ("angle_sign", "offset_deg"),
    ((1, 31.4), (-1, -47.7)),
)
def test_calibration_runner_capture_recovers_raycast_lidar_convention(
    monkeypatch: pytest.MonkeyPatch, tmp_path, angle_sign: int, offset_deg: float
) -> None:
    simulation = RunnerSimulation(monkeypatch)
    simulation.device.lidar.raw_revolution = lambda _now: _raycast_revolution(  # type: ignore[method-assign]
        simulation, angle_sign=angle_sign, offset_deg=offset_deg
    )
    capture_path = tmp_path / "capture.json"
    CalibrationRunner(
        simulation.device,
        simulation.lease,
        capture_path,
        monotonic=simulation.clock,
        sleep=simulation.sleep,
        boot_id="simulation-boot",
        executed_bundle_source_sha256="a" * 64,
    ).run()

    candidate_path = tmp_path / "candidate.json"
    result = run(capture_path, candidate_path)
    persisted = json.loads(candidate_path.read_text())

    assert result == persisted
    assert result["approval_status"] == "unapproved_candidate", json.dumps(result, indent=2)
    assert result["refusal_reasons"] == []
    candidate = result["candidate"]
    assert candidate["angle_sign"] == angle_sign
    assert candidate["offset_deg"] == pytest.approx(offset_deg, abs=0.2)
    assert simulation.device.motion is None
    assert not simulation.device.enabled


_WALLS = (
    ((-4.0, -3.0), (4.5, -2.5)),
    ((4.5, -2.5), (3.2, 3.8)),
    ((3.2, 3.8), (-1.0, 4.6)),
    ((-1.0, 4.6), (-4.8, 1.7)),
    ((-4.8, 1.7), (-4.0, -3.0)),
)


def _cross(left: tuple[float, float], right: tuple[float, float]) -> float:
    return left[0] * right[1] - left[1] * right[0]


def _wall_distance(origin: tuple[float, float], direction: tuple[float, float]) -> float:
    distances = []
    for start, end in _WALLS:
        edge = (end[0] - start[0], end[1] - start[1])
        denominator = _cross(direction, edge)
        if abs(denominator) < 1e-12:
            continue
        delta = (start[0] - origin[0], start[1] - origin[1])
        distance = _cross(delta, edge) / denominator
        fraction = _cross(delta, direction) / denominator
        if distance > 0 and 0 <= fraction <= 1:
            distances.append(distance)
    assert distances
    return min(distances)


def _wall_revolution(
    simulation: RunnerSimulation, *, angle_sign: int, offset_deg: float
) -> RawRevolution:
    pose = simulation.device.odometry.snapshot(simulation.clock())
    yaw = math.radians(pose.yaw_deg)
    sensor = (
        pose.x + math.cos(yaw) * _MOUNT_X_M - math.sin(yaw) * _MOUNT_Y_M,
        pose.y + math.sin(yaw) * _MOUNT_X_M + math.cos(yaw) * _MOUNT_Y_M,
    )
    points = []
    for angle_deg in range(360):
        bearing = yaw + math.radians(offset_deg + angle_sign * angle_deg)
        direction = (math.cos(bearing), math.sin(bearing))
        points.append(
            Measurement(
                angle_deg == 0,
                15,
                float(angle_deg),
                _wall_distance(sensor, direction) * 1_000.0,
            )
        )
    return RawRevolution(tuple(points), simulation.last_scan)


@pytest.mark.parametrize(
    ("angle_sign", "offset_deg"),
    ((1, 31.4), (-1, -47.7)),
)
def test_longer_calibration_runner_recovers_uniform_angle_asymmetric_room_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path, angle_sign: int, offset_deg: float
) -> None:
    simulation = RunnerSimulation(monkeypatch)
    simulation.device.lidar.raw_revolution = lambda _now: _wall_revolution(  # type: ignore[method-assign]
        simulation, angle_sign=angle_sign, offset_deg=offset_deg
    )
    capture_path = tmp_path / "capture.json"
    CalibrationRunner(
        simulation.device,
        simulation.lease,
        capture_path,
        monotonic=simulation.clock,
        sleep=simulation.sleep,
        config=CalibrationConfig.longer(),
        boot_id="simulation-boot",
        executed_bundle_source_sha256="a" * 64,
    ).run()

    result = run(capture_path, tmp_path / "candidate.json")

    assert result["approval_status"] == "unapproved_candidate", json.dumps(result, indent=2)
    assert result["refusal_reasons"] == []
    candidate = result["candidate"]
    assert candidate["angle_sign"] == angle_sign
    assert candidate["offset_deg"] == pytest.approx(offset_deg, abs=1.0)

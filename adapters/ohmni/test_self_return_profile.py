"""Offline regression coverage for the Unit 12 self-return candidate."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from .botshell import BotShell
from .device import Config, OhmniDevice
from .lidar import UNIT12_SELF_RETURN_CANDIDATE, Lidar, SelfReturnBand, SelfReturnProfile
from .odometry import Pose
from .spike.rplidar_protocol import Measurement, ScanParser, encode_measurement

FIXTURE = Path(__file__).parent / "fixtures" / "unit12_self_return_compact.json"
NOW = 10.0


class Shell:
    def __init__(self, _path: str) -> None:
        self.commands: list[str] = []

    def command(self, command: str) -> None:
        self.commands.append(command)

    def close(self) -> None:
        pass


def point(record: dict[str, float]) -> Measurement:
    return Measurement(False, 15, record["angle_deg"], record["distance_mm"])


def qualified_profile(*, expires_at: float = NOW + 1) -> SelfReturnProfile:
    return SelfReturnProfile(
        evidence_id="synthetic-unit12-physical-envelope",
        bands=(SelfReturnBand(178.8, 179.2, 179.5, 182.0),),
        binding=UNIT12_SELF_RETURN_CANDIDATE.binding,
        enabled=True,
        physical_qualification_id="offline-test-physical-envelope",
        qualification_valid_until_s=expires_at,
    )


def full_revolution(*extra: Measurement) -> list[Measurement]:
    points = [Measurement(index == 0, 15, float(index), 1000.0) for index in range(360)]
    return [*points, *extra]


def publish(
    profile: SelfReturnProfile | None,
    *extra: Measurement,
    binding=UNIT12_SELF_RETURN_CANDIDATE.binding,
    reference_at_candidate_angle: bool = True,
) -> Lidar:
    lidar = Lidar(
        BotShell(),
        "/unused-lidar",
        lambda: Pose(0.0, 0.0, 0.0, quality=1.0),
        offset_deg=131.269876,
        angle_sign=-1,
        self_return_profile=profile,
        self_return_binding=binding,
    )
    parser = ScanParser()
    revolution = full_revolution(*extra)
    if not reference_at_candidate_angle:
        revolution = [sample for sample in revolution if sample.angle_deg != 179.0]
    wire = b"".join(
        encode_measurement(sample.new_scan, sample.quality, sample.angle_deg, sample.distance_mm)
        for sample in revolution
    )
    lidar.publish(parser.feed(wire), NOW)
    assert parser.resyncs == 0
    return lidar


def guarded_device(lidar: Lidar) -> OhmniDevice:
    binding = UNIT12_SELF_RETURN_CANDIDATE.binding
    device = OhmniDevice(
        Config(
            spotter_present=True,
            footprint_radius_m=0.3,
            stopping_distance_m=0.1,
            clearance_margin_m=0.1,
            lidar_mount_x_m=binding.mount_x_m,
            lidar_mount_y_m=binding.mount_y_m,
            lidar_mount_z_m=binding.mount_z_m,
            lidar_offset_deg=binding.offset_deg,
            lidar_angle_sign=binding.angle_sign,
        ),
        shell_factory=Shell,
        lidar_discover=lambda: None,
        autostart=False,
    )
    device.odometry = SimpleNamespace(snapshot=lambda *args: Pose(0, 0, 0, quality=0.6), lost=False)
    device.lidar = lidar
    return device


def test_observational_candidate_is_inactive_and_preserves_raw_returns() -> None:
    retained = json.loads(FIXTURE.read_text())
    rear = [point(record) for record in retained["rear_180mm"]]
    lidar = publish(UNIT12_SELF_RETURN_CANDIDATE, *rear)

    assert all(
        not UNIT12_SELF_RETURN_CANDIDATE.matches(sample, UNIT12_SELF_RETURN_CANDIDATE.binding, NOW)
        for sample in rear
    )
    assert lidar.raw_revolution(NOW) is not None
    raw = lidar.raw_revolution(NOW).points
    assert [(sample.angle_deg, sample.distance_mm) for sample in raw[-len(rear) :]] == [
        (sample.angle_deg, round(sample.distance_mm * 4) / 4) for sample in rear
    ]
    assert guarded_device(lidar).guard_reason(now=NOW) == "obstacle_within_clearance"


def test_qualified_profile_filters_its_measured_ray_and_safety_refuses_unknown() -> None:
    profile = qualified_profile()
    candidate = Measurement(False, 15, 178.921875, 181.375)
    lidar = publish(profile, candidate)

    assert profile.matches(candidate, profile.binding, NOW)
    assert lidar.raw_revolution(NOW).points[-1].distance_mm == 181.5
    assert guarded_device(lidar).guard_reason(now=NOW) == "lidar_scan_coverage_sparse"


def test_filtered_self_ray_stays_unknown_when_no_other_return_reaches_that_ray() -> None:
    profile = qualified_profile()
    candidate = Measurement(False, 15, 178.921875, 181.375)
    lidar = publish(profile, candidate, reference_at_candidate_angle=False)

    assert 0 in lidar.scan.ranges_cm
    assert guarded_device(lidar).guard_reason(now=NOW) == "lidar_scan_coverage_sparse"


@pytest.mark.parametrize(
    "returns",
    [
        (
            Measurement(False, 15, 178.921875, 181.375),
            Measurement(False, 15, 179.0, 1000.0),
        ),
        (
            Measurement(False, 15, 179.0, 1000.0),
            Measurement(False, 15, 178.921875, 181.375),
        ),
    ],
)
def test_matched_ray_keeps_its_rounded_body_bin_unknown_regardless_of_packet_order(returns) -> None:
    lidar = publish(qualified_profile(), *returns)
    binding = UNIT12_SELF_RETURN_CANDIDATE.binding
    bucket = int(round(binding.offset_deg - 178.921875)) % 360

    assert lidar.scan.ranges_cm[bucket] == 0
    assert lidar.raw_revolution(NOW).points[-2:]
    assert guarded_device(lidar).guard_reason(now=NOW) == "lidar_scan_coverage_sparse"


def test_external_obstacles_in_or_next_to_candidate_angle_remain_blocking() -> None:
    profile = qualified_profile()
    inside_angle = Measurement(False, 15, 178.921875, 190.0)
    adjacent_angle = Measurement(False, 15, 178.7, 181.0)

    assert not profile.matches(inside_angle, profile.binding, NOW)
    assert not profile.matches(adjacent_angle, profile.binding, NOW)
    assert guarded_device(publish(profile, inside_angle)).guard_reason(now=NOW) == (
        "obstacle_within_clearance"
    )
    assert guarded_device(publish(profile, adjacent_angle)).guard_reason(now=NOW) == (
        "obstacle_within_clearance"
    )


def test_room_fixed_points_and_wrong_or_stale_bindings_are_never_filtered() -> None:
    retained = json.loads(FIXTURE.read_text())
    profile = qualified_profile()
    room = [point(record) for record in retained["room_fixed_receding"]]
    wrong_device = replace(profile.binding, device_id=13)
    wrong_calibration = replace(profile.binding, offset_deg=profile.binding.offset_deg + 1)
    wrong_source = replace(profile.binding, source_boot_id="later-boot")

    assert all(not profile.matches(sample, profile.binding, NOW) for sample in room)
    candidate = Measurement(False, 15, 178.921875, 181.375)
    assert not profile.matches(candidate, wrong_device, NOW)
    assert not profile.matches(candidate, wrong_calibration, NOW)
    assert not profile.matches(candidate, wrong_source, NOW)
    assert not profile.matches(candidate, profile.binding, NOW + 1.1)
    assert guarded_device(publish(profile, *room)).guard_reason(now=NOW) == (
        "obstacle_within_clearance"
    )
    assert guarded_device(publish(profile, candidate)).guard_reason(now=NOW) == (
        "lidar_scan_coverage_sparse"
    )
    assert (
        guarded_device(publish(profile, candidate, binding=wrong_device)).guard_reason(now=NOW)
        == "obstacle_within_clearance"
    )
    assert (
        guarded_device(
            publish(replace(profile, qualification_valid_until_s=NOW - 1), candidate)
        ).guard_reason(now=NOW)
        == "obstacle_within_clearance"
    )


def test_device_wires_an_explicitly_qualified_profile_to_its_lidar() -> None:
    binding = UNIT12_SELF_RETURN_CANDIDATE.binding
    device = OhmniDevice(
        Config(
            spotter_present=True,
            footprint_radius_m=0.3,
            stopping_distance_m=0.1,
            clearance_margin_m=0.1,
            lidar_mount_x_m=binding.mount_x_m,
            lidar_mount_y_m=binding.mount_y_m,
            lidar_mount_z_m=binding.mount_z_m,
            lidar_offset_deg=binding.offset_deg,
            lidar_angle_sign=binding.angle_sign,
            lidar_device_id=binding.device_id,
            lidar_source_boot_id=binding.source_boot_id,
            self_return_profile=qualified_profile(),
        ),
        shell_factory=Shell,
        lidar_discover=lambda: "/unused-lidar",
        autostart=False,
    )
    device.lidar.pose = lambda: Pose(0, 0, 0, quality=0.6)
    device.lidar.publish(full_revolution(Measurement(False, 15, 178.921875, 181.375)), NOW)

    assert device.lidar.self_return_profile == qualified_profile()
    assert device.lidar.self_return_binding == binding
    assert device.guard_reason(now=NOW) == "lidar_scan_coverage_sparse"


def test_boolean_angle_sign_is_not_a_calibration() -> None:
    binding = UNIT12_SELF_RETURN_CANDIDATE.binding
    with pytest.raises(ValueError, match="angle sign"):
        replace(binding, angle_sign=True)

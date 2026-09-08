"""The in-memory devices: drive kinematics, the drive vocabulary, and a synthetic scan."""

from __future__ import annotations

import pytest

from nodekit.device import scan_is_valid
from nodekit.fake import FakeAircraft, FakeGroundVehicle


def test_aircraft_body_pulse_uses_nose_direction_and_requires_hover_and_capability() -> None:
    aircraft = FakeAircraft(capabilities=("flight", "body_pulse_v1"))
    with pytest.raises(ValueError, match="hovering"):
        aircraft.body_pulse(250, 500)
    aircraft.takeoff(1.0)
    aircraft.rotate_to(90.0, 20.0)
    aircraft.body_pulse(250, 500)
    assert aircraft.x == pytest.approx(0.125)
    assert aircraft.y == pytest.approx(0.0, abs=1e-9)
    assert aircraft.z == 1.0
    aircraft.body_pulse(-250, 500)
    assert aircraft.x == pytest.approx(0.0)
    aircraft.capabilities.remove("body_pulse_v1")
    with pytest.raises(ValueError, match="unavailable"):
        aircraft.body_pulse(250, 500)


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 1_000.0

    def __call__(self) -> float:
        return self.seconds

    def ms(self) -> int:
        return int(self.seconds * 1000)

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def ground(clock: FakeClock, **changes: object) -> FakeGroundVehicle:
    return FakeGroundVehicle(monotonic=clock, clock_ms=clock.ms, **changes)  # type: ignore[arg-type]


def test_a_docked_robot_is_not_driving_until_it_is_enabled(clock: FakeClock) -> None:
    robot = ground(clock, docked=True)

    assert robot.status().state == "docked"
    assert robot.status().control_authority is False
    with pytest.raises(RuntimeError):
        robot.move_to(1.0, 0.0, 0.0, 0.5)

    assert robot.enable() is True
    assert robot.status().state == "idle"


def test_driving_takes_the_commanded_speed_and_reports_moving_on_the_way(
    clock: FakeClock,
) -> None:
    robot = ground(clock)

    motion = robot.move_to(1.0, 0.0, 0.0, 0.5)
    assert robot.motion_done(motion) is False

    clock.advance(1.0)
    status = robot.status()
    assert status.state == "moving"
    assert status.x == pytest.approx(0.5)
    assert (status.z, status.vz) == (0.0, 0.0)
    assert status.vx == pytest.approx(0.5)
    assert robot.motion_done(motion) is False

    clock.advance(1.0)
    assert robot.motion_done(motion) is True
    status = robot.status()
    assert (status.x, status.state) == (pytest.approx(1.0), "idle")
    assert (status.vx, status.vy) == (0.0, 0.0)


def test_a_stop_holds_where_it_is_and_abandons_the_motion(clock: FakeClock) -> None:
    robot = ground(clock)
    motion = robot.move_to(2.0, 0.0, 0.0, 1.0)
    clock.advance(1.0)

    robot.stop()

    assert robot.status().state == "stopped"
    assert robot.status().x == pytest.approx(1.0)
    assert robot.motion_done(motion) is None
    assert robot.status().control_authority is True


def test_disabling_keeps_the_wheels_off_until_the_robot_rejoins(clock: FakeClock) -> None:
    robot = ground(clock)

    robot.disable()

    assert robot.status().control_authority is False
    assert robot.status().extras["wheels_enabled"] is False
    with pytest.raises(RuntimeError):
        robot.move_to(1.0, 0.0, 0.0, 0.5)

    robot.enable()
    assert robot.motion_done(robot.move_to(0.0, 0.0, 0.0, 0.5)) is True


def test_rotating_turns_the_short_way_at_the_commanded_rate(clock: FakeClock) -> None:
    robot = ground(clock)

    motion = robot.rotate_to(350.0, 45.0)
    clock.advance(0.1)
    assert robot.status().yaw_deg == pytest.approx(355.5)

    clock.advance(1.0)
    assert robot.motion_done(motion) is True
    assert robot.status().yaw_deg == pytest.approx(350.0)


def test_a_new_command_supersedes_the_motion_it_interrupts(clock: FakeClock) -> None:
    robot = ground(clock)
    first = robot.move_to(4.0, 0.0, 0.0, 1.0)
    clock.advance(0.5)

    second = robot.move_to(0.0, 1.0, 0.0, 1.0)

    assert robot.motion_done(first) is None
    assert robot.motion_done(second) is False


def test_the_scan_sees_the_room_around_the_robot(clock: FakeClock) -> None:
    robot = ground(clock, room=(-4.0, -2.5, 4.0, 2.5))

    scan = robot.latest_scan()

    assert scan is not None
    assert scan_is_valid(scan)
    assert (scan.angle_increment_deg, len(scan.ranges_cm)) == (1.0, 360)
    assert scan.pose == (0.0, 0.0, 0.0)
    # Angle 0 is the robot's forward axis and angles increase counter-clockwise.
    assert scan.ranges_cm[0] == 400
    assert scan.ranges_cm[90] == 250
    assert scan.ranges_cm[180] == 400
    assert scan.ranges_cm[270] == 250
    assert 0 in scan.ranges_cm  # the fixture drops returns the way a real scanner does
    assert max(scan.ranges_cm) <= 65_535


def test_the_scan_turns_with_the_robot(clock: FakeClock) -> None:
    robot = ground(clock, start=(0.0, 0.0, 90.0))

    scan = robot.latest_scan()

    assert scan is not None
    assert scan.pose[2] == 90.0
    assert scan.ranges_cm[0] == 250  # forward is +y, the near wall
    assert scan.ranges_cm[90] == 400


def test_scans_are_produced_at_the_configured_rate(clock: FakeClock) -> None:
    robot = ground(clock, scan_hz=5.0)

    first = robot.latest_scan()
    clock.advance(0.1)
    assert robot.latest_scan() is first

    clock.advance(0.15)
    second = robot.latest_scan()
    assert second is not None and second is not first
    assert second.t_ms > first.t_ms  # type: ignore[union-attr]


def test_a_robot_without_the_lidar_kit_reports_no_scan_rather_than_an_error(
    clock: FakeClock,
) -> None:
    robot = ground(clock, capabilities=("ground_drive", "camera"))

    assert robot.latest_scan() is None


def test_the_ground_profile_fits_the_capabilities_frame(clock: FakeClock) -> None:
    profile = ground(clock).hardware_profile()

    assert profile["gimbal_pitch_min_deg"] < profile["gimbal_pitch_max_deg"]
    assert 0 < profile["horizontal_fov_deg"] <= 360


def test_the_fixture_aircraft_teleports_and_holds_the_way_it_always_has() -> None:
    aircraft = FakeAircraft(home=(1.0, 2.0, 0.0))

    assert aircraft.status().state == "landed"
    aircraft.takeoff(1.2)
    assert (aircraft.z, aircraft.state) == (1.2, "hovering")

    motion = aircraft.move_to(3.0, 4.0, 1.0, 0.5)
    assert aircraft.motion_done(motion) is True
    assert (aircraft.x, aircraft.y, aircraft.z) == (3.0, 4.0, 1.0)

    aircraft.land()
    assert (aircraft.z, aircraft.state) == (0.0, "landed")
    assert aircraft.latest_scan() is None

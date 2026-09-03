"""Fail-closed validation for deployment and state configuration."""

from dataclasses import replace
from math import inf, nan

import pytest

from adapters.sim.camera import SimCameraConfig
from planner.models import Command, CommandOperation, Geofence
from tests.autonomy_fixtures import (
    camera_config,
    make_aircraft,
    planning_config,
    safety_config,
)


@pytest.mark.parametrize("value", [nan, inf, -inf, True])
def test_geofence_rejects_nonfinite_or_boolean_bounds(value: float) -> None:
    with pytest.raises(ValueError, match="finite and ordered"):
        Geofence(value, 10.0, -10.0, 10.0, 0.0, 5.0)


def test_geofence_rejects_unordered_bounds() -> None:
    with pytest.raises(ValueError, match="finite and ordered"):
        Geofence(1.0, 1.0, -10.0, 10.0, 0.0, 5.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("takeoff_altitude_m", nan),
        ("translation_step_m", inf),
        ("flight_speed_m_s", -inf),
        ("capture_yaw_speed_deg_s", True),
        ("capture_yaw_tolerance_deg", nan),
        ("capture_yaw_tolerance_deg", inf),
        ("capture_yaw_tolerance_deg", 180.0),
        ("capture_pose_tolerance_m", nan),
        ("capture_pose_tolerance_m", -1.0),
        ("capture_min_overlap_deg", inf),
        ("capture_min_overlap_deg", 0.0),
        ("capture_min_overlap_deg", 180.0),
        ("capture_gimbal_pitch_deg", nan),
        ("capture_gimbal_pitch_deg", inf),
    ],
)
def test_planning_config_rejects_nonfinite_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(planning_config(), **{field: value})


@pytest.mark.parametrize("heading", [nan, inf, -inf, True])
def test_planning_config_rejects_invalid_headings(heading: object) -> None:
    headings = list(planning_config().reconstruct_headings_deg)
    headings[0] = heading  # type: ignore[assignment]

    with pytest.raises(ValueError, match="headings"):
        replace(planning_config(), reconstruct_headings_deg=tuple(headings))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ceiling_m", nan),
        ("min_spacing_m", inf),
        ("battery_reserve_fraction", nan),
        ("battery_critical_fraction", -inf),
        ("battery_cost_per_m", nan),
        ("min_link_quality", inf),
        ("min_position_quality", nan),
        ("max_link_age_ms", True),
        ("max_future_clock_skew_ms", 1.5),
        ("min_capture_storage_bytes", True),
        ("max_capture_pose_drift_m", nan),
        ("max_capture_gimbal_error_deg", inf),
        ("motion_conflict_window_ms", 1.5),
    ],
)
def test_safety_config_rejects_values_that_could_disable_a_gate(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(safety_config(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("horizontal_fov_deg", nan),
        ("gimbal_pitch_min_deg", -inf),
        ("gimbal_pitch_max_deg", inf),
        ("timestamp_step_ms", True),
    ],
)
def test_sim_camera_config_rejects_nonfinite_or_boolean_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(camera_config(), **{field: value})


def test_aircraft_safety_state_rejects_nonfinite_fraction() -> None:
    with pytest.raises(ValueError, match="battery must be a finite fraction"):
        replace(make_aircraft(1), battery=nan)


def test_command_parameters_reject_nonfinite_json_number() -> None:
    with pytest.raises(ValueError, match="JSON numbers must be finite"):
        Command(
            command_id="command-1",
            intent_id="intent-1",
            roster_version=1,
            drone_id=1,
            connection_epoch=1,
            operation=CommandOperation.GOTO,
            parameters={"x": nan},
        )


def test_camera_config_type_annotation_remains_concrete() -> None:
    assert isinstance(camera_config(), SimCameraConfig)

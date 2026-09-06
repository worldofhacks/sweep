from dataclasses import FrozenInstanceError, replace

import pytest

from perception.control_localization import ControlLocalizationSnapshot
from relay.control_localization import (
    ClockMapping,
    ControlLocalizationPins,
    ControlLocalizationProjector,
    ControlLocalizationWire,
    LocalizationProjectionError,
    to_wire_payload,
)

NOW_MS = 1_756_700_000_000
COVARIANCE = ((0.01, 0.0, 0.0), (0.0, 0.01, 0.0), (0.0, 0.0, 0.01))
SOURCES = ("tag-camera", "msdk-velocity", "tof-height")


def snapshot(**overrides: object) -> ControlLocalizationSnapshot:
    values: dict[str, object] = {
        "drone_id": 1,
        "connection_epoch": 1,
        "map_id": "map-id",
        "geometry_id": "geometry-id",
        "capture_clock_id": "camera-monotonic",
        "evaluated_at_s": 1.0,
        "position_map_enu_m": (2.0, 3.0, 1.0),
        "velocity_map_enu_mps": (0.1, 0.0, 0.0),
        "covariance_map_enu_m2": COVARIANCE,
        "fix_age_s": 0.1,
        "velocity_age_s": 0.05,
        "height_age_s": 0.03,
        "confidence": "green",
        "loss_age_s": None,
        "status": "ready",
        "control_eligible": True,
        "reason": "fresh_verified_measurements",
        "last_rejection": None,
        "active_contradictions": (),
        "source_ids": SOURCES,
        "camera_calibration_id": "camera-calibration-id",
        "body_extrinsics_id": "body-extrinsics-id",
        "retained_event_count": 3,
    }
    return ControlLocalizationSnapshot(**(values | overrides))  # type: ignore[arg-type]


def mapping(**overrides: object) -> ClockMapping:
    values: dict[str, object] = {
        "capture_clock_id": "camera-monotonic",
        "relay_clock_id": "unix_epoch_ms",
        "capture_reference_s": 0.0,
        "relay_reference_ms": NOW_MS - 1_000,
        "milliseconds_per_capture_second": 1_000.0,
        "max_error_ms": 5,
        "measured": True,
    }
    return ClockMapping(**(values | overrides))  # type: ignore[arg-type]


def wire(**overrides: object) -> ControlLocalizationWire:
    body = to_wire_payload(snapshot(), mapping()) | overrides
    return ControlLocalizationWire.from_mapping(body)


def projector(**overrides: object) -> ControlLocalizationProjector:
    values: dict[str, object] = {
        "relay_clock_id": "unix_epoch_ms",
        "max_clock_error_ms": 5,
        "max_fix_age_ms": 500,
        "max_velocity_age_ms": 200,
        "max_height_age_ms": 200,
        "max_position_uncertainty_p95_m": 0.3,
    }
    return ControlLocalizationProjector(
        {
            1: ControlLocalizationPins(
                drone_id=1,
                map_id="map-id",
                geometry_id="geometry-id",
                camera_calibration_id="camera-calibration-id",
                body_extrinsics_id="body-extrinsics-id",
                source_ids=SOURCES,
                clock_mapping=mapping(),
            )
        },
        **(values | overrides),  # type: ignore[arg-type]
    )


def project(
    evidence: ControlLocalizationWire | None = None,
    *,
    previous=None,
    epoch: int = 1,
):
    return projector().project(
        wire() if evidence is None else evidence,
        authenticated_drone_id=1,
        authenticated_connection_epoch=epoch,
        now_ms=NOW_MS,
        event_id="relay-pose-1",
        session="session-test",
        previous=previous,
    )


def test_snapshot_wire_preserves_health_and_explicit_non_approval() -> None:
    body = to_wire_payload(snapshot(), mapping())

    assert body["flight_approved"] is False
    assert body["fix_age_s"] == 0.1
    assert body["velocity_age_s"] == 0.05
    assert body["height_age_s"] == 0.03
    assert "last_fix_capture_time_s" not in body
    assert "velocity_map_enu_mps" not in body


def test_ready_projection_is_bounded_integer_diagnostic_only() -> None:
    pose = project()

    assert pose.unsigned_event() == {
        "v": 1,
        "t": NOW_MS,
        "type": "control_pose",
        "event_id": "relay-pose-1",
        "session": "session-test",
        "drone_id": 1,
        "connection_epoch": 1,
        "map_id": "map-id",
        "geometry_id": "geometry-id",
        "camera_calibration_id": "camera-calibration-id",
        "body_extrinsics_id": "body-extrinsics-id",
        "pose_time_ms": NOW_MS - 5,
        "fix_time_ms": NOW_MS - 105,
        "x_mm": 2_000,
        "y_mm": 3_000,
        "z_mm": 1_000,
        "position_frame": "map_enu",
        "position_uncertainty_mm": 280,
        "status": "ready",
        "flight_approved": False,
    }


def test_hold_and_land_preserve_real_retained_pose_and_never_synthesize_one() -> None:
    for status in ("hold", "land"):
        evidence = wire(
            localization_status=status,
            control_eligible=False,
            localization_confidence="amber" if status == "hold" else "red",
            localization_loss_age_s=0.6 if status == "hold" else 0.9,
            localization_reason="tag_fix_stale" if status == "hold" else "tag_fix_lost",
            fix_age_s=0.9,
        )
        assert project(evidence).status == status

    absent = wire(
        localization_status="hold",
        control_eligible=False,
        localization_confidence="red",
        localization_loss_age_s=1.0,
        localization_reason="tag_fix_missing",
        position_map_enu_m=None,
        covariance_map_enu_m2=None,
        fix_age_s=None,
    )
    with pytest.raises(LocalizationProjectionError, match="projectable pose"):
        project(absent)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("fix_age_s", 0.5, "localization_fix_stale"),
        ("velocity_age_s", 0.2, "localization_velocity_stale"),
        ("height_age_s", 0.2, "localization_height_stale"),
        (
            "covariance_map_enu_m2",
            ((0.05, 0.0, 0.0), (0.0, 0.05, 0.0), (0.0, 0.0, 0.05)),
            "localization_position_uncertain",
        ),
    ],
)
def test_ready_projection_rechecks_freshness_and_uncertainty(
    field: str, value: object, code: str
) -> None:
    with pytest.raises(LocalizationProjectionError) as raised:
        project(wire(**{field: value}))
    assert raised.value.code == code


def test_dynamic_epoch_comes_from_live_binding_not_static_pins() -> None:
    first = project()
    reconnected = replace(wire(), connection_epoch=2)
    second = project(reconnected, previous=first, epoch=2)

    assert first.connection_epoch == 1
    assert second.connection_epoch == 2


def test_projector_configuration_is_immutable() -> None:
    configured = projector()
    with pytest.raises(FrozenInstanceError):
        configured.max_fix_age_ms = 500  # type: ignore[misc]
    with pytest.raises(TypeError):
        configured.pins[2] = configured.pins[1]  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("map_id", "other-map"),
        ("geometry_id", "other-geometry"),
        ("source_ids", ("tag-camera",)),
        ("clock_mapping", mapping(relay_reference_ms=NOW_MS - 999)),
    ],
)
def test_host_owned_pins_are_exact(field: str, value: object) -> None:
    with pytest.raises(LocalizationProjectionError) as raised:
        project(replace(wire(), **{field: value}))
    assert raised.value.code == "localization_provenance_mismatch"


def test_projection_rejects_regression_but_allows_equal_time_status_evidence() -> None:
    first = project()
    with pytest.raises(LocalizationProjectionError) as duplicate:
        project(wire(), previous=first)
    assert duplicate.value.code == "duplicate_localization_state"

    same_times_hold = wire(
        localization_status="hold",
        control_eligible=False,
        localization_confidence="amber",
        localization_loss_age_s=0.5,
        localization_reason="tag_fix_stale",
    )
    held = project(same_times_hold, previous=first)
    assert held.status == "hold"
    same_times_land = wire(
        localization_status="land",
        control_eligible=False,
        localization_confidence="red",
        localization_loss_age_s=0.6,
        localization_reason="tag_fix_lost",
    )
    assert project(same_times_land, previous=held).status == "land"
    with pytest.raises(LocalizationProjectionError) as duplicate_hold:
        project(same_times_hold, previous=held)
    assert duplicate_hold.value.code == "duplicate_localization_state"
    with pytest.raises(LocalizationProjectionError) as restored:
        project(wire(), previous=held)
    assert restored.value.code == "localization_status_regressed"

    regressed = wire(evaluated_at_s=0.99, fix_age_s=0.1)
    with pytest.raises(LocalizationProjectionError) as regression:
        project(regressed, previous=first)
    assert regression.value.code == "localization_time_regressed"


def test_contract_rejects_bool_ids_extras_duplicate_sources_and_physical_overflow() -> None:
    valid = to_wire_payload(snapshot(), mapping())
    for invalid in (
        valid | {"drone_id": True},
        valid | {"connection_epoch": 0},
        valid | {"unexpected": "field"},
        valid | {"source_ids": [*SOURCES, SOURCES[0]]},
        valid | {"position_map_enu_m": (1_000.001, 0.0, 0.0)},
        valid | {"flight_approved": True},
    ):
        with pytest.raises(ValueError):
            ControlLocalizationWire.from_mapping(invalid)

    overflowing = mapping(relay_reference_ms=2**63 - 1)
    with pytest.raises(ValueError, match="64-bit"):
        overflowing.to_relay_ms(1.0)


def test_ready_contract_requires_pose_all_source_ages_and_green_health() -> None:
    valid = to_wire_payload(snapshot(), mapping())
    for invalid in (
        valid | {"position_map_enu_m": None, "covariance_map_enu_m2": None},
        valid | {"velocity_age_s": None},
        valid | {"height_age_s": None},
        valid | {"localization_confidence": "amber"},
        valid | {"source_ids": []},
    ):
        with pytest.raises(ValueError, match="ready localization"):
            ControlLocalizationWire.from_mapping(invalid)


def test_control_pose_session_uses_the_relay_512_character_bound() -> None:
    pose = projector().project(
        wire(),
        authenticated_drone_id=1,
        authenticated_connection_epoch=1,
        now_ms=NOW_MS,
        event_id="relay-pose-long-session",
        session="s" * 512,
        previous=None,
    )
    assert len(pose.session) == 512
    with pytest.raises(ValueError, match="at most 512"):
        replace(pose, session="s" * 513)

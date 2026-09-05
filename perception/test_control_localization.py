import numpy as np
import pytest

from perception.control_localization import (
    BodyExtrinsics,
    ControlLocalization,
    ControlLocalizationConfig,
    HeightObservation,
    TagFix,
    VelocityObservation,
)

IDENTITY = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))
COVARIANCE = ((0.01, 0.0, 0.0), (0.0, 0.01, 0.0), (0.0, 0.0, 0.01))


def config(**overrides):
    values = dict(
        drone_id=1,
        connection_epoch=7,
        map_id="map-sha",
        geometry_id="geometry-sha",
        clock_id="bridge-monotonic",
        tag_source_id="tag-camera",
        velocity_source_id="msdk-velocity",
        height_source_id="tof-height",
        camera_calibration_id="camera-calibration-sha",
        body_extrinsics_id="gimbal-calibration-sha",
        production_evidence_verified=True,
    )
    return ControlLocalizationConfig(**(values | overrides))


def extrinsics(capture_time, **overrides):
    values = dict(
        extrinsics_id="gimbal-calibration-sha",
        source_id="tag-camera",
        matrix=IDENTITY,
        capture_time=capture_time,
        gimbal_time=capture_time,
        attitude_time=capture_time,
        measured=True,
    )
    return BodyExtrinsics(**(values | overrides))


def tag(event_id, capture_time, position=(0.0, 0.0, 1.0), **overrides):
    values = dict(
        event_id=event_id,
        drone_id=1,
        connection_epoch=7,
        map_id="map-sha",
        geometry_id="geometry-sha",
        clock_id="bridge-monotonic",
        capture_time=capture_time,
        position_map_enu_m=position,
        covariance_map_enu_m2=COVARIANCE,
        source_id="tag-camera",
        camera_calibration_id="camera-calibration-sha",
        source_verified=True,
        timing_verified=True,
        extrinsics=extrinsics(capture_time),
    )
    return TagFix(**(values | overrides))


def velocity(event_id, capture_time, value=(1.0, 0.0, 0.0), **overrides):
    values = dict(
        event_id=event_id,
        drone_id=1,
        connection_epoch=7,
        map_id="map-sha",
        geometry_id="geometry-sha",
        clock_id="bridge-monotonic",
        capture_time=capture_time,
        velocity_map_enu_mps=value,
        covariance_m2ps2=COVARIANCE,
        source_id="msdk-velocity",
        source_verified=True,
        timing_verified=True,
    )
    return VelocityObservation(**(values | overrides))


def height(event_id, capture_time, value=1.0, **overrides):
    values = dict(
        event_id=event_id,
        drone_id=1,
        connection_epoch=7,
        map_id="map-sha",
        geometry_id="geometry-sha",
        clock_id="bridge-monotonic",
        capture_time=capture_time,
        height_map_enu_m=value,
        variance_m2=0.01,
        source_id="tof-height",
        source_verified=True,
        timing_verified=True,
    )
    return HeightObservation(**(values | overrides))


def test_delayed_tag_fix_replays_velocity_and_height_at_capture_time():
    tracker = ControlLocalization(config())
    tracker.ingest_velocity(velocity("velocity", 1.0), 1.1)
    tracker.ingest_height(height("height", 1.05), 1.1)
    result = tracker.ingest_tag_fix(tag("tag", 0.9), 1.1)

    assert result.control_eligible
    assert result.status == "ready"
    assert result.fix_age_s == pytest.approx(0.2)
    assert result.position_map_enu_m[0] > 0.15
    assert result.velocity_map_enu_mps[0] > 0.8


def test_out_of_order_inputs_match_capture_order_replay():
    chronological = ControlLocalization(config())
    delayed = ControlLocalization(config())
    events = [
        ("tag", tag("tag", 0.5), 1.1),
        ("velocity", velocity("velocity", 0.8), 1.1),
        ("height", height("height", 0.9, 1.1), 1.1),
    ]
    for method, observation, now in events:
        getattr(chronological, f"ingest_{method}" if method != "tag" else "ingest_tag_fix")(
            observation, now
        )
    for method, observation, now in [events[1], events[2], events[0]]:
        getattr(delayed, f"ingest_{method}" if method != "tag" else "ingest_tag_fix")(
            observation, now
        )

    expected, actual = chronological.snapshot(1.1), delayed.snapshot(1.1)
    assert actual.position_map_enu_m == pytest.approx(expected.position_map_enu_m)
    assert actual.velocity_map_enu_mps == pytest.approx(expected.velocity_map_enu_mps)
    assert np.allclose(actual.covariance_map_enu_m2, expected.covariance_map_enu_m2)


def test_generic_telemetry_cannot_refresh_tag_freshness():
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(tag("tag", 0.0), 0.1)
    tracker.ingest_velocity(velocity("velocity", 0.1), 0.1)
    tracker.ingest_height(height("height", 0.1), 0.1)

    stale = tracker.snapshot(0.7)
    assert stale.status == "hold"
    assert stale.reason == "tag_fix_stale"
    assert stale.fix_age_s == pytest.approx(0.7)


def test_rejects_wrong_map_epoch_source_and_capture_transform_without_refreshing_fix_age():
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(tag("good", 1.0), 1.0)
    wrong_map = tracker.ingest_velocity(velocity("wrong-map", 1.1, map_id="other"), 1.1)
    assert wrong_map.reason == "map_id_mismatch"
    assert wrong_map.fix_age_s == pytest.approx(0.1)
    wrong_epoch = tracker.ingest_height(height("wrong-epoch", 1.15, connection_epoch=8), 1.15)
    assert wrong_epoch.reason == "connection_epoch_mismatch"
    wrong_calibration = tracker.ingest_tag_fix(
        tag("wrong-calibration", 1.18, camera_calibration_id="other"), 1.18
    )
    assert wrong_calibration.reason == "camera_calibration_mismatch"
    with pytest.raises(ValueError, match="capture time"):
        extrinsics(1.2, gimbal_time=1.1)
    assert (
        tracker.ingest_tag_fix(tag("wrong-source", 1.2, source_id="other"), 1.2).reason
        == "source_id_mismatch"
    )


def test_unverified_inputs_and_unmeasured_production_configuration_fail_closed():
    with pytest.raises(ValueError, match="verified source"):
        tag("unverified", 0.0, source_verified=False)
    tracker = ControlLocalization(config(production_evidence_verified=False))
    tracker.ingest_tag_fix(tag("tag", 0.0), 0.0)
    tracker.ingest_velocity(velocity("velocity", 0.0), 0.0)
    state = tracker.ingest_height(height("height", 0.0), 0.0)
    assert state.status == "hold"
    assert state.reason == "production_evidence_unverified"
    assert not state.control_eligible


def test_nonfinite_measurements_are_rejected_before_the_filter():
    with pytest.raises(ValueError, match="finite"):
        velocity("nan", 0.0, (float("nan"), 0.0, 0.0))
    with pytest.raises(ValueError, match="finite"):
        tag("nan-time", float("inf"))


def test_history_is_bounded_and_late_pruned_events_fail_closed():
    tracker = ControlLocalization(config(max_events=3, horizon_s=0.5))
    for index in range(10):
        timestamp = index * 0.1
        tracker.ingest_tag_fix(tag(f"tag-{index}", timestamp, (timestamp, 0, 1)), timestamp)
        tracker.ingest_velocity(velocity(f"velocity-{index}", timestamp), timestamp)
        state = tracker.ingest_height(height(f"height-{index}", timestamp), timestamp)
        assert state.retained_event_count <= 3
    old = tracker.ingest_tag_fix(tag("late", 0.2), 0.9)
    assert old.reason == "capture_time_too_old"
    assert old.retained_event_count <= 3


def test_sustained_tag_loss_lands_and_adapter_output_is_explicit():
    tracker = ControlLocalization(config())
    assert tracker.snapshot(0.0).status == "hold"
    state = tracker.snapshot(2.0)
    assert state.status == "land"
    payload = state.to_relay_state()
    assert payload["localization_status"] == "land"
    assert payload["control_eligible"] is False
    assert payload["map_id"] == "map-sha"


def test_observations_require_literal_verification_and_integer_identities():
    with pytest.raises(ValueError, match="source_verified"):
        tag("tag", 0.0, source_verified="false")
    with pytest.raises(ValueError, match="timing_verified"):
        velocity("velocity", 0.0, timing_verified=1)
    with pytest.raises(ValueError, match="drone_id"):
        height("height", 0.0, drone_id=True)
    with pytest.raises(ValueError, match="connection_epoch"):
        height("height", 0.0, connection_epoch=7.0)
    with pytest.raises(ValueError, match="validated body extrinsics"):
        tag("tag", 0.0, extrinsics=object())


def test_retained_measurements_do_not_alias_caller_buffers():
    position = np.array([0.0, 0.0, 1.0])
    covariance = np.eye(3) * 0.01
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(
        tag("tag", 0.0, position=position, covariance_map_enu_m2=covariance), 0.0
    )
    tracker.ingest_velocity(velocity("velocity", 0.0), 0.0)
    expected = tracker.ingest_height(height("height", 0.0), 0.0)

    position[0] = 25.0
    covariance[0, 0] = 25.0
    actual = tracker.snapshot(0.0)

    assert actual.position_map_enu_m == pytest.approx(expected.position_map_enu_m)
    assert np.allclose(actual.covariance_map_enu_m2, expected.covariance_map_enu_m2)


def test_missing_fix_grace_period_starts_at_first_evaluation():
    tracker = ControlLocalization(config())

    assert tracker.snapshot(100_000.0).status == "hold"
    assert tracker.snapshot(100_002.0).status == "land"


def test_uncertain_tag_and_posterior_never_enable_control():
    uncertain = ((1e100, 0.0, 0.0), (0.0, 1e100, 0.0), (0.0, 0.0, 1e100))
    tracker = ControlLocalization(config(max_tag_variance_m2=1e101))
    tracker.ingest_tag_fix(tag("tag", 0.0, covariance_map_enu_m2=uncertain), 0.0)
    tracker.ingest_velocity(velocity("velocity", 0.0), 0.0)
    state = tracker.ingest_height(height("height", 0.0), 0.0)

    assert state.status == "hold"
    assert state.reason == "position_uncertainty_excessive"
    assert not state.control_eligible


def test_equal_time_fusion_has_semantic_order_and_deduplicates_evidence():
    tracker = ControlLocalization(config())
    tracker.ingest_velocity(velocity("a-velocity", 0.0), 0.0)
    tracker.ingest_height(height("b-height", 0.0), 0.0)
    state = tracker.ingest_tag_fix(tag("z-tag", 0.0), 0.0)

    assert state.status == "ready"
    duplicate = tracker.ingest_tag_fix(tag("another-tag", 0.0), 0.0)
    assert duplicate.reason == "duplicate_measurement"
    assert duplicate.retained_event_count == 3

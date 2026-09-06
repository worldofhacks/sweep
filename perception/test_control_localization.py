from itertools import permutations

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
        position_bounds_map_enu_m=((-10, 10), (-10, 10), (0, 3)),
        height_bounds_map_enu_m=(0, 3),
        max_speed_mps=0.5,
        position_variance_bounds_m2=(1e-6, 0.0625),
        velocity_variance_bounds_m2ps2=(1e-6, 1.0),
        height_variance_bounds_m2=(1e-6, 0.0625),
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


def velocity(event_id, capture_time, value=(0.25, 0.0, 0.0), **overrides):
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
    assert result.position_map_enu_m[0] > 0.04
    assert result.velocity_map_enu_mps[0] > 0.2


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
    assert wrong_map.last_rejection == "map_id_mismatch"
    assert wrong_map.fix_age_s == pytest.approx(0.1)
    wrong_epoch = tracker.ingest_height(height("wrong-epoch", 1.15, connection_epoch=8), 1.15)
    assert wrong_epoch.last_rejection == "connection_epoch_mismatch"
    wrong_calibration = tracker.ingest_tag_fix(
        tag("wrong-calibration", 1.18, camera_calibration_id="other"), 1.18
    )
    assert wrong_calibration.last_rejection == "camera_calibration_mismatch"
    with pytest.raises(ValueError, match="capture time"):
        extrinsics(1.2, gimbal_time=1.1)
    assert (
        tracker.ingest_tag_fix(tag("wrong-source", 1.2, source_id="other"), 1.2).last_rejection
        == "source_id_mismatch"
    )


def test_unverified_inputs_and_unmeasured_production_configuration_fail_closed():
    with pytest.raises(ValueError, match="must be verified"):
        tag("unverified", 0.0, source_verified=False)
    tracker = ControlLocalization(config(production_evidence_verified=False))
    tracker.ingest_tag_fix(tag("tag", 0.0), 0.0)
    tracker.ingest_velocity(velocity("velocity", 0.0), 0.0)
    state = tracker.ingest_height(height("height", 0.0), 0.0)
    assert state.status == "hold"
    assert state.reason == "production_evidence_unverified"
    assert not state.control_eligible
    assert tracker.snapshot(3.5).status == "land"


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
    assert old.last_rejection == "capture_time_too_old"
    assert old.retained_event_count <= 3


def test_sustained_tag_loss_lands_and_adapter_output_is_explicit():
    tracker = ControlLocalization(config())
    first = tracker.snapshot(1_000_000.0)
    assert first.status == "hold"
    assert first.confidence == "red"
    assert first.loss_age_s == 0
    assert tracker.snapshot(1_000_002.999).status == "hold"
    state = tracker.snapshot(1_000_003.0)
    assert state.status == "land"
    payload = state.to_relay_state()
    assert payload["localization_status"] == "land"
    assert payload["control_eligible"] is False
    assert payload["flight_approved"] is False
    assert payload["localization_loss_age_s"] == pytest.approx(3)
    assert payload["map_id"] == "map-sha"


def test_equal_capture_time_is_tag_first_for_every_arrival_order_and_event_id_order():
    observations = (
        ("ingest_velocity", velocity("a-velocity", 10.0)),
        ("ingest_height", height("b-height", 10.0)),
        ("ingest_tag_fix", tag("z-tag", 10.0)),
    )
    states = []
    for arrival_order in permutations(observations):
        tracker = ControlLocalization(config())
        for method, observation in arrival_order:
            state = getattr(tracker, method)(observation, 10.0)
        assert state.status == "ready"
        assert state.source_ids == ("tag-camera", "msdk-velocity", "tof-height")
        states.append(state)
    assert all(
        state.position_map_enu_m == pytest.approx(states[0].position_map_enu_m) for state in states
    )
    assert all(
        state.velocity_map_enu_mps == pytest.approx(states[0].velocity_map_enu_mps)
        for state in states
    )


def test_measurements_before_first_tag_are_explicitly_pending_until_post_tag_samples_arrive():
    tracker = ControlLocalization(config())
    tracker.ingest_velocity(velocity("velocity-before", 0.8), 1.0)
    tracker.ingest_height(height("height-before", 0.9), 1.0)
    initialized = tracker.ingest_tag_fix(tag("tag", 1.0), 1.0)

    assert initialized.status == "hold"
    assert initialized.reason == "velocity_stale"
    assert initialized.velocity_age_s is None
    assert initialized.height_age_s is None
    assert initialized.source_ids == ("tag-camera",)
    assert initialized.retained_event_count == 3

    tracker.ingest_velocity(velocity("velocity-after", 1.1), 1.1)
    ready = tracker.ingest_height(height("height-after", 1.1), 1.1)
    assert ready.status == "ready"


def test_canonical_measurements_cannot_be_rewritten_through_mutable_inputs():
    position = np.array([0.0, 0.0, 1.0])
    velocity_value = np.array([0.1, 0.0, 0.0])
    covariance = np.eye(3) * 0.01
    fix = tag("tag", 1.0, position, covariance_map_enu_m2=covariance)
    speed = velocity("velocity", 1.0, velocity_value, covariance_m2ps2=covariance)
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(fix, 1.0)
    tracker.ingest_velocity(speed, 1.0)
    tracker.ingest_height(height("height", 1.0), 1.0)
    expected = tracker.snapshot(1.1)

    position[0] = 9
    velocity_value[0] = 0.5
    covariance[:] = np.eye(3) * 0.06
    actual = tracker.snapshot(1.1)

    assert fix.position_map_enu_m == (0.0, 0.0, 1.0)
    assert speed.velocity_map_enu_mps == (0.1, 0.0, 0.0)
    assert actual.position_map_enu_m == pytest.approx(expected.position_map_enu_m)
    assert actual.velocity_map_enu_mps == pytest.approx(expected.velocity_map_enu_mps)
    assert np.allclose(actual.covariance_map_enu_m2, expected.covariance_map_enu_m2)


@pytest.mark.parametrize(
    "factory,overrides",
    [
        (tag, {"drone_id": True}),
        (tag, {"connection_epoch": 7.0}),
        (tag, {"source_verified": 1}),
        (tag, {"timing_verified": 1}),
        (velocity, {"drone_id": True}),
        (velocity, {"source_verified": 1}),
        (height, {"variance_m2": True}),
        (height, {"timing_verified": 1}),
    ],
)
def test_json_like_type_confusion_is_rejected(factory, overrides):
    with pytest.raises(ValueError):
        factory("event", 1.0, **overrides)


def test_physical_and_uncertainty_bounds_hold_until_that_source_recovers():
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(tag("tag", 1.0), 1.0)
    tracker.ingest_velocity(velocity("velocity", 1.0), 1.0)
    assert tracker.ingest_height(height("height", 1.0), 1.0).control_eligible

    unsafe = tracker.ingest_velocity(velocity("fast", 1.1, (0.6, 0, 0)), 1.1)
    assert unsafe.status == "hold"
    assert unsafe.reason == "velocity_out_of_bounds"
    assert unsafe.last_rejection == "velocity_out_of_bounds"
    assert unsafe.active_contradictions == ("velocity:velocity_out_of_bounds",)

    still_held = tracker.ingest_tag_fix(tag("new-tag", 1.15), 1.15)
    assert still_held.status == "hold"
    assert still_held.reason == "velocity_out_of_bounds"
    recovered = tracker.ingest_velocity(velocity("recovered", 1.15), 1.15)
    assert recovered.status == "ready"
    assert recovered.active_contradictions == ()


@pytest.mark.parametrize(
    "method,observation,reason",
    [
        ("ingest_tag_fix", tag("position", 1.1, (11, 0, 1)), "position_out_of_bounds"),
        (
            "ingest_tag_fix",
            tag(
                "position-uncertain",
                1.1,
                covariance_map_enu_m2=((1, 0, 0), (0, 1, 0), (0, 0, 1)),
            ),
            "position_uncertainty_out_of_bounds",
        ),
        (
            "ingest_velocity",
            velocity(
                "velocity-uncertain",
                1.1,
                covariance_m2ps2=((2, 0, 0), (0, 2, 0), (0, 0, 2)),
            ),
            "velocity_uncertainty_out_of_bounds",
        ),
        ("ingest_height", height("height-value", 1.1, 4), "height_out_of_bounds"),
        (
            "ingest_height",
            height("height-uncertain", 1.1, variance_m2=1),
            "height_uncertainty_out_of_bounds",
        ),
    ],
)
def test_each_configured_measurement_bound_fails_closed(method, observation, reason):
    state = getattr(ControlLocalization(config()), method)(observation, 1.1)
    assert state.status == "hold"
    assert state.last_rejection == reason
    assert reason in state.active_contradictions[0]


def test_unrelated_or_duplicate_side_channel_events_do_not_poison_a_ready_state():
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(tag("tag", 1.0), 1.0)
    tracker.ingest_velocity(velocity("velocity", 1.0), 1.0)
    assert tracker.ingest_height(height("height", 1.0), 1.0).status == "ready"

    wrong_source = tracker.ingest_velocity(velocity("wrong-source", 1.05, source_id="other"), 1.05)
    assert wrong_source.status == "ready"
    assert wrong_source.last_rejection == "source_id_mismatch"
    duplicate = tracker.ingest_velocity(velocity("retry", 1.0), 1.05)
    assert duplicate.status == "ready"
    assert duplicate.last_rejection == "duplicate_observation"
    assert duplicate.active_contradictions == ()


def test_wrong_map_holds_until_the_expected_source_recovers():
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(tag("tag", 1.0), 1.0)
    tracker.ingest_velocity(velocity("velocity", 1.0), 1.0)
    tracker.ingest_height(height("height", 1.0), 1.0)

    wrong = tracker.ingest_tag_fix(tag("wrong-map", 1.05, map_id="other"), 1.05)
    assert wrong.status == "hold"
    assert wrong.active_contradictions == ("tag:map_id_mismatch",)
    recovered = tracker.ingest_tag_fix(tag("recovered", 1.05), 1.05)
    assert recovered.status == "ready"
    assert recovered.active_contradictions == ()


def test_replay_admission_precedes_contradiction_latching():
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(tag("tag", 3.1), 3.1)
    tracker.ingest_velocity(velocity("velocity", 3.1), 3.1)
    tracker.ingest_height(height("height", 3.1), 3.1)

    expired = tracker.ingest_tag_fix(tag("expired-wrong-map", 1.0, map_id="other"), 3.1)
    assert expired.status == "ready"
    assert expired.last_rejection == "capture_time_too_old"
    assert expired.active_contradictions == ()

    future = tracker.ingest_velocity(velocity("future-fast", 3.2, (1.0, 0, 0)), 3.1)
    assert future.status == "ready"
    assert future.last_rejection == "capture_time_invalid"
    assert future.active_contradictions == ()


def test_older_accepted_sample_cannot_clear_a_newer_contradiction():
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(tag("tag", 1.0), 1.0)
    tracker.ingest_velocity(velocity("velocity", 1.0), 1.0)
    tracker.ingest_height(height("height", 1.0), 1.0)

    rejected = tracker.ingest_velocity(velocity("too-fast", 1.2, (0.6, 0, 0)), 1.2)
    assert rejected.active_contradictions == ("velocity:velocity_out_of_bounds",)
    older = tracker.ingest_velocity(velocity("older", 1.1), 1.2)
    assert older.status == "hold"
    assert older.active_contradictions == ("velocity:velocity_out_of_bounds",)

    recovered = tracker.ingest_velocity(velocity("recovered", 1.2), 1.2)
    assert recovered.status == "ready"
    assert recovered.active_contradictions == ()


def test_existing_newer_capture_reconciles_a_delayed_physical_contradiction():
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(tag("tag", 1.3), 1.3)
    tracker.ingest_velocity(velocity("velocity", 1.3), 1.3)
    tracker.ingest_height(height("height", 1.3), 1.3)

    delayed_bad = tracker.ingest_velocity(velocity("too-fast", 1.2, (0.6, 0, 0)), 1.3)
    assert delayed_bad.status == "ready"
    assert delayed_bad.last_rejection == "velocity_out_of_bounds"
    assert delayed_bad.active_contradictions == ()


def test_confidence_thresholds_and_three_second_loss_timer_are_independent():
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(tag("tag", 100.0), 100.0)
    tracker.ingest_velocity(velocity("velocity", 100.0), 100.0)
    tracker.ingest_height(height("height", 100.0), 100.0)

    assert tracker.snapshot(100.499).confidence == "green"
    at_loss = tracker.snapshot(100.5)
    assert at_loss.confidence == "amber"
    assert at_loss.loss_age_s == 0
    assert tracker.snapshot(101.999).confidence == "amber"
    red = tracker.snapshot(102.0)
    assert red.confidence == "red"
    assert red.status == "hold"
    assert red.loss_age_s == pytest.approx(1.5)
    assert tracker.snapshot(103.499).status == "hold"
    landed = tracker.snapshot(103.5)
    assert landed.status == "land"
    assert landed.loss_age_s == pytest.approx(3)


def test_only_accepted_measurement_sources_are_reported():
    tracker = ControlLocalization(config())
    assert tracker.snapshot(1.0).source_ids == ()
    assert tracker.ingest_tag_fix(tag("tag", 1.0), 1.0).source_ids == ("tag-camera",)


def test_rejected_regressing_time_cannot_mutate_diagnostics_or_readiness():
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(tag("tag", 1.0), 1.0)
    tracker.ingest_velocity(velocity("velocity", 1.0), 1.0)
    assert tracker.ingest_height(height("height", 1.0), 1.0).status == "ready"

    with pytest.raises(ValueError, match="monotonic"):
        tracker.ingest_velocity(velocity("unsafe", 0.9, (1.0, 0, 0)), 0.9)

    unchanged = tracker.snapshot(1.0)
    assert unchanged.status == "ready"
    assert unchanged.last_rejection is None
    assert unchanged.active_contradictions == ()


def test_delayed_measurement_reconciles_a_previous_innovation_rejection():
    tracker = ControlLocalization(config())
    tracker.ingest_tag_fix(tag("initial", 1.0), 1.0)
    rejected = tracker.ingest_tag_fix(tag("later", 1.1, (0.7, 0, 1)), 1.1)
    assert rejected.last_rejection == "innovation_rejected"
    assert rejected.active_contradictions == ("tag:innovation_rejected",)

    reconciled = tracker.ingest_tag_fix(tag("delayed", 1.05, (0.5, 0, 1)), 1.1)
    assert reconciled.active_contradictions == ()
    assert reconciled.fix_age_s == pytest.approx(0)


def test_config_and_extrinsics_are_canonical_immutable_values():
    bounds = np.array([[-10.0, 10.0], [-10.0, 10.0], [0.0, 3.0]])
    settings = config(position_bounds_map_enu_m=bounds)
    matrix = np.eye(4)
    transform = extrinsics(1.0, matrix=matrix)

    bounds[0, 0] = -100
    matrix[0, 3] = 100
    assert settings.position_bounds_map_enu_m[0] == (-10.0, 10.0)
    assert transform.matrix[0][3] == 0.0

    with pytest.raises(ValueError):
        config(map_id=" map-sha")
    with pytest.raises(ValueError):
        config(horizon_s=True)
    with pytest.raises(ValueError):
        extrinsics(1.0, measured=1)

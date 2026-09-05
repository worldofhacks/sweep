from dataclasses import replace

from arbiter.safety import SafetyArbiter
from perception.control_localization import (
    BodyExtrinsics,
    ControlLocalization,
    ControlLocalizationConfig,
    HeightObservation,
    TagFix,
    VelocityObservation,
)
from planner.models import RefusalReason
from relay.control_localization import (
    ClockMapping,
    ControlLocalizationPins,
    ControlLocalizationStore,
    ControlLocalizationWire,
    to_wire_payload,
)
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_intent, make_snapshot, safety_config

COVARIANCE = ((0.01, 0.0, 0.0), (0.0, 0.01, 0.0), (0.0, 0.0, 0.01))
IDENTITY = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def fuser() -> ControlLocalization:
    return ControlLocalization(
        ControlLocalizationConfig(
            drone_id=1,
            connection_epoch=1,
            map_id="map-sha",
            geometry_id="geometry-sha",
            clock_id="camera-clock",
            tag_source_id="tag-camera",
            velocity_source_id="msdk-velocity",
            height_source_id="tof-height",
            camera_calibration_id="camera-calibration-sha",
            body_extrinsics_id="body-extrinsics-sha",
            production_evidence_verified=True,
        )
    )


def fix(capture_time: float) -> TagFix:
    return TagFix(
        event_id="tag",
        drone_id=1,
        connection_epoch=1,
        map_id="map-sha",
        geometry_id="geometry-sha",
        clock_id="camera-clock",
        capture_time=capture_time,
        position_map_enu_m=(2.0, 3.0, 1.0),
        covariance_map_enu_m2=COVARIANCE,
        source_id="tag-camera",
        camera_calibration_id="camera-calibration-sha",
        source_verified=True,
        timing_verified=True,
        extrinsics=BodyExtrinsics(
            extrinsics_id="body-extrinsics-sha",
            source_id="tag-camera",
            matrix=IDENTITY,
            capture_time=capture_time,
            gimbal_time=capture_time,
            attitude_time=capture_time,
            measured=True,
        ),
    )


def fresh_snapshot():
    tracker = fuser()
    tracker.ingest_tag_fix(fix(0.9), 1.0)
    tracker.ingest_velocity(
        VelocityObservation(
            "velocity",
            1,
            1,
            "map-sha",
            "geometry-sha",
            "camera-clock",
            0.95,
            (1.0, 0.0, 0.0),
            COVARIANCE,
            "msdk-velocity",
            True,
            True,
        ),
        1.0,
    )
    tracker.ingest_height(
        HeightObservation(
            "height",
            1,
            1,
            "map-sha",
            "geometry-sha",
            "camera-clock",
            0.97,
            1.0,
            0.01,
            "tof-height",
            True,
            True,
        ),
        1.0,
    )
    return tracker.snapshot(1.0)


def mapping() -> ClockMapping:
    return ClockMapping("camera-clock", "relay-monotonic", 0.0, 100_000, 1_000.0, 5, True)


def payload() -> dict[str, object]:
    return to_wire_payload(fresh_snapshot(), mapping(), "authenticated-adapter-signature")


def store() -> ControlLocalizationStore:
    return ControlLocalizationStore(
        {
            1: ControlLocalizationPins(
                1,
                1,
                "map-sha",
                "geometry-sha",
                "camera-calibration-sha",
                "body-extrinsics-sha",
                "camera-clock",
                "relay-monotonic",
                ("tag-camera", "msdk-velocity", "tof-height"),
            )
        },
        max_clock_error_ms=5,
        max_fix_age_ms=500,
    )


def test_fuser_wire_store_replaces_generic_telemetry_pose_with_capture_timestamp():
    localization = fresh_snapshot()
    wire_payload = to_wire_payload(localization, mapping(), "authenticated-adapter-signature")
    decoded = ControlLocalizationWire.from_mapping(wire_payload)
    assert decoded.last_fix_capture_time_s == 0.9
    evidence = store()
    evidence.ingest(wire_payload, 1, 1, 101_000)

    applied = evidence.apply(make_snapshot(1, now_ms=101_000))
    aircraft = applied.aircraft[1]
    assert aircraft.pose.x == localization.position_map_enu_m[0]
    assert aircraft.pose.y == localization.position_map_enu_m[1]
    assert aircraft.position_quality == 1.0
    assert aircraft.position_last_seen_ms == 100_900


def test_stale_localization_replaces_quality_and_existing_safety_refuses_translation():
    evidence = store()
    evidence.ingest(payload(), 1, 1, 101_000)
    base = make_snapshot(1, now_ms=101_600)
    base = replace(base, aircraft={1: replace(base.aircraft[1], link_last_seen_ms=101_600)})
    stale = evidence.apply(base)
    assert stale.aircraft[1].position_quality == 0.0
    assert stale.aircraft[1].position_last_seen_ms == 100_900

    refusal = SafetyArbiter(safety_config()).check_intent(
        make_intent(IntentName.TRANSLATE, selection=(1,)), stale
    )
    assert refusal is not None
    assert refusal.reason is RefusalReason.POSITION_QUALITY


def test_mismatched_pins_and_unverified_webcam_shape_cannot_fall_back_to_telemetry():
    evidence = store()
    wire_payload = payload()
    wire_payload["map_id"] = "wrong-map"
    evidence.ingest(wire_payload, 1, 1, 101_000)
    mismatch = evidence.apply(make_snapshot(1, now_ms=101_000))
    assert mismatch.aircraft[1].position_quality == 0.0
    assert mismatch.aircraft[1].position_last_seen_ms == 0

    evidence.ingest({"type": "webcam_localization", "control_eligible": False}, 1, 1, 101_000)
    webcam = evidence.apply(make_snapshot(1, now_ms=101_000))
    assert webcam.aircraft[1].position_quality == 0.0


def test_clock_uncertainty_and_capture_regression_are_loss_states():
    evidence = store()
    wire_payload = payload()
    wire_payload["clock_mapping"] = {**wire_payload["clock_mapping"], "max_error_ms": 6}
    evidence.ingest(wire_payload, 1, 1, 101_000)
    assert evidence.apply(make_snapshot(1, now_ms=101_000)).aircraft[1].position_quality == 0.0

    evidence = store()
    evidence.ingest(payload(), 1, 1, 101_000)
    older = payload()
    older["last_fix_capture_time_s"] = 0.8
    older["fix_age_s"] = 0.2
    evidence.ingest(older, 1, 1, 101_000)
    assert evidence.apply(make_snapshot(1, now_ms=101_000)).aircraft[1].position_quality == 0.0

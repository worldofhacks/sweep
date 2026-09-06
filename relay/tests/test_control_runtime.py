from perception.control_localization import (
    BodyExtrinsics,
    ControlLocalization,
    ControlLocalizationConfig,
    HeightObservation,
    TagFix,
    VelocityObservation,
)
from relay.auth import verify_event_signature
from relay.control_frames import ControlLocalizationFrame, sign_localization_frame
from relay.control_localization import ClockMapping, ControlLocalizationWire, to_wire_payload
from relay.control_runtime import ControlRuntime, ControlRuntimeConfig
from tests.autonomy_fixtures import make_snapshot

KEY = b"node-key-for-control-pose-32bytes"


def config():
    return ControlRuntimeConfig.from_mapping(
        {
            "limits": {
                "max_clock_error_ms": 5,
                "max_fix_age_ms": 500,
                "max_position_uncertainty_m": 0.3,
                "land_after_fix_age_ms": 2_000,
            },
            "drones": [
                {
                    "drone_id": 1,
                    "connection_epoch": 1,
                    "map_id": "map",
                    "geometry_id": "geometry",
                    "camera_calibration_id": "camera",
                    "body_extrinsics_id": "body",
                    "capture_clock_id": "clock",
                    "relay_clock_id": "relay",
                    "source_ids": ["tag", "velocity", "height"],
                    "clock_mapping": {
                        "capture_clock_id": "clock",
                        "relay_clock_id": "relay",
                        "capture_reference_s": 0,
                        "relay_reference_ms": 100_000,
                        "milliseconds_per_capture_second": 1000,
                        "max_error_ms": 5,
                        "measured": True,
                    },
                }
            ],
        },
    )


def frame():
    clock = ClockMapping("clock", "relay", 0, 100_000, 1000, 5, True)
    fuser = ControlLocalization(
        ControlLocalizationConfig(
            1, 1, "map", "geometry", "clock", "tag", "velocity", "height", "camera", "body", True
        )
    )
    transform = ((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1))
    fuser.ingest_tag_fix(
        TagFix(
            "tag",
            1,
            1,
            "map",
            "geometry",
            "clock",
            0.9,
            (1, 2, 1),
            ((0.01, 0, 0), (0, 0.01, 0), (0, 0, 0.01)),
            "tag",
            "camera",
            True,
            True,
            BodyExtrinsics("body", "tag", transform, 0.9, 0.9, 0.9, True),
        ),
        1,
    )
    fuser.ingest_velocity(
        VelocityObservation(
            "velocity",
            1,
            1,
            "map",
            "geometry",
            "clock",
            0.95,
            (0, 0, 0),
            ((0.01, 0, 0), (0, 0.01, 0), (0, 0, 0.01)),
            "velocity",
            True,
            True,
        ),
        1,
    )
    fuser.ingest_height(
        HeightObservation(
            "height", 1, 1, "map", "geometry", "clock", 0.97, 1, 0.01, "height", True, True
        ),
        1,
    )
    wire = ControlLocalizationWire.from_mapping(
        to_wire_payload(fuser.snapshot(1), clock, "pending", "localization")
    )
    return ControlLocalizationFrame.parse(
        sign_localization_frame(
            wire, timestamp_ms=101_000, event_id="localization", session="s", signing_key=KEY
        )
    )


def test_verified_frame_projects_and_emits_a_signed_fixed_point_packet():
    runtime = ControlRuntime(config(), node_keys={1: KEY})
    assert runtime.ingest(frame(), 1, 1, 101_000).accepted
    applied = runtime.apply(make_snapshot(1, now_ms=101_000))
    packet = runtime.control_pose(1, applied, "s", 101_000)
    unsigned = dict(packet)
    del unsigned["signature"]
    assert packet["status"] == "ready"
    assert packet["fix_time_ms"] == 100_895
    assert packet["flight_approved"] is False
    assert packet["position_frame"] == "map_enu"
    assert packet["t"] >= packet["pose_time_ms"] >= packet["fix_time_ms"]
    assert packet["position_uncertainty_mm"] == 282
    assert verify_event_signature(unsigned, packet["signature"], KEY)


def test_duplicate_and_stale_evidence_hold_then_land():
    runtime = ControlRuntime(config(), node_keys={1: KEY})
    accepted = frame()
    assert runtime.ingest(accepted, 1, 1, 101_000).accepted
    assert runtime.ingest(accepted, 1, 1, 101_000).reason == "duplicate_event"
    ready = runtime.apply(make_snapshot(1, now_ms=101_000))
    assert runtime.control_pose(1, ready, "s", 101_000)["status"] == "ready"
    stale = runtime.apply(make_snapshot(1, now_ms=101_600))
    assert runtime.control_pose(1, stale, "s", 101_600)["status"] == "hold"
    landed = runtime.apply(make_snapshot(1, now_ms=103_600))
    assert runtime.control_pose(1, landed, "s", 103_600)["status"] == "land"


def test_repeated_loss_packets_preserve_fix_and_never_freshen():
    runtime = ControlRuntime(config(), node_keys={1: KEY})
    assert runtime.ingest(frame(), 1, 1, 101_000).accepted
    applied = runtime.apply(make_snapshot(1, now_ms=101_000))
    ready = runtime.control_pose(1, applied, "s", 101_000)
    stale = runtime.apply(make_snapshot(1, now_ms=101_600))
    first = runtime.control_pose(1, stale, "s", 101_600)
    again = runtime.control_pose(1, stale, "s", 101_700)
    assert first["fix_time_ms"] == again["fix_time_ms"] == ready["fix_time_ms"]
    assert again["pose_time_ms"] == first["pose_time_ms"] == ready["pose_time_ms"]


def test_config_rejects_unknown_keys_and_has_secret_free_identity():
    raw = {"limits": {}, "drones": [], "unknown": True}
    try:
        ControlRuntimeConfig.from_mapping(raw)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown config field accepted")
    assert len(config().identity) == 64


def test_config_loads_from_environment_path(tmp_path):
    path = tmp_path / "control.json"
    import json

    path.write_text(
        json.dumps(
            {
                "limits": {
                    "max_clock_error_ms": 5,
                    "max_fix_age_ms": 500,
                    "max_position_uncertainty_m": 0.3,
                    "land_after_fix_age_ms": 2000,
                },
                "drones": [
                    {
                        "drone_id": 1,
                        "connection_epoch": 1,
                        "map_id": "map",
                        "geometry_id": "geometry",
                        "camera_calibration_id": "camera",
                        "body_extrinsics_id": "body",
                        "capture_clock_id": "clock",
                        "relay_clock_id": "relay",
                        "source_ids": ["tag"],
                        "clock_mapping": {
                            "capture_clock_id": "clock",
                            "relay_clock_id": "relay",
                            "capture_reference_s": 0,
                            "relay_reference_ms": 100000,
                            "milliseconds_per_capture_second": 1000,
                            "max_error_ms": 5,
                            "measured": True,
                        },
                    }
                ],
            }
        )
    )
    assert (
        ControlRuntimeConfig.from_env({"SWEEP_CONTROL_LOCALIZATION_CONFIG": str(path)})
        .pins[1]
        .map_id
        == "map"
    )


def test_repeated_ready_publication_cannot_refresh_pose_time():
    runtime = ControlRuntime(config(), node_keys={1: KEY})
    runtime.ingest(frame(), 1, 1, 101_000)
    applied = runtime.apply(make_snapshot(1, now_ms=101_000))
    first = runtime.control_pose(1, applied, "s", 101_000)
    assert first["pose_time_ms"] == applied.aircraft[1].control_provenance.evaluated_at_relay_ms
    assert runtime.control_pose(1, applied, "s", 101_100) is None


def test_missing_evidence_emits_no_fictitious_control_pose():
    runtime = ControlRuntime(config(), node_keys={1: KEY})

    assert runtime.control_pose(1, make_snapshot(1, now_ms=101_000), "s", 101_000) is None


def test_packet_shape_matches_the_diagnostic_control_pose_contract():
    runtime = ControlRuntime(config(), node_keys={1: KEY})
    assert runtime.ingest(frame(), 1, 1, 101_000).accepted
    packet = runtime.control_pose(1, runtime.apply(make_snapshot(1, now_ms=101_000)), "s", 101_000)

    assert set(packet) == {
        "v",
        "type",
        "t",
        "event_id",
        "session",
        "drone_id",
        "connection_epoch",
        "map_id",
        "geometry_id",
        "camera_calibration_id",
        "body_extrinsics_id",
        "pose_time_ms",
        "fix_time_ms",
        "position_frame",
        "x_mm",
        "y_mm",
        "z_mm",
        "position_uncertainty_mm",
        "status",
        "flight_approved",
        "signature",
    }

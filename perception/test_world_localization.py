import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from perception.control_localization import ControlLocalizationSnapshot
from perception.control_publisher import ControlPublisher, ControlPublisherConfig, LiveBinding
from perception.world_localization import (
    CaptureAlignmentConfig,
    MeasurementUncertainty,
    WorldEnuTransform,
    WorldLocalizationAdapter,
    WorldLocalizationError,
    WorldLocalizationPins,
)
from perception.world_localization_runtime import WorldLocalizationRuntime
from relay.control_frames import ControlLocalizationFrame
from relay.observations import ClockMapping, Observation
from tests.test_measured_world_geometry import _authoring, _held_out_world_bundle
from tools.map_geometry import generate

IDENTITY = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))
_CANONICAL_ALIGNMENT_SHA256 = "0877d81fe8e9cd96e07ca6fd0374d8dd2b3fba072d96d6a2dff799a1bbe652d0"


@pytest.mark.parametrize("reader", ["config", "evidence"])
def test_localization_fifo_input_refuses_without_waiting_for_a_writer(tmp_path, reader):
    fifo = tmp_path / "input.json"
    os.mkfifo(fifo)
    program = """
import sys
from perception.world_localization import _evidence_document
from perception.world_localization_runtime import WorldLocalizationRuntimeConfig
try:
    if sys.argv[2] == 'config':
        WorldLocalizationRuntimeConfig.load(sys.argv[1])
    else:
        _evidence_document(sys.argv[1], 'geometry', '0' * 64)
except ValueError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    result = subprocess.run([sys.executable, "-c", program, str(fifo), reader], timeout=5)
    assert result.returncode == 0


COVARIANCE = ((0.01, 0.0, 0.0), (0.0, 0.02, 0.0), (0.0, 0.0, 0.03))
_UNSET = object()
_alignment_sha256 = ""


def mapping():
    return ClockMapping(
        "phone_snapshot_wall_ms", "phone_snapshot_wall_ms", "ms", 1_000_000, 1, 1, 1, 2
    )


def test_canonical_phone_capture_alignment_fixture_matches_the_host_contract():
    fixture_directory = Path(__file__).with_name("fixtures")
    config_bytes = (fixture_directory / "capture-alignment.json").read_bytes()
    assert sha256(config_bytes).hexdigest() == _CANONICAL_ALIGNMENT_SHA256
    config = CaptureAlignmentConfig.from_document(
        json.loads(config_bytes), _CANONICAL_ALIGNMENT_SHA256
    )
    observation = Observation.parse(
        json.loads((fixture_directory / "capture-alignment-observation.json").read_text())
        | {"t_ingest": 1}
    )

    assert config.alignment_config_id == "ohmni-alignment-fixture"
    assert observation.submission.source_id == "dji-body-camera"
    assert (
        observation.submission.payload["capture_alignment"]["alignment_config_sha256"]
        == config.sha256
    )


def measured_geometry(tmp_path):
    bundle, accepted = _held_out_world_bundle(tmp_path)
    authoring = _authoring(tmp_path, bundle)
    output = tmp_path / "geometry"
    generate(bundle, authoring, output, accepted)
    return (
        bundle,
        accepted,
        authoring,
        output,
        json.loads((output / "geometry.json").read_text()),
        sha256((output / "geometry.json").read_bytes()).hexdigest(),
    )


def evidence(tmp_path, manifest, geometry_directory, geometry_authoring):
    matrix = [
        [0.0, -1.0, 0.0, 10.0],
        [1.0, 0.0, 0.0, 20.0],
        [0.0, 0.0, 1.0, 30.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    documents = {
        "camera_calibration": {
            "schema_version": 2,
            "model": "fisheye",
            "status": "offline",
            "evidence_kind": "recorded_live",
            "camera_serial": "mini3-camera-1",
            "pipeline": {"resolution_px": [1280, 720]},
            "image_size_px": [1280, 720],
            "camera_matrix": [[600.0, 0.0, 640.0], [0.0, 600.0, 360.0], [0.0, 0.0, 1.0]],
            "distortion_coefficients": [0.0, 0.0, 0.0, 0.0],
            "rms_reprojection_error_px": 0.2,
            "accepted_image_count": 20,
            "image_sha256": {str(index): f"{index:064x}" for index in range(20)},
            "quality": {
                "accepted_image_count": 20,
                "minimum_accepted_image_count": 20,
                "rms_reprojection_error_px": 0.2,
                "maximum_rms_reprojection_error_px": 0.5,
                "minimum_pose_constraint_ratio": 0.005,
                "pose_constraint_ratio": 0.01,
                "opencv_check_cond": True,
            },
        },
        "uncertainty": {
            "artifact_id": "route-run",
            "kind": "uncertainty",
            "camera_calibration_id": "mini3-delivered-720p",
            "camera_pipeline_id": "o2-720p",
            "position_covariance_world_m2": [list(row) for row in COVARIANCE],
            "velocity_covariance_enu_m2ps2": [[0.04, 0.0, 0.0], [0.0, 0.05, 0.0], [0.0, 0.0, 0.06]],
            "height_variance_enu_m2": 0.07,
            "evidence_kind": "recorded_live",
        },
        "world_enu": {
            "artifact_id": "world-to-enu",
            "kind": "world_enu_transform",
            "map_id": manifest["map_id"],
            "map_version": manifest["bundle_version"],
            "physical_datum": manifest["frame"]["physical_datum"],
            "matrix_world_enu": matrix,
            "measured": True,
        },
        "height_alignment": {
            "artifact_id": "dji-relative-altitude-to-enu-z",
            "kind": "height_alignment",
            "map_id": manifest["map_id"],
            "map_version": manifest["bundle_version"],
            "telemetry_frame_id": "dji_enu",
            "height_datum_id": "dji-relative-altitude-to-enu-z",
            "measured": True,
            "variance_m2": 0.07,
        },
        "capture_alignment": {
            "v": 1,
            "enabled": True,
            "id": "dji-body-camera-v1",
            "scope": {
                "session": "live-session",
                "device_id": 1,
                "connection_epoch": 7,
                "map_id": manifest["map_id"],
                "source_id": "dji-body-camera",
                "frame": "body",
                "camera_frame": "camera",
            },
            "capture_clock": {"clock_id": "phone_snapshot_wall_ms", "unit": "ms"},
            "frame_pts_clock": {"clock_id": "dji_stream_presentation_ms", "unit": "ms"},
            "clock_mapping_id": "phone_snapshot_wall_ms",
            "frame_pts_to_capture": {
                "offset_ms": 0.0,
                "rate_numerator": 1,
                "rate_denominator": 1,
                "max_error_ms": 0.0,
            },
            "gimbal_callback": {
                "max_latency_ms": 0.0,
                "max_orientation_error_deg": 0.0,
                "angular_rate_bound_deg_s": 0.0,
            },
            "body_attitude_callback": {
                "max_latency_ms": 0.0,
                "max_orientation_error_deg": 0.0,
                "angular_rate_bound_deg_s": 0.0,
            },
            "max_extrinsics_angle_error_deg": 0.0,
            "kinematic_calibration": {
                "id": "dji-gimbal-camera-kinematics-v1",
                "sha256": "a" * 64,
                "gimbal_attitude_convention": "intrinsic_zyx_degrees",
                "body_to_gimbal": {
                    "parent_frame": "body",
                    "child_frame": "gimbal",
                    "x_m": 0.0,
                    "y_m": 0.0,
                    "z_m": 0.0,
                    "qx": 0.0,
                    "qy": 0.0,
                    "qz": 0.0,
                    "qw": 1.0,
                },
                "gimbal_to_camera": {
                    "parent_frame": "gimbal",
                    "child_frame": "camera",
                    "x_m": 0.0,
                    "y_m": 0.0,
                    "z_m": 0.0,
                    "qx": 0.0,
                    "qy": 0.0,
                    "qz": 0.0,
                    "qw": 1.0,
                },
            },
        },
    }
    paths = {"geometry_directory": geometry_directory, "geometry_authoring": geometry_authoring}
    hashes = {}
    for name, document in documents.items():
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        path = tmp_path / f"{name}.json"
        path.write_bytes(encoded)
        paths[name] = path
        hashes[name] = sha256(encoded).hexdigest()
    global _alignment_sha256
    _alignment_sha256 = hashes["capture_alignment"]
    return paths, hashes


def pins(manifest, hashes, geometry_report, geometry_sha256, **overrides):
    values = dict(
        drone_id=1,
        map_id=manifest["map_id"],
        map_version=manifest["bundle_version"],
        map_content_sha256=manifest["content_sha256"],
        geometry_id=geometry_report["authoring_sha256"],
        geometry_sha256=geometry_sha256,
        physical_datum=manifest["frame"]["physical_datum"],
        tag_source_id="laptop-detector",
        body_pose_source_id="dji-body-camera",
        capture_alignment_config_id="dji-body-camera-v1",
        capture_alignment_config_sha256=hashes["capture_alignment"],
        telemetry_source_id="dji-telemetry",
        telemetry_frame_id="dji_enu",
        height_datum_id="dji-relative-altitude-to-enu-z",
        height_alignment_artifact_id="dji-relative-altitude-to-enu-z",
        height_alignment_sha256=hashes["height_alignment"],
        height_alignment_measured=True,
        capture_clock_mapping_id="phone_snapshot_wall_ms",
        camera_calibration_id="mini3-delivered-720p",
        camera_calibration_sha256=hashes["camera_calibration"],
        camera_serial="mini3-camera-1",
        camera_pipeline_id="o2-720p",
        body_extrinsics_id="dji-gimbal-attitude-capture",
        world_enu=WorldEnuTransform(
            "world-to-enu",
            hashes["world_enu"],
            (
                (0.0, -1.0, 0.0, 10.0),
                (1.0, 0.0, 0.0, 20.0),
                (0.0, 0.0, 1.0, 30.0),
                (0.0, 0.0, 0.0, 1.0),
            ),
            True,
        ),
        uncertainty=MeasurementUncertainty(
            "route-run",
            hashes["uncertainty"],
            "recorded_live",
            "mini3-delivered-720p",
            "o2-720p",
            COVARIANCE,
            ((0.04, 0.0, 0.0), (0.0, 0.05, 0.0), (0.0, 0.0, 0.06)),
            0.07,
        ),
    )
    return WorldLocalizationPins(**(values | overrides))


def event(kind, source, frame, payload, capture=1_000_000, **overrides):
    raw = {
        "v": 1,
        "type": "observation",
        "event_id": f"{kind}-{capture}",
        "session": "live-session",
        "device_id": 1,
        "connection_epoch": 7,
        "source_id": source,
        "node_type": "aircraft",
        "frame": frame,
        "confidence": 0.9,
        "t_capture": {"clock_id": "phone_snapshot_wall_ms", "unit": "ms", "value": capture},
        "t_source_receipt": {
            "clock_id": "phone_snapshot_wall_ms",
            "unit": "ms",
            "value": capture + 1,
        },
        "clock_mapping_id": "phone_snapshot_wall_ms",
        "payload": payload,
        "t_ingest": 10,
    }
    return Observation.parse(raw | overrides)


def camera_frame(capture=1_000_000, **overrides):
    return event(
        "camera",
        "laptop-detector",
        "camera",
        {
            "kind": "camera_frame",
            "image_id": "frame-1",
            "sha256": "d" * 64,
            "width_px": 1280,
            "height_px": 720,
            "calibration_id": "mini3-delivered-720p",
        },
        capture,
        **overrides,
    )


def capture_alignment(capture):
    return {
        "v": 1,
        "alignment_config_id": "dji-body-camera-v1",
        "alignment_config_sha256": _alignment_sha256,
        "kinematic_calibration_id": "dji-gimbal-camera-kinematics-v1",
        "kinematic_calibration_sha256": "a" * 64,
        "frame_pts": {"clock_id": "dji_stream_presentation_ms", "unit": "ms", "value": capture},
        "gimbal_receipt": {"clock_id": "phone_snapshot_wall_ms", "unit": "ms", "value": capture},
        "body_attitude_receipt": {
            "clock_id": "phone_snapshot_wall_ms",
            "unit": "ms",
            "value": capture,
        },
        "gimbal_attitude": {"yaw_deg": 0.0, "pitch_deg": 0.0, "roll_deg": 0.0},
        "body_attitude": {"yaw_deg": 0.0, "pitch_deg": 0.0, "roll_deg": 0.0},
        "frame_capture_error_ms": 0.0,
        "gimbal_callback_latency_ms": 0.0,
        "body_attitude_callback_latency_ms": 0.0,
        "gimbal_callback_orientation_error_deg": 0.0,
        "body_attitude_callback_orientation_error_deg": 0.0,
        "gimbal_angular_rate_bound_deg_s": 0.0,
        "body_angular_rate_bound_deg_s": 0.0,
        "max_extrinsics_angle_error_deg": 0.0,
    }


def body_pose(capture=1_000_000, **overrides):
    return event(
        "body",
        "dji-body-camera",
        "body",
        {
            "kind": "pose",
            "pose": {
                "parent_frame": "body",
                "child_frame": "camera",
                "x_m": 0.0,
                "y_m": 0.0,
                "z_m": 0.0,
                "qx": 0.0,
                "qy": 0.0,
                "qz": 0.0,
                "qw": 1.0,
            },
            "capture_alignment": capture_alignment(capture),
        },
        capture,
        **overrides,
    )


def tag(capture=1_000_000, covariance_m2=_UNSET, **overrides):
    payload = {
        "kind": "tag_observation",
        "family": "tag36h11",
        "tag_id": 0,
        "image_id": "frame-1",
        "pose_accepted": True,
        "tag_pose": {
            "parent_frame": "camera",
            "child_frame": "tag:0",
            "x_m": 0.0,
            "y_m": 0.0,
            "z_m": 2.0,
            "qx": 0.0,
            "qy": 0.0,
            "qz": 0.0,
            "qw": 1.0,
        },
        "covariance_m2": [0.01, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.01],
        "reason": "pose",
        "size_m": 0.16,
        "corners_px": [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0]],
        "pixel_frame": "camera",
        "reprojection_rms_px": 0.2,
    }
    if covariance_m2 is not _UNSET:
        payload["covariance_m2"] = covariance_m2
    return event("tag", "laptop-detector", "camera", payload, capture, **overrides)


def telemetry(capture=2_000_000, z=3.0):
    return event(
        "telemetry",
        "dji-telemetry",
        "dji_enu",
        {
            "kind": "telemetry",
            "position": {"frame": "dji_enu", "x_m": 1.0, "y_m": 2.0, "z_m": z},
            "velocity": {"frame": "dji_enu", "x_m_s": 0.1, "y_m_s": 0.2, "z_m_s": 0.3},
            "battery": 0.8,
            "link": 0.9,
            "pos_quality": 0.7,
            "state": "flying",
        },
        capture,
    )


@pytest.fixture
def adapter(tmp_path):
    bundle, accepted, authoring, geometry_directory, report, geometry_sha256 = measured_geometry(
        tmp_path
    )
    manifest = json.loads((bundle / "manifest.yaml").read_text())
    evidence_paths, hashes = evidence(tmp_path, manifest, geometry_directory, authoring)
    active_pins = pins(manifest, hashes, report, geometry_sha256)
    return WorldLocalizationAdapter(
        bundle,
        accepted,
        active_pins,
        mapping(),
        evidence_paths=evidence_paths,
    )


def test_camera_tag_and_dynamic_capture_pose_become_rotated_enu_fix(adapter):
    assert adapter.ingest(camera_frame(), connection_epoch=7) == ()
    assert adapter.ingest(body_pose(), connection_epoch=7) == ()
    (fix,) = adapter.ingest(tag(), connection_epoch=7)
    np.testing.assert_allclose(fix.position_map_enu_m, (-20.0, 10.0, -32.0))
    np.testing.assert_allclose(
        fix.covariance_map_enu_m2, ((0.02, 0, 0), (0, 0.01, 0), (0, 0, 0.03))
    )
    assert fix.capture_time == pytest.approx(0.001)
    assert fix.extrinsics.capture_time == fix.capture_time


def test_tag_requires_same_capture_time_body_pose_and_camera_frame(adapter):
    adapter.ingest(camera_frame(), connection_epoch=7)
    adapter.ingest(body_pose(capture=999_999), connection_epoch=7)
    with pytest.raises(WorldLocalizationError, match="capture-time body extrinsics"):
        adapter.ingest(tag(), connection_epoch=7)


def test_dynamic_capture_pose_rotation_enters_the_world_body_transform(adapter):
    adapter.ingest(camera_frame(), connection_epoch=7)
    alignment = capture_alignment(1_000_000)
    alignment["gimbal_attitude"] = {"yaw_deg": 90.0, "pitch_deg": 0.0, "roll_deg": 0.0}
    adapter.ingest(
        event(
            "body",
            "dji-body-camera",
            "body",
            {
                "kind": "pose",
                "pose": {
                    "parent_frame": "body",
                    "child_frame": "camera",
                    "x_m": 0.0,
                    "y_m": 0.0,
                    "z_m": 0.0,
                    "qx": 0.0,
                    "qy": 0.0,
                    "qz": 2**-0.5,
                    "qw": 2**-0.5,
                },
                "capture_alignment": alignment,
            },
        ),
        connection_epoch=7,
    )
    (fix,) = adapter.ingest(tag(), connection_epoch=7)
    np.testing.assert_allclose(
        fix.extrinsics.matrix[:2],
        ((0.0, -1.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
        atol=1e-6,
    )


def test_body_camera_pose_must_match_the_pinned_gimbal_composition(adapter):
    raw = body_pose().to_mapping()
    payload = dict(raw["payload"])
    alignment = dict(payload["capture_alignment"])
    alignment["gimbal_attitude"] = {"yaw_deg": 90.0, "pitch_deg": 0.0, "roll_deg": 0.0}
    payload["capture_alignment"] = alignment

    with pytest.raises(WorldLocalizationError, match="measured mount and actual gimbal angles"):
        adapter.ingest(
            event("body-invalid-mount", "dji-body-camera", "body", payload), connection_epoch=7
        )


def test_local_enu_telemetry_is_preserved_and_height_is_explicit(adapter):
    velocity, height = adapter.ingest(telemetry(), connection_epoch=7)
    assert velocity.velocity_map_enu_mps == pytest.approx((0.1, 0.2, 0.3))
    assert height.height_map_enu_m == pytest.approx(3.0)
    assert height.variance_m2 == pytest.approx(0.07)


def test_unpinned_frame_map_or_epoch_is_refused(adapter):
    with pytest.raises(WorldLocalizationError, match="current aircraft epoch"):
        adapter.ingest(camera_frame(connection_epoch=8), connection_epoch=7)
    with pytest.raises(WorldLocalizationError, match="source is unpinned"):
        adapter.ingest(camera_frame(source_id="other"), connection_epoch=7)
    with pytest.raises(WorldLocalizationError, match="clock mapping is unpinned"):
        adapter.ingest(camera_frame(clock_mapping_id="other"), connection_epoch=7)


def test_runtime_drains_the_localization_socket_and_enqueues_only_canonical_measurements(adapter):
    class Publisher:
        config = SimpleNamespace(mode="live", drones={1: object()})

        def __init__(self):
            self.enqueued = []
            self.refused = []

        def take_live_observations(self, drone_id):
            assert drone_id == 1
            return SimpleNamespace(connection_epoch=7), tuple(
                item.to_mapping() for item in (camera_frame(), body_pose(), tag(), telemetry())
            )

        def enqueue(self, raw):
            self.enqueued.append(raw)

        def refuse_input(self, raw, reason):
            self.refused.append((raw, reason))

        def publish_live(self, drone_id, monotonic_s):
            return {"type": "control_localization", "drone_id": drone_id, "t": monotonic_s}

    publisher = Publisher()
    result = WorldLocalizationRuntime(publisher, {1: adapter}).tick(1, 2.0)
    assert result["type"] == "control_localization"
    assert [item["kind"] for item in publisher.enqueued] == ["tag", "velocity", "height"]
    assert publisher.refused == []


def test_host_pinned_uncertainty_allows_nullable_detector_covariance(adapter):
    adapter.ingest(camera_frame(), connection_epoch=7)
    adapter.ingest(body_pose(), connection_epoch=7)
    (fix,) = adapter.ingest(tag(covariance_m2=None), connection_epoch=7)
    np.testing.assert_allclose(
        fix.covariance_map_enu_m2, ((0.02, 0, 0), (0, 0.01, 0), (0, 0, 0.03))
    )


def test_capture_time_is_required_before_any_camera_measurement(adapter):
    with pytest.raises(WorldLocalizationError, match="requires a capture timestamp"):
        adapter.ingest(camera_frame(t_capture=None), connection_epoch=7)


def test_dynamic_body_pose_has_its_own_pinned_source(adapter):
    with pytest.raises(WorldLocalizationError, match="body-camera source is unpinned"):
        adapter.ingest(body_pose(source_id="laptop-detector"), connection_epoch=7)


def test_camera_cache_cannot_mix_another_session_at_the_same_capture_stamp(adapter):
    adapter.ingest(camera_frame(session="other-session"), connection_epoch=7)
    with pytest.raises(WorldLocalizationError, match="scope"):
        adapter.ingest(body_pose(session="other-session"), connection_epoch=7)


def test_nonpositive_canonical_confidence_is_refused(adapter):
    with pytest.raises(WorldLocalizationError, match="positive confidence"):
        adapter.ingest(camera_frame(confidence=0), connection_epoch=7)


def test_evidence_requires_a_pinned_regular_file_hash_and_scope(tmp_path):
    bundle, accepted, authoring, geometry_directory, report, geometry_sha256 = measured_geometry(
        tmp_path
    )
    manifest = json.loads((bundle / "manifest.yaml").read_text())
    evidence_paths, hashes = evidence(tmp_path, manifest, geometry_directory, authoring)

    evidence_paths["camera_calibration"].write_text("{}")
    with pytest.raises(WorldLocalizationError, match="hash"):
        WorldLocalizationAdapter(
            bundle,
            accepted,
            pins(manifest, hashes, report, geometry_sha256),
            mapping(),
            evidence_paths=evidence_paths,
        )

    calibration = {
        "schema_version": 2,
        "model": "fisheye",
        "status": "offline",
        "evidence_kind": "recorded_live",
        "camera_serial": "wrong-camera",
        "pipeline": {"resolution_px": [1280, 720]},
        "image_size_px": [1280, 720],
        "camera_matrix": [[600.0, 0.0, 640.0], [0.0, 600.0, 360.0], [0.0, 0.0, 1.0]],
        "distortion_coefficients": [0.0, 0.0, 0.0, 0.0],
        "rms_reprojection_error_px": 0.2,
        "accepted_image_count": 20,
        "image_sha256": {str(index): f"{index:064x}" for index in range(20)},
        "quality": {
            "accepted_image_count": 20,
            "minimum_accepted_image_count": 20,
            "rms_reprojection_error_px": 0.2,
            "maximum_rms_reprojection_error_px": 0.5,
            "minimum_pose_constraint_ratio": 0.005,
            "pose_constraint_ratio": 0.01,
            "opencv_check_cond": True,
        },
    }
    encoded = json.dumps(calibration, sort_keys=True, separators=(",", ":")).encode()
    evidence_paths["camera_calibration"].write_bytes(encoded)
    changed_hashes = dict(hashes, camera_calibration=sha256(encoded).hexdigest())
    with pytest.raises(WorldLocalizationError, match="pinned detector"):
        WorldLocalizationAdapter(
            bundle,
            accepted,
            pins(manifest, changed_hashes, report, geometry_sha256),
            mapping(),
            evidence_paths=evidence_paths,
        )

    target = evidence_paths["camera_calibration"]
    symlink = tmp_path / "calibration-link.json"
    symlink.symlink_to(target)
    symlink_paths = dict(evidence_paths, camera_calibration=symlink)
    with pytest.raises(WorldLocalizationError, match="opened safely"):
        WorldLocalizationAdapter(
            bundle,
            accepted,
            pins(manifest, changed_hashes, report, geometry_sha256),
            mapping(),
            evidence_paths=symlink_paths,
        )


def test_control_snapshot_is_projected_back_to_the_pinned_world_frame(adapter):
    adapter.ingest(camera_frame(), connection_epoch=7)
    snapshot = ControlLocalizationSnapshot(
        drone_id=1,
        connection_epoch=7,
        map_id=adapter.pins.map_id,
        geometry_id=adapter.pins.geometry_id,
        capture_clock_id=adapter.pins.capture_clock_mapping_id,
        evaluated_at_s=1.0,
        position_map_enu_m=(-20.0, 10.0, -32.0),
        velocity_map_enu_mps=None,
        covariance_map_enu_m2=((0.02, 0, 0), (0, 0.01, 0), (0, 0, 0.03)),
        fix_age_s=0.0,
        velocity_age_s=None,
        height_age_s=None,
        confidence="green",
        loss_age_s=None,
        status="ready",
        control_eligible=True,
        reason="ready",
        last_rejection=None,
        active_contradictions=(),
        source_ids=(adapter.pins.tag_source_id,),
        camera_calibration_id=adapter.pins.camera_calibration_id,
        body_extrinsics_id=adapter.pins.body_extrinsics_id,
        retained_event_count=1,
    )

    projected = adapter.project_world(snapshot)

    assert projected is not None
    assert projected.position_world_m == pytest.approx((0.0, 0.0, -2.0))
    np.testing.assert_allclose(
        projected.covariance_world_m2, ((0.01, 0, 0), (0, 0.02, 0), (0, 0, 0.03))
    )


def test_unproven_pose_cannot_claim_capture_time_gimbal_and_attitude(adapter, tmp_path):
    bundle, accepted, authoring, geometry_directory, report, geometry_sha256 = measured_geometry(
        tmp_path / "unproven"
    )
    manifest = json.loads((bundle / "manifest.yaml").read_text())
    evidence_paths, hashes = evidence(
        tmp_path / "unproven", manifest, geometry_directory, authoring
    )
    unproven = WorldLocalizationAdapter(
        bundle,
        accepted,
        pins(manifest, hashes, report, geometry_sha256),
        mapping(),
        evidence_paths=evidence_paths,
    )
    raw = body_pose().to_mapping()
    payload = dict(raw["payload"])
    payload.pop("capture_alignment")
    with pytest.raises(WorldLocalizationError, match="measured capture alignment"):
        unproven.ingest(
            event("body-unproven", "dji-body-camera", "body", payload), connection_epoch=7
        )


def test_live_canonical_events_flow_through_the_real_fuser_and_signed_frame(adapter, tmp_path):
    class Transport:
        def __init__(self):
            self.binding = LiveBinding("live-session", 1, 7, 1, "ready")
            self.events = [
                item.to_mapping()
                for item in (camera_frame(), body_pose(), tag(), telemetry(1_000_000, -32.0))
            ]
            self.frames = []

        def authenticate(self, drone_id, token, session):
            assert (drone_id, session) == (1, "live-session")
            return self.binding

        def current_binding(self, drone_id):
            assert drone_id == 1
            return self.binding

        def take_observations(self, drone_id):
            assert drone_id == 1
            result = tuple(self.events)
            self.events.clear()
            return result

        def send(self, drone_id, frame):
            assert drone_id == 1
            self.frames.append(dict(frame))

        def close(self):
            pass

    class Audit:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(dict(event))

    fuser = {
        "drone_id": 1,
        "connection_epoch": 0,
        "map_id": adapter.pins.map_id,
        "geometry_id": adapter.pins.geometry_id,
        "clock_id": adapter.pins.capture_clock_mapping_id,
        "tag_source_id": adapter.pins.tag_source_id,
        "velocity_source_id": adapter.pins.telemetry_source_id,
        "height_source_id": adapter.pins.telemetry_source_id,
        "camera_calibration_id": adapter.pins.camera_calibration_id,
        "body_extrinsics_id": adapter.pins.body_extrinsics_id,
        "position_bounds_map_enu_m": [[-100, 100], [-100, 100], [-100, 100]],
        "height_bounds_map_enu_m": [-100, 100],
        "max_speed_mps": 5,
        "position_variance_bounds_m2": [0.001, 1],
        "velocity_variance_bounds_m2ps2": [0.001, 1],
        "height_variance_bounds_m2": [0.001, 1],
        "production_evidence_verified": True,
    }
    config = ControlPublisherConfig.from_mapping(
        {
            "mode": "live",
            "session": "live-session",
            "websocket_url": "ws://relay.example/ws",
            "audit_dir": str(tmp_path / "audit"),
            "queue_limit": 8,
            "drones": [
                {
                    "key_environment": "LOCALIZATION_KEY_1",
                    "clock_mapping": {
                        "capture_clock_id": "phone_snapshot_wall_ms",
                        "relay_clock_id": "relay-unix",
                        "capture_reference_s": 0.001,
                        "relay_reference_ms": 1,
                        "milliseconds_per_capture_second": 1_000,
                        "max_error_ms": 2,
                        "measured": True,
                    },
                    "live_capture_clock": {
                        "source": "process_monotonic",
                        "boot_id": "test-boot",
                        "monotonic_reference_s": 10,
                        "capture_reference_s": 0.001,
                    },
                    "fuser": fuser,
                }
            ],
        }
    )
    transport = Transport()
    audit = Audit()
    publisher = ControlPublisher(
        config,
        transport,
        audit=audit,
        boot_identity=lambda: "test-boot",
        run_id="world-localization-test",
    )
    publisher.bind_credentials({"LOCALIZATION_KEY_1": "x" * 32})

    result = WorldLocalizationRuntime(publisher, {1: adapter}).tick(1, 10.001)
    frame = ControlLocalizationFrame.parse(result)

    assert frame.wire.status == "ready"
    assert frame.wire.control_eligible
    assert frame.signature_valid(b"x" * 32)
    assert len(transport.frames) == 1

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from perception.control_localization import ControlLocalizationConfig
from perception.control_publisher import (
    ControlPublisher,
    ControlPublisherConfig,
    PublisherDroneConfig,
    PublisherError,
)
from perception.webcam_control_adapter import (
    AdapterError,
    CaptureAlignmentDocument,
    GimbalAttitude,
    WebcamControlAdapterConfig,
    convert_pose_record,
    load_config,
)
from relay.control_localization import ClockMapping


class RecordingAudit:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(dict(event))


def alignment_document(**overrides):
    document = {
        "v": 1,
        "alignment_id": "bench-mount-1",
        "gimbal_attitude_convention": "intrinsic_zyx_degrees",
        "body_to_gimbal": {
            "parent_frame": "body",
            "child_frame": "gimbal",
            "x_m": 0.05,
            "y_m": 0.0,
            "z_m": -0.03,
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
            "qx": 0.5,
            "qy": -0.5,
            "qz": 0.5,
            "qw": -0.5,
        },
    }
    document.update(overrides)
    return document


def adapter_document(**overrides):
    document = {
        "v": 1,
        "drone_id": 1,
        "map_id": "map-sha",
        "geometry_id": "geometry-sha",
        "clock_id": "camera-clock-1",
        "source_id": "tag-camera",
        "camera_calibration_id": "camera-sha",
        "body_extrinsics_id": "body-sha",
        "source_verified": True,
        "timing_verified": True,
        "map_to_map_enu": {"measured": True, "matrix": np.eye(4).tolist()},
        "position_covariance_map_enu_m2": [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 0.01]],
        "gimbal_locked": True,
        "gimbal_attitude_deg": {"yaw_deg": 0.0, "pitch_deg": -90.0, "roll_deg": 0.0},
        "capture_alignment": alignment_document(),
    }
    document.update(overrides)
    return document


def pose_record(*, accepted=True, reason="pose", capture_time=10.0, position=(1.0, 2.0, 1.5)):
    body = np.eye(4)
    body[:3, 3] = position
    return {
        "pose_observation": {
            "accepted": accepted,
            "reason": reason,
            "capture_time": capture_time,
            "T_map_body": body.tolist(),
        }
    }


def velocity_record(*, capture_time=10.04):
    return {
        "kind": "velocity",
        "drone_id": 1,
        "event_id": "velocity-1",
        "connection_epoch": 7,
        "map_id": "map-sha",
        "geometry_id": "geometry-sha",
        "clock_id": "camera-clock-1",
        "capture_time": capture_time,
        "velocity_map_enu_mps": [0.0, 0.0, 0.0],
        "covariance_m2ps2": [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 0.01]],
        "source_id": "msdk-velocity",
        "source_verified": True,
        "timing_verified": True,
    }


def height_record(*, capture_time=10.04):
    return {
        "kind": "height",
        "drone_id": 1,
        "event_id": "height-1",
        "connection_epoch": 7,
        "map_id": "map-sha",
        "geometry_id": "geometry-sha",
        "clock_id": "camera-clock-1",
        "capture_time": capture_time,
        "height_map_enu_m": 1.5,
        "variance_m2": 0.01,
        "source_id": "tof-height",
        "source_verified": True,
        "timing_verified": True,
    }


def replay_publisher(tmp_path, *, audit=None):
    fuser = ControlLocalizationConfig(
        drone_id=1,
        connection_epoch=7,
        map_id="map-sha",
        geometry_id="geometry-sha",
        clock_id="camera-clock-1",
        tag_source_id="tag-camera",
        velocity_source_id="msdk-velocity",
        height_source_id="tof-height",
        camera_calibration_id="camera-sha",
        body_extrinsics_id="body-sha",
        position_bounds_map_enu_m=((-10, 10), (-10, 10), (0, 3)),
        height_bounds_map_enu_m=(0, 3),
        max_speed_mps=0.5,
        position_variance_bounds_m2=(0.000001, 0.0625),
        velocity_variance_bounds_m2ps2=(0.000001, 1),
        height_variance_bounds_m2=(0.000001, 0.0625),
        production_evidence_verified=True,
    )
    clock_mapping = ClockMapping(
        capture_clock_id="camera-clock-1",
        relay_clock_id="relay-unix",
        capture_reference_s=0,
        relay_reference_ms=100_000,
        milliseconds_per_capture_second=1_000,
        max_error_ms=5,
        measured=True,
    )
    drone = PublisherDroneConfig(
        fuser=fuser,
        clock_mapping=clock_mapping,
        key_environment="LOCALIZATION_KEY_1",
        live_capture_clock=None,
    )
    config = ControlPublisherConfig(
        mode="replay",
        session="session-1",
        websocket_url=None,
        audit_dir=tmp_path / "audit",
        drones={1: drone},
        queue_limit=8,
    )
    publisher = ControlPublisher(config, transport=None, audit=audit or RecordingAudit(), run_id="adapter-test")
    publisher.bind_credentials({"LOCALIZATION_KEY_1": "x" * 32})
    return publisher


# -- capture alignment ------------------------------------------------------


def test_capture_alignment_rejects_unsupported_convention():
    with pytest.raises(AdapterError):
        CaptureAlignmentDocument.from_document(
            alignment_document(gimbal_attitude_convention="euler_xyz_degrees")
        )


def test_capture_alignment_rejects_non_unit_quaternion():
    bad = alignment_document()
    bad["gimbal_to_camera"]["qw"] = 0.1
    with pytest.raises(AdapterError):
        CaptureAlignmentDocument.from_document(bad)


def test_body_to_camera_zero_attitude_composes_static_mounts():
    document = CaptureAlignmentDocument.from_document(alignment_document())
    attitude = GimbalAttitude(0.0, 0.0, 0.0)
    matrix = document.body_to_camera(attitude)
    expected = document.body_to_gimbal @ document.gimbal_to_camera
    np.testing.assert_allclose(matrix, expected, atol=1e-9)


def test_body_to_camera_matches_independently_recomputed_composition():
    document = CaptureAlignmentDocument.from_document(alignment_document())
    attitude = GimbalAttitude(30.0, -45.0, 5.0)
    matrix = document.body_to_camera(attitude)

    yaw, pitch, roll = np.deg2rad([attitude.yaw_deg, attitude.pitch_deg, attitude.roll_deg])
    rz = np.array([[np.cos(yaw), -np.sin(yaw), 0], [np.sin(yaw), np.cos(yaw), 0], [0, 0, 1]])
    ry = np.array([[np.cos(pitch), 0, np.sin(pitch)], [0, 1, 0], [-np.sin(pitch), 0, np.cos(pitch)]])
    rx = np.array([[1, 0, 0], [0, np.cos(roll), -np.sin(roll)], [0, np.sin(roll), np.cos(roll)]])
    dynamic = np.eye(4)
    dynamic[:3, :3] = rz @ ry @ rx
    expected = document.body_to_gimbal @ dynamic @ document.gimbal_to_camera
    np.testing.assert_allclose(matrix, expected, atol=1e-9)


def test_gimbal_attitude_rejects_out_of_range_angle():
    with pytest.raises(AdapterError):
        GimbalAttitude(400.0, 0.0, 0.0)


# -- adapter config -----------------------------------------------------------


def test_config_rejects_unmeasured_map_to_map_enu():
    document = adapter_document()
    document["map_to_map_enu"] = {"measured": False, "matrix": np.eye(4).tolist()}
    with pytest.raises(AdapterError):
        WebcamControlAdapterConfig.from_document(document)


def test_config_rejects_unlocked_gimbal():
    document = adapter_document(gimbal_locked=False)
    with pytest.raises(AdapterError):
        WebcamControlAdapterConfig.from_document(document)


def test_config_rejects_non_boolean_verified_flags():
    document = adapter_document(source_verified="true")
    with pytest.raises(AdapterError):
        WebcamControlAdapterConfig.from_document(document)


def test_config_accepts_well_formed_document():
    config = WebcamControlAdapterConfig.from_document(adapter_document())
    assert config.drone_id == 1
    assert config.source_verified is True


# -- convert_pose_record -------------------------------------------------------


def test_convert_pose_record_returns_none_without_accepted_pose():
    config = WebcamControlAdapterConfig.from_document(adapter_document())
    for record in (
        pose_record(accepted=False),
        pose_record(reason="no_tags"),
        {"pose_observation": None},
        {},
    ):
        assert convert_pose_record(record, config=config, event_id="e", connection_epoch=7) is None


def test_convert_pose_record_builds_expected_sensor_record():
    config = WebcamControlAdapterConfig.from_document(adapter_document())
    record = convert_pose_record(
        pose_record(capture_time=10.0, position=(1.0, 2.0, 1.5)),
        config=config,
        event_id="fix-1",
        connection_epoch=7,
    )
    assert record["kind"] == "tag"
    assert record["drone_id"] == 1
    assert record["connection_epoch"] == 7
    assert record["capture_time"] == pytest.approx(10.0)
    np.testing.assert_allclose(record["position_map_enu_m"], [1.0, 2.0, 1.5])
    assert record["extrinsics"]["extrinsics_id"] == "body-sha"
    assert record["extrinsics"]["source_id"] == "tag-camera"
    assert record["extrinsics"]["capture_time"] == pytest.approx(10.0)
    assert record["extrinsics"]["gimbal_time"] == pytest.approx(10.0)
    assert record["extrinsics"]["attitude_time"] == pytest.approx(10.0)


def test_convert_pose_record_applies_map_enu_alignment_rotation():
    # A 90 degree yaw from map into map_enu should rotate x into y.
    rotation = np.array([[0, -1, 0, 0], [1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float)
    document = adapter_document(map_to_map_enu={"measured": True, "matrix": rotation.tolist()})
    config = WebcamControlAdapterConfig.from_document(document)
    record = convert_pose_record(
        pose_record(position=(1.0, 0.0, 0.0)), config=config, event_id="fix-1", connection_epoch=7
    )
    np.testing.assert_allclose(record["position_map_enu_m"], [0.0, 1.0, 0.0], atol=1e-9)


# -- real validation path ------------------------------------------------------


def test_adapter_output_is_accepted_by_real_control_publisher_and_reaches_ready(tmp_path):
    config = WebcamControlAdapterConfig.from_document(adapter_document())
    tag = convert_pose_record(
        pose_record(capture_time=10.0, position=(1.0, 2.0, 1.5)),
        config=config,
        event_id="fix-1",
        connection_epoch=7,
    )
    publisher = replay_publisher(tmp_path)
    publisher.enqueue(tag)
    publisher.enqueue(velocity_record())
    publisher.enqueue(height_record())
    frame = publisher.publish(1, 10.05)
    assert frame["localization_status"] == "ready"
    assert frame["control_eligible"] is True
    assert frame["flight_approved"] is False
    np.testing.assert_allclose(frame["position_map_enu_m"], [1.0, 2.0, 1.5], atol=1e-6)


def test_real_control_publisher_refuses_unverified_source(tmp_path):
    document = adapter_document(source_verified=False, timing_verified=False)
    config = WebcamControlAdapterConfig.from_document(document)
    tag = convert_pose_record(
        pose_record(capture_time=10.0), config=config, event_id="fix-1", connection_epoch=7
    )
    assert tag["source_verified"] is False
    publisher = replay_publisher(tmp_path)
    with pytest.raises(PublisherError):
        publisher.enqueue(tag)


def test_real_control_publisher_refuses_mismatched_geometry_id(tmp_path):
    document = adapter_document(geometry_id="wrong-geometry")
    config = WebcamControlAdapterConfig.from_document(document)
    tag = convert_pose_record(
        pose_record(capture_time=10.0), config=config, event_id="fix-1", connection_epoch=7
    )
    publisher = replay_publisher(tmp_path)
    publisher.enqueue(tag)
    publisher.enqueue(velocity_record())
    publisher.enqueue(height_record())
    frame = publisher.publish(1, 10.05)
    assert frame["localization_status"] != "ready"
    assert frame["control_eligible"] is False


# -- config / CLI I/O -----------------------------------------------------------


def test_load_config_reads_document_from_disk(tmp_path):
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps(adapter_document()))
    config = load_config(path)
    assert config.drone_id == 1


def test_main_cli_converts_jsonl_stream(tmp_path):
    config_path = tmp_path / "adapter.json"
    config_path.write_text(json.dumps(adapter_document()))
    input_path = tmp_path / "pose.jsonl"
    input_path.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                pose_record(capture_time=10.0, position=(1.0, 2.0, 1.5)),
                pose_record(accepted=False, reason="no_tags"),
                pose_record(capture_time=10.1, position=(1.1, 2.0, 1.5)),
            )
        )
        + "\n"
    )
    output_path = tmp_path / "sensor.jsonl"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "perception.webcam_control_adapter",
            "--config",
            str(config_path),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--connection-epoch",
            "7",
            "--run-id",
            "cli-test",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    lines = output_path.read_text().strip().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["event_id"] == "cli-test-1"
    assert second["event_id"] == "cli-test-3"
    np.testing.assert_allclose(first["position_map_enu_m"], [1.0, 2.0, 1.5])
    np.testing.assert_allclose(second["position_map_enu_m"], [1.1, 2.0, 1.5])

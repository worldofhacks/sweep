from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import cv2
import numpy as np
import pytest

from perception.test_sensor_records import raw_common, recording_config, samples
from perception.verified_localization import VerifiedLocalizationIngestion, run_replay
from tests.test_tag_localization import scene


def config(tmp_path):
    _, image, camera, body_camera, localizer = scene(tmp_path)
    sensor = recording_config(tmp_path)
    publisher = sensor["publisher"]
    calibration_path = tmp_path / "calibration.yaml"
    calibration = json.loads(calibration_path.read_text())
    calibration["evidence_kind"] = "recorded_live"
    calibration_path.write_text(json.dumps(calibration))
    localizer["calibration_sha256"] = hashlib.sha256(calibration_path.read_bytes()).hexdigest()
    pipeline_sha256 = hashlib.sha256(
        json.dumps(localizer["pipeline"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    body_pose = camera @ np.linalg.inv(body_camera)
    raw = {
        "publisher": publisher,
        "sensor": sensor,
        "localizer": localizer,
        "identity": {
            **{
                name: raw_common("phone_velocity_raw")[name]
                for name in (
                    "recording_run_id",
                    "session",
                    "product_id",
                    "drone_id",
                    "connection_generation",
                    "connection_epoch",
                    "product_type",
                    "aircraft_firmware",
                    "rc_firmware",
                    "sdk_version",
                    "recorder_config_sha256",
                )
            },
            "pipeline_sha256": pipeline_sha256,
            "camera_configuration_id": "camera-config-1",
        },
        "timing": {
            name: {
                "measurement": {
                    "measurement_id": f"{name}-timing",
                    "measured": True,
                    "artifact_sha256": "b" * 64,
                },
                "capture_clock_id": "android_elapsed_realtime",
                "boot_id": "phone-boot-1",
                "receipt_to_capture_s": 0,
                "max_error_s": 0.01,
            }
            for name in ("frame", "attitude", "telemetry")
        },
        "camera": {
            "source_id": "tag-camera",
            "camera_serial": "test",
            "camera_calibration_id": "camera-calibration",
            "calibration_sha256": localizer["calibration_sha256"],
            "pipeline_sha256": pipeline_sha256,
            "body_extrinsics_id": "body-camera-measurement",
            "body_gimbal_mount": measured("body-gimbal", body_camera.tolist()),
            "gimbal_camera": measured("gimbal-camera", np.eye(4).tolist()),
            "map_ned_rotation": measured("map-ned", body_pose[:3, :3].tolist()),
            "max_attitude_age_s": 0.05,
            "max_attitude_skew_s": 0.05,
            "max_body_orientation_error_deg": 1,
            "position_covariance_map_enu_m2": {
                "measurement": {
                    "measurement_id": "tag-noise",
                    "measured": True,
                    "artifact_sha256": "c" * 64,
                },
                "matrix": (np.eye(3) * 0.01).tolist(),
            },
        },
    }
    frame = tmp_path / "recorded-720p.png"
    assert cv2.imwrite(str(frame), image)
    return raw, frame, body_pose


def measured(measurement_id, matrix):
    return {
        "measurement": {
            "measurement_id": measurement_id,
            "measured": True,
            "artifact_sha256": "e" * 64,
        },
        "matrix": matrix,
    }


def attitude(kind: str, receipt_ms: int = 1000):
    return raw_common("phone_attitude_raw") | {
        "event_id": f"{kind}-{receipt_ms}",
        "sdk_key": kind,
        "attitude_frame": "aircraft_body_to_ned"
        if kind == "KeyAircraftAttitude"
        else "raw_sdk_axes",
        "yaw_deg": 0,
        "pitch_deg": 0,
        "roll_deg": 0,
        "received_at_android_elapsed_realtime_ms": receipt_ms,
        "written_at_android_elapsed_realtime_ms": receipt_ms,
    }


def frame_record(raw, path, receipt_ms: int = 1000):
    identity = raw["identity"]
    return {
        **{
            name: identity[name]
            for name in (
                "recording_run_id",
                "session",
                "product_id",
                "drone_id",
                "connection_generation",
                "connection_epoch",
                "product_type",
                "aircraft_firmware",
                "rc_firmware",
                "sdk_version",
                "recorder_config_sha256",
            )
        },
        "kind": "decoded_frame",
        "event_id": "frame-1",
        "received_at_android_elapsed_realtime_ms": receipt_ms,
        "decoded_at_android_elapsed_realtime_ms": receipt_ms,
        "frame_path": str(path),
        "frame_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "camera_serial": "test",
        "camera_configuration_id": "camera-config-1",
        "pipeline_sha256": identity["pipeline_sha256"],
    }


def sensor_samples(receipt_ms: int = 1000):
    result = []
    for sample in samples():
        adjusted = sample | {
            "received_at_android_elapsed_realtime_ms": receipt_ms,
            "written_at_android_elapsed_realtime_ms": receipt_ms,
        }
        if adjusted["kind"] == "phone_velocity_raw":
            adjusted |= {"north_mps": 0.1, "east_mps": 0, "down_mps": 0}
        result.append(adjusted)
    return result


def test_720p_tag_pixels_flow_through_verified_adapter_and_signed_publisher(tmp_path, monkeypatch):
    raw, frame, expected_body = config(tmp_path)
    monkeypatch.setenv("LOCALIZATION_KEY_1", "x" * 32)
    ingestion = VerifiedLocalizationIngestion(raw)
    emitted = []
    for sample in [
        attitude("KeyAircraftAttitude"),
        attitude("KeyGimbalAttitude"),
        *sensor_samples(),
        frame_record(raw, frame),
    ]:
        emitted.extend(ingestion.records(sample))
    assert [record["kind"] for record in emitted] == ["velocity", "height", "tag"]
    assert all(record["source_verified"] and record["timing_verified"] for record in emitted)
    assert emitted[-1]["position_map_enu_m"] == pytest.approx(expected_body[:3, 3], abs=0.03)

    replay = "\n".join(
        json.dumps({"now_s": 1.01, "raw": sample})
        for sample in [
            attitude("KeyAircraftAttitude"),
            attitude("KeyGimbalAttitude"),
            *sensor_samples(),
            frame_record(raw, frame),
        ]
    )
    output = []

    class Sink:
        def write(self, value):
            output.append(value)

    run_replay(VerifiedLocalizationIngestion(raw), replay.splitlines(), Sink())
    signed = [json.loads(value) for value in output]
    assert len(signed) == 3
    assert signed[-1]["type"] == "control_localization"
    assert signed[-1]["localization_status"] == "ready"
    assert signed[-1]["flight_approved"] is False


def test_missing_or_asynchronous_attitude_and_wrong_pins_fail_closed(tmp_path):
    raw, frame, _ = config(tmp_path)
    ingestion = VerifiedLocalizationIngestion(raw)
    with pytest.raises(ValueError, match="missing"):
        ingestion.records(frame_record(raw, frame))
    raw["camera"]["max_attitude_skew_s"] = 0.01
    ingestion = VerifiedLocalizationIngestion(raw)
    ingestion.records(attitude("KeyAircraftAttitude", 980))
    ingestion.records(attitude("KeyGimbalAttitude", 1000))
    with pytest.raises(ValueError, match="asynchronous"):
        ingestion.records(frame_record(raw, frame))
    changed = deepcopy(raw)
    changed["identity"]["pipeline_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="camera evidence"):
        VerifiedLocalizationIngestion(changed)
    calibration_path = tmp_path / "calibration.yaml"
    calibration = json.loads(calibration_path.read_text())
    calibration["evidence_kind"] = "synthetic"
    calibration_path.write_text(json.dumps(calibration))
    changed = deepcopy(raw)
    changed["localizer"]["calibration_sha256"] = hashlib.sha256(
        calibration_path.read_bytes()
    ).hexdigest()
    changed["camera"]["calibration_sha256"] = changed["localizer"]["calibration_sha256"]
    with pytest.raises(ValueError, match="synthetic"):
        VerifiedLocalizationIngestion(changed)
    calibration["evidence_kind"] = "recorded_live"
    calibration_path.write_text(json.dumps(calibration))
    ingestion = VerifiedLocalizationIngestion(raw)
    ingestion.records(attitude("KeyAircraftAttitude", 800))
    ingestion.records(attitude("KeyGimbalAttitude", 800))
    with pytest.raises(ValueError, match="stale"):
        ingestion.records(frame_record(raw, frame))

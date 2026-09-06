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
    map_id = json.loads((tmp_path / "bundle" / "manifest.yaml").read_text())["content_sha256"]
    publisher["drones"][0]["fuser"]["map_id"] = map_id
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
        "evidence_scope": "test_double",
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
            "raw_run": {"measurement_id": "raw-run", "measured": True},
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
        }
        | {"max_telemetry_timing_error_s": 0.02},
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
    bind_evidence(raw, tmp_path)
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


def bind_measurement(tmp_path, measurement, value):
    artifact = tmp_path / f"{measurement['measurement_id']}.json"
    payload = json.dumps(
        {"measurement_id": measurement["measurement_id"], "value": value},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    artifact.write_bytes(payload)
    measurement["artifact_path"] = str(artifact)
    measurement["artifact_sha256"] = hashlib.sha256(payload).hexdigest()


def bind_evidence(raw, tmp_path):
    for name in ("frame", "attitude", "telemetry"):
        timing = raw["timing"][name]
        bind_measurement(
            tmp_path,
            timing["measurement"],
            {
                "capture_clock_id": timing["capture_clock_id"],
                "boot_id": timing["boot_id"],
                "receipt_to_capture_s": timing["receipt_to_capture_s"],
                "max_error_s": timing["max_error_s"],
            },
        )
    bind_measurement(
        tmp_path,
        raw["identity"]["raw_run"],
        {
            "identity": {
                name: raw["identity"][name]
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
            "android_boot_id": raw["timing"]["frame"]["boot_id"],
        },
    )
    camera = raw["camera"]
    for name in ("body_gimbal_mount", "gimbal_camera", "map_ned_rotation"):
        bind_measurement(tmp_path, camera[name]["measurement"], camera[name]["matrix"])
    bind_measurement(
        tmp_path,
        camera["position_covariance_map_enu_m2"]["measurement"],
        camera["position_covariance_map_enu_m2"]["matrix"],
    )


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


def test_raw_gimbal_axes_never_create_a_signed_ready_pose(tmp_path, monkeypatch):
    raw, frame, _ = config(tmp_path)
    monkeypatch.setenv("LOCALIZATION_KEY_1", "x" * 32)
    ingestion = VerifiedLocalizationIngestion(raw)
    with pytest.raises(ValueError, match="body-relative attitude adapter"):
        ingestion.records(attitude("KeyGimbalAttitude"))

    replay = "\n".join(
        json.dumps({"now_s": 1.01, "raw": sample})
        for sample in [
            *sensor_samples(),
        ]
    )
    output = []

    class Sink:
        def write(self, value):
            output.append(value)

    run_replay(VerifiedLocalizationIngestion(raw), replay.splitlines(), Sink())
    signed = [json.loads(value) for value in output]
    assert len(signed) == 2
    assert all(frame["type"] == "control_localization" for frame in signed)
    assert all(frame["localization_status"] != "ready" for frame in signed)


def test_missing_or_asynchronous_attitude_and_wrong_pins_fail_closed(tmp_path):
    raw, frame, _ = config(tmp_path)
    ingestion = VerifiedLocalizationIngestion(raw)
    with pytest.raises(ValueError, match="missing"):
        ingestion.records(frame_record(raw, frame))
    raw["camera"]["max_attitude_skew_s"] = 0.01
    ingestion = VerifiedLocalizationIngestion(raw)
    ingestion.records(attitude("KeyAircraftAttitude", 980))
    with pytest.raises(ValueError, match="body-relative attitude adapter"):
        ingestion.records(attitude("KeyGimbalAttitude", 1000))
    changed = deepcopy(raw)
    changed["identity"]["pipeline_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="camera evidence"):
        VerifiedLocalizationIngestion(changed)
    changed = deepcopy(raw)
    changed["publisher"]["drones"][0]["fuser"]["map_id"] = "other-map"
    with pytest.raises(ValueError, match="map identity"):
        VerifiedLocalizationIngestion(changed)
    calibration_path = tmp_path / "calibration.yaml"
    calibration = json.loads(calibration_path.read_text())
    calibration["evidence_kind"] = "synthetic"
    calibration_path.write_text(json.dumps(calibration))
    changed = deepcopy(raw)
    changed["evidence_scope"] = "hardware"
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
    with pytest.raises(ValueError, match="stale"):
        ingestion.records(frame_record(raw, frame))


def test_artifacts_bind_values_and_telemetry_timing_bound(tmp_path):
    raw, _, _ = config(tmp_path / "altered")
    raw["timing"]["telemetry"]["max_error_s"] = 0.03
    bind_evidence(raw, tmp_path)
    with pytest.raises(ValueError, match="telemetry timing uncertainty"):
        VerifiedLocalizationIngestion(raw)

    raw, _, _ = config(tmp_path)
    raw["camera"]["body_gimbal_mount"]["matrix"][0][3] = 2
    with pytest.raises(ValueError, match="does not bind"):
        VerifiedLocalizationIngestion(raw)

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
            "max_body_orientation_error_deg": 1,
            "capture_aligned_attitude": {
                "measurement": {
                    "measurement_id": "capture-aligned-attitude",
                    "measured": True,
                    "artifact_sha256": "d" * 64,
                },
                "body_convention_id": "aircraft-body-to-ned-rpy-zyx",
                "gimbal_convention_id": "mount-to-gimbal-calibrated-v1",
                "max_bracket_s": 0.02,
                "max_residual_s": 0.005,
                "max_uncertainty_deg": 0.5,
            },
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
    frame = tmp_path / "rendered-720p.png"
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
    aligned = camera["capture_aligned_attitude"]
    bind_measurement(
        tmp_path,
        aligned["measurement"],
        {
            "body_convention_id": aligned["body_convention_id"],
            "gimbal_convention_id": aligned["gimbal_convention_id"],
            "max_bracket_s": aligned["max_bracket_s"],
            "max_residual_s": aligned["max_residual_s"],
            "max_uncertainty_deg": aligned["max_uncertainty_deg"],
        },
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


def capture_aligned_attitude(raw, source: str, capture_time_s: float = 1.0):
    identity = raw["identity"]
    aligned = raw["camera"]["capture_aligned_attitude"]
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
        "kind": "capture_aligned_attitude",
        "event_id": f"{source}-capture-{capture_time_s}",
        "android_boot_id": raw["timing"]["frame"]["boot_id"],
        "source": source,
        "convention_id": (
            aligned["body_convention_id"]
            if source == "aircraft_body_to_ned"
            else aligned["gimbal_convention_id"]
        ),
        "capture_time_s": capture_time_s,
        "before_capture_time_s": capture_time_s - 0.005,
        "after_capture_time_s": capture_time_s + 0.005,
        "interpolation_residual_s": 0.001,
        "interpolation_uncertainty_deg": 0.1,
        "rotation": np.eye(3).tolist(),
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


def test_capture_aligned_attitudes_flow_through_tag_fuser_and_signed_publisher(
    tmp_path, monkeypatch
):
    raw, frame, _ = config(tmp_path)
    monkeypatch.setenv("LOCALIZATION_KEY_1", "x" * 32)
    ingestion = VerifiedLocalizationIngestion(raw)
    assert ingestion.records(attitude("KeyGimbalAttitude")) == []
    with pytest.raises(ValueError, match="capture-aligned"):
        ingestion.records(frame_record(raw, frame))

    emitted = []
    for sample in [
        capture_aligned_attitude(raw, "aircraft_body_to_ned"),
        capture_aligned_attitude(raw, "gimbal_mount_to_gimbal"),
        frame_record(raw, frame),
    ]:
        emitted.extend(ingestion.records(sample))
    assert [record["kind"] for record in emitted] == ["tag"]

    replay = "\n".join(
        json.dumps({"now_s": 1.01, "raw": sample})
        for sample in [
            capture_aligned_attitude(raw, "aircraft_body_to_ned"),
            capture_aligned_attitude(raw, "gimbal_mount_to_gimbal"),
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
    assert all(frame["type"] == "control_localization" for frame in signed)
    assert signed[-1]["localization_status"] == "ready"
    assert signed[-1]["flight_approved"] is False


def test_missing_or_asynchronous_attitude_and_wrong_pins_fail_closed(tmp_path):
    raw, frame, _ = config(tmp_path)
    ingestion = VerifiedLocalizationIngestion(raw)
    with pytest.raises(ValueError, match="capture-aligned"):
        ingestion.records(frame_record(raw, frame))
    ingestion = VerifiedLocalizationIngestion(raw)
    aligned = capture_aligned_attitude(raw, "gimbal_mount_to_gimbal")
    aligned["after_capture_time_s"] = 1.1
    with pytest.raises(ValueError, match="interpolation bounds"):
        ingestion.records(aligned)
    changed = deepcopy(raw)
    changed["identity"]["pipeline_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="camera evidence"):
        VerifiedLocalizationIngestion(changed)
    changed = deepcopy(raw)
    changed["publisher"]["drones"][0]["fuser"]["map_id"] = "other-map"
    with pytest.raises(ValueError, match="map identity"):
        VerifiedLocalizationIngestion(changed)
    changed = deepcopy(raw)
    changed["evidence_scope"] = "hardware"
    with pytest.raises(ValueError, match="synthetic"):
        VerifiedLocalizationIngestion(changed)
    ingestion = VerifiedLocalizationIngestion(raw)
    with pytest.raises(ValueError, match="capture-aligned"):
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


def test_nonidentity_gimbal_mount_composes_once_and_angular_error_does_not_wrap(tmp_path):
    raw, frame, expected_body_pose = config(tmp_path)
    rotation = cv2.Rodrigues(np.array([0.0, 0.0, 0.4]))[0]
    joint = np.eye(4)
    joint[:3, :3] = rotation
    body_camera = np.asarray(raw["camera"]["body_gimbal_mount"]["matrix"])
    mount = body_camera @ np.linalg.inv(joint)
    raw["camera"]["body_gimbal_mount"]["matrix"] = mount.tolist()
    raw["camera"]["capture_aligned_attitude"]["max_uncertainty_deg"] = 90.0
    bind_evidence(raw, tmp_path)
    ingestion = VerifiedLocalizationIngestion(raw)
    body = capture_aligned_attitude(raw, "aircraft_body_to_ned")
    gimbal = capture_aligned_attitude(raw, "gimbal_mount_to_gimbal")
    body["interpolation_uncertainty_deg"] = 90.0
    gimbal["interpolation_uncertainty_deg"] = 90.0
    gimbal["rotation"] = rotation.tolist()
    ingestion.records(body)
    ingestion.records(gimbal)

    (tag,) = ingestion.records(frame_record(raw, frame))

    np.testing.assert_allclose(tag["position_map_enu_m"], expected_body_pose[:3, 3], atol=0.02)
    covariance = np.asarray(tag["covariance_map_enu_m2"])
    configured = np.asarray(raw["camera"]["position_covariance_map_enu_m2"]["matrix"])
    assert np.all(np.diag(covariance - configured) >= 4 * np.linalg.norm(mount[:3, 3]) ** 2)


def test_uncertainty_above_a_half_turn_is_rejected_even_with_matching_artifact(tmp_path):
    raw, _, _ = config(tmp_path)
    raw["camera"]["capture_aligned_attitude"]["max_uncertainty_deg"] = 181.0
    bind_evidence(raw, tmp_path)
    with pytest.raises(ValueError, match="180 degrees"):
        VerifiedLocalizationIngestion(raw)

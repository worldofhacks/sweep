from __future__ import annotations

import json

import cv2
import pytest

from perception.control_publisher import ControlPublisher, ControlPublisherConfig
from perception.sensor_records import SensorRecordAdapter, main
from tests.test_tag_localization import scene


def recording_config(tmp_path):
    _, image, _, _, localizer = scene(tmp_path)
    image_path = tmp_path / "tag-frame.png"
    assert cv2.imwrite(str(image_path), image)
    map_id = next(iter(localizer["accepted_versions"].values()))
    publisher = {
        "mode": "replay",
        "session": "sensor-recording-test",
        "websocket_url": None,
        "drones": [
            {
                "key_environment": "LOCALIZATION_KEY_1",
                "max_position_uncertainty_m": 0.5,
                "clock_mapping": {
                    "capture_clock_id": "phone-monotonic",
                    "relay_clock_id": "relay-clock",
                    "capture_reference_s": 0,
                    "relay_reference_ms": 100_000,
                    "milliseconds_per_capture_second": 1000,
                    "max_error_ms": 5,
                    "measured": True,
                },
                "fuser": {
                    "drone_id": 1,
                    "connection_epoch": 3,
                    "map_id": map_id,
                    "geometry_id": "geometry-sha",
                    "clock_id": "phone-monotonic",
                    "tag_source_id": "tag-camera",
                    "velocity_source_id": "msdk-velocity",
                    "height_source_id": "tof-height",
                    "camera_calibration_id": localizer["calibration_sha256"],
                    "body_extrinsics_id": "body-camera-measurement",
                    "position_bounds_map_enu_m": [[-10, 10], [-10, 10], [0, 3]],
                    "height_bounds_map_enu_m": [0, 3],
                    "max_speed_mps": 5,
                    "position_variance_bounds_m2": [0.000001, 0.0625],
                    "velocity_variance_bounds_m2ps2": [0.000001, 1],
                    "height_variance_bounds_m2": [0.000001, 0.0625],
                    "production_evidence_verified": True,
                },
            }
        ],
    }
    config = {
        "publisher": publisher,
        "phone": {
            "drone_id": 1,
            "velocity_ned_to_map_rotation": [[0, 1, 0], [1, 0, 0], [0, 0, -1]],
            "height_datum_m": 1.0,
            "velocity": {
                "source_id": "msdk-velocity",
                "sdk_key": "KeyAircraftVelocity",
                "source_verified": True,
                "max_sample_age_s": 0.5,
                "covariance_m2ps2": [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 0.01]],
            },
            "height": {
                "source_id": "tof-height",
                "sdk_key": "KeyAltitude",
                "source_verified": True,
                "max_sample_age_s": 0.5,
                "variance_m2": 0.01,
            },
        },
        "tag": {
            "source_id": "tag-camera",
            "source_verified": True,
            "timing_evidence_verified": True,
            "max_frame_age_s": 0.5,
            "covariance_map_enu_m2": [[0.012, 0, 0], [0, 0.012, 0], [0, 0, 0.012]],
            "body_extrinsics": {
                "extrinsics_id": "body-camera-measurement",
                "require_measured": True,
            },
            "localizer": localizer,
        },
    }
    return config, image_path


def samples(image_path, *, tag_timestamp=True, include_extrinsics=True):
    tag = {
        "kind": "tag_frame",
        "event_id": "tag-1",
        "drone_id": 1,
        "image_path": str(image_path),
        "received_at_s": 10.2,
        "decode_time_s": 10.1,
    }
    if tag_timestamp:
        tag["sdk_capture_time_s"] = 10.0
    else:
        tag["decode_time_s"] = 10.2
    if include_extrinsics:
        tag["body_extrinsics"] = {
            "extrinsics_id": "body-camera-measurement",
            "source_id": "tag-camera",
            "matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "gimbal_time_s": 10.0,
            "attitude_time_s": 10.0,
            "measured": True,
        }
    return [
        {
            "kind": "phone_velocity_raw",
            "event_id": "velocity-1",
            "received_at_monotonic_ms": 10_100,
            "sdk_key": "KeyAircraftVelocity",
            "velocity_ned_mps": [1, 2, -3],
        },
        {
            "kind": "phone_height_raw",
            "event_id": "height-1",
            "received_at_monotonic_ms": 10_100,
            "sdk_key": "KeyAltitude",
            "height_m": 0.62,
        },
        tag,
    ]


def test_real_tag_pixels_and_phone_raw_samples_create_publisher_records(tmp_path):
    config, image_path = recording_config(tmp_path)
    adapter = SensorRecordAdapter(config)

    records = [adapter.record(sample) for sample in samples(image_path)]

    tag = records[-1]
    assert sorted(tag["tag_ids"]) == [0, 1]
    assert tag["timing_verified"] is True
    assert tag["covariance_map_enu_m2"] == config["tag"]["covariance_map_enu_m2"]
    assert tag["covariance_map_enu_m2"][0][0] != 0.1
    assert tag["extrinsics"]["measured"] is True
    assert records[0]["velocity_map_enu_mps"] == [2.0, 1.0, 3.0]
    assert records[1]["height_map_enu_m"] == 1.62
    assert records[0]["timing_provenance"] == "android_callback_receipt"
    assert records[0]["timing_verified"] is False

    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(config["publisher"]))
    for record in records:
        publisher.enqueue(record)
    assert publisher.publish(1, 10.2)["control_eligible"] is False


def test_receipt_timing_remains_unverified_and_cannot_make_fuser_ready(tmp_path):
    config, image_path = recording_config(tmp_path)
    adapter = SensorRecordAdapter(config)
    records = [adapter.record(sample) for sample in samples(image_path, tag_timestamp=False)]

    assert records[-1]["capture_time"] == 10.2
    assert records[-1]["timing_provenance"] == "receipt_timestamp"
    assert records[-1]["timing_verified"] is False
    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(config["publisher"]))
    for record in records:
        publisher.enqueue(record)
    frame = publisher.publish(1, 10.2)
    assert frame["localization_status"] == "hold"
    assert frame["control_eligible"] is False


def test_missing_capture_time_extrinsics_cannot_make_fuser_ready(tmp_path):
    config, image_path = recording_config(tmp_path)
    adapter = SensorRecordAdapter(config)
    records = [adapter.record(sample) for sample in samples(image_path, include_extrinsics=False)]

    assert records[-1]["extrinsics"] is None
    assert records[-1]["timing_verified"] is False
    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(config["publisher"]))
    for record in records:
        publisher.enqueue(record)
    assert publisher.publish(1, 10.2)["control_eligible"] is False


def test_missing_gimbal_and_attitude_times_emit_unverified_tag_record(tmp_path):
    config, image_path = recording_config(tmp_path)
    adapter = SensorRecordAdapter(config)
    tag = samples(image_path)[-1]
    del tag["body_extrinsics"]["gimbal_time_s"]
    del tag["body_extrinsics"]["attitude_time_s"]

    record = adapter.record(tag)
    assert record["extrinsics"] is None
    assert record["timing_verified"] is False
    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(config["publisher"]))
    publisher.enqueue(record)
    assert publisher.publish(1, 10.2)["control_eligible"] is False


def test_synthetic_tag_calibration_is_preserved_as_unverified(tmp_path):
    config, image_path = recording_config(tmp_path)
    tag = SensorRecordAdapter(config).record(samples(image_path)[-1])

    assert tag["calibration_evidence_kind"] == "synthetic"
    assert tag["source_verified"] is False


def test_rtsp_recording_times_out_when_no_frame_arrives(tmp_path, monkeypatch):
    config, _ = recording_config(tmp_path)
    adapter = SensorRecordAdapter(config)

    class EmptyStream:
        def __init__(self, _: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read_timed(self, _: float):
            return None

    ticks = iter([0.0, 0.1, 0.2])
    monkeypatch.setattr("perception.sensor_records.WebcamStream", EmptyStream)
    with pytest.raises(RuntimeError, match="timed out"):
        list(
            adapter.record_rtsp(
                "rtsp://localhost/drone1", frames=1, timeout_s=0.1, clock=lambda: next(ticks)
            )
        )


def test_stale_or_mismatched_raw_samples_are_rejected_before_recording(tmp_path):
    config, image_path = recording_config(tmp_path)
    adapter = SensorRecordAdapter(config)
    stale = samples(image_path)[-1] | {"received_at_s": 11.0}
    with pytest.raises(ValueError, match="stale"):
        adapter.record(stale)
    wrong_drone = samples(image_path)[-1] | {"drone_id": 2}
    with pytest.raises(ValueError, match="not configured"):
        adapter.record(wrong_drone)
    wrong_height_source = samples(image_path)[1] | {"sdk_key": "KeyUltrasonicHeight"}
    with pytest.raises(ValueError, match="SDK key"):
        adapter.record(wrong_height_source)
    mismatched = config.copy()
    mismatched["tag"] = config["tag"].copy()
    mismatched["tag"]["body_extrinsics"] = config["tag"]["body_extrinsics"].copy()
    mismatched["tag"]["body_extrinsics"]["extrinsics_id"] = "other"
    with pytest.raises(ValueError, match="identities"):
        SensorRecordAdapter(mismatched)


def test_cli_converts_jsonl_to_control_publisher_input(tmp_path, monkeypatch, capsys):
    config, image_path = recording_config(tmp_path)
    path = tmp_path / "recording.json"
    path.write_text(json.dumps(config))
    input_path = tmp_path / "phone.jsonl"
    input_path.write_text("\n".join(json.dumps(sample) for sample in samples(image_path)) + "\n")

    monkeypatch.setattr(
        "sys.argv", ["sensor_records", "--config", str(path), "--input", str(input_path)]
    )
    assert main() == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [item["kind"] for item in output] == ["velocity", "height", "tag"]


def test_cli_skips_the_known_unselected_height_key_in_a_mixed_phone_log(
    tmp_path, monkeypatch, capsys
):
    config, image_path = recording_config(tmp_path)
    path = tmp_path / "recording.json"
    path.write_text(json.dumps(config))
    input_path = tmp_path / "phone-mixed.jsonl"
    samples_for_log = samples(image_path)[:2]
    samples_for_log.insert(
        1,
        {
            "kind": "phone_height_raw",
            "event_id": "ultrasonic-1",
            "received_at_monotonic_ms": 10_110,
            "sdk_key": "KeyUltrasonicHeight",
            "height_m": 0.61,
        },
    )
    input_path.write_text("\n".join(json.dumps(sample) for sample in samples_for_log) + "\n")

    monkeypatch.setattr(
        "sys.argv", ["sensor_records", "--config", str(path), "--input", str(input_path)]
    )
    assert main() == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [item["kind"] for item in output] == ["velocity", "height"]

from __future__ import annotations

import json
import sys
from copy import deepcopy

import pytest

from perception.control_publisher import ControlPublisher, ControlPublisherConfig, PublisherError
from perception.sensor_records import SensorRecordAdapter, main


def recording_config(tmp_path):
    publisher = {
        "mode": "replay",
        "session": "sensor-recording-test",
        "websocket_url": None,
        "audit_dir": str(tmp_path / "publisher-audit"),
        "queue_limit": 8,
        "drones": [
            {
                "key_environment": "LOCALIZATION_KEY_1",
                "live_capture_clock": None,
                "clock_mapping": {
                    "capture_clock_id": "android_elapsed_realtime",
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
                    "map_id": "map-sha",
                    "geometry_id": "geometry-sha",
                    "clock_id": "android_elapsed_realtime",
                    "tag_source_id": "tag-camera",
                    "velocity_source_id": "msdk-velocity",
                    "height_source_id": "tof-height",
                    "camera_calibration_id": "camera-calibration",
                    "body_extrinsics_id": "body-camera-measurement",
                    "position_bounds_map_enu_m": [[-10, 10], [-10, 10], [0, 3]],
                    "height_bounds_map_enu_m": [0, 3],
                    "max_speed_mps": 0.5,
                    "position_variance_bounds_m2": [0.000001, 0.0625],
                    "velocity_variance_bounds_m2ps2": [0.000001, 1],
                    "height_variance_bounds_m2": [0.000001, 0.0625],
                    "production_evidence_verified": True,
                },
            }
        ],
    }
    return {
        "publisher": publisher,
        "phone": {
            "drone_id": 1,
            "recorder_config_sha256": "a" * 64,
            "velocity": {
                "source_id": "msdk-velocity",
                "sdk_key": "KeyAircraftVelocity",
                "map_rotation": {
                    "measurement": {"measurement_id": "ned-to-map-survey", "measured": True},
                    "matrix": [[0, 1, 0], [1, 0, 0], [0, 0, -1]],
                },
                "covariance_map_enu_m2ps2": {
                    "measurement": {"measurement_id": "velocity-noise-flight-1", "measured": True},
                    "matrix": [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 0.01]],
                },
            },
            "height": {
                "source_id": "tof-height",
                "sdk_key": "KeyUltrasonicHeight",
                "map_datum": {
                    "measurement": {"measurement_id": "height-datum-survey", "measured": True},
                    "offset_m": 1.0,
                },
                "variance_m2": {
                    "measurement": {"measurement_id": "height-noise-flight-1", "measured": True},
                    "value": 0.01,
                },
            },
        },
    }


def raw_common(kind: str):
    return {
        "record_schema_version": 3,
        "kind": kind,
        "event_id": f"{kind}-event",
        "recording_run_id": "run-id",
        "run_sequence": 4,
        "session": "sensor-recording-test",
        "product_id": 12,
        "drone_id": 1,
        "connection_generation": 8,
        "connection_epoch": 3,
        "product_type": "Mini 3",
        "aircraft_firmware": "01.00.00",
        "rc_firmware": "01.00.00",
        "sdk_version": "5.18.0",
        "recorder_config_sha256": "a" * 64,
        "time_basis": "android_callback_receipt_elapsed_realtime_ms",
        "source_timestamp_status": "not_provided_by_msdk_key_listener",
        "received_at_android_elapsed_realtime_ms": 10_100,
        "written_at_android_elapsed_realtime_ms": 10_101,
    }


def samples():
    return [
        raw_common("phone_velocity_raw")
        | {
            "sdk_key": "KeyAircraftVelocity",
            "coordinate_frame": "ned",
            "north_mps": 1,
            "east_mps": 2,
            "down_mps": -3,
        },
        raw_common("phone_height_raw")
        | {
            "sdk_key": "KeyUltrasonicHeight",
            "height_value": 6.2,
            "height_unit": "dm",
        },
    ]


def test_android_v3_phone_samples_emit_unverified_publisher_records(tmp_path):
    config = recording_config(tmp_path)
    records = [SensorRecordAdapter(config).record(sample) for sample in samples()]

    assert records[0]["velocity_map_enu_mps"] == [2.0, 1.0, 3.0]
    assert records[1]["height_map_enu_m"] == pytest.approx(1.62)
    assert all(record["capture_time"] == 10.1 for record in records)
    assert all(record["clock_id"] == "android_elapsed_realtime" for record in records)
    assert all(record["source_verified"] is False for record in records)
    assert all(record["timing_verified"] is False for record in records)

    publisher = ControlPublisher(ControlPublisherConfig.from_mapping(config["publisher"]))
    for record in records:
        with pytest.raises(PublisherError, match="sensor record is invalid"):
            publisher.enqueue(record)


def test_adapter_requires_the_current_android_v3_contract(tmp_path):
    adapter = SensorRecordAdapter(recording_config(tmp_path))
    sample = samples()[0]
    for field, value, message in [
        ("record_schema_version", 2, "schema version"),
        ("time_basis", "android_elapsed_realtime", "time basis"),
        ("source_timestamp_status", "capture_timestamp", "timestamp status"),
        ("received_at_android_elapsed_realtime_ms", 10.1, "received timestamp"),
        ("connection_epoch", 4, "connection epoch"),
        ("run_sequence", 0, "run_sequence must be positive"),
        ("recorder_config_sha256", "b" * 64, "recorder configuration"),
    ]:
        with pytest.raises(ValueError, match=message):
            adapter.record(sample | {field: value})
    with pytest.raises(ValueError, match="fields do not match"):
        adapter.record(sample | {"unexpected": True})


def test_attitude_is_validated_then_kept_out_of_publisher_input(tmp_path):
    adapter = SensorRecordAdapter(recording_config(tmp_path))
    attitude = raw_common("phone_attitude_raw") | {
        "sdk_key": "KeyGimbalAttitude",
        "attitude_frame": "raw_sdk_axes",
        "yaw_deg": 1,
        "pitch_deg": 2,
        "roll_deg": 3,
    }
    assert adapter.record_if_selected(attitude) is None
    with pytest.raises(ValueError, match="not publisher input"):
        adapter.record(attitude)
    with pytest.raises(ValueError, match="key and frame"):
        adapter.record_if_selected(
            attitude | {"attitude_frame": "gimbal_body_relative_to_aircraft"}
        )


def test_unselected_height_key_is_validated_and_skipped(tmp_path):
    adapter = SensorRecordAdapter(recording_config(tmp_path))
    barometric = raw_common("phone_height_raw") | {
        "sdk_key": "KeyAltitude",
        "height_value": 0.62,
        "height_unit": "m",
    }
    assert adapter.record_if_selected(barometric) is None
    with pytest.raises(ValueError, match="fields do not match"):
        adapter.record_if_selected(barometric | {"extra": True})


def test_config_demands_identified_measured_geometry_and_uncertainty(tmp_path):
    config = recording_config(tmp_path)
    missing_measurement = deepcopy(config)
    del missing_measurement["phone"]["velocity"]["map_rotation"]["measurement"]
    with pytest.raises(ValueError, match="unsupported fields"):
        SensorRecordAdapter(missing_measurement)
    unmeasured = deepcopy(config)
    unmeasured["phone"]["height"]["variance_m2"]["measurement"]["measured"] = False
    with pytest.raises(ValueError, match="must be measured"):
        SensorRecordAdapter(unmeasured)


def test_cli_converts_only_selected_android_v3_samples(tmp_path, monkeypatch, capsys):
    config = recording_config(tmp_path)
    path = tmp_path / "recording.json"
    path.write_text(json.dumps(config))
    attitude = raw_common("phone_attitude_raw") | {
        "sdk_key": "KeyAircraftAttitude",
        "attitude_frame": "aircraft_body_to_ned",
        "yaw_deg": 1,
        "pitch_deg": 2,
        "roll_deg": 3,
    }
    input_path = tmp_path / "phone.jsonl"
    input_path.write_text("\n".join(json.dumps(sample) for sample in [*samples(), attitude]) + "\n")

    monkeypatch.setattr(
        sys, "argv", ["sensor_records", "--config", str(path), "--input", str(input_path)]
    )
    assert main() == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [item["kind"] for item in output] == ["velocity", "height"]

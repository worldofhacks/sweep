import json
from pathlib import Path

import pytest

from relay.observations import (
    ClockMapping,
    FrameDeclaration,
    FrameRegistry,
    ObservationError,
    ObservationSubmission,
    RatePolicy,
    SourceBinding,
    TimingPolicy,
    decode_observation,
    decode_submission,
    ingest,
)

FIXTURES = Path(__file__).parent / "fixtures" / "observation_v1"


def world_registry() -> FrameRegistry:
    return FrameRegistry(
        (
            FrameDeclaration(
                "world",
                "world",
                "right_handed_z_up",
                "m",
                map_id="level-1",
                map_version="sha256:map-v1",
                physical_datum="level-1-survey-2026-09",
            ),
        )
    )


def local_registry(epoch: int = 3) -> FrameRegistry:
    declarations = tuple(
        FrameDeclaration(
            frame,
            kind,
            "right_handed_z_up"
            if frame == "odom"
            else "east_north_up"
            if frame == "legacy_map_enu"
            else "right_down_forward"
            if frame == "camera"
            else "right_up_outward"
            if frame == "tag:42"
            else "forward_left_up",
            "m",
            session="demo-1",
            device_id=9,
            connection_epoch=epoch,
            source_id="ohmni-lidar",
        )
        for frame, kind in (
            ("odom", "odom"),
            ("lidar", "lidar"),
            ("camera", "camera"),
            ("tag:42", "tag"),
            ("legacy_map_enu", "legacy_map_enu"),
        )
    )
    return FrameRegistry(declarations)


def world_binding() -> SourceBinding:
    return SourceBinding(
        "demo-1",
        7,
        7,
        "dji-bridge",
        "aircraft",
        ("world",),
        "level-1",
        "sha256:map-v1",
        "level-1-survey-2026-09",
        ("aircraft-ms",),
    )


def local_binding(epoch: int = 3) -> SourceBinding:
    return SourceBinding(
        "demo-1",
        9,
        epoch,
        "ohmni-lidar",
        "ground",
        ("odom", "lidar", "camera", "tag:42", "legacy_map_enu"),
    )


def legacy_binding() -> SourceBinding:
    return SourceBinding("demo-1", 7, 7, "dji-bridge", "aircraft", ("legacy_map_enu",))


def test_aircraft_golden_decodes_ingests_and_reencodes_exactly() -> None:
    encoded = (FIXTURES / "aircraft-world.json").read_bytes()
    event = decode_observation(encoded)
    result = ingest(
        event.submission,
        t_ingest=event.t_ingest,
        frames=world_registry(),
        binding=world_binding(),
        mappings={
            "aircraft-ms": ClockMapping("aircraft-ms", "bridge-ms", "ms", 100, 1_000, 1, 1, 5)
        },
        timing=TimingPolicy(25),
    )

    assert result.encode() == encoded.rstrip(b"\n")
    assert result.submission.payload["kind"] == "aircraft_telemetry"


def test_unregistered_odom_scan_is_a_valid_mapping_diagnostic() -> None:
    encoded = (FIXTURES / "ground-odom-range-scan.json").read_bytes()
    event = decode_observation(encoded)
    result = ingest(
        event.submission,
        t_ingest=event.t_ingest,
        frames=local_registry(),
        binding=local_binding(),
        mappings={},
        timing=TimingPolicy(25),
    )

    assert result.encode() == encoded.rstrip(b"\n")
    assert result.submission.t_capture is None
    assert result.submission.payload["sensor_pose"]["parent_frame"] == "odom"


def test_camera_tag_vector_keeps_its_camera_frame_without_registration() -> None:
    encoded = (FIXTURES / "camera-tag-observation.json").read_bytes()
    event = decode_observation(encoded)
    result = ingest(
        event.submission,
        t_ingest=event.t_ingest,
        frames=local_registry(),
        binding=local_binding(),
        mappings={},
        timing=TimingPolicy(25),
    )

    assert result.encode() == encoded.rstrip(b"\n")
    assert result.submission.payload["kind"] == "tag_observation"


def test_explicit_legacy_map_enu_frame_is_local_compatibility_evidence() -> None:
    encoded = (FIXTURES / "legacy-aircraft-local.json").read_bytes()
    event = decode_observation(encoded)
    result = ingest(
        event.submission,
        t_ingest=event.t_ingest,
        frames=FrameRegistry(
            (
                FrameDeclaration(
                    "legacy_map_enu",
                    "legacy_map_enu",
                    "east_north_up",
                    "m",
                    session="demo-1",
                    device_id=7,
                    connection_epoch=7,
                    source_id="dji-bridge",
                ),
            )
        ),
        binding=legacy_binding(),
        mappings={},
        timing=TimingPolicy(25),
    )

    assert result.submission.frame == "legacy_map_enu"


def test_capture_precedes_source_receipt_and_mapping_binds_the_declared_clock() -> None:
    raw = json.loads((FIXTURES / "aircraft-world.json").read_text())
    submission = ObservationSubmission.parse({key: raw[key] for key in raw if key != "t_ingest"})
    accepted = ingest(
        submission,
        t_ingest=1_008,
        frames=world_registry(),
        binding=world_binding(),
        mappings={
            "aircraft-ms": ClockMapping("aircraft-ms", "bridge-ms", "ms", 100, 1_000, 1, 1, 5)
        },
        timing=TimingPolicy(25),
    )

    assert accepted.t_ingest == 1_008

    with pytest.raises(ObservationError) as skew:
        ingest(
            submission,
            t_ingest=970,
            frames=world_registry(),
            binding=world_binding(),
            mappings={
                "aircraft-ms": ClockMapping("aircraft-ms", "bridge-ms", "ms", 100, 1_000, 1, 1, 5)
            },
            timing=TimingPolicy(25),
        )
    assert skew.value.code == "source_receipt_in_future"

    invalid = dict(raw)
    invalid["t_capture"] = {"clock_id": "bridge-ms", "unit": "ms", "value": 102}
    invalid["t_source_receipt"] = {"clock_id": "bridge-ms", "unit": "ms", "value": 101}
    with pytest.raises(ObservationError, match="capture time exceeds"):
        ObservationSubmission.parse({key: invalid[key] for key in invalid if key != "t_ingest"})


def test_unknown_frame_stale_epoch_and_unconfigured_mapping_fail_closed() -> None:
    scan = json.loads((FIXTURES / "ground-odom-range-scan.json").read_text())
    submission = ObservationSubmission.parse({key: scan[key] for key in scan if key != "t_ingest"})

    with pytest.raises(ObservationError) as unknown:
        ingest(
            submission,
            t_ingest=42,
            frames=world_registry(),
            binding=local_binding(),
            mappings={},
            timing=TimingPolicy(25),
        )
    assert unknown.value.code == "unknown_frame"

    with pytest.raises(ObservationError) as stale:
        ingest(
            submission,
            t_ingest=42,
            frames=local_registry(epoch=4),
            binding=local_binding(),
            mappings={},
            timing=TimingPolicy(25),
        )
    assert stale.value.code == "frame_scope_mismatch"

    changed = dict(scan)
    changed["clock_mapping_id"] = "missing"
    mapped = ObservationSubmission.parse(
        {key: changed[key] for key in changed if key != "t_ingest"}
    )
    with pytest.raises(ObservationError) as mapping:
        ingest(
            mapped,
            t_ingest=42,
            frames=local_registry(),
            binding=SourceBinding(
                "demo-1",
                9,
                3,
                "ohmni-lidar",
                "ground",
                ("odom", "lidar"),
                allowed_clock_mapping_ids=("missing",),
            ),
            mappings={},
            timing=TimingPolicy(25),
        )
    assert mapping.value.code == "unknown_clock_mapping"


def test_decoder_rejects_duplicate_keys_and_unknown_payload_kind() -> None:
    duplicate = b'{"v":1,"v":1}'
    with pytest.raises(ObservationError) as error:
        decode_submission(duplicate)
    assert error.value.code == "duplicate_json_key"

    raw = json.loads((FIXTURES / "ground-odom-range-scan.json").read_text())
    raw.pop("t_ingest")
    raw["payload"] = {"kind": "raw_sensor", "value": 1}
    with pytest.raises(ObservationError) as payload:
        ObservationSubmission.parse(raw)
    assert payload.value.code == "invalid_payload"


def test_rate_policy_is_a_pure_bounded_admission_check() -> None:
    policy = RatePolicy(200)

    assert policy.accepts(None, 1_000)
    assert not policy.accepts(1_000, 1_199)
    assert policy.accepts(1_000, 1_200)
    assert not policy.accepts(1_000, 999)


def test_registry_resolves_repeated_local_frame_ids_by_the_full_source_scope() -> None:
    first = FrameDeclaration(
        "camera",
        "camera",
        "right_down_forward",
        "m",
        session="demo-1",
        device_id=9,
        connection_epoch=3,
        source_id="ohmni-lidar",
    )
    second = FrameDeclaration(
        "camera",
        "camera",
        "right_down_forward",
        "m",
        session="demo-1",
        device_id=10,
        connection_epoch=2,
        source_id="ohmni-camera",
    )
    registry = FrameRegistry((first, second))
    raw = json.loads((FIXTURES / "camera-tag-observation.json").read_text())
    submission = ObservationSubmission.parse({key: raw[key] for key in raw if key != "t_ingest"})

    assert registry.require("camera", submission) is first


def test_world_requires_matching_host_binding_pins_and_numeric_confidence() -> None:
    raw = json.loads((FIXTURES / "aircraft-world.json").read_text())
    submission = ObservationSubmission.parse({key: raw[key] for key in raw if key != "t_ingest"})
    wrong_world = SourceBinding(
        "demo-1",
        7,
        7,
        "dji-bridge",
        "aircraft",
        ("world",),
        "level-1",
        "sha256:other-map",
        "level-1-survey-2026-09",
        ("aircraft-ms",),
    )

    with pytest.raises(ObservationError) as pins:
        ingest(
            submission,
            t_ingest=1_005,
            frames=world_registry(),
            binding=wrong_world,
            mappings={
                "aircraft-ms": ClockMapping("aircraft-ms", "bridge-ms", "ms", 100, 1_000, 1, 1, 5)
            },
            timing=TimingPolicy(25),
        )
    assert pins.value.code == "world_pin_mismatch"

    raw["confidence"] = "high"
    with pytest.raises(ObservationError, match="confidence"):
        ObservationSubmission.parse({key: raw[key] for key in raw if key != "t_ingest"})


def test_pose_camera_and_status_payloads_have_closed_encodable_shapes() -> None:
    base = json.loads((FIXTURES / "ground-odom-range-scan.json").read_text())
    payloads = (
        (
            "odom",
            {
                "kind": "pose",
                "pose": {
                    "parent_frame": "odom",
                    "child_frame": "lidar",
                    "x_m": 0.0,
                    "y_m": 0.0,
                    "z_m": 0.25,
                    "qx": 0.0,
                    "qy": 0.0,
                    "qz": 0.0,
                    "qw": 1.0,
                },
            },
        ),
        (
            "camera",
            {
                "kind": "camera_frame",
                "image_id": "image-001",
                "sha256": "a" * 64,
                "width_px": 1280,
                "height_px": 720,
                "calibration_id": "ohmni-head-v1",
            },
        ),
        (
            "lidar",
            {
                "kind": "status",
                "code": "lidar_ready",
                "detail": "driver is receiving scans",
                "capabilities": ["range_scan"],
            },
        ),
    )

    for index, (frame, payload) in enumerate(payloads):
        raw = {**base, "event_id": f"other-{index}", "frame": frame, "payload": payload}
        submission = ObservationSubmission.parse(
            {key: raw[key] for key in raw if key != "t_ingest"}
        )
        result = ingest(
            submission,
            t_ingest=raw["t_ingest"],
            frames=local_registry(),
            binding=local_binding(),
            mappings={},
            timing=TimingPolicy(25),
        )
        assert decode_observation(result.encode()).submission.payload["kind"] == payload["kind"]


def test_submission_state_is_deeply_immutable_and_export_returns_fresh_values() -> None:
    raw = json.loads((FIXTURES / "ground-odom-range-scan.json").read_text())
    submission = ObservationSubmission.parse({key: raw[key] for key in raw if key != "t_ingest"})

    with pytest.raises(TypeError):
        submission.payload["kind"] = "status"  # type: ignore[index]
    with pytest.raises(TypeError):
        submission.payload["ranges_m"][0] = 9.0  # type: ignore[index]

    exported = submission.to_mapping()
    exported["payload"]["ranges_m"][0] = 9.0  # type: ignore[index]
    assert submission.to_mapping()["payload"]["ranges_m"][0] != 9.0  # type: ignore[index]


def test_parser_rejects_boolean_version_huge_numbers_and_non_utf8_json() -> None:
    raw = json.loads((FIXTURES / "aircraft-world.json").read_text())
    raw["v"] = True
    with pytest.raises(ObservationError) as version:
        ObservationSubmission.parse({key: raw[key] for key in raw if key != "t_ingest"})
    assert version.value.code == "invalid_observation"

    raw["v"] = 1
    raw["device_id"] = 2**31
    with pytest.raises(ObservationError) as device:
        ObservationSubmission.parse({key: raw[key] for key in raw if key != "t_ingest"})
    assert device.value.code == "invalid_observation"

    raw["device_id"] = 7
    raw["payload"]["position"]["x_m"] = 10**100_000
    with pytest.raises(ObservationError) as coordinate:
        ObservationSubmission.parse({key: raw[key] for key in raw if key != "t_ingest"})
    assert coordinate.value.code == "invalid_observation"

    with pytest.raises(ObservationError) as encoding:
        decode_submission(b"\xff")
    assert encoding.value.code == "invalid_observation"

"""The kit must agree with the relay it cannot import.

Every assertion here reads a vector that ``nodekit/vectors.py`` rendered from the relay's
own contracts and signer, then holds ``nodekit.protocol`` to it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nodekit import protocol, vectors
from nodekit.fake import FakeGroundVehicle
from relay.contracts import (
    parse_adapter_acknowledgement,
    parse_capabilities,
    parse_membership_request,
    parse_node_status,
    parse_sensor,
    parse_telemetry,
)

REFRESH = "run `uv run python -m nodekit.vectors`"


def committed(name: str) -> dict[str, Any]:
    path = vectors.VECTOR_DIR / name
    assert path.is_file(), f"{path} is missing; {REFRESH}"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def node_protocol() -> dict[str, Any]:
    return committed("node_protocol.json")


def envelope_of(wire: dict[str, Any]) -> dict[str, Any]:
    return {"t": wire["t"], "event_id": wire["event_id"], "session": wire["session"]}


def test_vector_files_are_current() -> None:
    for name, text in vectors.render().items():
        path = vectors.VECTOR_DIR / name
        assert path.is_file(), f"{path} is missing; {REFRESH}"
        assert path.read_text(encoding="utf-8") == text, f"{path} is stale; {REFRESH}"


def test_the_kit_canonicalizes_exactly_as_the_relay_does() -> None:
    for case in committed("canonical_json.json")["cases"]:
        assert protocol.canonical_event_bytes(case["value"]).decode("utf-8") == case["canonical"]


def test_the_kit_signs_exactly_as_the_relay_does() -> None:
    for case in committed("hmac_sha256.json")["cases"]:
        signature = protocol.sign_event(case["unsigned_event"], case["key"])
        assert signature == case["signature"]
        assert protocol.verify_event_signature(case["unsigned_event"], signature, case["key"])


def test_the_kit_knows_the_relays_command_set(node_protocol: dict[str, Any]) -> None:
    assert protocol.COMMAND_OPERATIONS == set(node_protocol["command_operations"])
    assert protocol.COMMAND_ARGUMENT_FIELDS == node_protocol["command_argument_fields"]
    for operation, args in node_protocol["command_args"].items():
        assert protocol.command_arguments(operation, args) == args


def test_the_kit_knows_the_relays_reasons_and_classes(node_protocol: dict[str, Any]) -> None:
    assert protocol.NODE_ACKNOWLEDGEMENT_REASONS == set(node_protocol["acknowledgement_reasons"])
    assert protocol.DEVICE_CLASSES == set(node_protocol["device_classes"])
    assert (
        protocol.DEVICE_CLASS_CAPABILITY_PREFIX == node_protocol["device_class_capability_prefix"]
    )
    bounds = node_protocol["sensor_bounds"]
    assert list(protocol.SENSOR_ANGLE_INCREMENTS_DEG) == bounds["angle_increments_deg"]
    assert protocol.MAX_SENSOR_RANGES == bounds["max_ranges"]
    assert protocol.MAX_SENSOR_RANGE_CM == bounds["max_range_cm"]
    assert protocol.MAX_SENSOR_FRAME_CANONICAL_BYTES == bounds["max_canonical_bytes"]


def test_the_kit_parses_the_relays_command(node_protocol: dict[str, Any]) -> None:
    entry = node_protocol["frames"]["command"]

    command = protocol.parse_command(entry["wire"])

    assert command.verifies(entry["key"])
    assert command.operation == "goto"
    assert {**command.unsigned_event(), "signature": command.signature} == entry["wire"]


def test_the_kit_parses_the_relays_control_heartbeat(node_protocol: dict[str, Any]) -> None:
    entry = node_protocol["frames"]["control_heartbeat"]

    heartbeat = protocol.parse_control_heartbeat(entry["wire"])

    assert heartbeat.verifies(entry["key"])
    assert not heartbeat.verifies("another-key-0123456789abcdef01234567")
    assert {**heartbeat.unsigned_event(), "signature": heartbeat.signature} == entry["wire"]


def test_the_kit_builds_the_relays_signed_membership_frames(
    node_protocol: dict[str, Any],
) -> None:
    frames = node_protocol["frames"]
    key = frames["ground_join"]["key"]

    join = frames["ground_join"]["wire"]
    assert (
        protocol.join_frame(
            device_id=vectors.GROUND_ID,
            adapter_id=vectors.GROUND_ADAPTER_ID,
            device_class="ground_vehicle",
            capabilities=list(vectors.GROUND_CAPABILITIES),
            key=key,
            **envelope_of(join),
        )
        == join
    )

    readiness = frames["ground_readiness"]["wire"]
    assert (
        protocol.readiness_frame(
            device_id=vectors.GROUND_ID,
            connection_epoch=vectors.GROUND_EPOCH,
            home_pose_confirmed=True,
            control_authority=True,
            rc_safety_operator_present=True,
            key=key,
            **envelope_of(readiness),
        )
        == readiness
    )

    leave = frames["ground_graceful_leave"]["wire"]
    assert (
        protocol.graceful_leave_frame(
            device_id=vectors.GROUND_ID,
            connection_epoch=vectors.GROUND_EPOCH,
            key=key,
            **envelope_of(leave),
        )
        == leave
    )


def test_the_kit_builds_the_relays_unsigned_node_frames(node_protocol: dict[str, Any]) -> None:
    frames = node_protocol["frames"]

    telemetry = frames["ground_telemetry"]["wire"]
    assert (
        protocol.telemetry_frame(
            device_id=vectors.GROUND_ID,
            connection_epoch=vectors.GROUND_EPOCH,
            x=1.2,
            y=-0.4,
            z=0.0,
            vx=0.25,
            vy=0.1,
            vz=0.0,
            battery=0.86,
            state="moving",
            link=0.95,
            pos_quality=0.6,
            **envelope_of(telemetry),
        )
        == telemetry
    )

    sensor = frames["ground_sensor"]["wire"]
    assert (
        protocol.sensor_frame(
            device_id=vectors.GROUND_ID,
            connection_epoch=vectors.GROUND_EPOCH,
            kind="lidar_scan",
            pose=(1.2, -0.4, 87.5),
            angle_min_deg=0.0,
            angle_increment_deg=2.0,
            range_min_m=0.15,
            range_max_m=12.0,
            ranges_cm=vectors.ground_scan_ranges(),
            **envelope_of(sensor),
        )
        == sensor
    )

    capabilities = frames["ground_capabilities"]["wire"]
    assert (
        protocol.capabilities_frame(
            device_id=vectors.GROUND_ID,
            connection_epoch=vectors.GROUND_EPOCH,
            profile=FakeGroundVehicle().hardware_profile(),
            **envelope_of(capabilities),
        )
        == capabilities
    )

    node_status = frames["ground_node_status"]["wire"]
    assert (
        protocol.node_status_frame(
            device_id=vectors.GROUND_ID,
            connection_epoch=vectors.GROUND_EPOCH,
            virtual_stick_enabled=False,
            control_authority=True,
            authority_change_reason=None,
            watchdog_state="hold",
            video_publish_state="publishing",
            phone_battery_percent=86,
            **envelope_of(node_status),
        )
        == node_status
    )


def test_the_kit_builds_the_relays_unsupported_operation_acknowledgement(
    node_protocol: dict[str, Any],
) -> None:
    wire = node_protocol["frames"]["ground_acknowledgement"]["wire"]
    command = protocol.Command(
        1,
        7000,
        "command",
        "evt-cmd-9",
        vectors.SESSION,
        wire["command_id"],
        wire["intent_id"],
        wire["roster_version"],
        vectors.GROUND_ID,
        vectors.GROUND_EPOCH,
        1,
        7000,
        2000,
        "takeoff",
        {"z_mm": 1200},
        "0" * 64,
    )

    assert (
        protocol.acknowledgement_frame(
            device_id=vectors.GROUND_ID,
            command=command,
            status=protocol.FAILED,
            reason=protocol.UNSUPPORTED_OPERATION,
            detail=wire["detail"],
            **envelope_of(wire),
        )
        == wire
    )


def test_the_relay_accepts_every_frame_the_kit_builds(node_protocol: dict[str, Any]) -> None:
    """The other direction: the relay's own parsers on the kit's output."""
    frames = node_protocol["frames"]
    assert parse_membership_request(frames["ground_join"]["wire"]).device_class is not None
    assert parse_membership_request(frames["ground_readiness"]["wire"]).connection_epoch == (
        vectors.GROUND_EPOCH
    )
    assert parse_telemetry(frames["ground_telemetry"]["wire"]).state == "moving"
    assert parse_sensor(frames["ground_sensor"]["wire"]).kind.value == "lidar_scan"
    assert parse_capabilities(frames["ground_capabilities"]["wire"]).aircraft_model == (
        "fake-ground-vehicle"
    )
    assert parse_node_status(frames["ground_node_status"]["wire"]).watchdog_state.value == "hold"
    assert parse_adapter_acknowledgement(frames["ground_acknowledgement"]["wire"]).reason == (
        protocol.UNSUPPORTED_OPERATION
    )

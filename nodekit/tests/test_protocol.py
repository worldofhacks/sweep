"""Frame builders, parsers, and command admission, without a socket."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from nodekit import protocol
from nodekit.node import Admission, admit_command

KEY = "node-key-0123456789abcdef0123456789abcdef"
OTHER_KEY = "other-key-0123456789abcdef0123456789ab"
SESSION = "session-a"
DEVICE_ID = 11
EPOCH = 3
ROSTER = 5


def command(**changes: object) -> protocol.Command:
    unsigned = {
        "v": 1,
        "t": 4000,
        "type": "command",
        "event_id": "evt-cmd-1",
        "session": SESSION,
        "command_id": "cmd-1",
        "intent_id": "intent-1",
        "roster_version": ROSTER,
        "drone_id": DEVICE_ID,
        "connection_epoch": EPOCH,
        "seq": 7,
        "issued_at": 4000,
        "ttl_ms": 1500,
        "operation": "goto",
        "args": {"x_mm": 1000, "y_mm": 2500, "z_mm": 0, "speed_mm_s": 500},
    }
    key = changes.pop("key", KEY)
    unsigned.update(changes)
    wire = {**unsigned, "signature": protocol.sign_event(unsigned, key)}
    return protocol.parse_command(wire)


def admit(frame: protocol.Command, **changes: object) -> Admission:
    arguments: dict[str, object] = {
        "session": SESSION,
        "device_id": DEVICE_ID,
        "key": KEY,
        "connection_epoch": EPOCH,
        "roster_version": ROSTER,
        "last_seq": 6,
        "now_ms": 4500,
    }
    arguments.update(changes)
    return admit_command(frame, **arguments)  # type: ignore[arg-type]


def test_a_current_command_is_admitted() -> None:
    admission = admit(command())

    assert (admission.admitted, admission.reason) == (True, None)


def test_a_stale_epoch_is_refused_as_a_stale_command() -> None:
    admission = admit(command(connection_epoch=EPOCH - 1))

    assert (admission.admitted, admission.acknowledge) == (False, True)
    assert admission.reason == protocol.STALE_COMMAND
    assert "epoch" in (admission.detail or "")


def test_a_stale_roster_is_refused_as_a_stale_command() -> None:
    admission = admit(command(roster_version=ROSTER - 1))

    assert admission.reason == protocol.STALE_COMMAND
    assert "roster" in (admission.detail or "")


def test_a_command_older_than_its_ttl_is_refused() -> None:
    admission = admit(command(), now_ms=4000 + 1500 + 1)

    assert (admission.acknowledge, admission.reason) == (True, protocol.STALE_COMMAND)
    assert admission.detail == "command is older than its ttl"


def test_a_replayed_sequence_is_refused_as_out_of_order() -> None:
    admission = admit(command(seq=6))

    assert (admission.acknowledge, admission.reason) == (True, protocol.OUT_OF_ORDER_COMMAND)


def test_a_command_signed_with_another_key_is_dropped_without_an_acknowledgement() -> None:
    admission = admit(command(key=OTHER_KEY))

    assert (admission.admitted, admission.acknowledge) == (False, False)
    assert admission.reason == "invalid_signature"


def test_a_command_for_another_device_is_dropped_without_an_acknowledgement() -> None:
    admission = admit(command(drone_id=DEVICE_ID + 1))

    assert (admission.admitted, admission.acknowledge) == (False, False)
    assert admission.reason == "misaddressed"


def test_a_command_before_the_join_is_dropped_because_there_is_no_epoch_yet() -> None:
    admission = admit(command(), connection_epoch=None)

    assert (admission.admitted, admission.acknowledge) == (False, False)
    assert admission.reason == protocol.STALE_COMMAND


def test_command_parsing_fails_closed_on_shape() -> None:
    wire = {**command().unsigned_event(), "signature": "0" * 64}
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_command({**wire, "extra": 1})
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_command({**wire, "operation": "fly_home"})
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_command({**wire, "args": {"x_mm": 1}})
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_command({**wire, "seq": 0})


def test_goto_arguments_reject_a_zero_speed_and_a_boolean() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.command_arguments("goto", {"x_mm": 0, "y_mm": 0, "z_mm": 0, "speed_mm_s": 0})
    with pytest.raises(protocol.ProtocolError):
        protocol.command_arguments("goto", {"x_mm": True, "y_mm": 0, "z_mm": 0, "speed_mm_s": 5})


def test_a_join_carries_exactly_one_class_capability() -> None:
    capabilities = protocol.join_capabilities("ground_vehicle", ["ground_drive", "lidar"])

    assert capabilities == ["ground_drive", "lidar", "class:ground_vehicle"]
    with pytest.raises(protocol.ProtocolError):
        protocol.join_capabilities("ground_vehicle", ["ground_drive", "class:aircraft"])
    with pytest.raises(protocol.ProtocolError):
        protocol.join_capabilities("submarine", ["dive"])


def test_a_signed_membership_frame_verifies_against_its_own_key() -> None:
    frame = protocol.join_frame(
        t=1000,
        event_id="evt-join-1",
        session=SESSION,
        device_id=DEVICE_ID,
        adapter_id="ohmni-1",
        device_class="ground_vehicle",
        capabilities=["ground_drive"],
        key=KEY,
    )
    unsigned = {key: value for key, value in frame.items() if key != "signature"}

    assert protocol.verify_event_signature(unsigned, frame["signature"], KEY)
    assert not protocol.verify_event_signature(unsigned, frame["signature"], OTHER_KEY)
    assert not protocol.verify_event_signature(unsigned, "not-hex", KEY)


def test_a_heartbeat_parses_and_verifies_like_the_relay_signs_it() -> None:
    unsigned = {
        "v": 1,
        "t": 5000,
        "type": "control_heartbeat",
        "event_id": "evt-hb-1",
        "session": SESSION,
        "source": "relay",
        "drone_id": DEVICE_ID,
        "connection_epoch": EPOCH,
        "roster_version": ROSTER,
        "seq": 12,
    }
    heartbeat = protocol.parse_control_heartbeat(
        {**unsigned, "signature": protocol.sign_event(unsigned, KEY)}
    )

    assert heartbeat.verifies(KEY)
    assert heartbeat.unsigned_event() == unsigned
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_control_heartbeat({**unsigned, "source": "console", "signature": "a" * 64})


def test_a_sensor_frame_is_refused_locally_before_it_reaches_the_relay() -> None:
    ranges = [100] * 360

    frame = protocol.sensor_frame(
        t=6000,
        event_id="evt-sensor-1",
        session=SESSION,
        device_id=DEVICE_ID,
        connection_epoch=EPOCH,
        kind="lidar_scan",
        pose=(1.0, -2.0, 380.0),
        angle_min_deg=0.0,
        angle_increment_deg=1.0,
        range_min_m=0.15,
        range_max_m=12.0,
        ranges_cm=ranges,
    )
    assert frame["pose"] == {"x": 1.0, "y": -2.0, "yaw_deg": 20.0}
    assert len(frame["ranges_cm"]) == 360

    with pytest.raises(protocol.ProtocolError):
        protocol.sensor_frame(
            t=6000,
            event_id="evt-sensor-2",
            session=SESSION,
            device_id=DEVICE_ID,
            connection_epoch=EPOCH,
            kind="lidar_scan",
            pose=(0.0, 0.0, 0.0),
            angle_min_deg=0.0,
            angle_increment_deg=1.5,
            range_min_m=0.15,
            range_max_m=12.0,
            ranges_cm=ranges,
        )
    with pytest.raises(protocol.ProtocolError):
        protocol.sensor_frame(
            t=6000,
            event_id="evt-sensor-3",
            session=SESSION,
            device_id=DEVICE_ID,
            connection_epoch=EPOCH,
            kind="lidar_scan",
            pose=(0.0, 0.0, 0.0),
            angle_min_deg=0.0,
            angle_increment_deg=1.0,
            range_min_m=0.15,
            range_max_m=12.0,
            ranges_cm=ranges[:359],
        )
    with pytest.raises(protocol.ProtocolError):
        protocol.sensor_frame(
            t=6000,
            event_id="evt-sensor-4",
            session=SESSION,
            device_id=DEVICE_ID,
            connection_epoch=EPOCH,
            kind="lidar_scan",
            pose=(0.0, 0.0, 0.0),
            angle_min_deg=0.0,
            angle_increment_deg=1.0,
            range_min_m=0.15,
            range_max_m=12.0,
            ranges_cm=[70_000] * 360,
        )


def test_an_unknown_hardware_profile_field_is_refused_rather_than_dropped() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.capabilities_frame(
            t=1,
            event_id="evt-cap-1",
            session=SESSION,
            device_id=DEVICE_ID,
            connection_epoch=EPOCH,
            profile={"wheel_diameter_mm": 150},
        )


# Everything the kit ships to a device. ``vectors.py`` is a development tool that
# regenerates the wire vectors from the relay and never runs there.
SHIPPED = ("__init__.py", "protocol.py", "device.py", "node.py", "fake.py", "cli.py")


def shipped_sources() -> list[Path]:
    package = Path(__file__).resolve().parent.parent
    return [package / name for name in SHIPPED]


def test_the_kit_stays_within_python_3_9_syntax() -> None:
    """The kit runs on the robot's own interpreter, which is older than the repo's."""
    package = Path(__file__).resolve().parent.parent
    for path in sorted(package.glob("*.py")):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))


def test_the_kit_imports_nothing_from_this_repository_but_itself() -> None:
    """The kit is copied onto the robot on its own: only the standard library and
    ``websockets`` are there with it. Equivalence with the relay is proven by the
    generated vectors, never by importing it."""
    allowed = {"nodekit", "websockets"}
    for path in shipped_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                roots = [(node.module or "").split(".")[0]] if node.level == 0 else []
            else:
                continue
            for root in roots:
                assert root in allowed or _is_standard_library(root), (
                    f"{path.name} imports {root}, which will not exist on the device"
                )


def _is_standard_library(name: str) -> bool:
    return name in sys.stdlib_module_names

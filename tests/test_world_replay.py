import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcap.reader import make_reader

from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.contracts import CommandOperation, NodeType
from relay.observation_ingress import ObservationConfiguration
from relay.observations import (
    ClockMapping,
    FrameDeclaration,
    FrameRegistry,
    SourceBinding,
    TimingPolicy,
    decode_observation,
    ingest,
)
from relay.session import RelayLimits, RelaySession
from relay.tests.conftest import (
    ADAPTER_KEY,
    SESSION,
    MutableClock,
    acknowledgement_payload,
    membership_payload,
    telemetry_payload,
)
from tools.world_replay import (
    CommittedTail,
    ReplayError,
    channel_schema,
    export_audit,
    read_replay,
)


def _schema_valid(channel: str, event: dict[str, object]) -> bool:
    schema = json.loads(channel_schema(channel))
    return Draft202012Validator(schema).is_valid({"seq": 1, "event": event})


@pytest.mark.parametrize(
    ("channel", "event"),
    (
        (
            "roster",
            {
                "type": "membership",
                "session": SESSION,
                "event_id": "member-without-epoch",
                "drone_id": 1,
                "node_type": "aircraft",
            },
        ),
        (
            "acknowledgements",
            {
                "type": "acknowledgement",
                "session": SESSION,
                "event_id": "ack-without-roster",
                "intent_id": "intent-1",
                "command_id": None,
            },
        ),
        (
            "plans",
            {
                "type": "navigation_route_authorization",
                "session": SESSION,
                "event_id": "route-without-transform",
                "device_id": 1,
                "connection_epoch": 1,
                "command_id": "command-1",
                "route_id": "route-1",
                "position_frame": "map_enu",
                "map_sha256": "a" * 64,
                "geometry_sha256": "b" * 64,
                "segments": [],
            },
        ),
        (
            "ground",
            {
                "type": "world_observation",
                "session": SESSION,
                "event_id": "world-without-registration",
                "observation": {
                    "drone_id": 9,
                    "node_type": "ground_vehicle",
                    "connection_epoch": 1,
                    "source_id": "ground-localizer",
                    "frame": "world",
                    "payload": {"position": {"x_m": 1, "y_m": 2, "z_m": 0, "frame": "world"}},
                    "t_capture": None,
                    "t_ingest": 1,
                    "frame_provenance": {},
                    "authority": "diagnostic",
                },
            },
        ),
        (
            "map",
            {
                "type": "map_identity",
                "session": SESSION,
                "event_id": "map-without-source",
                "drone_id": 9,
                "connection_epoch": 1,
                "node_type": "ground_vehicle",
                "frame": "world",
                "map_id": "demo-map",
                "map_version": "v1",
                "map_sha256": "a" * 64,
                "floor_id": "level-1",
                "static_grid_sha256": "b" * 64,
                "manifest": {},
            },
        ),
    ),
)
def test_channel_schemas_reject_missing_type_specific_identity(channel, event):
    assert not _schema_valid(channel, event)


def test_channel_schemas_preserve_nullable_and_optional_acknowledgement_context():
    event = {
        "type": "acknowledgement",
        "session": SESSION,
        "event_id": "intent-level-ack",
        "intent_id": "intent-1",
        "command_id": None,
        "roster_version": 3,
    }

    assert _schema_valid("acknowledgements", event)


def test_real_membership_and_refusal_survive_mcap_round_trip(tmp_path):
    audit = SessionAuditLog(tmp_path / "audit", SESSION)
    session = RelaySession(
        session_id=SESSION,
        audit_log=audit,
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=MutableClock(),
    )
    session.process_membership(
        membership_payload(action="join", event_id="joined"),
        Principal("adapter", 1, ADAPTER_KEY),
    )
    session.record_refusal(
        intent_id=None, source="arbiter", reason="observation_expired", detail="pose expired"
    )
    output = tmp_path / "session.mcap"

    assert export_audit(audit.path, SESSION, output) == audit.last_sequence
    assert list(read_replay(output, SESSION)) == audit.replay()
    with output.open("rb") as stream:
        messages = list(make_reader(stream, validate_crcs=True).iter_messages())
    assert {channel.topic for _, channel, _ in messages} == {"/sweep/roster", "/sweep/safety"}
    refusal = next(message for _, channel, message in messages if channel.topic == "/sweep/safety")
    assert json.loads(refusal.data)["event"]["reason"] == "observation_expired"
    assert refusal.log_time == 1_756_700_000_000_000_000


def test_delayed_device_events_replay_at_relay_arrival_without_changing_wire_time(tmp_path):
    clock = MutableClock()
    source_time = clock.value
    audit = SessionAuditLog(tmp_path / "audit", SESSION)
    session = RelaySession(
        session_id=SESSION,
        audit_log=audit,
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=clock,
    )
    principal = Principal("adapter", 1, ADAPTER_KEY)
    clock.advance(200)
    membership = session.process_membership(
        membership_payload(action="join", event_id="delayed-join", timestamp=source_time),
        principal,
    )[0]
    clock.advance(300)
    telemetry = session.process_telemetry(
        telemetry_payload(event_id="delayed-telemetry", timestamp=source_time), principal
    )[0]
    session.issue_command(
        command_id="command-1",
        intent_id="intent-1",
        roster_version=session.registry.roster_version,
        drone_id=1,
        connection_epoch=1,
        operation=CommandOperation.HOVER,
        args={},
        signing_key=ADAPTER_KEY,
    )
    clock.advance(200)
    acknowledgement = session.process_acknowledgement(
        acknowledgement_payload(event_id="delayed-ack", timestamp=source_time), principal
    )[0]
    for wire, kind in (
        (membership, "membership"),
        (telemetry, "telemetry"),
        (acknowledgement, "acknowledgement"),
    ):
        assert wire["type"] == kind
        assert wire["t"] == source_time
        assert "t_ingest" not in wire
    expected = {
        "delayed-join": source_time + 200,
        "delayed-telemetry": source_time + 500,
        "delayed-ack": source_time + 700,
    }
    output = tmp_path / "delayed.mcap"
    export_audit(audit.path, SESSION, output)
    assert list(read_replay(output, SESSION)) == audit.replay()
    seen = set()
    with output.open("rb") as stream:
        for _, channel, message in make_reader(stream, validate_crcs=True).iter_messages():
            event = json.loads(message.data)["event"]
            if event["event_id"] in expected:
                seen.add(event["event_id"])
                assert event["t"] == source_time
                assert event["t_ingest"] == expected[event["event_id"]]
                assert message.log_time == message.publish_time == event["t_ingest"] * 1_000_000
                assert channel.metadata == {
                    "session": SESSION,
                    "clock": "unix_ns",
                    "timestamp_policy": "t_ingest_else_t",
                }
    assert seen == set(expected)


def test_historical_source_time_fallback_does_not_claim_known_arrival(tmp_path):
    audit = SessionAuditLog(tmp_path / "audit", SESSION)
    event = telemetry_payload(event_id="historical-telemetry")
    audit.append(event)
    output = tmp_path / "historical.mcap"
    export_audit(audit.path, SESSION, output)
    assert list(read_replay(output, SESSION)) == audit.replay()
    with output.open("rb") as stream:
        ((_, channel, message),) = make_reader(stream, validate_crcs=True).iter_messages()
    assert json.loads(message.data)["event"] == event
    assert message.log_time == message.publish_time == event["t"] * 1_000_000
    assert channel.metadata == {
        "session": SESSION,
        "clock": "unix_ns",
        "timestamp_policy": "t_ingest_else_t",
    }


def test_real_world_observations_keep_native_capture_clock_and_rejected_epochs(tmp_path):
    clock = MutableClock()
    mapping = ClockMapping("camera-clock", "native", "ns", 100, clock.value, 1, 1_000_000, 1)
    config = ObservationConfiguration(
        bindings=tuple(
            SourceBinding(
                SESSION,
                device,
                1,
                f"pose-{device}",
                node_type,
                ("world", "body"),
                ("pose",),
                "room",
                "sha256:measured-grid",
                "measured-datum",
                ("camera-clock",),
            )
            for device, node_type in ((1, "aircraft"), (9, "ground"))
        ),
        frames=FrameRegistry(
            (
                FrameDeclaration(
                    "world",
                    "world",
                    "right_handed_z_up",
                    "m",
                    map_id="room",
                    map_version="sha256:measured-grid",
                    physical_datum="measured-datum",
                ),
                *(
                    FrameDeclaration(
                        "body", "body", "forward_left_up", "m", SESSION, device, 1, f"pose-{device}"
                    )
                    for device in (1, 9)
                ),
            )
        ),
        clock_mappings=(mapping,),
    )
    audit = SessionAuditLog(tmp_path / "audit", SESSION)
    session = RelaySession(
        session_id=SESSION,
        audit_log=audit,
        clock=clock,
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        node_types={9: NodeType.GROUND},
        observation_configuration=config,
    )
    for device, node_type in ((1, "aircraft"), (9, "ground")):
        principal = Principal("adapter", device, ADAPTER_KEY)
        session.process_membership(
            membership_payload(
                action="join",
                event_id=f"join-{device}",
                drone_id=device,
                node_type=node_type,
                capabilities=["ground_drive"] if device == 9 else ["takeoff"],
            ),
            principal,
        )
        observation = {
            "v": 1,
            "type": "observation",
            "session": SESSION,
            "event_id": f"pose-{device}",
            "device_id": device,
            "connection_epoch": 1,
            "source_id": f"pose-{device}",
            "node_type": node_type,
            "frame": "world",
            "confidence": 0.9,
            "t_capture": {"clock_id": "native", "unit": "ns", "value": 100},
            "t_source_receipt": {"clock_id": "native", "unit": "ns", "value": 101},
            "clock_mapping_id": "camera-clock",
            "payload": {
                "kind": "pose",
                "pose": {
                    "parent_frame": "world",
                    "child_frame": "body",
                    "x_m": 1.25,
                    "y_m": 2.5,
                    "z_m": 0.0 if device == 9 else 1.5,
                    "qx": 0.0,
                    "qy": 0.0,
                    "qz": 0.0,
                    "qw": 1.0,
                },
            },
        }
        assert session.process_observation(observation, principal)[0]["type"] == "observation"
        assert (
            session.process_observation(
                {**observation, "event_id": f"expired-{device}", "connection_epoch": 2}, principal
            )[0]["reason"]
            == "stale_connection_epoch"
        )
        assert (
            session.process_observation(
                {**observation, "event_id": f"wrong-frame-{device}", "frame": "odom"}, principal
            )[0]["type"]
            == "refusal"
        )
    output = tmp_path / "observations.mcap"
    export_audit(audit.path, SESSION, output)
    assert list(read_replay(output, SESSION)) == audit.replay()
    with output.open("rb") as stream:
        messages = list(make_reader(stream).iter_messages())
    poses = [
        (channel, message)
        for _, channel, message in messages
        if channel.topic in {"/sweep/aircraft", "/sweep/ground"}
    ]
    assert len(poses) == 2
    for schema, _, message in messages:
        Draft202012Validator(json.loads(schema.data)).validate(json.loads(message.data))
    for _, message in poses:
        event = json.loads(message.data)["event"]
        assert event["t_capture"] == {"clock_id": "native", "unit": "ns", "value": 100}
        assert message.publish_time == message.log_time == clock.value * 1_000_000
        assert event["payload"]["pose"]["x_m"] == 1.25
    scenes = [
        (schema, json.loads(message.data))
        for schema, channel, message in messages
        if channel.topic == "/sweep/scene"
    ]
    assert len(scenes) == 2
    for schema, scene in scenes:
        assert schema.name == "foxglove.SceneUpdate"
        entity = scene["entities"][0]
        assert entity["frame_id"] == "world"
        assert entity["spheres"][0]["pose"]["position"] == {
            "x": 1.25,
            "y": 2.5,
            "z": 0.0
            if dict((v["key"], v["value"]) for v in entity["metadata"])["device_id"] == "9"
            else 1.5,
        }


def test_mirror_ignores_pending_operations_and_never_mutates_source(tmp_path):
    audit = SessionAuditLog(tmp_path / "audit", SESSION)
    first = audit.append({"type": "state", "t": 1, "session": SESSION, "event_id": "first"})
    tail = CommittedTail(audit.path, SESSION)
    assert tail.read_batch() == [first]
    pending = audit.begin_operation()
    assert tail.read_batch() == []
    second = audit.append_batch(
        [{"type": "refusal", "t": 2, "session": SESSION, "event_id": "second"}],
        operation_id=pending,
    )
    assert tail.read_batch() == second
    assert audit.replay() == [first, *second]


@pytest.mark.parametrize("mutation", ["data", "summary", "truncated", "trailing"])
def test_corrupt_mcap_never_yields_a_partial_replay(tmp_path, mutation):
    audit = SessionAuditLog(tmp_path / "audit", SESSION)
    audit.append({"type": "refusal", "t": 1, "session": SESSION, "event_id": "known-event"})
    output = tmp_path / "audit.mcap"
    export_audit(audit.path, SESSION, output)
    data = bytearray(output.read_bytes())
    if mutation == "data":
        offset = data.index(b"known-event")
        data[offset] = ord("X")
    elif mutation == "summary":
        data[-45] ^= 1
    elif mutation == "truncated":
        data = data[:-20]
    else:
        data += b"extra"
    output.write_bytes(data)
    replay = read_replay(output, SESSION)
    with pytest.raises(ReplayError):
        next(replay)


def test_wrong_session_and_occupied_destination_leave_audit_usable(tmp_path):
    audit = SessionAuditLog(tmp_path / "audit", SESSION)
    audit.append({"type": "state", "t": 1, "session": SESSION, "event_id": "first"})
    output = tmp_path / "audit.mcap"
    output.write_bytes(b"existing")
    with pytest.raises(ReplayError, match="already exists"):
        export_audit(audit.path, SESSION, output)
    with pytest.raises(ReplayError, match="session"):
        CommittedTail(audit.path, "another-session")
    assert (
        audit.append({"type": "state", "t": 2, "session": SESSION, "event_id": "second"})["seq"]
        == 2
    )
    assert output.read_bytes() == b"existing"


@pytest.mark.parametrize("damage", ["checksum", "partial"])
def test_incomplete_or_changed_jsonl_never_publishes_an_export(tmp_path, damage):
    audit = SessionAuditLog(tmp_path / "audit", SESSION)
    audit.append({"type": "state", "t": 1, "session": SESSION, "event_id": "original"})
    encoded = audit.path.read_bytes()
    audit.path.write_bytes(
        encoded.replace(b"original", b"replaced") if damage == "checksum" else encoded[:-2]
    )
    output = tmp_path / "invalid.mcap"
    with pytest.raises(ReplayError):
        export_audit(audit.path, SESSION, output)
    assert not output.exists()


def test_live_tail_refuses_replaced_audit_files(tmp_path):
    audit = SessionAuditLog(tmp_path / "audit", SESSION)
    audit.append({"type": "state", "t": 1, "session": SESSION, "event_id": "original"})
    tail = CommittedTail(audit.path, SESSION)
    assert len(tail.read_batch()) == 1
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(audit.path.read_bytes())
    replacement.replace(audit.path)
    with pytest.raises(ReplayError, match="replaced"):
        tail.read_batch()


def test_recorded_scan_and_tag_fixtures_are_ingested_and_rendered_without_world_registration(
    tmp_path,
):
    from relay.tests.test_observations import local_binding, local_registry

    fixtures = Path(__file__).parents[1] / "relay/tests/fixtures/observation_v1"
    audit = SessionAuditLog(tmp_path / "audit", "demo-1")
    for filename in ("ground-odom-range-scan.json", "camera-tag-observation.json"):
        observation = decode_observation((fixtures / filename).read_bytes())
        accepted = ingest(
            observation.submission,
            t_ingest=observation.t_ingest,
            frames=local_registry(),
            binding=local_binding(),
            mappings={},
            timing=TimingPolicy(25),
        )
        audit.append(accepted.to_mapping())
    output = tmp_path / "sensors.mcap"
    export_audit(audit.path, "demo-1", output)
    assert list(read_replay(output, "demo-1")) == audit.replay()
    with output.open("rb") as stream:
        messages = list(make_reader(stream).iter_messages())
    raw = [
        json.loads(message.data)["event"]
        for _, channel, message in messages
        if channel.topic != "/sweep/scene"
    ]
    assert all(event["t_capture"] is None for event in raw)
    assert raw[0]["payload"]["ranges_m"] == [1.0, None, 2.0]
    scenes = [
        json.loads(message.data)
        for _, channel, message in messages
        if channel.topic == "/sweep/scene"
    ]
    scan, tag = (scene["entities"][0] for scene in scenes)
    assert scan["frame_id"].startswith("local/") and scan["frame_id"].endswith("/odom")
    assert len(scan["spheres"]) == 2
    assert scan["spheres"][0]["pose"]["position"] == pytest.approx(
        {"x": 0.9950041653, "y": -0.0998334166, "z": 0.25}
    )
    assert tag["frame_id"].endswith("/camera")
    assert tag["spheres"][0]["pose"]["position"] == {"x": 0.2, "y": 0.0, "z": 1.0}
    for schema, _, message in messages:
        Draft202012Validator(json.loads(schema.data)).validate(json.loads(message.data))


def test_actual_navigation_publisher_replays_exact_route_map_and_transform_pins(tmp_path):
    from relay.tests.test_navigation_wire import _publisher, _request

    publisher, plan, snapshots, _, _ = _publisher()
    with publisher.command_scope(plan, lambda: snapshots[0]):
        route, pose = publisher.prepare_request(_request(plan))
    audit = SessionAuditLog(tmp_path / "audit", "test-session")
    for event in (route, pose):
        audit.append({key: value for key, value in event.items() if key != "signature"})
    output = tmp_path / "route.mcap"
    export_audit(audit.path, "test-session", output)
    records = list(read_replay(output, "test-session"))
    assert records == audit.replay()
    assert records[0]["event"]["map_sha256"] == "a" * 64
    assert records[0]["event"]["world_transform_sha256"] == "e" * 64
    with output.open("rb") as stream:
        messages = list(make_reader(stream).iter_messages())
    scene = next(
        json.loads(message.data)
        for _, channel, message in messages
        if channel.topic == "/sweep/scene"
    )
    entity = scene["entities"][0]
    assert entity["frame_id"].endswith("/map_enu")
    assert entity["lines"][0]["points"] == [
        {"x": 0.5, "y": 1.5, "z": 1.0},
        {"x": 6.5, "y": 1.5, "z": 1.0},
    ]
    for schema, _, message in messages:
        Draft202012Validator(json.loads(schema.data)).validate(json.loads(message.data))

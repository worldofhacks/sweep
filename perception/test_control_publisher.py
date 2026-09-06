import io
import json
import threading
import time
from dataclasses import replace

import pytest

from perception.control_publisher import (
    ControlPublisher,
    ControlPublisherConfig,
    LiveBinding,
    MonotonicCaptureClock,
    PublisherAuditError,
    PublisherError,
    PublisherOverflowError,
    PublisherTransportError,
    WebSocketPublisherTransport,
    _binding_from_state,
    _decode_message,
    _enqueue_json_line,
    _run_live,
    _run_replay,
    _validate_auth_accepted,
)
from relay.control_frames import ControlLocalizationFrame
from relay.control_localization import ControlLocalizationPins, ControlLocalizationProjector


class RecordingAudit:
    def __init__(self):
        self.events = []
        self.fail = False

    def append(self, event):
        if self.fail:
            raise PublisherError("disk full")
        self.events.append(dict(event))


class FakeTransport:
    def __init__(self, bindings):
        self.bindings = dict(bindings)
        self.authenticated = []
        self.frames = []
        self.failed_current = set()
        self.failed_send = set()
        self.closed = False

    def authenticate(self, drone_id, token, session):
        self.authenticated.append((drone_id, token, session))
        self.failed_current.discard(drone_id)
        return self.bindings[drone_id]

    def current_binding(self, drone_id):
        if drone_id in self.failed_current:
            raise PublisherTransportError("disconnected")
        return self.bindings[drone_id]

    def send(self, drone_id, frame):
        if drone_id in self.failed_send:
            raise PublisherTransportError("send failed")
        self.frames.append((drone_id, dict(frame)))

    def close(self):
        self.closed = True


def config_mapping(
    mode="replay",
    *,
    audit_dir="/private/tmp/sweep-publisher-test-audit",
    drones=(1,),
    queue_limit=8,
):
    return {
        "mode": mode,
        "session": "session-1",
        "websocket_url": None if mode == "replay" else "ws://relay.example/ws",
        "audit_dir": audit_dir,
        "queue_limit": queue_limit,
        "drones": [drone_config(drone_id, mode) for drone_id in drones],
    }


def drone_config(drone_id, mode):
    return {
        "key_environment": f"LOCALIZATION_KEY_{drone_id}",
        "clock_mapping": {
            "capture_clock_id": f"camera-clock-{drone_id}",
            "relay_clock_id": "relay-unix",
            "capture_reference_s": 0,
            "relay_reference_ms": 100_000,
            "milliseconds_per_capture_second": 1_000,
            "max_error_ms": 5,
            "measured": True,
        },
        "live_capture_clock": (
            None
            if mode == "replay"
            else {
                "source": "process_monotonic",
                "boot_id": "boot-current",
                "monotonic_reference_s": 10,
                "capture_reference_s": 0,
            }
        ),
        "fuser": {
            "drone_id": drone_id,
            "connection_epoch": 7 if mode == "replay" else 0,
            "map_id": "map-sha",
            "geometry_id": "geometry-sha",
            "clock_id": f"camera-clock-{drone_id}",
            "tag_source_id": "tag-camera",
            "velocity_source_id": "msdk-velocity",
            "height_source_id": "tof-height",
            "camera_calibration_id": "camera-sha",
            "body_extrinsics_id": "body-sha",
            "position_bounds_map_enu_m": [[-10, 10], [-10, 10], [0, 3]],
            "height_bounds_map_enu_m": [0, 3],
            "max_speed_mps": 0.5,
            "position_variance_bounds_m2": [0.000001, 0.0625],
            "velocity_variance_bounds_m2ps2": [0.000001, 1],
            "height_variance_bounds_m2": [0.000001, 0.0625],
            "production_evidence_verified": True,
        },
    }


def binding(drone_id=1, epoch=7, roster=1, membership="ready"):
    return LiveBinding("session-1", drone_id, epoch, roster, membership)


def environment(*drones):
    return {
        f"LOCALIZATION_KEY_{drone_id}": f"localization-secret-for-drone-{drone_id}-key"
        for drone_id in drones
    }


def tag(event_id="tag", *, drone_id=1, epoch=7, capture_time=0.9):
    return {
        "kind": "tag",
        "drone_id": drone_id,
        "event_id": event_id,
        "connection_epoch": epoch,
        "map_id": "map-sha",
        "geometry_id": "geometry-sha",
        "clock_id": f"camera-clock-{drone_id}",
        "capture_time": capture_time,
        "position_map_enu_m": [1, 2, 1],
        "covariance_map_enu_m2": [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 0.01]],
        "source_id": "tag-camera",
        "camera_calibration_id": "camera-sha",
        "source_verified": True,
        "timing_verified": True,
        "extrinsics": {
            "extrinsics_id": "body-sha",
            "source_id": "tag-camera",
            "matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "capture_time": capture_time,
            "gimbal_time": capture_time,
            "attitude_time": capture_time,
            "measured": True,
        },
    }


def velocity(event_id="velocity", *, drone_id=1, epoch=7, capture_time=0.95):
    return {
        "kind": "velocity",
        "drone_id": drone_id,
        "event_id": event_id,
        "connection_epoch": epoch,
        "map_id": "map-sha",
        "geometry_id": "geometry-sha",
        "clock_id": f"camera-clock-{drone_id}",
        "capture_time": capture_time,
        "velocity_map_enu_mps": [0, 0, 0],
        "covariance_m2ps2": [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 0.01]],
        "source_id": "msdk-velocity",
        "source_verified": True,
        "timing_verified": True,
    }


def height(event_id="height", *, drone_id=1, epoch=7, capture_time=0.97):
    return {
        "kind": "height",
        "drone_id": drone_id,
        "event_id": event_id,
        "connection_epoch": epoch,
        "map_id": "map-sha",
        "geometry_id": "geometry-sha",
        "clock_id": f"camera-clock-{drone_id}",
        "capture_time": capture_time,
        "height_map_enu_m": 1,
        "variance_m2": 0.01,
        "source_id": "tof-height",
        "source_verified": True,
        "timing_verified": True,
    }


def ready_records(*, drone_id=1, epoch=7):
    return [
        tag(drone_id=drone_id, epoch=epoch),
        velocity(drone_id=drone_id, epoch=epoch),
        height(drone_id=drone_id, epoch=epoch),
    ]


def replay_publisher(mapping=None, *, audit=None, run_id="replay-run"):
    config = ControlPublisherConfig.from_mapping(mapping or config_mapping())
    publisher = ControlPublisher(config, audit=audit or RecordingAudit(), run_id=run_id)
    publisher.bind_credentials(environment(*config.drones))
    return publisher


def live_publisher(tmp_path, *, transport=None, drones=(1,), run_id="live-run"):
    mapping = config_mapping(
        "live",
        audit_dir=str(tmp_path / "publisher-audit"),
        drones=drones,
    )
    config = ControlPublisherConfig.from_mapping(mapping)
    bindings = {drone_id: binding(drone_id) for drone_id in drones}
    active_transport = transport or FakeTransport(bindings)
    audit = RecordingAudit()
    publisher = ControlPublisher(
        config,
        active_transport,
        audit=audit,
        boot_identity=lambda: "boot-current",
        run_id=run_id,
    )
    publisher.bind_credentials(environment(*drones))
    return publisher, active_transport, audit


def test_replay_records_use_canonical_wire_and_signing_path():
    publisher = replay_publisher()
    for record in ready_records():
        publisher.enqueue(record)

    event = publisher.publish(1, 1.0)
    frame = ControlLocalizationFrame.parse(event)

    assert frame.wire.control_eligible
    assert frame.wire.status == "ready"
    assert frame.wire.flight_approved is False
    assert frame.wire.connection_epoch == 7
    assert frame.t == 101_000
    assert frame.signature_valid(b"localization-secret-for-drone-1-key")
    assert event["type"] == "control_localization"


def test_published_wire_projects_through_the_canonical_diagnostic_relay():
    publisher = replay_publisher()
    for record in ready_records():
        publisher.enqueue(record)
    frame = ControlLocalizationFrame.parse(publisher.publish(1, 1.0))
    clock_mapping = publisher.config.drones[1].clock_mapping
    projector = ControlLocalizationProjector(
        {
            1: ControlLocalizationPins(
                drone_id=1,
                map_id="map-sha",
                geometry_id="geometry-sha",
                camera_calibration_id="camera-sha",
                body_extrinsics_id="body-sha",
                source_ids=("tag-camera", "msdk-velocity", "tof-height"),
                clock_mapping=clock_mapping,
            )
        },
        relay_clock_id="relay-unix",
        max_clock_error_ms=5,
        max_fix_age_ms=500,
        max_velocity_age_ms=200,
        max_height_age_ms=200,
        max_position_uncertainty_p95_m=0.5,
    )

    pose = projector.project(
        frame.wire,
        authenticated_drone_id=1,
        authenticated_connection_epoch=7,
        now_ms=101_000,
        event_id="relay-pose-1",
        session="session-1",
        previous=None,
    )

    assert pose.position_frame == "map_enu"
    assert pose.flight_approved is False
    assert pose.status == "ready"


def test_replay_cli_is_deterministic_and_never_needs_a_transport():
    lines = []
    for index, record in enumerate(ready_records()):
        lines.append(json.dumps({**record, "now_s": 1.0 + index * 0.01}))
    first = replay_publisher(run_id="ignored-one")
    second = replay_publisher(run_id="ignored-two")
    first_output, second_output = io.StringIO(), io.StringIO()

    _run_replay(first, lines, first_output)
    _run_replay(second, lines, second_output)

    assert first_output.getvalue() == second_output.getvalue()
    assert len(first_output.getvalue().splitlines()) == 3


def test_input_is_copied_before_enqueue_and_caller_mutation_cannot_change_evidence():
    publisher = replay_publisher()
    raw = tag()
    publisher.enqueue(raw)
    raw["position_map_enu_m"][0] = 999
    raw["extrinsics"]["matrix"][0][0] = float("nan")

    event = publisher.publish(1, 1.0)

    wire = ControlLocalizationFrame.parse(event).wire
    assert wire.position_map_enu_m[0] == pytest.approx(1)


def test_queue_overflow_is_durably_refused_without_evicting_old_record():
    audit = RecordingAudit()
    publisher = replay_publisher(config_mapping(queue_limit=1), audit=audit)
    publisher.enqueue(tag("retained"))

    with pytest.raises(PublisherOverflowError, match="no evidence was evicted"):
        publisher.enqueue(tag("refused", capture_time=0.91))

    event = publisher.publish(1, 1.0)
    assert ControlLocalizationFrame.parse(event).wire.position_map_enu_m == pytest.approx((1, 2, 1))
    refusal = [entry for entry in audit.events if entry["type"] == "sensor_refused"]
    assert refusal[-1]["sensor_event_id"] == "refused"
    assert refusal[-1]["reason"] == "queue_full"


def test_audit_failure_prevents_overflow_from_mutating_the_queue():
    audit = RecordingAudit()
    publisher = replay_publisher(config_mapping(queue_limit=1), audit=audit)
    publisher.enqueue(tag("retained"))
    audit.fail = True
    with pytest.raises(PublisherAuditError, match="append failed"):
        publisher.enqueue(tag("overflow", capture_time=0.91))
    audit.fail = False

    event = publisher.publish(1, 1.0)

    assert ControlLocalizationFrame.parse(event).wire.position_map_enu_m == pytest.approx((1, 2, 1))


def test_path_backed_audit_durably_records_admission_and_overflow(tmp_path):
    mapping = config_mapping(audit_dir=str(tmp_path / "audit"), queue_limit=1)
    config = ControlPublisherConfig.from_mapping(mapping)
    publisher = ControlPublisher(config, run_id="audit-run")
    publisher.bind_credentials(environment(1))
    publisher.enqueue(tag("retained"))
    with pytest.raises(PublisherOverflowError):
        publisher.enqueue(tag("overflow", capture_time=0.91))
    publisher.close()

    mirror = next((tmp_path / "audit").glob("*.jsonl"))
    records = [json.loads(line) for line in mirror.read_text().splitlines()]
    events = [record["event"] for record in records]
    assert [event["type"] for event in events] == [
        "publisher_started",
        "sensor_refused",
        "publisher_stopped",
    ]
    assert events[1]["reason"] == "queue_full"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.update({"unknown": True}),
        lambda raw: raw.pop("timing_verified"),
        lambda raw: raw.update({"source_verified": False}),
        lambda raw: raw.update({"position_map_enu_m": [float("nan"), 0, 1]}),
        lambda raw: raw.update({"drone_id": 2**31}),
        lambda raw: raw.update({"connection_epoch": 2**31}),
        lambda raw: raw.update({"kind": "webcam"}),
    ],
)
def test_sensor_schema_is_exact_and_verified(mutate):
    publisher = replay_publisher()
    raw = tag()
    mutate(raw)
    with pytest.raises(PublisherError):
        publisher.enqueue(raw)


def test_malformed_input_is_audited_but_does_not_latch_or_refresh_fuser():
    audit = RecordingAudit()
    publisher = replay_publisher(audit=audit)
    for record in ready_records():
        publisher.enqueue(record)
    with pytest.raises(PublisherError, match="strict JSON"):
        _enqueue_json_line(publisher, '{"drone_id":1,"drone_id":1}', replay=False)

    event = publisher.publish(1, 1.0)

    assert ControlLocalizationFrame.parse(event).wire.status == "ready"
    assert [entry for entry in audit.events if entry["type"] == "input_refused"][-1][
        "reason"
    ] == "invalid_json"


def test_config_schema_is_exact_deeply_copied_and_immutable():
    raw = config_mapping()
    config = ControlPublisherConfig.from_mapping(raw)
    raw["drones"][0]["fuser"]["map_id"] = "mutated"

    assert config.drones[1].fuser.map_id == "map-sha"
    with pytest.raises(TypeError):
        config.drones[2] = config.drones[1]
    unknown = config_mapping()
    unknown["unknown"] = True
    with pytest.raises(PublisherError, match="fields"):
        ControlPublisherConfig.from_mapping(unknown)

    too_many_drones = config_mapping(drones=(1, 2, 3, 4, 5))
    with pytest.raises(PublisherError, match="drones are invalid"):
        ControlPublisherConfig.from_mapping(too_many_drones)


def test_audit_initialization_failure_is_a_typed_refusal(tmp_path):
    not_a_directory = tmp_path / "audit"
    not_a_directory.write_text("occupied")
    config = ControlPublisherConfig.from_mapping(config_mapping(audit_dir=str(not_a_directory)))

    with pytest.raises(PublisherAuditError, match="could not be initialized"):
        ControlPublisher(config, run_id="audit-init-failure")


def test_live_config_requires_dynamic_epoch_and_measured_clock(tmp_path):
    static_epoch = config_mapping("live", audit_dir=str(tmp_path / "audit"))
    static_epoch["drones"][0]["fuser"]["connection_epoch"] = 7
    with pytest.raises(PublisherError, match="unbound epoch"):
        ControlPublisherConfig.from_mapping(static_epoch)

    missing_clock = config_mapping("live", audit_dir=str(tmp_path / "audit"))
    missing_clock["drones"][0]["live_capture_clock"] = None
    with pytest.raises(PublisherError, match="measured capture clock"):
        ControlPublisherConfig.from_mapping(missing_clock)


def test_live_handshake_binds_current_epoch_before_any_evidence(tmp_path):
    publisher, transport, audit = live_publisher(tmp_path)
    for record in ready_records(epoch=7):
        publisher.enqueue(record)

    event = publisher.publish_live(1, 11.0)
    frame = ControlLocalizationFrame.parse(event)

    assert transport.authenticated == [(1, "localization-secret-for-drone-1-key", "session-1")]
    assert frame.wire.connection_epoch == 7
    assert frame.wire.status == "ready"
    assert frame.signature_valid(b"localization-secret-for-drone-1-key")
    assert any(
        entry["type"] == "epoch_bound" and entry["connection_epoch"] == 7 for entry in audit.events
    )


def test_live_epoch_change_rebuilds_fuser_and_old_evidence_cannot_cross(tmp_path):
    publisher, transport, audit = live_publisher(tmp_path)
    for record in ready_records(epoch=7):
        publisher.enqueue(record)
    assert ControlLocalizationFrame.parse(publisher.publish_live(1, 11.0)).wire.status == "ready"

    transport.bindings[1] = binding(epoch=8, roster=2)
    rebound = ControlLocalizationFrame.parse(publisher.publish_live(1, 11.1)).wire

    assert rebound.connection_epoch == 8
    assert rebound.status == "hold"
    assert rebound.position_map_enu_m is None
    epochs = [entry["connection_epoch"] for entry in audit.events if entry["type"] == "epoch_bound"]
    assert epochs == [7, 8]


def test_old_epoch_queue_is_processed_as_refused_after_rebind(tmp_path):
    publisher, transport, audit = live_publisher(tmp_path)
    transport.bindings[1] = binding(epoch=8, roster=2)
    publisher.enqueue(tag(epoch=7))

    rebound = ControlLocalizationFrame.parse(publisher.publish_live(1, 11.0)).wire

    assert rebound.connection_epoch == 8
    assert rebound.status == "hold"
    processed = [entry for entry in audit.events if entry["type"] == "sensor_processed"]
    assert processed[-1]["reason"] == "connection_epoch_mismatch"


def test_transport_disconnect_reauthenticates_but_does_not_reuse_a_stale_epoch(tmp_path):
    publisher, transport, _audit = live_publisher(tmp_path)
    transport.failed_current.add(1)
    transport.bindings[1] = binding(epoch=9, roster=3)

    event = publisher.publish_live(1, 11.0)

    assert transport.authenticated[-1] == (
        1,
        "localization-secret-for-drone-1-key",
        "session-1",
    )
    assert ControlLocalizationFrame.parse(event).wire.connection_epoch == 9


def test_send_failure_is_explicit_and_next_drone_remains_independent(tmp_path):
    publisher, transport, audit = live_publisher(tmp_path, drones=(1, 2))
    transport.failed_send.add(1)

    with pytest.raises(PublisherTransportError, match="not delivered"):
        publisher.publish_live(1, 11.0)
    second = publisher.publish_live(2, 11.0)

    assert ControlLocalizationFrame.parse(second).wire.drone_id == 2
    refused = [entry for entry in audit.events if entry["type"] == "frame_refused"]
    assert refused[-1]["drone_id"] == 1
    assert [drone for drone, _frame in transport.frames] == [2]


def test_live_runner_ages_during_input_stall_and_isolates_aircraft_failure(tmp_path):
    send_blocked = threading.Event()
    release_send = threading.Event()

    class BlockingTransport(FakeTransport):
        def send(self, drone_id, frame):
            if drone_id == 1:
                send_blocked.set()
                release_send.wait(timeout=2)
                raise PublisherTransportError("blocked send failed")
            super().send(drone_id, frame)

    transport = BlockingTransport({1: binding(1), 2: binding(2)})
    publisher, transport, _audit = live_publisher(
        tmp_path,
        transport=transport,
        drones=(1, 2),
    )
    input_started = threading.Event()
    finish_input = threading.Event()
    failures = []
    tick = [10.0]

    def stalled_lines():
        input_started.set()
        finish_input.wait(timeout=2)
        return
        yield  # pragma: no cover - this makes the function a blocking iterator

    def clock():
        tick[0] += 0.5
        return tick[0]

    def run():
        try:
            _run_live(publisher, stalled_lines(), clock=clock, wait=lambda _: time.sleep(0.001))
        except Exception as error:  # pragma: no cover - asserted through failures
            failures.append(error)

    runner = threading.Thread(target=run)
    runner.start()
    assert input_started.wait(timeout=1)
    assert send_blocked.wait(timeout=1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not any(
        drone_id == 2 and frame["localization_status"] == "land"
        for drone_id, frame in transport.frames
    ):
        time.sleep(0.005)
    release_send.set()
    finish_input.set()
    runner.join(timeout=2)

    assert not runner.is_alive()
    assert failures == []
    assert all(drone_id == 2 for drone_id, _frame in transport.frames)
    assert any(frame["localization_status"] == "land" for _, frame in transport.frames)


def test_capture_clock_uses_injected_boot_identity_and_rejects_regression():
    clock = MonotonicCaptureClock("process_monotonic", "boot", 10, 2)
    clock.verify_boot(lambda: "boot")
    assert clock.capture_time(11.5) == pytest.approx(3.5)
    with pytest.raises(PublisherError, match="different system boot"):
        clock.verify_boot(lambda: "other")
    with pytest.raises(PublisherError, match="precedes"):
        clock.capture_time(9.9)


def test_live_publisher_rejects_monotonic_regression_between_frames(tmp_path):
    publisher, _transport, _audit = live_publisher(tmp_path)
    publisher.publish_live(1, 11.0)

    with pytest.raises(PublisherError, match="regressed"):
        publisher.publish_live(1, 10.9)


def test_live_event_ids_are_bounded_and_unique_across_process_runs(tmp_path):
    first, _, _ = live_publisher(tmp_path / "one", run_id="run-one")
    second, _, _ = live_publisher(tmp_path / "two", run_id="run-two")

    first_id = first.publish_live(1, 11.0)["event_id"]
    second_id = second.publish_live(1, 11.0)["event_id"]

    assert first_id != second_id
    assert len(first_id) <= 128
    assert len(second_id) <= 128


def test_auth_acceptance_and_state_binding_are_strict():
    accepted = {
        "v": 1,
        "t": 100,
        "type": "auth.accepted",
        "event_id": "auth-1",
        "session": "session-1",
        "source": "localization",
        "drone_id": 1,
        "node": None,
    }
    _validate_auth_accepted(accepted, 1, "session-1")
    with pytest.raises(PublisherTransportError):
        _validate_auth_accepted(accepted | {"extra": True}, 1, "session-1")
    with pytest.raises(PublisherTransportError):
        _validate_auth_accepted(accepted | {"source": "adapter"}, 1, "session-1")

    state = {
        "v": 1,
        "t": 100,
        "type": "state",
        "event_id": "state-1",
        "session": "session-1",
        "roster_version": 4,
        "drones": [{"drone_id": 1, "connection_epoch": 9, "membership": "degraded"}],
    }
    assert _binding_from_state(state, 1, "session-1") == binding(1, 9, 4, "degraded")
    with pytest.raises(PublisherTransportError, match="not active"):
        _binding_from_state(
            {
                **state,
                "drones": [{"drone_id": 1, "connection_epoch": 9, "membership": "disconnected"}],
            },
            1,
            "session-1",
        )
    with pytest.raises(PublisherTransportError, match="identities"):
        _binding_from_state(
            {**state, "drones": [state["drones"][0], state["drones"][0]]},
            1,
            "session-1",
        )


def test_message_decoder_rejects_duplicate_keys_nan_and_oversize():
    with pytest.raises(PublisherTransportError, match="strict JSON"):
        _decode_message('{"v":1,"v":1}')
    with pytest.raises(PublisherTransportError, match="strict JSON"):
        _decode_message('{"v":NaN}')
    with pytest.raises(PublisherTransportError, match="strict JSON"):
        _decode_message('{"v":1e999}')
    with pytest.raises(PublisherTransportError, match="too large"):
        _decode_message("x" * 1_048_577)
    with pytest.raises(PublisherTransportError, match="strict JSON"):
        _decode_message('{"nested":' * 10_000 + "0" + "}" * 10_000)


def test_credentials_are_required_and_never_written_to_audit():
    audit = RecordingAudit()
    config = ControlPublisherConfig.from_mapping(config_mapping())
    publisher = ControlPublisher(config, audit=audit, run_id="secret-test")
    with pytest.raises(PublisherError, match="not bound"):
        publisher.publish(1, 1.0)
    with pytest.raises(PublisherError, match="missing"):
        publisher.bind_credentials({})
    with pytest.raises(PublisherError, match="valid UTF-8"):
        publisher.bind_credentials({"LOCALIZATION_KEY_1": "x" * 31 + "\ud800"})
    publisher.bind_credentials(environment(1))
    publisher.publish(1, 1.0)

    assert "localization-secret-for-drone-1-key" not in json.dumps(audit.events)


@pytest.mark.parametrize(
    "url",
    [
        "https://relay.example/ws",
        "ws://user@relay.example/ws",
        "ws://relay.example/other",
        "ws://relay example/ws",
        "ws://relay.example:0/ws",
        "ws://relay.example:70000/ws",
        "ws://relay.example/ws?token=secret",
    ],
)
def test_live_websocket_url_is_bounded_credential_free_and_exact(tmp_path, url):
    raw = config_mapping("live", audit_dir=str(tmp_path / "audit"))
    raw["websocket_url"] = url

    with pytest.raises(PublisherError, match="websocket_url"):
        ControlPublisherConfig.from_mapping(raw)


def test_replay_config_requires_audit_directory_and_rejects_live_only_fields(tmp_path):
    mapping = config_mapping(audit_dir=str(tmp_path / "audit"))
    assert ControlPublisherConfig.from_mapping(mapping).audit_dir == tmp_path / "audit"
    live_clock = config_mapping()
    live_clock["drones"][0]["live_capture_clock"] = {
        "source": "process_monotonic",
        "boot_id": "boot",
        "monotonic_reference_s": 0,
        "capture_reference_s": 0,
    }
    with pytest.raises(PublisherError, match="no live clock"):
        ControlPublisherConfig.from_mapping(live_clock)


def test_fuser_recovery_accepts_new_same_kind_evidence_while_other_sources_are_fresh():
    publisher = replay_publisher()
    for record in ready_records():
        publisher.enqueue(record)
    assert ControlLocalizationFrame.parse(publisher.publish(1, 1.0)).wire.status == "ready"

    bad = velocity("wrong-map", capture_time=1.1)
    bad["map_id"] = "other"
    publisher.enqueue(bad)
    refused = ControlLocalizationFrame.parse(publisher.publish(1, 1.1)).wire
    assert refused.status == "hold"

    publisher.enqueue(velocity("new-velocity", capture_time=1.11))
    recovered = ControlLocalizationFrame.parse(publisher.publish(1, 1.11)).wire
    assert recovered.status == "ready"


def test_live_config_object_can_be_reused_without_mutating_epoch_template(tmp_path):
    config = ControlPublisherConfig.from_mapping(
        config_mapping("live", audit_dir=str(tmp_path / "audit"))
    )
    transport = FakeTransport({1: binding(epoch=12)})
    publisher = ControlPublisher(
        config,
        transport,
        audit=RecordingAudit(),
        boot_identity=lambda: "boot-current",
        run_id="immutable-template",
    )
    publisher.bind_credentials(environment(1))

    assert config.drones[1].fuser.connection_epoch == 0
    assert ControlLocalizationFrame.parse(publisher.publish_live(1, 11)).wire.connection_epoch == 12


def test_binding_value_rejects_boolean_and_inactive_membership():
    with pytest.raises(ValueError):
        LiveBinding("session-1", 1, True, 1, "ready")
    with pytest.raises(PublisherTransportError):
        LiveBinding("session-1", 1, 1, 1, "leaving")


def test_clock_mapping_and_fuser_clock_reference_must_match():
    raw = config_mapping()
    raw["drones"][0]["clock_mapping"]["capture_clock_id"] = "other"
    with pytest.raises(PublisherError, match="configuration is invalid"):
        ControlPublisherConfig.from_mapping(raw)


def test_live_send_uses_current_status_without_independent_safety_projection(tmp_path):
    publisher, _transport, _audit = live_publisher(tmp_path)
    for record in ready_records(epoch=7):
        publisher.enqueue(record)
    frame = ControlLocalizationFrame.parse(publisher.publish_live(1, 11.0))

    assert frame.wire.status == "ready"
    assert frame.wire.control_eligible is True
    assert frame.wire.flight_approved is False
    assert not hasattr(publisher, "control_pose")
    assert not hasattr(publisher, "apply")


def test_config_identity_excludes_secret_values_and_changes_with_pins():
    config = ControlPublisherConfig.from_mapping(config_mapping())
    changed_mapping = config_mapping()
    changed_mapping["drones"][0]["fuser"]["map_id"] = "new-map"
    changed = ControlPublisherConfig.from_mapping(changed_mapping)

    assert len(config.identity_sha256) == 64
    assert config.identity_sha256 != changed.identity_sha256
    assert "secret" not in config.identity_sha256


def test_close_is_idempotent_and_closes_transport(tmp_path):
    publisher, transport, _audit = live_publisher(tmp_path)

    publisher.close()
    publisher.close()

    assert transport.closed
    with pytest.raises(PublisherError, match="closed"):
        publisher.publish_live(1, 11)


def test_websocket_transport_cannot_reopen_after_shutdown():
    transport = WebSocketPublisherTransport("ws://relay.example/ws")
    transport.close()

    with pytest.raises(PublisherTransportError, match="closed"):
        transport.authenticate(1, "localization-secret-for-drone-1-key", "session-1")


def test_live_current_state_roster_only_change_does_not_reset_fuser(tmp_path):
    publisher, transport, _audit = live_publisher(tmp_path)
    for record in ready_records(epoch=7):
        publisher.enqueue(record)
    assert ControlLocalizationFrame.parse(publisher.publish_live(1, 11)).wire.status == "ready"
    transport.bindings[1] = replace(binding(), roster_version=2)

    retained = ControlLocalizationFrame.parse(publisher.publish_live(1, 11.1)).wire

    assert retained.connection_epoch == 7
    assert retained.position_map_enu_m is not None

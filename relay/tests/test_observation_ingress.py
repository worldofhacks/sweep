from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace

import pytest
from fastapi.testclient import TestClient

from relay.app import RelayRuntime, create_app
from relay.auth import Principal
from relay.observation_ingress import ObservationConfiguration
from relay.observations import FrameDeclaration, FrameRegistry, SourceBinding
from relay.settings import RelaySettings
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, SESSION, MutableClock, membership_payload


def configuration() -> ObservationConfiguration:
    return ObservationConfiguration(
        bindings=(SourceBinding(SESSION, 1, 1, "lidar", "ground", ("lidar",), ("status",)),),
        frames=FrameRegistry(
            (FrameDeclaration("lidar", "lidar", "forward_left_up", "m", SESSION, 1, 1, "lidar"),)
        ),
    )


def observation(**changes: object) -> dict[str, object]:
    return {
        "v": 1,
        "type": "observation",
        "event_id": "scan-1",
        "session": SESSION,
        "device_id": 1,
        "connection_epoch": 1,
        "source_id": "lidar",
        "node_type": "ground",
        "frame": "lidar",
        "confidence": 0.0,
        "t_capture": None,
        "t_source_receipt": {"clock_id": "hal-monotonic", "unit": "ns", "value": 100},
        "clock_mapping_id": None,
        "payload": {
            "kind": "status",
            "code": "unregistered",
            "detail": "No world registration",
            "capabilities": [],
        },
        **changes,
    }


@pytest.fixture
def settings(tmp_path):
    return RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        log_dir=tmp_path,
        observation_configuration=configuration(),
    )


def test_websocket_observation_is_stamped_audited_and_delivered_to_console(settings):
    clock = MutableClock()
    app = create_app(settings, clock=clock)
    with TestClient(app) as client, client.websocket_connect(f"/ws/{SESSION}") as console:
        console.send_json(
            {"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()}
        )
        console.receive_json()
        console.receive_json()
        with client.websocket_connect(f"/ws/{SESSION}") as adapter:
            adapter.send_json(
                {
                    "v": 1,
                    "type": "auth",
                    "source": "adapter",
                    "drone_id": 1,
                    "token": ADAPTER_KEY.decode(),
                }
            )
            adapter.receive_json()
            adapter.receive_json()
            adapter.send_json(membership_payload(action="join", event_id="join-1"))
            adapter.send_json(observation())
            for _ in range(20):
                event = console.receive_json()
                if event["type"] == "observation":
                    break
            else:
                pytest.fail("observation was not delivered")
            assert event == {**observation(), "t_ingest": clock.value}
            audit = app.state.relay_runtime.session(SESSION).audit_log.replay()
            assert any(row["event"] == event for row in audit)


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"device_id": 2}, "drone_identity_mismatch"),
        ({"session": "other"}, "session_mismatch"),
        ({"connection_epoch": 2}, "stale_connection_epoch"),
        ({"source_id": "camera"}, "source_not_configured"),
        ({"node_type": "aircraft"}, "source_binding_mismatch"),
        ({"frame": "world"}, "frame_not_authorized"),
        ({"t_ingest": 1}, "invalid_observation"),
    ],
)
def test_authenticated_producer_cannot_change_host_identity_or_stamp(settings, changes, reason):
    runtime = RelayRuntime(settings, clock=MutableClock())
    session = runtime.session(SESSION)
    principal = Principal("adapter", 1, ADAPTER_KEY)
    session.process_frame(membership_payload(action="join", event_id="join-1"), principal)
    events = session.process_frame(observation(**changes), principal)
    assert events[0]["reason"] == reason
    assert not any(row["event"]["type"] == "observation" for row in session.audit_log.replay())


@pytest.mark.parametrize(
    "encoded",
    [
        json.dumps(observation()).replace('"device_id": 1', '"device_id": 2, "device_id": 1'),
        " " * 65_536 + json.dumps(observation()),
        "[" * 40 + "]" * 40,
    ],
    ids=["duplicate-identity", "oversized", "deeply-nested"],
)
def test_websocket_rejects_duplicate_oversized_and_deep_json(settings, encoded):
    with TestClient(create_app(settings, clock=MutableClock())) as client:
        with client.websocket_connect(f"/ws/{SESSION}") as socket:
            socket.send_json(
                {
                    "v": 1,
                    "type": "auth",
                    "source": "adapter",
                    "drone_id": 1,
                    "token": ADAPTER_KEY.decode(),
                }
            )
            socket.receive_json()
            socket.receive_json()
            socket.send_text(encoded)
            for _ in range(20):
                event = socket.receive_json()
                if event["type"] == "refusal":
                    break
            else:
                pytest.fail("malformed event was not refused")
            assert event["reason"] == "invalid_json"


def test_membership_replay_rate_and_clock_are_checked_before_audit(settings):
    clock = MutableClock()
    session = RelayRuntime(settings, clock=clock).session(SESSION)
    principal = Principal("adapter", 1, ADAPTER_KEY)
    assert session.process_frame(observation(), principal)[0]["type"] == "refusal"
    session.process_frame(membership_payload(action="join", event_id="join-1"), principal)
    assert session.process_frame(observation(), principal)[0]["type"] == "observation"
    clock.advance(10)
    assert session.process_frame(observation(), principal)[0]["reason"] == "replayed_observation"
    assert (
        session.process_frame(observation(event_id="scan-2"), principal)[0]["type"] == "observation"
    )
    assert (
        session.process_frame(observation(event_id="scan-3"), principal)[0]["reason"]
        == "observation_rate_limited"
    )
    clock.advance(10)
    old = {"clock_id": "hal-monotonic", "unit": "ns", "value": 99}
    assert (
        session.process_frame(observation(t_source_receipt=old), principal)[0]["reason"]
        == "stale_observation"
    )
    changed = {"clock_id": "other", "unit": "ns", "value": 101}
    assert (
        session.process_frame(observation(t_source_receipt=changed), principal)[0]["reason"]
        == "source_clock_changed"
    )
    assert (
        len([row for row in session.audit_log.replay() if row["event"]["type"] == "observation"])
        == 2
    )


def test_observations_fan_out_only_to_console(settings):
    async def exercise():
        runtime = RelayRuntime(settings)
        runtime.session(SESSION)
        subscriptions = {
            source: await runtime.subscribe(SESSION, Principal(source, device, key))
            for source, device, key in (
                ("console", None, CONSOLE_KEY),
                ("keyboard", None, CONSOLE_KEY),
                ("webcam", None, CONSOLE_KEY),
                ("language", None, CONSOLE_KEY),
                ("adapter", 1, ADAPTER_KEY),
                ("localization", 1, ADAPTER_KEY),
            )
        }
        event = {**observation(), "t_ingest": 1000}
        await runtime.publish(SESSION, [event])
        assert subscriptions.pop("console").queue.get_nowait().event == event
        assert all(subscription.queue.empty() for subscription in subscriptions.values())

    asyncio.run(exercise())


def test_host_configuration_loads_and_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "observations.json"
    document = asdict(configuration())
    document["frames"] = document["frames"]["declarations"]
    path.write_text(json.dumps(document))
    assert ObservationConfiguration.load(path) == configuration()
    settings = RelaySettings.from_env(
        {"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(), "SWEEP_OBSERVATIONS_FILE": str(path)}
    )
    assert settings.observation_configuration == configuration()
    path.write_text('{"bindings": [], "bindings": []}')
    with pytest.raises(ValueError, match="duplicate"):
        ObservationConfiguration.load(path)


def test_unconfigured_ingress_refuses_and_other_sources_cannot_submit(settings):
    session = RelayRuntime(
        replace(settings, observation_configuration=None), clock=MutableClock()
    ).session(SESSION)
    principal = Principal("adapter", 1, ADAPTER_KEY)
    session.process_frame(membership_payload(action="join", event_id="join-1"), principal)
    assert session.process_frame(observation(), principal)[0]["reason"] == "source_not_configured"
    assert (
        session.process_frame(observation(), Principal("console", None, CONSOLE_KEY))[0]["reason"]
        == "frame_not_allowed"
    )

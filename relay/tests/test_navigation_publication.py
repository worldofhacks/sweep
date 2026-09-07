from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from planner.models import Position
from relay.app import RelayRuntime
from relay.auth import Principal, verify_event_signature
from relay.autonomy import AutonomyComposition, AutonomyConfig
from relay.settings import RelaySettings
from relay.tests.test_navigation_wire import NODE_KEY, _publisher, _request
from tests.autonomy_fixtures import planning_config, replace_aircraft, safety_config


def test_retained_tracking_updates_reach_only_the_commanded_phone(tmp_path, monkeypatch):
    publisher, plan, snapshots, poses, clock = _publisher()
    request = _request(plan)
    with publisher.command_scope(plan, lambda: snapshots[0]):
        publisher.prepare_request(request)
    publisher.activate(request.command_id)
    composition = AutonomyComposition(AutonomyConfig(planning_config(), safety_config()))
    runtime = RelayRuntime(
        RelaySettings(
            relay_token=b"console-key-for-navigation-test-01",
            adapter_keys={1: NODE_KEY, 2: b"other-node-key-for-navigation-001"},
            log_dir=tmp_path,
        ),
        clock=clock,
        navigation_events=composition.navigation_events,
    )
    composition.bind(runtime)
    session = runtime.session("test-session")
    monkeypatch.setattr(session, "control_pose", lambda _: poses[0])
    composition._sessions["test-session"] = SimpleNamespace(navigation_wire=publisher)
    poses[0] = replace(poses[0], t=100_000, x_mm=600)
    snapshots[0] = replace_aircraft(snapshots[0], 1, pose=Position(0.6, 1.5, 1.0))

    async def exercise():
        phone = await runtime.subscribe("test-session", Principal("adapter", 1, NODE_KEY))
        other = await runtime.subscribe(
            "test-session", Principal("adapter", 2, b"other-node-key-for-navigation-001")
        )
        console = await runtime.subscribe(
            "test-session", Principal("console", None, b"console-key-for-navigation-test-01")
        )
        event = {"type": "control_pose", "drone_id": 1}
        await runtime.publish("test-session", [event])
        assert phone.queue.get_nowait().event == event
        update = phone.queue.get_nowait().event
        assert update["type"] == "navigation_pose"
        assert update["command_id"] == request.command_id
        assert update["x_mm"] == 600
        assert verify_event_signature(
            {key: value for key, value in update.items() if key != "signature"},
            update["signature"],
            NODE_KEY,
        )
        assert other.queue.empty()
        assert console.queue.empty()
        await runtime.publish(
            "test-session",
            [
                {
                    "type": "lifecycle",
                    "status": "invalidated",
                    "intent_id": plan.intent_id,
                }
            ],
        )
        assert publisher.update(poses[0]) == []

    asyncio.run(exercise())

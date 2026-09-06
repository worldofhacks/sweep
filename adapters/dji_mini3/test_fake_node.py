from __future__ import annotations

import asyncio

from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig
from planner.models import CommandOperation
from relay.contracts import CommandFrame


def test_periodic_telemetry_waits_for_own_admission_but_terminal_ack_has_fresh_state() -> None:
    node = FakeNode(
        FakeNodeConfig(
            relay_url="ws://relay.test",
            session="session-1",
            drone_id=1,
            token="token",
            adapter_id="node-1",
        )
    )

    async def queued_frames() -> list[dict[str, object]]:
        node._outbound = asyncio.Queue()
        node._connection_epoch = 1
        node._enqueue_periodic_telemetry()
        first_periodic = node._outbound.get_nowait()
        node._enqueue_periodic_telemetry()
        node._enqueue_periodic_telemetry()
        node._release_admitted_periodic_telemetry(
            {
                "drones": [
                    {
                        "drone_id": 2,
                        "connection_epoch": 1,
                        "last_seen_at": first_periodic["t"],
                    },
                    {
                        "drone_id": 1,
                        "connection_epoch": 2,
                        "last_seen_at": first_periodic["t"],
                    },
                ]
            }
        )
        assert node._outbound.empty()
        node._enqueue_periodic_telemetry()
        assert node._outbound.empty()
        node._release_admitted_periodic_telemetry(
            {
                "drones": [
                    {
                        "drone_id": 1,
                        "connection_epoch": 1,
                        "last_seen_at": first_periodic["t"],
                    }
                ]
            }
        )
        assert node._outbound.empty()
        node._enqueue_periodic_telemetry()
        node._finish_command(
            CommandFrame(
                v=1,
                t=1,
                type="command",
                event_id="command-event",
                session="session-1",
                command_id="command-1",
                intent_id="intent-1",
                roster_version=1,
                drone_id=1,
                connection_epoch=1,
                seq=1,
                issued_at=1,
                ttl_ms=1_000,
                operation=CommandOperation.TAKEOFF,
                args={"z_mm": 1_000},
                signature="signature",
            )
        )
        return [node._outbound.get_nowait() for _ in range(node._outbound.qsize())]

    frames = asyncio.run(queued_frames())

    assert [(frame["type"], frame.get("state"), frame.get("status")) for frame in frames] == [
        ("telemetry", "landed", None),
        ("telemetry", "hovering", None),
        ("acknowledgement", None, "completed"),
    ]


def test_refused_periodic_telemetry_frees_the_next_scheduled_sample() -> None:
    node = FakeNode(
        FakeNodeConfig(
            relay_url="ws://relay.test",
            session="session-1",
            drone_id=1,
            token="token",
            adapter_id="node-1",
        )
    )

    async def queued_frames() -> list[dict[str, object]]:
        node._outbound = asyncio.Queue()
        node._connection_epoch = 1
        node._enqueue_periodic_telemetry()
        refused = node._outbound.get_nowait()
        node._release_refused_periodic_telemetry()
        assert node._outbound.empty()
        node._enqueue_periodic_telemetry()
        return [refused, node._outbound.get_nowait()]

    refused, replacement = asyncio.run(queued_frames())

    assert replacement["type"] == "telemetry"
    assert replacement["event_id"] != refused["event_id"]
    assert replacement["t"] >= refused["t"]

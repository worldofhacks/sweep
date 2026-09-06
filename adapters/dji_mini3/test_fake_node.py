from __future__ import annotations

import asyncio

from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig
from planner.models import CommandOperation
from relay.contracts import CommandFrame


def test_periodic_telemetry_is_bounded_but_terminal_ack_has_fresh_state() -> None:
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
        node._enqueue_periodic_telemetry()
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

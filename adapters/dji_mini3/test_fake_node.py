from __future__ import annotations

import asyncio

from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig


def test_telemetry_does_not_accumulate_while_the_writer_is_behind() -> None:
    node = FakeNode(
        FakeNodeConfig(
            relay_url="ws://relay.test",
            session="session-1",
            drone_id=1,
            token="token",
            adapter_id="node-1",
        )
    )

    async def queued_frames() -> int:
        node._outbound = asyncio.Queue()
        node._enqueue_telemetry()
        node._enqueue_telemetry()
        return node._outbound.qsize()

    assert asyncio.run(queued_frames()) == 1

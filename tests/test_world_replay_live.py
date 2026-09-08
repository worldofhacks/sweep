import asyncio
import json
import struct

from websockets.asyncio.client import connect

from relay.audit import SessionAuditLog
from relay.tests.conftest import SESSION
from tools.world_replay import read_replay
from tools.world_replay_live import FoxgloveMirror, run_mirror


def test_live_subscriber_receives_committed_audit_and_cannot_publish_control(tmp_path):
    audit = SessionAuditLog(tmp_path / "audit", SESSION)
    audit.append({"type": "state", "t": 1, "session": SESSION, "event_id": "first"})

    async def scenario():
        async with FoxgloveMirror(audit.path, SESSION, port=0) as mirror:
            async with connect(
                f"ws://127.0.0.1:{mirror.port}", subprotocols=["foxglove.websocket.v1"]
            ) as client:
                info = json.loads(await client.recv())
                assert info["capabilities"] == []
                channels = json.loads(await client.recv())["channels"]
                channel = next(
                    channel for channel in channels if channel["topic"] == "/sweep/safety"
                )
                await client.send(
                    json.dumps(
                        {
                            "op": "subscribe",
                            "subscriptions": [{"id": 7, "channelId": channel["id"]}],
                        }
                    )
                )
                record = audit.append(
                    {
                        "type": "refusal",
                        "t": 2,
                        "session": SESSION,
                        "event_id": "held",
                        "reason": "pose_missing",
                    }
                )
                message = await asyncio.wait_for(client.recv(), timeout=2)
                assert struct.unpack("<BIQ", message[:13]) == (1, 7, 2_000_000)
                assert json.loads(message[13:]) == record
                await client.send(json.dumps({"op": "advertise", "channels": []}))
                await client.wait_closed()
                assert client.close_code == 1008
        assert (
            audit.append({"type": "state", "t": 3, "session": SESSION, "event_id": "after-mirror"})[
                "seq"
            ]
            == 3
        )

    asyncio.run(scenario())


def test_live_recording_follows_appends_until_bounded_shutdown(tmp_path):
    audit = SessionAuditLog(tmp_path / "audit", SESSION)
    audit.append({"type": "state", "t": 1, "session": SESSION, "event_id": "first"})
    output = tmp_path / "live.mcap"

    async def scenario():
        task = asyncio.create_task(run_mirror(audit.path, SESSION, output, port=0, seconds=1))
        await asyncio.sleep(0.1)
        audit.append({"type": "refusal", "t": 2, "session": SESSION, "event_id": "second"})
        await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())
    assert list(read_replay(output, SESSION)) == audit.replay()


def test_unread_socket_cannot_backpressure_audit_or_another_subscriber(tmp_path):
    audit = SessionAuditLog(tmp_path / "audit", SESSION)
    audit.append({"type": "state", "t": 1, "session": SESSION, "event_id": "first"})

    async def scenario():
        async with FoxgloveMirror(audit.path, SESSION, port=0) as mirror:
            url = f"ws://127.0.0.1:{mirror.port}"
            async with (
                connect(
                    url, subprotocols=["foxglove.websocket.v1"], max_queue=1, close_timeout=0.1
                ) as slow,
                connect(url, subprotocols=["foxglove.websocket.v1"]) as healthy,
            ):
                for client in (slow, healthy):
                    await client.recv()
                    channels = json.loads(await client.recv())["channels"]
                    topic = "/sweep/roster" if client is slow else "/sweep/safety"
                    channel = next(channel for channel in channels if channel["topic"] == topic)
                    await client.send(
                        json.dumps(
                            {
                                "op": "subscribe",
                                "subscriptions": [{"id": 1, "channelId": channel["id"]}],
                            }
                        )
                    )

                def append_during_backpressure():
                    for index in range(24):
                        audit.append(
                            {
                                "type": "state",
                                "t": index + 2,
                                "session": SESSION,
                                "event_id": f"large-{index}",
                                "detail": "x" * 500_000,
                            }
                        )
                    return audit.append(
                        {
                            "type": "refusal",
                            "t": 30,
                            "session": SESSION,
                            "event_id": "healthy",
                            "reason": "pose_missing",
                        }
                    )

                record = await asyncio.wait_for(
                    asyncio.to_thread(append_during_backpressure), timeout=5
                )
                message = await asyncio.wait_for(healthy.recv(), timeout=3)
                assert json.loads(message[13:]) == record
                assert record["seq"] == 26

    asyncio.run(scenario())
    assert audit.last_sequence == 26

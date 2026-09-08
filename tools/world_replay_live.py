"""Serve committed audit data through a read-only localhost Foxglove connection."""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from tools.world_replay import (
    TOPICS,
    CommittedTail,
    Recording,
    ReplayError,
    channel_name,
    channel_schema,
    timestamp_ns,
)
from tools.world_replay_scene import SCENE_SCHEMA, scene_update

MAX_CLIENTS = 4
SEND_TIMEOUT_S = 0.25


class FoxgloveMirror:
    def __init__(self, audit: Path, session: str, *, port: int = 8765):
        self.audit, self.session, self.port = audit, session, port
        self.clients = 0
        self.channels = [
            {
                "id": index,
                "topic": f"/sweep/{name}",
                "encoding": "json",
                "schemaName": f"sweep.{name}.v1",
                "schemaEncoding": "jsonschema",
                "schema": channel_schema(name).decode(),
            }
            for index, name in enumerate(TOPICS, 1)
        ]
        self.channels.append(
            {
                "id": len(TOPICS) + 1,
                "topic": "/sweep/scene",
                "encoding": "json",
                "schemaName": "foxglove.SceneUpdate",
                "schemaEncoding": "jsonschema",
                "schema": SCENE_SCHEMA.decode(),
            }
        )

    async def __aenter__(self):
        self.server = await serve(
            self._client,
            "127.0.0.1",
            self.port,
            subprotocols=["foxglove.websocket.v1"],
            max_size=8192,
            max_queue=4,
            write_limit=64 << 10,
            close_timeout=0.25,
            compression=None,
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()

    async def _send(self, client, message):
        async with asyncio.timeout(SEND_TIMEOUT_S):
            await client.send(message)

    async def _client(self, client):
        if self.clients >= MAX_CLIENTS:
            await client.close(1013, "mirror client limit")
            return
        self.clients += 1
        tasks = []
        try:
            await self._send(
                client,
                json.dumps(
                    {
                        "op": "serverInfo",
                        "name": "Sweep audit mirror",
                        "capabilities": [],
                        "sessionId": self.session,
                    }
                ),
            )
            await self._send(client, json.dumps({"op": "advertise", "channels": self.channels}))
            subscriptions = {}
            tasks = [
                asyncio.create_task(self._subscriptions(client, subscriptions)),
                asyncio.create_task(self._stream(client, subscriptions)),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (ReplayError, OSError, ValueError, TypeError, KeyError, TimeoutError):
            await client.close(1008, "read-only mirror refused input or source")
        except ConnectionClosed:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.clients -= 1

    async def _subscriptions(self, client, subscriptions):
        async for raw in client:
            if not isinstance(raw, str):
                raise ReplayError("client publishing disabled")
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ReplayError("subscription must be an object")
            if message.get("op") == "subscribe":
                values = message["subscriptions"]
                if not isinstance(values, list) or len(values) > len(self.channels):
                    raise ReplayError("subscription limit")
                for value in values:
                    identity, channel = value["id"], value["channelId"]
                    if (
                        type(identity) is not int
                        or not 0 <= identity <= 0xFFFFFFFF
                        or type(channel) is not int
                        or not 1 <= channel <= len(self.channels)
                        or identity in subscriptions
                        or len(subscriptions) >= len(self.channels)
                    ):
                        raise ReplayError("invalid subscription")
                    subscriptions[identity] = channel
            elif message.get("op") == "unsubscribe":
                values = message["subscriptionIds"]
                if not isinstance(values, list) or len(values) > len(self.channels):
                    raise ReplayError("subscription limit")
                for identity in values:
                    if type(identity) is not int:
                        raise ReplayError("invalid subscription")
                    subscriptions.pop(identity, None)
            else:
                raise ReplayError("unsupported read-only operation")

    async def _stream(self, client, subscriptions):
        tail = CommittedTail(self.audit, self.session)
        while True:
            if subscriptions:
                records = await asyncio.to_thread(tail.read_batch)
                for record in records:
                    channel = TOPICS.index(channel_name(record)) + 1
                    messages = [(channel, record)]
                    if scene := scene_update(record):
                        messages.append((len(TOPICS) + 1, scene))
                    for channel, payload in messages:
                        encoded = json.dumps(
                            payload, separators=(",", ":"), allow_nan=False
                        ).encode()
                        for identity, wanted in tuple(subscriptions.items()):
                            if wanted == channel:
                                await self._send(
                                    client,
                                    struct.pack("<BIQ", 1, identity, timestamp_ns(record))
                                    + encoded,
                                )
            await asyncio.sleep(0.05)


async def run_mirror(audit: Path, session: str, output: Path, *, port=8765, seconds=3600):
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ReplayError("live recording duration must be 1 through 3600 seconds")
    tail, recording = CommittedTail(audit, session), Recording(output, session)
    try:
        async with FoxgloveMirror(audit, session, port=port) as mirror:
            print(f"Foxglove read-only connection: ws://127.0.0.1:{mirror.port}", flush=True)
            deadline = asyncio.get_running_loop().time() + seconds
            while asyncio.get_running_loop().time() < deadline:
                for record in await asyncio.to_thread(tail.read_batch):
                    recording.append(record)
                await asyncio.sleep(0.05)
    finally:
        recording.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--session", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    try:
        asyncio.run(
            run_mirror(args.audit, args.session, args.output, port=args.port, seconds=args.seconds)
        )
    except KeyboardInterrupt:
        pass
    except (ReplayError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()

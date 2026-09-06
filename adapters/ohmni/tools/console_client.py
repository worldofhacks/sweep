"""Send Intent v1 frames to the running relay as the console source and print outcomes.

Usage: console_client.py <intent> [--drones 11,12] [--args '{"dx":1,"dy":0}'] [--watch 12]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid

import websockets


def load_env() -> dict:
    """Use the process environment, e.g. uv run --env-file /path/to/private.env."""
    return dict(os.environ)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("intents", nargs="+", help="intent names in order")
    parser.add_argument("--drones", default="")
    parser.add_argument("--args", default="{}")
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--relay", default="ws://127.0.0.1:8010")
    options = parser.parse_args()

    env = load_env()
    session = env["SWEEP_SESSION_ID"]
    selection = [int(x) for x in options.drones.split(",") if x.strip()]
    extra = json.loads(options.args)

    async with websockets.connect(f"{options.relay}/ws/{session}", max_size=2**22) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "v": 1,
                    "type": "auth",
                    "source": "console",
                    "token": env["SWEEP_RELAY_TOKEN"],
                }
            )
        )
        deadline = time.time() + options.seconds
        pending = list(options.intents)
        sent = []

        async def send_next() -> None:
            if not pending:
                return
            name = pending.pop(0)
            frame = {
                "v": 1,
                "t": int(time.time() * 1000),
                "type": "intent",
                "intent_id": str(uuid.uuid4()),
                "session": session,
                "source": "console",
                "retry_of": None,
                "name": name,
                "selection": selection,
                "args": (
                    {"ids": selection}
                    if name == "select"
                    else extra
                    if name in {"translate", "formation_set", "spacing", "altitude"}
                    else {}
                ),
                "mode": "indoor",
                "confirm": name in {"takeoff", "land", "land_all", "sweep", "capture_room"},
            }
            sent.append((frame["intent_id"], name))
            print(f"-> {name} drones={selection} args={frame['args']}", flush=True)
            await websocket.send(json.dumps(frame))

        while time.time() < deadline:
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
            except TimeoutError:
                if pending:
                    await send_next()
                continue
            event = json.loads(message)
            kind = event.get("type")
            if kind == "auth.accepted":
                await send_next()
            elif kind == "auth.refused":
                print("auth refused:", event.get("reason"))
                return 2
            elif kind in {"acknowledgement", "refusal"}:
                name = dict(sent).get(event.get("intent_id"), "?")
                print(
                    f"<- {kind} {name} status={event.get('status')} drone={event.get('drone_id')} "
                    f"reason={event.get('reason')} detail={event.get('detail')}",
                    flush=True,
                )
                if event.get("status") in {"completed", "refused", "failed"} and pending:
                    await send_next()
            elif kind == "safety_action":
                print(
                    f"<- safety_action {event.get('action')} drone={event.get('drone_id')} "
                    f"reason={event.get('reason')}",
                    flush=True,
                )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

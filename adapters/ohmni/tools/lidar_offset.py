"""Measure where a robot's forward axis sits in its lidar's angular frame.

Drives one short burst and reports, per 10-degree sector, how the median range changed.
Travelling forward shortens the sector the robot is heading into, so the minimum of that
curve is the forward direction in lidar coordinates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import statistics
import time

import websockets


def load_env() -> dict:
    """Use the process environment, e.g. uv run --env-file /path/to/private.env."""
    return dict(os.environ)


class Shell:
    def __init__(self, port: int) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(1.0)
        time.sleep(0.3)
        try:
            self.sock.recv(4096)
        except OSError:
            pass

    def command(self, text: str, wait: float = 0.4) -> None:
        self.sock.sendall((text + "\n").encode())
        self.sock.settimeout(wait)
        try:
            while True:
                if not self.sock.recv(8192):
                    break
        except OSError:
            pass


def sector_median(scan: list[int], centre: int, half: int = 5) -> float | None:
    values = [
        scan[(centre + offset) % 360]
        for offset in range(-half, half + 1)
        if scan[(centre + offset) % 360] > 0
    ]
    return statistics.median(values) if len(values) >= 4 else None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=int, default=12)
    parser.add_argument("--port", type=int, default=19002)
    parser.add_argument("--left", type=int, default=250)
    parser.add_argument("--right", type=int, default=-250)
    parser.add_argument("--burst", type=float, default=1.6)
    parser.add_argument("--relay", default=os.environ.get("SWEEP_RELAY_URL", "ws://127.0.0.1:8010"))
    parser.add_argument("--spotter-confirmed", action="store_true")
    options = parser.parse_args()
    if not options.spotter_confirmed:
        parser.error("a person beside the robot must confirm --spotter-confirmed")
    if not 0 < options.burst <= 2 or max(abs(options.left), abs(options.right)) > 250:
        parser.error("burst must be at most 2 s and wheel goals at most 250")

    env = load_env()
    shell = Shell(options.port)

    async with websockets.connect(
        f"{options.relay}/ws/{env['SWEEP_SESSION_ID']}", max_size=2**22
    ) as websocket:
        await websocket.send(
            json.dumps(
                {"v": 1, "type": "auth", "source": "console", "token": env["SWEEP_RELAY_TOKEN"]}
            )
        )

        async def fresh_scan(previous: list[int] | None, timeout: float = 8.0):
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except TimeoutError:
                    continue
                event = json.loads(message)
                if event.get("type") == "sensor" and event.get("drone_id") == options.device:
                    scan = list(event["ranges_cm"])
                    if previous is None or scan != previous:
                        return scan
            return None

        before = await fresh_scan(None)
        if before is None:
            print("no scan; is the lidar running on this robot?")
            return 1
        shell.command("init", wait=1.0)
        try:
            shell.command(f"manual_move {options.left} {options.right}")
            await asyncio.sleep(options.burst)
        finally:
            shell.command("manual_move 0 0")
        await asyncio.sleep(1.5)
        after = await fresh_scan(before)
        shell.command("manual_move 0 0")
        if after is None:
            print("no fresh scan after the burst")
            return 1

    changes = []
    for centre in range(0, 360, 10):
        a, b = sector_median(before, centre), sector_median(after, centre)
        if a and b:
            changes.append((centre, (b - a) / 100.0))
    if not changes:
        print("not enough returns to compare")
        return 1
    changes.sort(key=lambda item: item[1])
    closing = changes[0]
    opening = changes[-1]
    print(f"burst manual_move {options.left} {options.right} for {options.burst:.1f} s")
    print(f"  sector closing fastest: {closing[0]:3d} deg ({closing[1]:+.2f} m)  <- forward axis")
    print(f"  sector opening fastest: {opening[0]:3d} deg ({opening[1]:+.2f} m)")
    print("  per-sector change (deg: metres):")
    print("   " + "  ".join(f"{centre}:{delta:+.2f}" for centre, delta in sorted(changes)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

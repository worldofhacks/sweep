"""Calibrate wheel-goal signs on an Ohmni using its own lidar as ground truth.

Subscribes to the relay as a console, captures a scan, drives one short burst over the
bot shell, captures another scan, and reports the angular shift (rotation) and the
forward range change (translation) between them.
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

    def command(self, text: str, wait: float = 0.4) -> str:
        self.sock.sendall((text + "\n").encode())
        self.sock.settimeout(wait)
        buf = b""
        try:
            while True:
                chunk = self.sock.recv(8192)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            pass
        return buf.decode("utf-8", "replace")


def angular_shift(before: list[int], after: list[int]) -> tuple[int, float]:
    """Best circular alignment of two scans: (degrees, agreement fraction)."""
    best_shift, best_score = 0, -1.0
    for shift in range(-90, 91):
        matched = 0
        total = 0
        for angle in range(360):
            a = before[angle]
            b = after[(angle + shift) % 360]
            if a > 0 and b > 0:
                total += 1
                if abs(a - b) <= max(5, 0.05 * a):
                    matched += 1
        if total >= 40:
            score = matched / total
            if score > best_score:
                best_shift, best_score = shift, score
    return best_shift, best_score


def sector_median(scan: list[int], centre: int, half_width: int = 15) -> float | None:
    values = [
        scan[(centre + offset) % 360]
        for offset in range(-half_width, half_width + 1)
        if scan[(centre + offset) % 360] > 0
    ]
    return statistics.median(values) if len(values) >= 5 else None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=int, default=12)
    parser.add_argument("--port", type=int, default=19002, help="bot shell forward port")
    parser.add_argument("--left", type=int, required=True)
    parser.add_argument("--right", type=int, required=True)
    parser.add_argument("--burst", type=float, default=0.7)
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

        async def latest_scan(timeout: float = 6.0) -> list[int] | None:
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except TimeoutError:
                    continue
                event = json.loads(message)
                if event.get("type") == "sensor" and event.get("drone_id") == options.device:
                    return list(event["ranges_cm"])
            return None

        before = await latest_scan()
        if before is None:
            print("no scan from the device; is its lidar running?")
            return 1
        shell.command("init", wait=1.0)
        try:
            shell.command(f"manual_move {options.left} {options.right}")
            await asyncio.sleep(options.burst)
        finally:
            shell.command("manual_move 0 0")
        await asyncio.sleep(1.2)
        after = await latest_scan()
        shell.command("manual_move 0 0")
        if after is None:
            print("no scan after the burst")
            return 1

    shift, score = angular_shift(before, after)
    lines = [f"manual_move {options.left} {options.right} for {options.burst:.1f} s"]
    lines.append(f"  scan alignment: {shift:+d} deg (agreement {score:.2f})")
    for name, centre in (("front 0", 0), ("left 90", 90), ("back 180", 180), ("right 270", 270)):
        a, b = sector_median(before, centre), sector_median(after, centre)
        if a and b:
            lines.append(
                f"  {name:9s}: {a / 100:.2f} m -> {b / 100:.2f} m ({(b - a) / 100:+.2f} m)"
            )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

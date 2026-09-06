"""Measured encoder geometry, 14-bit unwrap, and launch-frame differential odometry."""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass

from .botshell import BotShell

TICKS_PER_MM = 16384 * (30 / 11) / (math.pi * 150.5)
BASE_MM = 332.0
MAX_SAMPLE_GAP_S = 0.35  # Under half a motor wrap at the measured 0.18 m/s cap.


def encoder_delta(previous: int, current: int) -> int:
    return (current - previous + 8192) % 16384 - 8192


def encoder_pair(text: str) -> tuple[int, int] | None:
    values = {
        int(side): int(value)
        for side, value in re.findall(r"apos\s+([01])\s*=\s*(\d+)\s*(?:\n|$)", text)
    }
    if set(values) != {0, 1} or any(not 0 <= v < 16384 for v in values.values()):
        return None
    return values[0], values[1]


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    yaw_deg: float
    vx: float = 0.0
    vy: float = 0.0
    quality: float = 0.0


class Odometry:
    def __init__(self, shell: BotShell, launch: tuple[float, float, float]) -> None:
        self.shell = shell
        self.pose = Pose(*launch)
        self._previous: tuple[int, int] | None = None
        self.updated = 0.0
        self.lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ohmni-encoders", daemon=True)

    def update(self, pair: tuple[int, int], now: float) -> None:
        if self.lost:
            return
        pose = self.pose
        if self._previous is not None:
            dt = now - self.updated
            if not 0 < dt <= MAX_SAMPLE_GAP_S:
                # Lost wraps cannot be recovered from absolute 14-bit positions. Do not
                # quietly restart integrating from a false room pose after a Wi-Fi/IO gap.
                self.lost = True
                self.pose = Pose(pose.x, pose.y, pose.yaw_deg)
                return
            left = encoder_delta(self._previous[0], pair[0]) / TICKS_PER_MM
            right = -encoder_delta(self._previous[1], pair[1]) / TICKS_PER_MM
            distance = (left + right) / 2000
            turn = (right - left) / BASE_MM
            yaw = math.radians(pose.yaw_deg)
            dx = distance * math.cos(yaw + turn / 2)
            dy = distance * math.sin(yaw + turn / 2)
            self.pose = Pose(
                pose.x + dx, pose.y + dy, math.degrees(yaw + turn) % 360, dx / dt, dy / dt, 0.6
            )
        else:
            self.pose = Pose(pose.x, pose.y, pose.yaw_deg, quality=0.6)
        self._previous = pair
        self.updated = now

    def snapshot(self, now: float | None = None) -> Pose:
        now = time.monotonic() if now is None else now
        pose = self.pose
        if now - self.updated > MAX_SAMPLE_GAP_S or self.lost:
            return Pose(pose.x, pose.y, pose.yaw_deg)
        return pose

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                text = self.shell.command("apos 0", expected=r"apos 0\s*=\s*\d+\s*\n")
                text += self.shell.command("apos 1", expected=r"apos 1\s*=\s*\d+\s*\n")
                pair = encoder_pair(text)
                if pair is not None:
                    self.update(pair, time.monotonic())
            except OSError:
                pass  # snapshot freshness independently withdraws position quality.
            self._stop.wait(max(0.001, 0.1 - (time.monotonic() - started)))

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self.shell.close()

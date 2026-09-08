"""Capture bounded sequential main and lower Ohmni calibration frames over ADB."""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

ADB = "/var/tmp/gauntlet/sweep-android-sdk/platform-tools/adb"


def run(serial: str, output: Path, *, count: int = 20, duration_s: float = 30) -> None:
    if not 1 <= count <= 60 or not 0 < duration_s <= 30:
        raise ValueError("count must be 1..60 and duration at most 30s")
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()

    def adb(*args: str) -> None:
        subprocess.run([ADB, "-P", "5037", "-s", serial, *args], check=True, timeout=10)

    boot = subprocess.check_output(
        [ADB, "-P", "5037", "-s", serial, "shell", "cat /proc/sys/kernel/random/boot_id"],
        text=True,
        timeout=10,
    ).strip()
    for index in range(count):
        if time.monotonic() - started >= duration_s:
            break
        for node, name in (("video0", "main.uyvy"), ("video1", "lower.mjpg")):
            remote = f"/data/local/tmp/cal-{index}-{name}"
            capture = (
                f"v4l2-ctl -d /dev/{node} --stream-mmap=1 --stream-skip=3 "
                f"--stream-count=1 --stream-to={remote}"
            )
            adb("shell", f'su 0 sh -c "{capture}"')
            adb("pull", remote, str(output / f"{index:06}-{name}"))
        main = output / f"{index:06}-main.uyvy"
        if main.stat().st_size != 1843200:
            raise ValueError("truncated main UYVY frame")
        subprocess.run(
            [
                "uv",
                "run",
                "python",
                "tools/ohmni_calibration_frame_record.py",
                "--image",
                str(output / f"{index:06}-lower.mjpg"),
                "--output",
                str(output / f"frame-{index:06}.json"),
                "--frame-index",
                str(index),
                "--boot-id",
                boot,
            ],
            check=True,
        )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--serial", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--duration-s", type=float, default=30)
    a = p.parse_args()
    run(a.serial, a.output, count=a.count, duration_s=a.duration_s)


if __name__ == "__main__":
    main()

"""Capture bounded sequential main and lower Ohmni calibration frames over ADB."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np

if __package__:
    from tools.ohmni_calibration_frame_record import record
else:
    from ohmni_calibration_frame_record import record

ADB = "/var/tmp/gauntlet/sweep-android-sdk/platform-tools/adb"
MAIN_SHAPE = (720, 1280, 2)
CAMERAS = (("video0", "main", "uyvy"), ("video1", "lower", "mjpg"))


def _decode(raw: Path, camera: str) -> np.ndarray:
    if camera == "main":
        packed = np.fromfile(raw, dtype=np.uint8)
        if packed.size != int(np.prod(MAIN_SHAPE)):
            raise ValueError("truncated main UYVY frame")
        return cv2.cvtColor(packed.reshape(MAIN_SHAPE), cv2.COLOR_YUV2BGR_UYVY)
    if camera != "lower":
        raise ValueError(f"unknown camera {camera}")
    image = cv2.imread(str(raw))
    if image is None:
        raise ValueError("lower MJPEG frame could not be decoded")
    return image


def _remaining_timeout(deadline_ns: int) -> float:
    remaining_s = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
    if remaining_s <= 0:
        raise TimeoutError("capture duration elapsed")
    return min(10.0, remaining_s)


def _aggregate(camera_dir: Path) -> None:
    frames = [json.loads(path.read_text()) for path in sorted(camera_dir.glob("frame-*.json"))]
    (camera_dir / "result.json").write_text(json.dumps({"frames": frames}, sort_keys=True) + "\n")


def _write_manifest(output: Path, serial: str, boot: str, started_ns: int, ended_ns: int) -> None:
    payload = {
        "schema_version": "ohmni-dual-calibration-capture/v1",
        "status": "complete",
        "serial": serial,
        "boot_id": boot,
        "capture": {
            "started_monotonic_ns": started_ns,
            "ended_monotonic_ns": ended_ns,
            "clock_domain": "capture_host_monotonic",
            "pairing": "sequential main then lower; not simultaneous",
        },
        "cameras": {
            "main": {"device": "/dev/video0", "pixel_format": "UYVY", "shape_px": [1280, 720]},
            "lower": {"device": "/dev/video1", "pixel_format": "MJPEG"},
        },
    }
    (output / "manifest.json").write_text(json.dumps(payload, sort_keys=True) + "\n")


def run(
    serial: str,
    output: Path,
    *,
    expected_boot_id: str,
    count: int = 20,
    duration_s: float = 30,
) -> None:
    if not 1 <= count <= 60 or not 0 < duration_s <= 30:
        raise ValueError("count must be 1..60 and duration at most 30s")
    if not expected_boot_id:
        raise ValueError("an expected boot ID is required")
    output.mkdir(parents=True, exist_ok=False)
    (output / "INCOMPLETE").write_text("capture in progress\n")
    started_ns = time.monotonic_ns()
    total_deadline_ns = started_ns + int(duration_s * 1_000_000_000)
    cleanup_reserve_ns = min(1_000_000_000, int(duration_s * 100_000_000))
    deadline_ns = total_deadline_ns - cleanup_reserve_ns
    remote_prefix = f"/data/local/tmp/ohmni-cal-{uuid4().hex}"

    def adb(*args: str) -> None:
        subprocess.run(
            [ADB, "-P", "5037", "-s", serial, *args],
            check=True,
            timeout=_remaining_timeout(deadline_ns),
        )

    def boot_id() -> str:
        return subprocess.check_output(
            [ADB, "-P", "5037", "-s", serial, "shell", "cat /proc/sys/kernel/random/boot_id"],
            text=True,
            timeout=_remaining_timeout(deadline_ns),
        ).strip()

    try:
        boot = boot_id()
        if boot != expected_boot_id:
            raise ValueError("device boot ID differs from the expected boot ID")
        for index in range(count):
            _remaining_timeout(deadline_ns)
            for node, camera, suffix in CAMERAS:
                camera_dir = output / camera
                camera_dir.mkdir(exist_ok=True)
                remote = f"{remote_prefix}-{index}-{camera}.{suffix}"
                raw = camera_dir / f"raw-{index:06}.{suffix}"
                receipt_started_ns = time.monotonic_ns()
                capture_timeout_s = max(1, int(_remaining_timeout(deadline_ns)))
                capture = (
                    f"timeout {capture_timeout_s} v4l2-ctl -d /dev/{node} --stream-mmap=1 "
                    f"--stream-skip=3 --stream-count=1 --stream-to={remote}"
                )
                adb("shell", f'su 0 sh -c "{capture}"')
                adb("pull", remote, str(raw))
                receipt_ended_ns = time.monotonic_ns()
                image_path = camera_dir / f"frame-{index:06}.png"
                if not cv2.imwrite(str(image_path), _decode(raw, camera)):
                    raise ValueError(f"could not write {camera} PNG")
                record(
                    image_path,
                    camera_dir / f"frame-{index:06}.json",
                    index,
                    boot,
                    None,
                    receipt_started_ns,
                    receipt_ended_ns,
                    raw,
                )
        if boot_id() != boot:
            raise ValueError("device rebooted during capture")
        for _, camera, _ in CAMERAS:
            _aggregate(output / camera)
        _write_manifest(output, serial, boot, started_ns, time.monotonic_ns())
        (output / "INCOMPLETE").unlink()
    finally:
        try:
            cleanup_timeout_s = _remaining_timeout(total_deadline_ns)
        except TimeoutError:
            cleanup_timeout_s = 0
        if cleanup_timeout_s:
            subprocess.run(
                [ADB, "-P", "5037", "-s", serial, "shell", f"rm -f {remote_prefix}-*"],
                check=False,
                timeout=cleanup_timeout_s,
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-boot-id", required=True)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--duration-s", type=float, default=30)
    args = parser.parse_args()
    run(
        args.serial,
        args.output,
        expected_boot_id=args.expected_boot_id,
        count=args.count,
        duration_s=args.duration_s,
    )


if __name__ == "__main__":
    main()

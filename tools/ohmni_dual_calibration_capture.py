"""Capture bounded sequential Ohmni calibration frames over ADB."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
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
CAMERAS = (
    ("video0", "main", "uyvy", "See3CAM_CU135", "UYVY", (1280, 720), "2560", "c1d1"),
    ("video1", "lower", "mjpg", "HD USB Camera", "MJPG", (640, 480), "32e4", "9230"),
)


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


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pipeline_sha256(pipeline: dict[str, object]) -> str:
    encoded = json.dumps(pipeline, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _parse_probe(
    output: str,
    expected_name: str,
    expected_format: str,
    expected_shape: tuple[int, int],
    expected_vendor: str,
    expected_product: str,
) -> dict[str, object]:
    fields = dict(
        line.split("=", 1) for line in output.splitlines() if "=" in line and line.count("=") == 1
    )
    name = fields.get("name")
    serial = fields.get("usb_serial")
    vendor, product, parent = (
        fields.get("id_vendor"),
        fields.get("id_product"),
        fields.get("usb_parent"),
    )
    size = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", output)
    pixel = re.search(r"Pixel Format\s*:\s*'([^']+)'", output)
    if (
        name != expected_name
        or vendor is None
        or product is None
        or parent is None
        or not re.fullmatch(r"/[A-Za-z0-9_./:-]+", parent)
        or vendor.lower() != expected_vendor
        or product.lower() != expected_product
        or size is None
        or pixel is None
        or pixel[1] != expected_format
        or (int(size[1]), int(size[2])) != expected_shape
    ):
        raise ValueError("camera identity or negotiated format does not match the capture contract")
    result: dict[str, object] = {
        "usb_parent": parent,
        "usb_vendor_id": vendor.lower(),
        "usb_product_id": product.lower(),
        "device_name": name,
        "pixel_format": pixel[1],
        "shape_px": [int(size[1]), int(size[2])],
    }
    if serial:
        result["usb_serial"] = serial
    return result


def _camera_pipeline(
    serial: str,
    cameras: tuple[tuple[str, str, str, str, str, tuple[int, int], str, str], ...],
    deadline_ns: int,
) -> dict[str, object]:
    pipeline: dict[str, object] = {}
    parents = set()
    for node, name, _, expected_name, expected_format, expected_shape, vendor, product in cameras:
        script = f"""video=/sys/class/video4linux/{node}
set -eu
parent=$(readlink -f "$video/device")
case "$parent" in
  /*) ;;
  *) exit 1 ;;
esac
found=0
while [ "$parent" != / ]; do
  if [ -f "$parent/idVendor" ] && [ -f "$parent/idProduct" ]; then
    printf 'usb_parent=%s\\n' "$parent"
    printf 'id_vendor=%s\\n' "$(cat "$parent/idVendor")"
    printf 'id_product=%s\\n' "$(cat "$parent/idProduct")"
    if [ -f "$parent/serial" ]; then
      printf 'usb_serial=%s\\n' "$(cat "$parent/serial")"
    fi
    found=1
    break
  fi
  parent=$(dirname "$parent")
done
[ "$found" -eq 1 ] || exit 1
printf 'name=%s\\n' "$(cat "$video/name")"
v4l2-ctl -d /dev/{node} --get-fmt-video"""
        output = subprocess.check_output(
            [ADB, "-P", "5037", "-s", serial, "shell", shlex.join(["sh", "-c", script])],
            text=True,
            timeout=_remaining_timeout(deadline_ns),
        )
        details = _parse_probe(
            output, expected_name, expected_format, expected_shape, vendor, product
        )
        parent = details["usb_parent"]
        if not isinstance(parent, str) or parent in parents:
            raise ValueError("selected cameras do not have distinct USB parents")
        parents.add(parent)
        stream = {"device": f"/dev/{node}", **details}
        stream["calibration_pipeline"] = _calibration_pipeline(stream, serial)
        pipeline[name] = stream
    return pipeline


def _remote_raw_fingerprint(serial: str, raw: str, deadline_ns: int) -> tuple[int, str]:
    script = f"""set -eu
raw={shlex.quote(raw)}
digest=$(toybox sha256sum "$raw")
digest=${{digest%% *}}
size=$(toybox wc -c < "$raw")
set -- $size
printf 'sha256=%s\\nsize=%s\\n' "$digest" "$1"""
    output = subprocess.check_output(
        [
            ADB,
            "-P",
            "5037",
            "-s",
            serial,
            "shell",
            shlex.join(["su", "0", "sh", "-c", script]),
        ],
        text=True,
        timeout=_remaining_timeout(deadline_ns),
    )
    match = re.fullmatch(r"sha256=([0-9a-f]{64})\nsize=([0-9]+)\n", output)
    if match is None:
        raise ValueError("device raw fingerprint has an invalid format")
    return int(match[2]), match[1]


def _calibration_pipeline(stream: dict[str, object], android_device_id: str) -> dict[str, object]:
    shape = stream["shape_px"]
    if not isinstance(shape, list) or len(shape) != 2:
        raise ValueError("camera pipeline shape is invalid")
    device = stream["device"]
    pixel_format = stream["pixel_format"]
    name = stream["device_name"]
    vendor = stream["usb_vendor_id"]
    product = stream["usb_product_id"]
    if not all(
        isinstance(value, str) and value
        for value in (device, pixel_format, name, vendor, product, android_device_id)
    ):
        raise ValueError("camera pipeline identity is invalid")
    identity = f"{vendor}:{product} {name}"
    if isinstance(stream.get("usb_serial"), str):
        identity += f" serial={stream['usb_serial']}"
    return {
        "resolution_px": shape,
        "codec": "raw UYVY" if pixel_format == "UYVY" else "MJPEG",
        "decoder_path": "tools.ohmni_dual_calibration_capture._decode",
        "camera_mode": f"{device} {shape[0]}x{shape[1]}",
        "camera_identity": identity,
        "network_id": "ADB",
        "android_device_id": android_device_id,
    }


def _write_stream_pipeline(
    camera_dir: Path, pipeline_sha256: str, stream: dict[str, object]
) -> None:
    payload = {
        "capture_pipeline_sha256": pipeline_sha256,
        "calibration_pipeline": stream["calibration_pipeline"],
    }
    (camera_dir / "calibration-pipeline.json").write_text(
        json.dumps(payload, sort_keys=True) + "\n"
    )


def _aggregate(camera_dir: Path) -> None:
    frames = [json.loads(path.read_text()) for path in sorted(camera_dir.glob("frame-*.json"))]
    (camera_dir / "result.json").write_text(json.dumps({"frames": frames}, sort_keys=True) + "\n")


def _bind_result_to_manifest(camera_dir: Path, manifest_path: Path) -> None:
    manifest = manifest_path.read_bytes()
    snapshot = camera_dir / "manifest.json"
    snapshot.write_bytes(manifest)
    result_path = camera_dir / "result.json"
    result = json.loads(result_path.read_text())
    result["recorded_live_provenance"] = {
        "manifest_file": snapshot.name,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "stream_id": camera_dir.name,
    }
    result_path.write_text(json.dumps(result, sort_keys=True) + "\n")


def _write_manifest(
    output: Path,
    serial: str,
    boot: str,
    started_ns: int,
    ended_ns: int,
    collection: str,
    pipeline: dict[str, object],
    cleanup: dict[str, object],
) -> None:
    payload = {
        "schema_version": "ohmni-dual-calibration-capture/v2",
        "status": "complete",
        "serial": serial,
        "boot_id": boot,
        "raw_capture_collection": collection,
        "capture_pipeline": pipeline,
        "capture_pipeline_sha256": _pipeline_sha256(pipeline),
        "remote_raw_cleanup": cleanup,
        "capture": {
            "started_monotonic_ns": started_ns,
            "ended_monotonic_ns": ended_ns,
            "clock_domain": "capture_host_monotonic",
            "pairing": "sequential main then lower; not simultaneous",
        },
        "cameras": pipeline,
    }
    (output / "manifest.json").write_text(json.dumps(payload, sort_keys=True) + "\n")


def _write_failure_manifest(
    output: Path,
    serial: str,
    boot: str | None,
    collection: str,
    pipeline: dict[str, object] | None,
    phase: str,
    commands: list[str],
    remote_raw: list[str],
    error: BaseException,
) -> None:
    artifacts = [
        {"path": str(path.relative_to(output)), "sha256": _hash(path)}
        for path in output.rglob("*")
        if path.is_file() and path.name not in {"INCOMPLETE", "failure-manifest.json"}
    ]
    payload = {
        "schema_version": "ohmni-dual-calibration-capture/v2",
        "status": "failed",
        "serial": serial,
        "boot_id": boot,
        "raw_capture_collection": collection,
        "capture_pipeline": pipeline,
        "capture_pipeline_sha256": None if pipeline is None else _pipeline_sha256(pipeline),
        "failure_phase": phase,
        "capture_commands": commands,
        "retained_remote_raw": remote_raw,
        "local_artifacts": artifacts,
        "error_type": type(error).__name__,
        "error": str(error),
    }
    (output / "failure-manifest.json").write_text(json.dumps(payload, sort_keys=True) + "\n")


def run(
    serial: str,
    output: Path,
    *,
    expected_boot_id: str,
    count: int = 20,
    duration_s: float = 30,
    camera: str = "both",
    interval_s: float = 0,
    warmup_frames: int = 3,
) -> None:
    if not 1 <= count <= 60 or not 0 < duration_s <= 30:
        raise ValueError("count must be 1..60 and duration at most 30s")
    if (
        camera not in {"both", "main", "lower"}
        or not 0 <= interval_s <= 5
        or not 3 <= warmup_frames <= 60
    ):
        raise ValueError("invalid camera, interval, or warmup count")
    cameras = tuple(item for item in CAMERAS if camera == "both" or item[1] == camera)
    if not expected_boot_id:
        raise ValueError("an expected boot ID is required")
    output.mkdir(parents=True, exist_ok=False)
    (output / "INCOMPLETE").write_text("capture in progress\n")
    started_ns = time.monotonic_ns()
    total_deadline_ns = started_ns + int(duration_s * 1_000_000_000)
    cleanup_reserve_ns = min(1_000_000_000, int(duration_s * 100_000_000))
    deadline_ns = total_deadline_ns - cleanup_reserve_ns
    collection = uuid4().hex
    remote_prefix = f"/data/local/tmp/ohmni-cal-{collection}"
    boot: str | None = None
    pipeline: dict[str, object] | None = None
    phase = "preflight"
    commands: list[str] = []
    retained_remote_raw: list[str] = []
    cleanup_remote_raw: list[str] = []
    cleanup_status: dict[str, object] = {
        "status": "not_attempted",
        "retained_paths": list(cleanup_remote_raw),
    }

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
        pipeline = _camera_pipeline(serial, cameras, deadline_ns)
        pipeline_sha256 = _pipeline_sha256(pipeline)
        for _, name, _, _, _, _, _, _ in cameras:
            camera_dir = output / name
            camera_dir.mkdir(exist_ok=True)
            _write_stream_pipeline(camera_dir, pipeline_sha256, pipeline[name])
        for index in range(count):
            _remaining_timeout(deadline_ns)
            if index and interval_s:
                time.sleep(min(interval_s, _remaining_timeout(deadline_ns)))
            for node, name, suffix, _, _, expected_shape, _, _ in cameras:
                camera_dir = output / name
                remote = f"{remote_prefix}-{index}-{name}.{suffix}"
                raw = camera_dir / f"raw-{index:06}.{suffix}"
                retained_remote_raw.append(remote)
                capture_timeout_s = max(1, int(_remaining_timeout(deadline_ns)))
                capture = (
                    f"timeout {capture_timeout_s} v4l2-ctl -d /dev/{node} --stream-mmap=1 "
                    f"--stream-skip={warmup_frames} --stream-count=1 --stream-to={remote}"
                )
                commands.append(capture)
                phase = f"capture:{index}:{name}"
                receipt_started_ns = time.monotonic_ns()
                adb("shell", shlex.join(["su", "0", "sh", "-c", capture]))
                phase = f"remote-fingerprint:{index}:{name}"
                remote_size, remote_sha256 = _remote_raw_fingerprint(serial, remote, deadline_ns)
                phase = f"pull:{index}:{name}"
                adb("pull", remote, str(raw))
                receipt_ended_ns = time.monotonic_ns()
                if (
                    not raw.is_file()
                    or raw.stat().st_size != remote_size
                    or _hash(raw) != remote_sha256
                ):
                    raise ValueError("pulled raw camera frame does not match the device source")
                phase = f"decode:{index}:{name}"
                image_path = camera_dir / f"frame-{index:06}.png"
                image = _decode(raw, name)
                if [image.shape[1], image.shape[0]] != list(expected_shape):
                    raise ValueError("decoded camera frame does not match negotiated resolution")
                if not cv2.imwrite(str(image_path), image):
                    raise ValueError(f"could not write {name} PNG")
                phase = f"record:{index}:{name}"
                record(
                    image_path,
                    camera_dir / f"frame-{index:06}.json",
                    index,
                    boot,
                    None,
                    receipt_started_ns,
                    receipt_ended_ns,
                    raw,
                    raw_capture_collection=collection,
                    camera=name,
                    capture_pipeline_sha256=pipeline_sha256,
                    source_device_sha256=remote_sha256,
                    source_device_size_bytes=remote_size,
                )
                cleanup_remote_raw.append(remote)
        phase = "postflight-boot"
        if boot_id() != boot:
            raise ValueError("device rebooted during capture")
        for _, name, _, _, _, _, _, _ in cameras:
            phase = f"aggregate:{name}"
            _aggregate(output / name)
        phase = "cleanup"
        if not cleanup_remote_raw:
            cleanup_status = {"status": "not_needed", "retained_paths": []}
        else:
            cleanup = "rm -f " + " ".join(shlex.quote(path) for path in cleanup_remote_raw)
            try:
                cleanup_timeout_s = _remaining_timeout(total_deadline_ns)
                result = subprocess.run(
                    [ADB, "-P", "5037", "-s", serial, "shell", cleanup],
                    check=False,
                    timeout=cleanup_timeout_s,
                )
            except TimeoutError as error:
                cleanup_status = {
                    "status": "deadline_elapsed",
                    "retained_paths": list(cleanup_remote_raw),
                    "error": str(error),
                }
            except (OSError, subprocess.TimeoutExpired) as error:
                cleanup_status = {
                    "status": "failed",
                    "retained_paths": list(cleanup_remote_raw),
                    "error": str(error),
                }
            else:
                cleanup_status = {
                    "status": "completed" if result.returncode == 0 else "failed",
                    "retained_paths": [] if result.returncode == 0 else list(cleanup_remote_raw),
                    "returncode": result.returncode,
                }
        phase = "manifest"
        _write_manifest(
            output,
            serial,
            boot,
            started_ns,
            time.monotonic_ns(),
            collection,
            pipeline,
            cleanup_status,
        )
        phase = "bind-manifest"
        manifest_path = output / "manifest.json"
        for _, name, _, _, _, _, _, _ in cameras:
            _bind_result_to_manifest(output / name, manifest_path)
        phase = "complete"
        (output / "INCOMPLETE").unlink()
    except BaseException as error:
        _write_failure_manifest(
            output, serial, boot, collection, pipeline, phase, commands, retained_remote_raw, error
        )
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-boot-id", required=True)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--duration-s", type=float, default=30)
    parser.add_argument("--camera", choices=("both", "main", "lower"), default="both")
    parser.add_argument("--interval-s", type=float, default=0)
    parser.add_argument("--warmup-frames", type=int, default=3)
    args = parser.parse_args()
    run(
        args.serial,
        args.output,
        expected_boot_id=args.expected_boot_id,
        count=args.count,
        duration_s=args.duration_s,
        camera=args.camera,
        interval_s=args.interval_s,
        warmup_frames=args.warmup_frames,
    )


if __name__ == "__main__":
    main()

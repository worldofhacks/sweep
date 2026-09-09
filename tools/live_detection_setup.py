"""Prepare and check read-only live detections against the relay's actual camera roster."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import urlopen

from perception.object_detection import MAX_MODEL_BYTES, YOLOX_S_ONNX_SHA256, YOLOX_S_ONNX_URL
from relay.live_detection import LiveDetectionService, load_live_detection_sources
from relay.settings import RelaySettings, SettingsError


def environment(path: Path | None) -> dict[str, str]:
    if path is None:
        return dict(os.environ)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise ValueError("runtime JSON must be a private regular file (0600)")
    if metadata.st_size > 1024 * 1024:
        raise ValueError("runtime JSON exceeds 1 MiB")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError("runtime JSON must map environment names to strings")
    return value


def source_records(values: dict[str, str], cameras: list[str], origin: str) -> list[dict]:
    roster = RelaySettings.from_env(values).configured_cameras()
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"rtsp", "rtsps"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (parsed.port is not None and not 1 <= parsed.port <= 65535)
    ):
        raise ValueError("RTSP origin must be a host/port without credentials or a path")
    username, password = (
        values.get("SWEEP_MEDIA_READ_USERNAME"),
        values.get("SWEEP_MEDIA_READ_PASSWORD"),
    )
    if not username or not password:
        raise ValueError("the runtime must supply the dedicated media reader credentials")
    authority = f"{quote(username, safe='')}:{quote(password, safe='')}@{parsed.netloc}"
    if not 1 <= len(cameras) <= 8:
        raise ValueError("select one to eight actual cameras")
    sources, seen = [], set()
    for specification in cameras:
        try:
            device, camera_id, dimensions = specification.split(":")
            width, height = (int(v) for v in dimensions.split("x"))
            device_id = int(device)
        except ValueError:
            raise ValueError("camera must be DEVICE_ID:CAMERA_ID:WIDTHxHEIGHT") from None
        if (
            device != str(device_id)
            or not 1 <= width <= 1920
            or not 1 <= height <= 1920
            or width * height > 1920 * 1080
        ):
            raise ValueError("camera identity or measured dimensions are invalid")
        camera = next((c for c in roster.get(device_id, ()) if c.camera_id == camera_id), None)
        if camera is None or (device_id, camera_id) in seen:
            raise ValueError(
                "select each camera once using its exact configured device and camera IDs"
            )
        seen.add((device_id, camera_id))
        sources.append(
            {
                "device_id": device_id,
                "camera_id": camera.camera_id,
                "stream": camera.stream,
                "stream_url": urlunsplit((parsed.scheme, authority, "/" + camera.stream, "", "")),
                "resolution": [width, height],
                "model_path": "models/yolox_s.onnx",
                "model_sha256": YOLOX_S_ONNX_SHA256,
            }
        )
    return sources


def prepare(
    values: dict[str, str], cameras: list[str], origin: str, output: Path, model: Path | None
) -> Path:
    sources = source_records(values, cameras, origin)
    # A new directory prevents accidental replacement of a running configuration.
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        (output / "models").mkdir(mode=0o700)
        destination = output / "models/yolox_s.onnx"
        with (
            model.open("rb") if model is not None else urlopen(YOLOX_S_ONNX_URL, timeout=30)
        ) as incoming:
            data = incoming.read(MAX_MODEL_BYTES + 1)
        if len(data) > MAX_MODEL_BYTES or hashlib.sha256(data).hexdigest() != YOLOX_S_ONNX_SHA256:
            raise ValueError("model does not match the pinned YOLOX-s artifact")
        destination.write_bytes(data)
        destination.chmod(0o600)
        config = output / "live-detection.json"
        with os.fdopen(os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump({"schema_version": 1, "sources": sources}, stream, indent=2)
            stream.write("\n")
        load_live_detection_sources(config)
        return config
    except BaseException:
        shutil.rmtree(output)
        raise


def check(
    values: dict[str, str],
    config: Path,
    timeout: float = 20,
    *,
    service_factory=LiveDetectionService,
) -> dict:
    if not 1 <= timeout <= 60:
        raise ValueError("check timeout must be between 1 and 60 seconds")
    sources = load_live_detection_sources(config)
    roster = RelaySettings.from_env(values).configured_cameras()
    for source in sources:
        if not any(
            c.camera_id == source.camera_id and c.stream == source.stream
            for c in roster.get(source.device_id, ())
        ):
            raise ValueError("detection source no longer matches the relay camera roster")
    service = service_factory(sources)
    evidence = {
        (s.device_id, s.camera_id): {
            "device_id": s.device_id,
            "camera_id": s.camera_id,
            "stream": s.stream,
            "resolution": list(s.resolution),
            "state": "starting",
            "sequences": set(),
            "last_detection_count": 0,
        }
        for s in sources
    }
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            for source in sources:
                entry = evidence[source.device_id, source.camera_id]
                result = service.snapshot(source.device_id, source.camera_id, 1, source.stream)
                entry["state"] = result["state"]
                frame = result.get("frame")
                if result["state"] == "live" and frame:
                    entry["sequences"].add(frame["sequence"])
                    entry["last_detection_count"] = len(frame["detections"])
            if all(e["state"] == "failed" or len(e["sequences"]) >= 2 for e in evidence.values()):
                break
            time.sleep(0.2)
    finally:
        service.close()
    results = [
        {
            **{k: v for k, v in e.items() if k != "sequences"},
            "fresh_frames": len(e["sequences"]),
            "passed": e["state"] == "live" and len(e["sequences"]) >= 2,
        }
        for e in evidence.values()
    ]
    return {
        "passed": all(r["passed"] for r in results),
        "scope": "camera decode and model inference only; no commands or navigation qualification",
        "cameras": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-json",
        type=Path,
        help="private environment JSON; otherwise use this process environment",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("prepare")
    setup.add_argument(
        "--camera",
        action="append",
        required=True,
        help="DEVICE_ID:CAMERA_ID:WIDTHxHEIGHT (repeatable)",
    )
    setup.add_argument("--rtsp-origin", default="rtsp://127.0.0.1:8554")
    setup.add_argument("--output", type=Path, required=True, help="new private directory")
    models = setup.add_mutually_exclusive_group(required=True)
    models.add_argument("--model", type=Path)
    models.add_argument("--download-model", action="store_true")
    probe = commands.add_parser("check")
    probe.add_argument("--config", type=Path, required=True)
    probe.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args(argv)
    try:
        values = environment(args.runtime_json)
        if args.command == "prepare":
            config = prepare(
                values, args.camera, args.rtsp_origin, args.output.resolve(), args.model
            )
            print(
                json.dumps(
                    {
                        "prepared": True,
                        "config": str(config),
                        "enable_setting": "SWEEP_LIVE_DETECTION_CONFIG",
                        "relay_restarted": False,
                    }
                )
            )
            return 0
        report = check(values, args.config, args.timeout)
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] else 1
    except (OSError, ValueError, SettingsError):
        # Runtime JSON and stream URLs carry credentials; never print exception values.
        print(
            json.dumps(
                {
                    "passed": False,
                    "error": (
                        "Setup failed. Check file permissions, camera IDs/dimensions, "
                        "model hash, and runtime configuration."
                    ),
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

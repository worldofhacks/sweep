"""Generate camera-specific Compose permissions without reading media credentials.

This only writes configuration. It does not start MediaMTX, contact a device, or
create a publisher. Input maps use the relay's explicit camera/stream contracts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from media.streams import (
    MAX_DEVICE_ID,
    MAX_FLEET_DEVICES,
    CameraStream,
    parse_camera_mapping,
    validate_camera_mapping,
)

LOCKED_PASSWORD = "sha256:aacd03b07c2bf6584162876d3591c9be0b9c5e7023c2a2e989fb51774e4afa76"
LEGACY_STREAMS = tuple(f"{kind}{unit}" for kind in ("drone", "ground") for unit in range(1, 5))
BASE_ACCOUNT_COUNT = 10  # drone1..4, reader, API, ground1..4 in mediamtx.yml
MAX_MAP_BYTES = 256 * 1024
RESERVED_USERS = {"any", "sweep-reader", "sweep-api"}


def _device_ids(raw: object) -> set[int]:
    if not isinstance(raw, Mapping) or len(raw) > MAX_FLEET_DEVICES:
        raise ValueError("media mapping must be an object with at most 64 device IDs")
    for key in raw:
        if (
            not isinstance(key, str)
            or re.fullmatch(r"[1-9][0-9]{0,9}", key) is None
            or int(key) > MAX_DEVICE_ID
        ):
            raise ValueError("media mapping keys must be canonical positive device IDs")
    return {int(key) for key in raw}


def explicit_cameras(cameras: object, streams: object) -> dict[int, tuple[CameraStream, ...]]:
    """Apply explicit camera lists over legacy primary mappings, including empty lists."""
    configured_ids = _device_ids(cameras) | _device_ids(streams)
    legacy = {}
    for device_id, stream in streams.items():
        # Legacy stream settings and the current Ohmni publisher are bounded to 64.
        if not isinstance(stream, str) or len(stream) > 64:
            raise ValueError("legacy media stream must be a bounded path name")
        legacy[int(device_id)] = (CameraStream("primary", "Primary camera", stream),)
    validate_camera_mapping(legacy, configured_ids)
    legacy.update(parse_camera_mapping(cameras, configured_ids))
    return validate_camera_mapping(legacy, configured_ids)


def password_environment(stream: str) -> str:
    if stream in LEGACY_STREAMS:
        return f"SWEEP_MEDIA_{stream.upper()}_PASSWORD"
    return f"SWEEP_MEDIA_STREAM_{stream.upper().replace('-', '_')}_PASSWORD"


def compose_override(cameras: object, streams: object) -> dict[str, object]:
    configured = explicit_cameras(cameras, streams)
    names = {camera.stream for entries in configured.values() for camera in entries}
    if names & RESERVED_USERS:
        raise ValueError("camera streams cannot use MediaMTX anonymous or service account names")
    additional = sorted(names - set(LEGACY_STREAMS))
    credential_names = [password_environment(stream) for stream in additional]
    if len(credential_names) != len(set(credential_names)):
        raise ValueError("camera stream names collide after credential environment normalization")

    # Flat stream names contain no regular-expression operators. Keep the legacy
    # paths and grant read/playback only to these exact additional camera names.
    allowed = "~^(" + "|".join((*LEGACY_STREAMS, *additional)) + ")$"
    environment = {
        "MTX_AUTHINTERNALUSERS_4_PERMISSIONS_0_PATH": allowed,
        "MTX_AUTHINTERNALUSERS_4_PERMISSIONS_1_PATH": allowed,
    }
    for index, stream in enumerate(additional, start=BASE_ACCOUNT_COUNT):
        prefix = f"MTX_AUTHINTERNALUSERS_{index}"
        environment.update(
            {
                f"{prefix}_USER": stream,
                f"{prefix}_PASS": "${"
                + password_environment(stream)
                + ":-"
                + LOCKED_PASSWORD
                + "}",
                f"{prefix}_PERMISSIONS_0_ACTION": "publish",
                f"{prefix}_PERMISSIONS_0_PATH": stream,
            }
        )
    # Compose accepts JSON. Only environment overrides are emitted; image,
    # listeners, loopback API, recording policy and volumes stay in base Compose.
    return {"services": {"mediamtx": {"environment": environment}}}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("media JSON contains a duplicate key")
        value[key] = item
    return value


def _read_map(environ: Mapping[str, str], name: str) -> object:
    raw = environ.get(name, "{}") or "{}"
    if len(raw.encode("utf-8")) > MAX_MAP_BYTES:
        raise ValueError("media JSON exceeds the configuration size bound")
    try:
        return json.loads(raw, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("media mapping must be valid bounded JSON") from error


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new local Compose JSON file")
    arguments = parser.parse_args(argv)
    values = os.environ if environ is None else environ
    try:
        override = compose_override(
            _read_map(values, "SWEEP_MEDIA_CAMERAS_JSON"),
            _read_map(values, "SWEEP_MEDIA_STREAMS_JSON"),
        )
        # Refuse replacement and symlink targets. Reconfigure into a new file so
        # generating permissions cannot silently mutate an active deployment.
        descriptor = os.open(arguments.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(override, indent=2) + "\n")
    except (ValueError, OSError) as error:
        parser.exit(2, f"media configuration refused: {error}\n")
    print("Wrote media permissions; no service or publisher was started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

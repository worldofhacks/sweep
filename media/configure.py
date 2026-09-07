"""Generate a private, explicit MediaMTX/Compose bundle without starting services."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from media.recording import IMAGE_REF
from media.streams import MAX_MEDIA_STREAMS, parse_camera_mapping

MAX_PROVISIONING_BYTES = 4 * 1024 * 1024
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")


def _credentials(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"user", "password"}:
        raise ValueError("media accounts require exactly user and password")
    user, password = value["user"], value["password"]
    if not isinstance(user, str) or user == "any" or _USERNAME.fullmatch(user) is None:
        raise ValueError("media account name is outside the bounded contract")
    if (
        not isinstance(password, str)
        or not 32 <= len(password.encode()) <= 4096
        or not password.isprintable()
        or password != password.strip()
    ):
        raise ValueError("media account password must be 32 through 4096 printable bytes")
    return {"user": user, "pass": password}


def _hosts(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError("webrtc_hosts requires one through sixteen explicit hosts")
    for host in value:
        if not isinstance(host, str):
            raise ValueError("WebRTC hosts must be IP addresses or DNS names")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if (
                _HOST.fullmatch(host) is None
                or any(not label or len(label) > 63 for label in host.split("."))
                or any(label.startswith("-") or label.endswith("-") for label in host.split("."))
            ):
                raise ValueError("WebRTC hosts must be IP addresses or DNS names") from None
    if len(set(value)) != len(value):
        raise ValueError("WebRTC hosts must be unique")
    return value


def configuration(raw: object) -> dict[str, object]:
    """Use only declared streams and supplied media-only credentials."""
    if not isinstance(raw, dict) or set(raw) != {
        "cameras",
        "publishers",
        "reader",
        "api",
        "webrtc_hosts",
    }:
        raise ValueError("provisioning requires cameras, publishers, reader, api and webrtc_hosts")
    camera_input = raw["cameras"]
    if not isinstance(camera_input, dict):
        raise ValueError("cameras must be an explicit device mapping")
    ids = {
        int(key)
        for key in camera_input
        if isinstance(key, str) and key.isascii() and key.isdecimal() and len(key) <= 10
    }
    cameras = parse_camera_mapping(camera_input, ids)
    streams = {camera.stream for entries in cameras.values() for camera in entries}
    if not streams:
        raise ValueError("provisioning requires at least one explicitly configured camera stream")
    publishers = raw["publishers"]
    if not isinstance(publishers, list) or not 1 <= len(publishers) <= MAX_MEDIA_STREAMS:
        raise ValueError("publishers must be a bounded list of explicit media accounts")
    accounts: list[dict[str, object]] = []
    assigned: set[str] = set()
    for publisher in publishers:
        if not isinstance(publisher, dict) or set(publisher) != {"user", "password", "streams"}:
            raise ValueError("publishers require exactly user, password and streams")
        account: dict[str, object] = _credentials(
            {key: publisher[key] for key in ("user", "password")}
        )
        paths = publisher["streams"]
        if (
            not isinstance(paths, list)
            or not 1 <= len(paths) <= MAX_MEDIA_STREAMS
            or any(not isinstance(path, str) or path not in streams for path in paths)
            or len(set(paths)) != len(paths)
            or assigned.intersection(paths)
        ):
            raise ValueError("publish permissions must name unique explicitly configured streams")
        assigned.update(paths)
        account["permissions"] = [{"action": "publish", "path": path} for path in paths]
        accounts.append(account)
    if assigned != streams:
        raise ValueError("each configured stream requires exactly one publisher")
    reader: dict[str, object] = _credentials(raw["reader"])
    reader["permissions"] = [
        {"action": action, "path": path}
        for path in sorted(streams)
        for action in ("read", "playback")
    ]
    api: dict[str, object] = _credentials(raw["api"])
    api["permissions"] = [{"action": "api"}]
    accounts.extend((reader, api))
    if len({account["user"] for account in accounts}) != len(accounts):
        raise ValueError("media account names must be unique")
    return {
        "logLevel": "info",
        "rtsp": True,
        "webrtc": True,
        "hls": True,
        "rtmp": False,
        "srt": False,
        "moq": False,
        "api": True,
        "apiAddress": ":9997",
        "webrtcAdditionalHosts": _hosts(raw["webrtc_hosts"]),
        "authMethod": "internal",
        "authInternalUsers": accounts,
        "pathDefaults": {"record": False},
        "paths": {stream: {} for stream in sorted(streams)},
    }


def compose_configuration(config_path: Path) -> dict[str, object]:
    # A standalone file deliberately has no legacy positional password overrides.
    # The shared identity prevents accidentally running beside the existing service.
    return {
        "name": "sweep",
        "services": {
            "mediamtx": {
                "image": IMAGE_REF,
                "container_name": "sweep-mediamtx",
                "ports": [
                    "8554:8554",
                    "8000-8001:8000-8001/udp",
                    "8889:8889",
                    "8189:8189/udp",
                    "8888:8888",
                    "127.0.0.1:9997:9997",
                ],
                "environment": {"MTX_PATHDEFAULTS_RECORD": "false"},
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(config_path).replace("$", "$$"),
                        "target": "/mediamtx.yml",
                        "read_only": True,
                        "bind": {"create_host_path": False},
                    }
                ],
            }
        },
    }


def write_bundle(source: Path, output: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PROVISIONING_BYTES:
            raise ValueError("provisioning input must be a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(MAX_PROVISIONING_BYTES + 1)
        if len(payload) > MAX_PROVISIONING_BYTES:
            raise ValueError("provisioning input exceeds its size bound")
        config = configuration(json.loads(payload))
    finally:
        os.close(descriptor)
    target = output.absolute()
    if target.exists() or target.is_symlink():
        raise ValueError("output bundle already exists; choose a new private directory")
    stage = Path(tempfile.mkdtemp(prefix=".sweep-media-", dir=target.parent))
    try:
        for name, content in (
            ("mediamtx.json", config),
            ("compose.json", compose_configuration(target / "mediamtx.json")),
        ):
            file = stage / name
            with file.open("x", encoding="utf-8") as handle:
                os.chmod(file, 0o600)
                json.dump(content, handle, indent=2, allow_nan=False)
                handle.write("\n")
        # mkdir reserves ownership without replacing any existing bundle.
        target.mkdir(mode=0o700)
        try:
            for name in ("mediamtx.json", "compose.json"):
                os.replace(stage / name, target / name)
        except BaseException:
            shutil.rmtree(target)
            raise
    finally:
        shutil.rmtree(stage)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provisioning", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        write_bundle(args.provisioning, args.output_dir)
    except (OSError, UnicodeError, ValueError, TypeError):
        # Never echo parser data or credential-bearing input in failures.
        print(
            "media provisioning failed; check the private input and unused output path",
            file=sys.stderr,
        )
        return 2
    print("Media configuration bundle written; no service was started or changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

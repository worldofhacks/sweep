"""Export one verified flight deployment as Android navigation-admission files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from planner.navigation_deployment import load_navigation_deployment, read_document
from relay.auth import sign_event
from tools.map_common import parse_document

_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_KEY_BYTES = 4_096
_FILENAMES = {
    "navigation_config": "navigation_config.json",
    "map": "map.json",
    "geometry": "geometry.json",
    "camera_calibration": "camera_calibration.json",
    "body_extrinsics": "body_extrinsics.json",
    "world_transform": "world_transform.json",
}


def _read_file(path: Path, name: str, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= maximum:
            raise ValueError(f"{name} must be a nonempty regular file within its size limit")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
    finally:
        os.close(descriptor)
    if not 1 <= len(value) <= maximum:
        raise ValueError(f"{name} must be within its size limit")
    return value


def _private_key(path: Path) -> bytes:
    if path.stat().st_mode & 0o077:
        raise ValueError("provenance key must have mode 0600")
    key = _read_file(path, "provenance key", _MAX_KEY_BYTES)
    if len(key) < 32:
        raise ValueError("provenance key must contain at least 32 bytes")
    return key


def _relative_path(root: Path, value: object, name: str) -> Path:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a nonempty path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be relative to its configuration")
    return root / path


def _world_evidence(world_path: Path, device_id: int) -> tuple[Path, Mapping[str, object]]:
    raw = read_document(world_path)
    devices = raw.get("devices")
    if not isinstance(devices, list):
        raise ValueError("world localization devices must be a list")
    selected = [
        item
        for item in devices
        if isinstance(item, Mapping)
        and isinstance(item.get("pins"), Mapping)
        and item["pins"].get("drone_id") == device_id
    ]
    if len(selected) != 1:
        raise ValueError("world localization must describe exactly one selected aircraft")
    evidence = selected[0].get("evidence_paths")
    if not isinstance(evidence, Mapping):
        raise ValueError("world localization evidence paths are invalid")
    return _relative_path(world_path.parent, raw.get("bundle"), "world bundle"), evidence


def _evidence_path(root: Path, evidence: Mapping[str, object], name: str) -> Path:
    return _relative_path(root, evidence.get(name), f"{name} evidence")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exact_digest(value: bytes, expected: str, name: str) -> None:
    if _digest(value) != expected:
        raise ValueError(f"{name} bytes do not match the approved semantic digest")


def _map_bytes(bundle: Path, profile) -> bytes:
    encoded = _read_file(bundle / "manifest.yaml", "world bundle manifest", _MAX_ARTIFACT_BYTES)
    manifest = parse_document(encoded, "world bundle manifest")
    if (
        manifest.get("bundle_version") != profile.map_version
        or manifest.get("content_sha256") != profile.map_sha256
    ):
        raise ValueError("world bundle manifest does not match the approved wire profile")
    return encoded


def _artifact_bytes(deployment, device_id: int) -> dict[str, tuple[bytes, str]]:
    profile = deployment.wire_profiles.get(device_id)
    if profile is None:
        raise ValueError("navigation deployment has no wire profile for the selected aircraft")
    raw = read_document(deployment.path)
    wire_files = raw.get("wire_navigation_files")
    if not isinstance(wire_files, Mapping):
        raise ValueError("flight navigation deployment has no per-device wire tuning")
    tuning_path = _relative_path(
        deployment.path.parent, wire_files.get(str(device_id)), "wire navigation tuning"
    )
    world_path = _relative_path(
        deployment.path.parent, raw.get("world_localization_file"), "world localization"
    )
    bundle, evidence = _world_evidence(world_path, device_id)
    sources = {
        "navigation_config": (tuning_path, profile.navigation_config_sha256),
        "geometry": (
            _evidence_path(world_path.parent, evidence, "geometry_directory") / "geometry.json",
            profile.geometry_sha256,
        ),
        "camera_calibration": (
            _evidence_path(world_path.parent, evidence, "camera_calibration"),
            profile.camera_calibration_sha256,
        ),
        "body_extrinsics": (
            _evidence_path(world_path.parent, evidence, "capture_alignment"),
            profile.body_extrinsics_sha256,
        ),
        "world_transform": (
            _evidence_path(world_path.parent, evidence, "world_enu"),
            profile.world_transform_sha256,
        ),
    }
    result = {}
    for kind in (
        "navigation_config",
        "geometry",
        "camera_calibration",
        "body_extrinsics",
        "world_transform",
    ):
        path, expected = sources[kind]
        encoded = _read_file(path, f"{kind} artifact", _MAX_ARTIFACT_BYTES)
        _exact_digest(encoded, expected, kind)
        result[kind] = (encoded, expected)
        if kind == "navigation_config":
            result["map"] = (_map_bytes(bundle, profile), profile.map_sha256)
    return result


def export_phone_navigation_admission(
    deployment_path: str | Path,
    device_id: int,
    provenance_key_path: str | Path,
    output_directory: str | Path,
) -> dict[str, object]:
    """Write Android's bounded navigation-admission bundle for one flight aircraft."""
    if type(device_id) is not int or device_id < 1:
        raise ValueError("device_id must be a positive integer")
    deployment = load_navigation_deployment(deployment_path)
    if deployment.approval.mode != "flight":
        raise ValueError("phone navigation admission requires a flight deployment")
    artifacts = _artifact_bytes(deployment, device_id)
    profile = deployment.wire_profiles[device_id]
    key = _private_key(Path(provenance_key_path))
    bindings = [
        {
            "kind": kind,
            "file": _FILENAMES[kind],
            "semantic_sha256": semantic_sha256,
            "byte_sha256": _digest(encoded),
        }
        for kind, (encoded, semantic_sha256) in artifacts.items()
    ]
    provenance = {
        "v": 1,
        "session": deployment.approval.session,
        "device_id": device_id,
        "bindings": bindings,
    }
    manifest = {
        "v": 1,
        "enabled": True,
        "navigation_config_id": profile.navigation_config_id,
        "navigation_config_sha256": profile.navigation_config_sha256,
        "map_version": profile.map_version,
        "map_sha256": profile.map_sha256,
        "geometry_sha256": profile.geometry_sha256,
        "camera_calibration_sha256": profile.camera_calibration_sha256,
        "body_extrinsics_sha256": profile.body_extrinsics_sha256,
        "world_transform_sha256": profile.world_transform_sha256,
        "control_source_ids": list(profile.control_source_ids),
        "clock_lease_id": profile.clock_lease_id,
        "clock_lease_expires_at_ms": profile.clock_lease_expires_at_ms,
        "max_authorization_lifetime_ms": profile.max_authorization_lifetime_ms,
        "provenance": {**provenance, "signature": sign_event(provenance, key)},
    }
    encoded_manifest = json.dumps(
        manifest, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()
    if len(encoded_manifest) > 16 * 1024:
        raise ValueError("navigation admission manifest exceeds Android's 16 KiB bound")
    destination = Path(output_directory)
    if destination.exists():
        raise ValueError("output directory must not already exist")
    destination.mkdir(mode=0o700)
    for kind, (encoded, _) in artifacts.items():
        with (destination / _FILENAMES[kind]).open("xb") as stream:
            stream.write(encoded)
        (destination / _FILENAMES[kind]).chmod(0o600)
    with (destination / "navigation-admission.json").open("xb") as stream:
        stream.write(encoded_manifest)
    (destination / "navigation-admission.json").chmod(0o600)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", type=Path)
    parser.add_argument("device_id", type=int)
    parser.add_argument("--provenance-key-file", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        export_phone_navigation_admission(
            arguments.deployment,
            arguments.device_id,
            arguments.provenance_key_file,
            arguments.output_directory,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

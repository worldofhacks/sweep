"""Fit an unapproved fixed-head Ohmni body-to-camera mount from floor AprilTags."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import cv2
import numpy as np

from perception.camera_tags import CameraTagDetector

TAG_SIZE_M = 0.199898
FIXED_HEAD_POSITION = 368
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_CAPTURES = 64


def _read(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
            raise ValueError(f"{path} must be a bounded regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            value = stream.read(maximum + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(value) > maximum:
        raise ValueError(f"{path} exceeds its byte limit")
    return value


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _object(value: bytes, name: str) -> dict[str, object]:
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{name} is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be an object")
    return parsed


def _pin(root: Path, value: object, name: str, maximum: int) -> tuple[dict[str, str], bytes]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise ValueError(f"{name} must be a hash pin")
    path, digest = value["path"], value["sha256"]
    relative = Path(path) if isinstance(path, str) else Path()
    if (
        not isinstance(path, str)
        or relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        raise ValueError(f"{name} pin is invalid")
    payload = _read(root / relative, maximum)
    if _digest(payload) != digest:
        raise ValueError(f"{name} pin does not match evidence bytes")
    return {"path": path, "sha256": digest}, payload


def _matrix(value: object, name: str) -> np.ndarray:
    matrix = np.asarray(value, dtype=float)
    if (
        matrix.shape != (4, 4)
        or not np.isfinite(matrix).all()
        or not np.allclose(matrix[3], [0, 0, 0, 1])
    ):
        raise ValueError(f"{name} must be a finite rigid transform")
    rotation = matrix[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6) or np.linalg.det(rotation) <= 0:
        raise ValueError(f"{name} rotation is invalid")
    return matrix


def _pose(value: object, name: str) -> np.ndarray:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    try:
        x, y, yaw = (float(value[key]) for key in ("x_m", "y_m", "yaw_deg"))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{name} is invalid") from error
    if not np.isfinite([x, y, yaw]).all():
        raise ValueError(f"{name} must be finite")
    angle = math.radians(yaw)
    result = np.eye(4)
    result[:2, :2] = [[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]]
    result[:2, 3] = [x, y]
    return result


def _motion(root: Path, value: object) -> tuple[dict[str, str], bytes, np.ndarray]:
    if not isinstance(value, Mapping) or set(value) != {"evidence", "before_stage", "after_stage"}:
        raise ValueError("motion must bind evidence and two stage names")
    if not isinstance(value["before_stage"], str) or not isinstance(value["after_stage"], str):
        raise ValueError("motion stages must be text")
    pin, payload = _pin(root, value["evidence"], "motion evidence", 2 * 1024 * 1024)
    stages = _object(payload, "motion evidence").get("stages")
    if not isinstance(stages, Mapping):
        raise ValueError("motion evidence has no stages")
    try:
        before, after = stages[value["before_stage"]], stages[value["after_stage"]]
        before_pose, after_pose = before["pose"], after["pose"]
    except (KeyError, TypeError) as error:
        raise ValueError("motion evidence does not contain declared stages") from error
    return (
        pin,
        payload,
        np.linalg.inv(_pose(before_pose, "motion before pose"))
        @ _pose(after_pose, "motion after pose"),
    )


def _rotation(vector: np.ndarray) -> np.ndarray:
    return cv2.Rodrigues(vector.reshape(3, 1))[0]


def _log(rotation: np.ndarray) -> np.ndarray:
    return cv2.Rodrigues(rotation)[0].reshape(3)


def _residual(
    parameters: np.ndarray, observations: Sequence[tuple[np.ndarray, np.ndarray, int]], tags: int
) -> np.ndarray:
    camera = np.eye(4)
    camera[:3, :3], camera[:3, 3] = _rotation(parameters[:3]), parameters[3:6]
    tag_parameters = parameters[6:].reshape(tags, 3)
    residuals = []
    for body, camera_tag, tag_id in observations:
        world_tag = body @ camera @ camera_tag
        x, y, yaw = tag_parameters[tag_id]
        expected = np.array(
            [[math.cos(yaw), -math.sin(yaw), 0], [math.sin(yaw), math.cos(yaw), 0], [0, 0, 1]]
        )
        residuals.extend(world_tag[:3, 3] - [x, y, 0])
        residuals.extend(_log(expected.T @ world_tag[:3, :3]))
    return np.asarray(residuals)


def _fit(
    observations: Sequence[tuple[np.ndarray, np.ndarray, int]], tags: int
) -> tuple[np.ndarray, int, float, np.ndarray]:
    if len(observations) < 2:
        raise ValueError("at least two accepted tag observations are required")
    parameters = np.zeros(6 + 3 * tags)
    damping = 1e-5
    for _ in range(80):
        residual = _residual(parameters, observations, tags)
        jacobian = np.empty((len(residual), len(parameters)))
        for column in range(len(parameters)):
            step = 1e-6
            shifted = parameters.copy()
            shifted[column] += step
            jacobian[:, column] = (_residual(shifted, observations, tags) - residual) / step
        update = np.linalg.solve(
            jacobian.T @ jacobian + damping * np.eye(len(parameters)), -jacobian.T @ residual
        )
        parameters += update
        if np.linalg.norm(update) < 1e-8:
            break
    singular = np.linalg.svd(jacobian, compute_uv=False)
    rank = int(np.count_nonzero(singular > singular[0] * 1e-8)) if len(singular) else 0
    condition = float(singular[0] / singular[-1]) if singular[-1] > 0 else float("inf")
    return parameters, rank, condition, _residual(parameters, observations, tags)


def build(request_path: Path, evidence_root: Path, output: Path) -> dict[str, object]:
    request_payload = _read(request_path, 1024 * 1024)
    request = _object(request_payload, "mount request")
    required = {
        "schema_version",
        "kind",
        "camera_serial",
        "intrinsics",
        "floor",
        "tag_ids",
        "motions",
        "captures",
    }
    if (
        set(request) != required
        or request["schema_version"] != 1
        or request["kind"] != "ohmni_fixed_head_mount_request"
    ):
        raise ValueError("mount request schema is invalid")
    floor = request["floor"]
    if floor != {"z_m": 0.0, "normal": [0.0, 0.0, 1.0], "tag_size_m": TAG_SIZE_M}:
        raise ValueError("mount request requires the declared floor plane and tag size")
    if (
        not isinstance(request["camera_serial"], str)
        or not isinstance(request["motions"], list)
        or not isinstance(request["captures"], list)
        or not isinstance(request["tag_ids"], list)
        or not 1 <= len(request["tag_ids"]) <= 64
        or any(type(identifier) is not int for identifier in request["tag_ids"])
    ):
        raise ValueError("mount request fields are invalid")
    if not 2 <= len(request["captures"]) <= MAX_CAPTURES or len(request["motions"]) + 1 < len(
        request["captures"]
    ):
        raise ValueError("mount request requires ordered captures and motions")
    intrinsics_pin, intrinsics_payload = _pin(
        evidence_root, request["intrinsics"], "intrinsics", 1024 * 1024
    )
    calibration = _object(intrinsics_payload, "intrinsics")
    detector = CameraTagDetector(
        calibration,
        camera_serial=request["camera_serial"],
        tag_sizes_m={identifier: TAG_SIZE_M for identifier in request["tag_ids"]},
    )
    transforms = [np.eye(4)]
    motion_pins, motion_payloads, deltas = [], [], []
    for item in request["motions"]:
        pin, payload, delta = _motion(evidence_root, item)
        motion_pins.append(pin)
        motion_payloads.append(payload)
        deltas.append(delta)
        transforms.append(transforms[-1] @ delta)
    rotations = [abs(math.atan2(delta[1, 0], delta[0, 0])) for delta in deltas]
    translations = [float(np.linalg.norm(delta[:2, 3])) for delta in deltas]
    if not any(value > math.radians(1) for value in rotations) or not any(
        value > 0.02 for value in translations
    ):
        raise ValueError("mount observations require both yaw and translation excitation")
    observations, frame_pins, frame_payloads = [], [], []
    tag_indexes: dict[int, int] = {}
    for item in request["captures"]:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"chain_index", "head_position", "frame"}
            or item["head_position"] != FIXED_HEAD_POSITION
        ):
            raise ValueError("each capture must use fixed head position 368")
        index = item["chain_index"]
        if type(index) is not int or not 0 <= index < len(transforms):
            raise ValueError("capture chain index is invalid")
        pin, payload = _pin(evidence_root, item["frame"], "raw frame", MAX_FRAME_BYTES)
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("raw frame cannot be decoded")
        accepted = [item for item in detector.detect(image) if item["pose_accepted"] is True]
        if not accepted:
            raise ValueError("raw frame has no accepted floor-tag pose")
        for detection in accepted:
            tag_id = detection["tag_id"]
            tag_indexes.setdefault(tag_id, len(tag_indexes))
            observations.append(
                (
                    transforms[index],
                    np.asarray(detection["T_camera_tag"], dtype=float),
                    tag_indexes[tag_id],
                )
            )
        frame_pins.append(pin)
        frame_payloads.append(payload)
    parameters, rank, condition, residual = _fit(observations, len(tag_indexes))
    if rank < len(parameters) or not math.isfinite(condition) or condition > 1e8:
        raise ValueError("mount observations are rank-deficient or poorly conditioned")
    output = output.absolute()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    published = False
    try:
        inputs = temporary / "inputs"
        inputs.mkdir()
        (inputs / "request.json").write_bytes(request_payload)
        (inputs / "intrinsics.json").write_bytes(intrinsics_payload)
        for index, payload in enumerate(frame_payloads):
            (inputs / f"frame-{index:03d}.bin").write_bytes(payload)
        for index, payload in enumerate(motion_payloads):
            (inputs / f"motion-{index:03d}.json").write_bytes(payload)
        camera = np.eye(4)
        camera[:3, :3], camera[:3, 3] = _rotation(parameters[:3]), parameters[3:6]
        result = {
            "schema_version": 1,
            "kind": "ohmni_fixed_head_body_camera_mount_candidate",
            "approval_status": "unapproved",
            "head_position": FIXED_HEAD_POSITION,
            "camera_serial": request["camera_serial"],
            "T_body_camera": camera.tolist(),
            "inputs": {
                "request_sha256": _digest(request_payload),
                "intrinsics": intrinsics_pin,
                "frames": frame_pins,
                "motions": motion_pins,
            },
            "diagnostics": {
                "observation_count": len(observations),
                "tag_count": len(tag_indexes),
                "jacobian_rank": rank,
                "jacobian_columns": len(parameters),
                "condition_number": condition,
                "rms_transform_residual": float(np.sqrt(np.mean(residual * residual))),
                "held_out": "not_evaluated",
                "leave_one_motion_out": "not_evaluated",
            },
        }
        (temporary / "candidate.json").write_text(
            json.dumps(result, allow_nan=False, sort_keys=True) + "\n"
        )
        os.rename(temporary, output)
        published = True
        return result
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = build(args.request, args.evidence_root, args.output)
    except (OSError, ValueError, TypeError, cv2.error) as error:
        raise SystemExit(f"fixed-head mount candidate failed: {error}") from error
    print(json.dumps({"output": str(args.output), "approval_status": result["approval_status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

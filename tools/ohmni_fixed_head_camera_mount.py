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
from perception.tag_localization import tag_corners

TAG_SIZE_M = 0.199898
FIXED_HEAD_POSITION = 368
FIXED_HEAD_RAW_TARGET = 66363
ENCODER_MODULUS = 16384
MAX_ENCODER_PAIR_SKEW_NS = 50_000_000
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


def _absolute_pin(value: object, name: str, maximum: int) -> tuple[dict[str, str], bytes]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise ValueError(f"{name} must be an absolute hash pin")
    path, digest = value["path"], value["sha256"]
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(f"{name} pin is invalid")
    payload = _read(Path(path), maximum)
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


def _motion(
    root: Path, value: object
) -> tuple[dict[str, str], bytes, dict[str, object], np.ndarray]:
    if not isinstance(value, Mapping) or set(value) != {"evidence", "before_stage", "after_stage"}:
        raise ValueError("motion must bind evidence and two stage names")
    if not isinstance(value["before_stage"], str) or not isinstance(value["after_stage"], str):
        raise ValueError("motion stages must be text")
    pin, payload = _pin(root, value["evidence"], "motion evidence", 2 * 1024 * 1024)
    document = _object(payload, "motion evidence")
    if set(document) != {
        "schema_version",
        "kind",
        "device_id",
        "boot_id",
        "camera_serial",
        "motion_chain_id",
        "chain_index",
        "stages",
    }:
        raise ValueError("motion evidence schema is invalid")
    if (
        document["schema_version"] != 1
        or document["kind"] != "ohmni_fixed_head_motion"
        or type(document["device_id"]) is not int
        or any(
            not isinstance(document[key], str) or not document[key]
            for key in ("boot_id", "camera_serial", "motion_chain_id")
        )
        or type(document["chain_index"]) is not int
        or document["chain_index"] < 0
    ):
        raise ValueError("motion evidence values are invalid")
    stages = document["stages"]
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
        document,
        np.linalg.inv(_pose(before_pose, "motion before pose"))
        @ _pose(after_pose, "motion after pose"),
    )


def _stage(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "pose",
        "encoder",
        "neck_position",
        "stationary",
        "state_id",
        "started_monotonic_s",
        "completed_monotonic_s",
    }:
        raise ValueError(f"{name} is invalid")
    _pose(value["pose"], f"{name} pose")
    encoder = value["encoder"]
    if (
        not isinstance(encoder, Mapping)
        or set(encoder) != {"left", "right"}
        or any(type(encoder[key]) is not int for key in encoder)
        or value["neck_position"] != FIXED_HEAD_POSITION
        or value["stationary"] is not True
        or not isinstance(value["state_id"], str)
        or not value["state_id"]
        or not all(
            type(value[key]) in (int, float) and math.isfinite(value[key])
            for key in ("started_monotonic_s", "completed_monotonic_s")
        )
        or value["completed_monotonic_s"] < value["started_monotonic_s"]
    ):
        raise ValueError(f"{name} must retain stationary fixed-head encoder evidence")
    return {
        "pose": dict(value["pose"]),
        "encoder": dict(encoder),
        "state_id": value["state_id"],
        "started_monotonic_s": value["started_monotonic_s"],
        "completed_monotonic_s": value["completed_monotonic_s"],
    }


def _capture_manifest(
    root: Path,
    value: object,
    *,
    frame_pin: Mapping[str, str],
    identity: Mapping[str, object],
    expected_stage: str,
    stage: Mapping[str, object],
) -> tuple[dict[str, str], bytes]:
    pin, payload = _pin(root, value, "raw frame manifest", 1024 * 1024)
    document = _object(payload, "raw frame manifest")
    if set(document) != {
        "schema_version",
        "kind",
        "device_id",
        "boot_id",
        "camera_serial",
        "motion_chain_id",
        "stage",
        "state_id",
        "neck_position",
        "stationary",
        "encoder",
        "frame",
    }:
        raise ValueError("raw frame manifest schema is invalid")
    if (
        document["schema_version"] != 1
        or document["kind"] != "ohmni_fixed_head_camera_capture"
        or any(document[key] != identity[key] for key in identity)
        or document["stage"] != expected_stage
        or document["state_id"] != stage["state_id"]
        or document["neck_position"] != FIXED_HEAD_POSITION
        or document["stationary"] is not True
        or document["encoder"] != stage["encoder"]
        or document["frame"] != frame_pin
    ):
        raise ValueError("raw frame manifest does not bind the fixed-head motion endpoint")
    return pin, payload


def _rotation(vector: np.ndarray) -> np.ndarray:
    return cv2.Rodrigues(vector.reshape(3, 1))[0]


def _log(rotation: np.ndarray) -> np.ndarray:
    return cv2.Rodrigues(rotation)[0].reshape(3)


def _pixel_residual(
    parameters: np.ndarray,
    observations: Sequence[tuple[np.ndarray, np.ndarray, int]],
    tags: int,
    camera_matrix: np.ndarray,
    distortion: np.ndarray,
) -> np.ndarray:
    camera = np.eye(4)
    camera[:3, :3], camera[:3, 3] = _rotation(parameters[:3]), parameters[3:6]
    tag_parameters = parameters[6:].reshape(tags, 3)
    residuals: list[float] = []
    for body, corners, tag_id, *_ in observations:
        x, y, yaw = tag_parameters[tag_id]
        world_tag = np.eye(4)
        world_tag[:3, :3] = [
            [math.cos(yaw), -math.sin(yaw), 0],
            [math.sin(yaw), math.cos(yaw), 0],
            [0, 0, 1],
        ]
        world_tag[:3, 3] = [x, y, 0]
        camera_tag = np.linalg.inv(body @ camera) @ world_tag
        projected = cv2.projectPoints(
            tag_corners(TAG_SIZE_M),
            cv2.Rodrigues(camera_tag[:3, :3])[0],
            camera_tag[:3, 3],
            camera_matrix,
            distortion,
        )[0].reshape(4, 2)
        residuals.extend((projected - corners).reshape(-1))
    return np.asarray(residuals)


def _initial_parameters(
    observations: Sequence[tuple[np.ndarray, np.ndarray, int]],
    tags: int,
    camera_matrix: np.ndarray,
    distortion: np.ndarray,
) -> np.ndarray:
    parameters = np.zeros(6 + 3 * tags)
    body, corners, _, *seed = observations[0]
    if seed:
        camera_tag = _matrix(seed[0], "detected camera-tag pose")
    else:
        success, vector, translation = cv2.solvePnP(
            tag_corners(TAG_SIZE_M),
            corners,
            camera_matrix,
            distortion,
            flags=cv2.SOLVEPNP_IPPE_SQUARE,
        )
        if not success or translation[2, 0] <= 0:
            raise ValueError("could not initialize floor-tag reprojection fit")
        camera_tag = np.eye(4)
        camera_tag[:3, :3] = _rotation(vector.reshape(3))
        camera_tag[:3, 3] = translation.reshape(3)
    inferred_tag = body @ camera_tag
    yaw = math.atan2(inferred_tag[1, 0], inferred_tag[0, 0])
    world_tag = np.eye(4)
    world_tag[:3, :3] = [
        [math.cos(yaw), -math.sin(yaw), 0],
        [math.sin(yaw), math.cos(yaw), 0],
        [0, 0, 1],
    ]
    world_tag[:2, 3] = inferred_tag[:2, 3]
    camera = np.linalg.inv(body) @ world_tag @ np.linalg.inv(camera_tag)
    parameters[:3] = _log(camera[:3, :3])
    parameters[3:6] = camera[:3, 3]
    for observation_body, observation_corners, tag_id, *seed in observations:
        if seed:
            camera_tag = _matrix(seed[0], "detected camera-tag pose")
        else:
            success, vector, translation = cv2.solvePnP(
                tag_corners(TAG_SIZE_M),
                observation_corners,
                camera_matrix,
                distortion,
                flags=cv2.SOLVEPNP_IPPE_SQUARE,
            )
            if not success or translation[2, 0] <= 0:
                continue
            camera_tag = np.eye(4)
            camera_tag[:3, :3] = _rotation(vector.reshape(3))
            camera_tag[:3, 3] = translation.reshape(3)
        inferred_tag = observation_body @ camera @ camera_tag
        parameters[6 + 3 * tag_id : 9 + 3 * tag_id] = (
            inferred_tag[0, 3],
            inferred_tag[1, 3],
            math.atan2(inferred_tag[1, 0], inferred_tag[0, 0]),
        )
    return parameters


def _fit(
    observations: Sequence[tuple[np.ndarray, np.ndarray, int]],
    tags: int,
    camera_matrix: np.ndarray,
    distortion: np.ndarray,
) -> tuple[np.ndarray, int, float, np.ndarray]:
    if len(observations) < 2:
        raise ValueError("at least two accepted tag observations are required")
    parameters = _initial_parameters(observations, tags, camera_matrix, distortion)
    damping = 1e-5
    for _ in range(80):
        residual = _pixel_residual(parameters, observations, tags, camera_matrix, distortion)
        jacobian = np.empty((len(residual), len(parameters)))
        for column in range(len(parameters)):
            step = 1e-6
            shifted = parameters.copy()
            shifted[column] += step
            jacobian[:, column] = (
                _pixel_residual(shifted, observations, tags, camera_matrix, distortion) - residual
            ) / step
        try:
            update = np.linalg.solve(
                jacobian.T @ jacobian + damping * np.eye(len(parameters)), -jacobian.T @ residual
            )
        except np.linalg.LinAlgError as error:
            raise ValueError("floor-tag reprojection fit is singular") from error
        candidate = parameters + update
        candidate_residual = _pixel_residual(
            candidate, observations, tags, camera_matrix, distortion
        )
        if np.dot(candidate_residual, candidate_residual) < np.dot(residual, residual):
            parameters = candidate
            damping = max(damping / 3, 1e-10)
        else:
            damping *= 10
        if np.linalg.norm(update) < 1e-8:
            break
    singular = np.linalg.svd(jacobian, compute_uv=False)
    rank = int(np.count_nonzero(singular > singular[0] * 1e-8)) if len(singular) else 0
    condition = float(singular[0] / singular[-1]) if singular[-1] > 0 else float("inf")
    return (
        parameters,
        rank,
        condition,
        _pixel_residual(parameters, observations, tags, camera_matrix, distortion),
    )


def _resampling(
    grouped: Sequence[tuple[int, tuple[np.ndarray, np.ndarray, int]]],
    tags: int,
    camera_matrix: np.ndarray,
    distortion: np.ndarray,
) -> dict[str, object]:
    def evaluate(name: str, held: set[int]) -> dict[str, object]:
        train = [observation for index, observation in grouped if index not in held]
        test = [observation for index, observation in grouped if index in held]
        if not train or not test:
            return {"name": name, "status": "unavailable"}
        parameters, rank, condition, _ = _fit(train, tags, camera_matrix, distortion)
        if rank < len(parameters) or not math.isfinite(condition) or condition > 1e8:
            return {"name": name, "status": "underconstrained", "rank": rank}
        residual = _pixel_residual(parameters, test, tags, camera_matrix, distortion)
        return {
            "name": name,
            "status": "evaluated",
            "rms_reprojection_error_px": float(np.sqrt(np.mean(residual * residual))),
        }

    last_capture = max(index for index, _ in grouped)
    return {
        "held_out": evaluate("last_capture", {last_capture}),
        "leave_one_capture_state_out": [
            evaluate(f"capture_state_{index}", {index})
            for index in sorted({index for index, _ in grouped})
        ],
    }


def _retained_neck(value: object, name: str, boot_id: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "boot_id",
        "flags",
        "position",
        "receipt_ns",
        "request_ns",
        "target",
    }:
        raise ValueError(f"{name} neck feedback schema is invalid")
    if (
        value["boot_id"] != boot_id
        or value["flags"] != "NONE"
        or value["target"] != FIXED_HEAD_RAW_TARGET
        or any(type(value[key]) is not int for key in ("position", "receipt_ns", "request_ns"))
        or value["receipt_ns"] < value["request_ns"]
        or abs(value["position"] - FIXED_HEAD_RAW_TARGET) >= 400
    ):
        raise ValueError(f"{name} does not prove the settled fixed head")
    return dict(value)


def _tick_distance(left: int, right: int) -> int:
    difference = abs(left - right) % ENCODER_MODULUS
    return min(difference, ENCODER_MODULUS - difference)


def _retained_encoders(
    value: object, name: str, boot_id: str
) -> tuple[dict[str, int], tuple[int, int]]:
    if not isinstance(value, Mapping) or set(value) != {"boot_id", "clock", "read_only", "samples"}:
        raise ValueError(f"{name} encoder schema is invalid")
    samples = value["samples"]
    if (
        value["boot_id"] != boot_id
        or value["clock"] != "linux_monotonic"
        or value["read_only"] is not True
        or not isinstance(samples, list)
        or len(samples) < 3
    ):
        raise ValueError(f"{name} does not retain paired stationary encoders")
    for sample in samples:
        if (
            not isinstance(sample, Mapping)
            or set(sample) != {"poll_id", "left", "right", "left_receipt_ns", "right_receipt_ns"}
            or any(type(sample[key]) is not int for key in sample)
            or not 0 <= sample["left"] < ENCODER_MODULUS
            or not 0 <= sample["right"] < ENCODER_MODULUS
        ):
            raise ValueError(f"{name} encoder samples are invalid")
    if any(
        later["poll_id"] <= earlier["poll_id"]
        or later["left_receipt_ns"] <= earlier["left_receipt_ns"]
        or later["right_receipt_ns"] <= earlier["right_receipt_ns"]
        for earlier, later in zip(samples, samples[1:], strict=False)
    ):
        raise ValueError(f"{name} encoder samples are not ordered")
    if any(
        abs(sample["left_receipt_ns"] - sample["right_receipt_ns"]) > MAX_ENCODER_PAIR_SKEW_NS
        for sample in samples
    ):
        raise ValueError(f"{name} paired encoder receipt skew is too large")
    for wheel in ("left", "right"):
        values = [sample[wheel] for sample in samples]
        if any(_tick_distance(first, second) > 16 for first in values for second in values):
            raise ValueError(f"{name} encoder samples are not stationary")
    return (
        {"left": samples[-1]["left"], "right": samples[-1]["right"]},
        (samples[0]["left_receipt_ns"], samples[-1]["right_receipt_ns"]),
    )


def _motion_endpoint_encoder(stage: Mapping[str, object], name: str) -> tuple[dict[str, int], int]:
    direct = stage.get("encoder")
    if isinstance(direct, Mapping):
        if (
            type(direct.get("left")) is int
            and type(direct.get("right")) is int
            and type(direct.get("right_receipt_ns")) is int
        ):
            return {"left": direct["left"], "right": direct["right"]}, direct["right_receipt_ns"]
    pairs = stage.get("paired_pose_samples")
    if isinstance(pairs, list) and pairs:
        endpoint = pairs[-1]
        if isinstance(endpoint, Mapping) and isinstance(endpoint.get("encoder"), Mapping):
            return _motion_endpoint_encoder({"encoder": endpoint["encoder"]}, name)
    raise ValueError(f"retained motion {name} has no paired encoder endpoint")


def _retained_step(value: object, boot_id: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "boot_id",
        "status",
        "command",
        "before",
        "settled",
        "samples",
    }:
        raise ValueError("neck step schema is invalid")
    if (
        value["boot_id"] != boot_id
        or value["status"] != "completed"
        or value["command"] != "neck_angle 368"
    ):
        raise ValueError("neck step does not command the fixed head")
    settled = value["settled"]
    if not isinstance(settled, Mapping):
        raise ValueError("neck step settled feedback is invalid")
    settled_with_boot = dict(settled)
    if "boot_id" not in settled_with_boot:
        settled_with_boot["boot_id"] = boot_id
    _retained_neck(settled_with_boot, "neck step settled", boot_id)
    samples = value["samples"]
    if not isinstance(samples, list):
        raise ValueError("neck step samples are invalid")
    settled = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise ValueError("neck step samples are invalid")
        if (
            sample.get("flags") == "NONE"
            and sample.get("target") == FIXED_HEAD_RAW_TARGET
            and type(sample.get("position")) is int
            and abs(sample["position"] - FIXED_HEAD_RAW_TARGET) < 400
        ):
            settled.append(sample["position"])
        else:
            settled.clear()
        if len(settled) >= 3 and max(settled[-3:]) - min(settled[-3:]) < 80:
            return
    raise ValueError("neck step does not retain three settled feedback samples")


def _retained_motion(
    document: Mapping[str, object],
    item: Mapping[str, object],
    identity: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    if set(item) != {"record", "before_stage", "after_stage"}:
        raise ValueError("retained motion must bind a record and two stages")
    if (
        document.get("schema_version") != 1
        or document.get("kind") != "camera_calibration_poses"
        or any(document.get(key) != identity[key] for key in identity)
        or not isinstance(item["before_stage"], str)
        or not isinstance(item["after_stage"], str)
        or not isinstance(document.get("stages"), Mapping)
    ):
        raise ValueError("retained motion schema or identity is invalid")
    try:
        before = document["stages"][item["before_stage"]]
        after = document["stages"][item["after_stage"]]
    except KeyError as error:
        raise ValueError("retained motion has no declared stage") from error
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise ValueError("retained motion stages are invalid")
    for name, stage in (("before", before), ("after", after)):
        _pose(stage.get("pose"), f"retained motion {name} pose")
        encoder = stage.get("encoder")
        pairs = stage.get("paired_pose_samples")
        direct_encoder = (
            isinstance(encoder, Mapping)
            and type(encoder.get("left")) is int
            and type(encoder.get("right")) is int
            and type(encoder.get("left_receipt_ns")) is int
            and type(encoder.get("right_receipt_ns")) is int
            and abs(encoder["left_receipt_ns"] - encoder["right_receipt_ns"])
            <= MAX_ENCODER_PAIR_SKEW_NS
        )
        paired_encoder = (
            isinstance(pairs, list)
            and bool(pairs)
            and all(
                isinstance(pair, Mapping)
                and isinstance(pair.get("pose"), Mapping)
                and isinstance(pair.get("encoder"), Mapping)
                and type(pair["encoder"].get("left")) is int
                and type(pair["encoder"].get("right")) is int
                and type(pair["encoder"].get("left_receipt_ns")) is int
                and type(pair["encoder"].get("right_receipt_ns")) is int
                for pair in pairs
            )
        )
        if pairs is not None and not paired_encoder:
            raise ValueError(f"retained motion {name} paired samples are invalid")
        if paired_encoder:
            for pair in pairs:
                _pose(pair["pose"], f"retained motion {name} paired pose")
        if (
            (not direct_encoder and not paired_encoder)
            or not all(
                type(stage.get(key)) in (int, float) and math.isfinite(stage[key])
                for key in ("stage_started_monotonic_s", "stage_completed_monotonic_s")
            )
            or stage["stage_completed_monotonic_s"] < stage["stage_started_monotonic_s"]
        ):
            raise ValueError(f"retained motion {name} stage is invalid")
    return dict(before), dict(after)


def _copy_staged(root: Path, name: str, payload: bytes) -> dict[str, str]:
    (root / name).write_bytes(payload)
    return {"path": name, "sha256": _digest(payload)}


def _derived_id(label: str, pins: Sequence[Mapping[str, str]]) -> str:
    source = json.dumps({"label": label, "pins": list(pins)}, sort_keys=True, separators=(",", ":"))
    return _digest(source.encode())


def build(request_path: Path, evidence_root: Path, output: Path) -> dict[str, object]:
    request_payload = _read(request_path, 1024 * 1024)
    request = _object(request_payload, "mount request")
    required = {
        "schema_version",
        "kind",
        "device_id",
        "boot_id",
        "camera_serial",
        "motion_chain_id",
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
        type(request["device_id"]) is not int
        or not isinstance(request["boot_id"], str)
        or not request["boot_id"]
        or not isinstance(request["camera_serial"], str)
        or not isinstance(request["motion_chain_id"], str)
        or not request["motion_chain_id"]
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
    identity = {
        key: request[key] for key in ("device_id", "boot_id", "camera_serial", "motion_chain_id")
    }
    motion_pins, motion_payloads, deltas, motion_documents = [], [], [], []
    previous_completed: float | None = None
    for motion_index, item in enumerate(request["motions"]):
        pin, payload, document, delta = _motion(evidence_root, item)
        if any(document[key] != identity[key] for key in identity):
            raise ValueError("motion evidence identity does not match the camera request")
        if document["chain_index"] != motion_index:
            raise ValueError("motion evidence is not in the declared chain order")
        for stage_name in (item["before_stage"], item["after_stage"]):
            _stage(document["stages"].get(stage_name), f"motion {stage_name}")
        before = _stage(document["stages"][item["before_stage"]], "motion before")
        after = _stage(document["stages"][item["after_stage"]], "motion after")
        if previous_completed is not None and before["started_monotonic_s"] < previous_completed:
            raise ValueError("motion evidence is not chronological")
        previous_completed = float(after["completed_monotonic_s"])
        motion_pins.append(pin)
        motion_payloads.append(payload)
        motion_documents.append(document)
        deltas.append(delta)
        transforms.append(transforms[-1] @ delta)
    rotations = [abs(math.atan2(delta[1, 0], delta[0, 0])) for delta in deltas]
    translations = [float(np.linalg.norm(delta[:2, 3])) for delta in deltas]
    if not any(value > math.radians(1) for value in rotations) or not any(
        value > 0.02 for value in translations
    ):
        raise ValueError("mount observations require both yaw and translation excitation")
    if {
        item.get("chain_index") for item in request["captures"] if isinstance(item, Mapping)
    } != set(range(len(transforms))):
        raise ValueError("captures must retain every motion-chain endpoint")
    (
        observations,
        grouped,
        frame_pins,
        frame_payloads,
        frame_manifest_pins,
        frame_manifest_payloads,
    ) = ([], [], [], [], [], [])
    tag_indexes: dict[int, int] = {}
    for item in request["captures"]:
        if not isinstance(item, Mapping) or set(item) != {"chain_index", "frame", "manifest"}:
            raise ValueError("each capture must use fixed head position 368")
        index = item["chain_index"]
        if type(index) is not int or not 0 <= index < len(transforms):
            raise ValueError("capture chain index is invalid")
        pin, payload = _pin(evidence_root, item["frame"], "raw frame", MAX_FRAME_BYTES)
        motion_index = 0 if index == 0 else index - 1
        stage_name = (
            request["motions"][motion_index]["before_stage"]
            if index == 0
            else request["motions"][motion_index]["after_stage"]
        )
        stage = _stage(motion_documents[motion_index]["stages"][stage_name], f"motion {stage_name}")
        manifest_pin, manifest_payload = _capture_manifest(
            evidence_root,
            item["manifest"],
            frame_pin=pin,
            identity=identity,
            expected_stage=stage_name,
            stage=stage,
        )
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("raw frame cannot be decoded")
        accepted = [item for item in detector.detect(image) if item["pose_accepted"] is True]
        if not accepted:
            raise ValueError("raw frame has no accepted floor-tag pose")
        for detection in accepted:
            tag_id = detection["tag_id"]
            tag_indexes.setdefault(tag_id, len(tag_indexes))
            observation = (
                transforms[index],
                np.asarray(detection["corners_px"], dtype=float),
                tag_indexes[tag_id],
                np.asarray(detection["T_camera_tag"], dtype=float),
            )
            observations.append(observation)
            grouped.append((index, observation))
        frame_pins.append(pin)
        frame_payloads.append(payload)
        frame_manifest_pins.append(manifest_pin)
        frame_manifest_payloads.append(manifest_payload)
    parameters, rank, condition, residual = _fit(
        observations, len(tag_indexes), detector.K, detector.D
    )
    if rank < len(parameters) or not math.isfinite(condition) or condition > 1e8:
        raise ValueError("mount observations are rank-deficient or poorly conditioned")
    resampling = _resampling(grouped, len(tag_indexes), detector.K, detector.D)
    evaluations = [resampling["held_out"], *resampling["leave_one_capture_state_out"]]
    if any(
        item["status"] != "evaluated" or item["rms_reprojection_error_px"] > 3
        for item in evaluations
    ):
        raise ValueError("mount held-out capture-state validation failed")
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
        for index, payload in enumerate(frame_manifest_payloads):
            (inputs / f"frame-{index:03d}-manifest.json").write_bytes(payload)
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
                "frame_manifests": frame_manifest_pins,
                "motions": motion_pins,
            },
            "diagnostics": {
                "observation_count": len(observations),
                "tag_count": len(tag_indexes),
                "jacobian_rank": rank,
                "jacobian_columns": len(parameters),
                "condition_number": condition,
                "rms_reprojection_error_px": float(np.sqrt(np.mean(residual * residual))),
                **resampling,
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


def build_retained(request_path: Path, output: Path) -> dict[str, object]:
    """Build a candidate from pinned camera and motion artifacts retained by the robot runs."""
    request_payload = _read(request_path, 1024 * 1024)
    request = _object(request_payload, "retained mount request")
    required = {
        "schema_version",
        "kind",
        "device_id",
        "boot_id",
        "camera_serial",
        "intrinsics",
        "tag_ids",
        "motions",
        "captures",
    }
    if (
        set(request) != required
        or request["schema_version"] != 1
        or request["kind"] != "ohmni_retained_fixed_head_mount_request"
        or type(request["device_id"]) is not int
        or not isinstance(request["boot_id"], str)
        or not request["boot_id"]
        or not isinstance(request["camera_serial"], str)
        or not request["camera_serial"]
        or not isinstance(request["tag_ids"], list)
        or not 1 <= len(request["tag_ids"]) <= 64
        or any(type(tag_id) is not int for tag_id in request["tag_ids"])
        or not isinstance(request["motions"], list)
        or not isinstance(request["captures"], list)
        or len(request["captures"]) != len(request["motions"]) + 1
        or not 2 <= len(request["captures"]) <= MAX_CAPTURES
    ):
        raise ValueError("retained mount request schema is invalid")
    identity = {key: request[key] for key in ("device_id", "boot_id", "camera_serial")}
    intrinsics_pin, intrinsics_payload = _absolute_pin(
        request["intrinsics"], "retained intrinsics", 1024 * 1024
    )
    _object(intrinsics_payload, "retained intrinsics")

    motion_pins: list[dict[str, str]] = []
    motion_documents: list[tuple[dict[str, object], dict[str, object], bytes]] = []
    previous_motion_completed: float | None = None
    for item in request["motions"]:
        if not isinstance(item, Mapping):
            raise ValueError("retained motion is invalid")
        pin, payload = _absolute_pin(item.get("record"), "retained motion", 2 * 1024 * 1024)
        before, after = _retained_motion(_object(payload, "retained motion"), item, identity)
        if (
            previous_motion_completed is not None
            and before["stage_started_monotonic_s"] < previous_motion_completed
        ):
            raise ValueError("retained motions are not chronological")
        previous_motion_completed = float(after["stage_completed_monotonic_s"])
        motion_pins.append(pin)
        motion_documents.append((before, after, payload))

    capture_keys = (
        "plan",
        "neck_helper",
        "neck_step",
        "neck_before",
        "neck_after",
        "encoders_before",
        "encoders_after",
        "capture_manifest",
        "capture_result",
        "frame",
        "raw_frame",
    )
    captures: list[dict[str, object]] = []
    capture_pins: list[dict[str, dict[str, str]]] = []
    capture_payloads: list[dict[str, bytes]] = []
    previous_capture_after_ns: int | None = None
    for item in request["captures"]:
        if not isinstance(item, Mapping) or set(item) != set(capture_keys):
            raise ValueError("retained capture schema is invalid")
        pins: dict[str, dict[str, str]] = {}
        payloads: dict[str, bytes] = {}
        limits = {
            "plan": 1024 * 1024,
            "neck_helper": 1024 * 1024,
            "neck_step": 1024 * 1024,
            "neck_before": 1024 * 1024,
            "neck_after": 1024 * 1024,
            "encoders_before": 1024 * 1024,
            "encoders_after": 1024 * 1024,
            "capture_manifest": 1024 * 1024,
            "capture_result": 4 * 1024 * 1024,
            "frame": MAX_FRAME_BYTES,
            "raw_frame": MAX_FRAME_BYTES,
        }
        for key in capture_keys:
            pins[key], payloads[key] = _absolute_pin(
                item[key], f"retained capture {key}", limits[key]
            )
        plan = _object(payloads["plan"], "retained capture plan")
        source_hashes = plan.get("source_sha256")
        if (
            plan.get("boot_id") != identity["boot_id"]
            or plan.get("base_actuation") != "none"
            or not isinstance(plan.get("head_angles"), list)
            or FIXED_HEAD_POSITION not in plan["head_angles"]
            or not isinstance(source_hashes, Mapping)
            or source_hashes.get(pins["neck_helper"]["path"]) != pins["neck_helper"]["sha256"]
        ):
            raise ValueError("retained capture plan does not pin the fixed-head helper")
        helper = payloads["neck_helper"].decode("utf-8", errors="replace").replace(" ", "")
        if not any(
            expression in helper
            for expression in (
                "target=int(-(args.target-480)*18961/32)",
                "target=int(-({args.target}-480)*18961/32)",
            )
        ):
            raise ValueError("retained neck helper does not prove the head conversion")
        _retained_step(_object(payloads["neck_step"], "retained neck step"), identity["boot_id"])
        before_neck = _retained_neck(
            _object(payloads["neck_before"], "retained neck before"),
            "retained neck before",
            identity["boot_id"],
        )
        after_neck = _retained_neck(
            _object(payloads["neck_after"], "retained neck after"),
            "retained neck after",
            identity["boot_id"],
        )
        if abs(before_neck["position"] - after_neck["position"]) >= 80:
            raise ValueError("retained neck endpoints are not stationary")
        if payloads["encoders_before"] == payloads["encoders_after"]:
            raise ValueError("retained encoder endpoints must be independently sampled")
        before_encoder, before_encoder_interval = _retained_encoders(
            _object(payloads["encoders_before"], "retained encoders before"),
            "retained encoders before",
            identity["boot_id"],
        )
        after_encoder, after_encoder_interval = _retained_encoders(
            _object(payloads["encoders_after"], "retained encoders after"),
            "retained encoders after",
            identity["boot_id"],
        )
        if before_encoder_interval[1] >= after_encoder_interval[0]:
            raise ValueError("retained encoder endpoint intervals overlap")
        if any(
            _tick_distance(before_encoder[wheel], after_encoder[wheel]) > 16
            for wheel in ("left", "right")
        ):
            raise ValueError("retained encoder endpoints are not stationary")
        manifest = _object(payloads["capture_manifest"], "retained capture manifest")
        try:
            camera = manifest["cameras"]["main"]
            shape = camera["shape_px"]
        except (KeyError, TypeError) as error:
            raise ValueError("retained capture manifest has no main camera") from error
        if (
            manifest.get("schema_version") != "ohmni-dual-calibration-capture/v2"
            or manifest.get("status") != "complete"
            or manifest.get("boot_id") != identity["boot_id"]
            or not isinstance(camera, Mapping)
            or camera.get("usb_serial") != identity["camera_serial"]
            or camera.get("pixel_format") != "UYVY"
            or not isinstance(shape, list)
            or len(shape) != 2
            or not all(type(value) is int and value > 0 for value in shape)
            or not isinstance(manifest.get("capture_pipeline_sha256"), str)
        ):
            raise ValueError("retained capture manifest identity is invalid")
        result = _object(payloads["capture_result"], "retained capture result")
        frames = result.get("frames")
        provenance = result.get("recorded_live_provenance")
        if (
            not isinstance(frames, list)
            or len(frames) != 1
            or not isinstance(frames[0], Mapping)
            or not isinstance(provenance, Mapping)
            or provenance.get("manifest_sha256") != pins["capture_manifest"]["sha256"]
        ):
            raise ValueError("retained capture result does not bind its manifest")
        frame = frames[0]
        if (
            frame.get("boot_id") != identity["boot_id"]
            or frame.get("camera") != "main"
            or frame.get("image_file") != "frame-000000.png"
            or frame.get("image_sha256") != pins["frame"]["sha256"]
            or frame.get("source_file") != "raw-000000.uyvy"
            or frame.get("source_sha256") != pins["raw_frame"]["sha256"]
            or frame.get("source_device_sha256") != pins["raw_frame"]["sha256"]
            or frame.get("capture_pipeline_sha256") != manifest["capture_pipeline_sha256"]
            or frame.get("shape_px") != shape
        ):
            raise ValueError("retained capture result does not bind its raw frame")
        width, height = shape
        if len(payloads["raw_frame"]) != width * height * 2:
            raise ValueError("retained raw UYVY size does not match the camera shape")
        png = cv2.imdecode(np.frombuffer(payloads["frame"], dtype=np.uint8), cv2.IMREAD_COLOR)
        raw = np.frombuffer(payloads["raw_frame"], dtype=np.uint8).reshape(height, width, 2)
        decoded = cv2.cvtColor(raw, cv2.COLOR_YUV2BGR_UYVY)
        if png is None or png.shape != decoded.shape or not np.array_equal(png, decoded):
            raise ValueError("retained PNG does not match the decoded raw UYVY frame")
        before_evidence_ns = min(before_neck["receipt_ns"], before_encoder_interval[0])
        after_evidence_ns = max(after_neck["receipt_ns"], after_encoder_interval[1])
        if (
            previous_capture_after_ns is not None
            and before_evidence_ns <= previous_capture_after_ns
        ):
            raise ValueError("retained captures are not chronological")
        previous_capture_after_ns = after_evidence_ns
        captures.append(
            {
                "encoder": after_encoder,
                "before_encoder": before_encoder,
                "before_evidence_ns": before_evidence_ns,
                "after_evidence_ns": after_evidence_ns,
                "frame_payload": payloads["frame"],
                "pins": pins,
            }
        )
        capture_pins.append(pins)
        capture_payloads.append(payloads)

    for index, (before, after, _) in enumerate(motion_documents):
        before_ns = int(before["stage_started_monotonic_s"] * 1_000_000_000)
        after_ns = int(after["stage_completed_monotonic_s"] * 1_000_000_000)
        if captures[index]["after_evidence_ns"] >= before_ns:
            raise ValueError("retained capture chronology does not bracket the motion")
        if captures[index + 1]["before_evidence_ns"] <= after_ns:
            raise ValueError("retained capture chronology does not bracket the motion")
        before_encoder, _ = _motion_endpoint_encoder(before, "before")
        after_encoder, _ = _motion_endpoint_encoder(after, "after")
        for capture, endpoint in (
            (captures[index], before_encoder),
            (captures[index + 1], after_encoder),
        ):
            if any(
                _tick_distance(capture["encoder"][wheel], endpoint[wheel]) > 16
                or _tick_distance(capture["before_encoder"][wheel], endpoint[wheel]) > 16
                for wheel in ("left", "right")
            ):
                raise ValueError("retained capture encoders do not bind the motion endpoint")

    all_pins = [intrinsics_pin, *motion_pins]
    for pins in capture_pins:
        all_pins.extend(pins[key] for key in capture_keys)
    chain_id = _derived_id("motion-chain", all_pins)
    state_ids = [_derived_id(f"capture-state-{index}", all_pins) for index in range(len(captures))]
    output = output.absolute()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    publication = Path(tempfile.mkdtemp(prefix=f".{output.name}.retained.", dir=output.parent))
    staging = Path(tempfile.mkdtemp(prefix="intermediate.", dir=publication))
    published = False
    try:
        staged_intrinsics = _copy_staged(staging, "intrinsics.json", intrinsics_payload)
        staged_motions = []
        staged_captures = []
        for index, (before, after, _payload) in enumerate(motion_documents):
            stages = {}
            for stage_name, source_stage, capture in (
                ("before", before, captures[index]),
                ("after", after, captures[index + 1]),
            ):
                stages[stage_name] = {
                    "pose": source_stage["pose"],
                    "encoder": capture["encoder"],
                    "neck_position": FIXED_HEAD_POSITION,
                    "stationary": True,
                    "state_id": state_ids[index if stage_name == "before" else index + 1],
                    "started_monotonic_s": source_stage["stage_started_monotonic_s"],
                    "completed_monotonic_s": source_stage["stage_completed_monotonic_s"],
                }
            intermediate = {
                "schema_version": 1,
                "kind": "ohmni_fixed_head_motion",
                **identity,
                "motion_chain_id": chain_id,
                "chain_index": index,
                "stages": stages,
            }
            motion_pin = _copy_staged(
                staging,
                f"motion-{index:03d}.json",
                json.dumps(intermediate, sort_keys=True).encode(),
            )
            staged_motions.append(
                {"evidence": motion_pin, "before_stage": "before", "after_stage": "after"}
            )
        for index, capture in enumerate(captures):
            frame_pin = _copy_staged(staging, f"frame-{index:03d}.png", capture["frame_payload"])
            stage = "before" if index == 0 else "after"
            manifest = {
                "schema_version": 1,
                "kind": "ohmni_fixed_head_camera_capture",
                **identity,
                "motion_chain_id": chain_id,
                "stage": stage,
                "state_id": state_ids[index],
                "neck_position": FIXED_HEAD_POSITION,
                "stationary": True,
                "encoder": capture["encoder"],
                "frame": frame_pin,
            }
            manifest_pin = _copy_staged(
                staging,
                f"frame-{index:03d}-manifest.json",
                json.dumps(manifest, sort_keys=True).encode(),
            )
            staged_captures.append(
                {"chain_index": index, "frame": frame_pin, "manifest": manifest_pin}
            )
        intermediate_request = {
            "schema_version": 1,
            "kind": "ohmni_fixed_head_mount_request",
            **identity,
            "motion_chain_id": chain_id,
            "intrinsics": staged_intrinsics,
            "floor": {"z_m": 0.0, "normal": [0.0, 0.0, 1.0], "tag_size_m": TAG_SIZE_M},
            "tag_ids": request["tag_ids"],
            "motions": staged_motions,
            "captures": staged_captures,
        }
        intermediate_path = staging / "request.json"
        intermediate_path.write_text(json.dumps(intermediate_request, sort_keys=True))
        candidate = publication / "candidate"
        result = build(intermediate_path, staging, candidate)
        retained = candidate / "retained-inputs"
        retained.mkdir()

        def snapshot(name: str, payload: bytes) -> dict[str, str]:
            (retained / name).parent.mkdir(parents=True, exist_ok=True)
            (retained / name).write_bytes(payload)
            return {"path": f"retained-inputs/{name}", "sha256": _digest(payload)}

        snapshot_request = snapshot("request.json", request_payload)
        snapshot_intrinsics = snapshot("intrinsics.json", intrinsics_payload)
        snapshot_motions = [
            snapshot(f"motions/{index:03d}.json", payload)
            for index, (_, _, payload) in enumerate(motion_documents)
        ]
        snapshot_captures = []
        suffixes = {
            "plan": "plan.json",
            "neck_helper": "step-neck-lowest-reconnect.py",
            "neck_step": "step-368.json",
            "neck_before": "neck-before.json",
            "neck_after": "neck-after.json",
            "encoders_before": "encoders-before.json",
            "encoders_after": "encoders-after.json",
            "capture_manifest": "capture/main/manifest.json",
            "capture_result": "capture/main/result.json",
            "frame": "capture/main/frame-000000.png",
            "raw_frame": "capture/main/raw-000000.uyvy",
        }
        for index, payloads in enumerate(capture_payloads):
            snapshot_captures.append(
                {
                    key: snapshot(f"captures/{index:03d}/{suffixes[key]}", payloads[key])
                    for key in capture_keys
                }
            )
        result["retained_inputs"] = {
            "request_sha256": _digest(request_payload),
            "intrinsics": intrinsics_pin,
            "motions": motion_pins,
            "captures": capture_pins,
            "snapshot": {
                "request": snapshot_request,
                "intrinsics": snapshot_intrinsics,
                "motions": snapshot_motions,
                "captures": snapshot_captures,
            },
        }
        (candidate / "candidate.json").write_text(
            json.dumps(result, allow_nan=False, sort_keys=True) + "\n"
        )
        os.rename(candidate, output)
        published = True
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if not published:
            shutil.rmtree(publication, ignore_errors=True)
        else:
            shutil.rmtree(publication, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    request_group = parser.add_mutually_exclusive_group(required=True)
    request_group.add_argument("--request", type=Path)
    request_group.add_argument("--retained-request", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.retained_request:
            if args.evidence_root:
                raise ValueError("retained requests resolve their own pinned artifacts")
            result = build_retained(args.retained_request, args.output)
        else:
            if args.evidence_root is None:
                raise ValueError("--evidence-root is required with --request")
            result = build(args.request, args.evidence_root, args.output)
    except (OSError, ValueError, TypeError, cv2.error) as error:
        raise SystemExit(f"fixed-head mount candidate failed: {error}") from error
    print(json.dumps({"output": str(args.output), "approval_status": result["approval_status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

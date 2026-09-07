"""Fuse time-associated canonical Ohmni observations into unapproved world-tag candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path

from perception.camera_tags import CameraTagDetector
from relay.observations import Observation, decode_observation
from tools.map_common import finite_number, parse_document, validate_transform

MAX_REQUEST_BYTES = 256 * 1024
MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_OBSERVATIONS = 1024
MAX_TAGS = 64
MAX_IDENTIFIER_CHARS = 128
MAX_SESSION_CHARS = 512
MAX_ASSOCIATION_NS = 100_000_000
MAX_TRANSLATION_SPREAD_M = 1.0
MAX_ROTATION_SPREAD_RAD = math.pi
MAX_TAPE_ERROR_M = 0.10


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _text(value: object, name: str, maximum: int = MAX_IDENTIFIER_CHARS) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value) > maximum
    ):
        raise ValueError(f"{name} must be canonical printable text")
    return value


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in range")
    return value


def _number(value: object, name: str, *, minimum: float | None = None) -> float:
    number = finite_number(value, name)
    if abs(number) > 1_000_000:
        raise ValueError(f"{name} exceeds the metric bound")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} is below its lower bound")
    return number


def _digest(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


def _scope(value: object, name: str = "source_scope") -> dict[str, object]:
    _require(
        isinstance(value, Mapping)
        and set(value) == {"session", "device_id", "connection_epoch", "source_id"},
        f"{name} requires the canonical source identity",
    )
    return {
        "session": _text(value["session"], f"{name}.session", MAX_SESSION_CHARS),
        "device_id": _integer(
            value["device_id"], f"{name}.device_id", minimum=1, maximum=2**31 - 1
        ),
        "connection_epoch": _integer(
            value["connection_epoch"], f"{name}.connection_epoch", minimum=1
        ),
        "source_id": _text(value["source_id"], f"{name}.source_id"),
    }


def _same_scope(event: Observation, scope: Mapping[str, object]) -> bool:
    submission = event.submission
    return all(getattr(submission, key) == value for key, value in scope.items())


def _matrix(value: object, name: str) -> list[list[float]]:
    return validate_transform(value)


def _multiply(
    left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]
) -> list[list[float]]:
    return [
        [sum(left[row][index] * right[index][column] for index in range(4)) for column in range(4)]
        for row in range(4)
    ]


def _translation(matrix: Sequence[Sequence[float]]) -> tuple[float, float, float]:
    return (matrix[0][3], matrix[1][3], matrix[2][3])


def _rotation_distance(left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]) -> float:
    trace = sum(left[row][column] * right[row][column] for row in range(3) for column in range(3))
    return math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0)))


def _pose_matrix(pose: Mapping[str, object]) -> list[list[float]]:
    x, y, z = (_number(pose[name], f"pose.{name}") for name in ("x_m", "y_m", "z_m"))
    qx, qy, qz, qw = (_number(pose[name], f"pose.{name}") for name in ("qx", "qy", "qz", "qw"))
    return [
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw), x],
        [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw), y],
        [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy), z],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _relative(value: object, name: str) -> Path:
    text = _text(value, name, maximum=512)
    path = Path(text)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"{name} must be a confined relative path")
    return path


class _Snapshots:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.total_bytes = 0

    def read(self, reference: object, name: str, *, maximum: int) -> tuple[bytes, dict[str, str]]:
        _require(
            isinstance(reference, Mapping) and set(reference) == {"path", "sha256"},
            f"{name} requires a path and SHA-256",
        )
        path = _relative(reference["path"], f"{name}.path")
        expected = _digest(reference["sha256"], f"{name}.sha256")
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory_fd = root_fd
        descriptor: int | None = None
        try:
            for part in path.parts[:-1]:
                next_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
                )
                if directory_fd != root_fd:
                    os.close(directory_fd)
                directory_fd = next_fd
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
                raise ValueError(f"{name} must be a bounded regular file")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                payload = stream.read(maximum + 1)
            if len(payload) > maximum:
                raise ValueError(f"{name} exceeds its byte limit")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory_fd != root_fd:
                os.close(directory_fd)
            os.close(root_fd)
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError(f"{name} hash does not match its pin")
        self.total_bytes += len(payload)
        if self.total_bytes > MAX_INPUT_BYTES:
            raise ValueError("fusion inputs exceed the global byte limit")
        return payload, {"path": path.as_posix(), "sha256": expected}


def _calibration(document: object, pin: Mapping[str, str]) -> dict[str, object]:
    _require(isinstance(document, dict), "fisheye calibration must be an object")
    _require(
        document.get("schema_version") == 2 and document.get("model") == "fisheye",
        "fusion requires a schema-v2 fisheye calibration",
    )
    serial = _text(document.get("camera_serial"), "calibration.camera_serial")
    # Reuse the detector's calibration loader so fusion accepts the same measured artifact.
    CameraTagDetector(document, camera_serial=serial, tag_sizes_m={0: 0.16})
    return {"camera_serial": serial, "pin": dict(pin)}


def _mount(document: object, calibration: Mapping[str, object]) -> dict[str, object]:
    _require(
        isinstance(document, Mapping)
        and set(document)
        == {
            "schema_version",
            "kind",
            "measurement_kind",
            "camera_serial",
            "body_frame",
            "camera_frame",
            "T_body_camera",
        },
        "mount schema is invalid",
    )
    _require(
        document["schema_version"] == 1
        and document["kind"] == "ohmni_camera_mount"
        and document["measurement_kind"] == "measured_rigid_mount",
        "mount must be a measured rigid body-to-camera transform",
    )
    _require(
        document["camera_serial"] == calibration["camera_serial"],
        "mount camera serial mismatches calibration",
    )
    body_frame = _text(document["body_frame"], "mount.body_frame")
    camera_frame = _text(document["camera_frame"], "mount.camera_frame")
    _require(
        body_frame != "world" and camera_frame != "world" and body_frame != camera_frame,
        "mount frames must be distinct local frames",
    )
    return {
        "body_frame": body_frame,
        "camera_frame": camera_frame,
        "T_body_camera": _matrix(document["T_body_camera"], "mount.T_body_camera"),
    }


def _registration(
    document: object, scope: Mapping[str, object], odom_frame: str
) -> list[list[float]]:
    _require(isinstance(document, Mapping), "registration must be an object")
    _require(
        document.get("schema_version") == 1
        and document.get("kind") == "ohmni_world_registration_candidate"
        and document.get("approval_status") == "unapproved",
        "registration must be an unapproved Ohmni world-registration candidate",
    )
    source = document.get("source")
    target = document.get("target")
    _require(
        isinstance(source, Mapping) and isinstance(target, Mapping),
        "registration source and target are required",
    )
    _require(
        source.get("frame") == odom_frame and target.get("frame") == "world",
        "registration frames do not bind odom to world",
    )
    _require(
        all(source.get(key) == value for key, value in scope.items()),
        "registration source scope mismatches observations",
    )
    transform = document.get("T_target_source")
    _require(
        isinstance(transform, Mapping) and set(transform) == {"dx_m", "dy_m", "yaw_rad"},
        "registration transform is invalid",
    )
    dx = _number(transform["dx_m"], "registration.dx_m")
    dy = _number(transform["dy_m"], "registration.dy_m")
    yaw = _number(transform["yaw_rad"], "registration.yaw_rad")
    cosine, sine = math.cos(yaw), math.sin(yaw)
    return [
        [cosine, -sine, 0.0, dx],
        [sine, cosine, 0.0, dy],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _request(document: object) -> dict[str, object]:
    _require(
        isinstance(document, Mapping)
        and set(document)
        == {
            "schema_version",
            "kind",
            "source_scope",
            "odom_frame",
            "calibration_id",
            "maximum_association_error_ns",
            "maximum_translation_spread_m",
            "maximum_rotation_spread_rad",
            "minimum_observations_per_tag",
            "observations",
            "calibration",
            "mount",
            "registration",
            "tape_checkpoint",
        },
        "fusion request schema is invalid",
    )
    _require(
        document["schema_version"] == 2
        and document["kind"] == "ohmni_tag_candidate_fusion_request",
        "fusion request version is invalid",
    )
    maximum_association = _integer(
        document["maximum_association_error_ns"],
        "maximum_association_error_ns",
        minimum=0,
        maximum=MAX_ASSOCIATION_NS,
    )
    minimum_observations = _integer(
        document["minimum_observations_per_tag"],
        "minimum_observations_per_tag",
        minimum=2,
        maximum=MAX_OBSERVATIONS,
    )
    translation_spread = _number(
        document["maximum_translation_spread_m"],
        "maximum_translation_spread_m",
        minimum=0,
    )
    rotation_spread = _number(
        document["maximum_rotation_spread_rad"],
        "maximum_rotation_spread_rad",
        minimum=0,
    )
    _require(
        translation_spread <= MAX_TRANSLATION_SPREAD_M
        and rotation_spread <= MAX_ROTATION_SPREAD_RAD,
        "fusion spread limits exceed the bounded envelope",
    )
    return {
        **document,
        "source_scope": _scope(document["source_scope"]),
        "odom_frame": _text(document["odom_frame"], "odom_frame"),
        "calibration_id": _text(document["calibration_id"], "calibration_id", maximum=512),
        "maximum_association_error_ns": maximum_association,
        "maximum_translation_spread_m": translation_spread,
        "maximum_rotation_spread_rad": rotation_spread,
        "minimum_observations_per_tag": minimum_observations,
    }


def _capture_time(event: Observation) -> tuple[str, int] | None:
    capture = event.submission.t_capture
    if capture is None or capture.unit != "ns":
        return None
    return capture.clock_id, capture.value


def _weight(payload: Mapping[str, object]) -> float:
    covariance = payload.get("covariance_m2")
    if not isinstance(covariance, tuple | list) or len(covariance) != 9:
        raise ValueError("accepted tag observation requires a 3x3 measured covariance")
    trace = sum(_number(covariance[index], "tag covariance", minimum=0) for index in (0, 4, 8))
    if trace <= 0:
        raise ValueError("tag covariance trace must be positive for weighted fusion")
    return 1 / trace


def _quaternion_from_rotation(
    matrix: Sequence[Sequence[float]],
) -> tuple[float, float, float, float]:
    trace = matrix[0][0] + matrix[1][1] + matrix[2][2]
    if trace > 0:
        scale = math.sqrt(trace + 1.0) * 2
        return (
            (matrix[2][1] - matrix[1][2]) / scale,
            (matrix[0][2] - matrix[2][0]) / scale,
            (matrix[1][0] - matrix[0][1]) / scale,
            0.25 * scale,
        )
    axis = max(range(3), key=lambda index: matrix[index][index])
    if axis == 0:
        scale = math.sqrt(1 + matrix[0][0] - matrix[1][1] - matrix[2][2]) * 2
        return (
            0.25 * scale,
            (matrix[0][1] + matrix[1][0]) / scale,
            (matrix[0][2] + matrix[2][0]) / scale,
            (matrix[2][1] - matrix[1][2]) / scale,
        )
    if axis == 1:
        scale = math.sqrt(1 + matrix[1][1] - matrix[0][0] - matrix[2][2]) * 2
        return (
            (matrix[0][1] + matrix[1][0]) / scale,
            0.25 * scale,
            (matrix[1][2] + matrix[2][1]) / scale,
            (matrix[0][2] - matrix[2][0]) / scale,
        )
    scale = math.sqrt(1 + matrix[2][2] - matrix[0][0] - matrix[1][1]) * 2
    return (
        (matrix[0][2] + matrix[2][0]) / scale,
        (matrix[1][2] + matrix[2][1]) / scale,
        0.25 * scale,
        (matrix[1][0] - matrix[0][1]) / scale,
    )


def _weighted_pose(
    samples: Sequence[dict[str, object]], request: Mapping[str, object]
) -> dict[str, object]:
    _require(
        len(samples) >= request["minimum_observations_per_tag"],
        "tag lacks enough associated observations",
    )
    translations = [sample["translation"] for sample in samples]
    rotations = [sample["transform"] for sample in samples]
    weights = [sample["weight"] for sample in samples]
    total = sum(weights)
    translation = [
        sum(weight * point[index] for weight, point in zip(weights, translations, strict=True))
        / total
        for index in range(3)
    ]
    anchor = rotations[0]
    maximum_translation = max(math.dist(point, translation) for point in translations)
    maximum_rotation = max(_rotation_distance(anchor, rotation) for rotation in rotations)
    _require(
        maximum_translation <= request["maximum_translation_spread_m"],
        "tag translation spread exceeds the measured bound",
    )
    _require(
        maximum_rotation <= request["maximum_rotation_spread_rad"],
        "tag rotation spread exceeds the measured bound",
    )
    quaternion = [_quaternion_from_rotation(rotation) for rotation in rotations]
    reference = quaternion[0]
    fused_quaternion = [0.0, 0.0, 0.0, 0.0]
    for weight, item in zip(weights, quaternion, strict=True):
        sign = 1 if sum(a * b for a, b in zip(reference, item, strict=True)) >= 0 else -1
        for index in range(4):
            fused_quaternion[index] += sign * weight * item[index]
    norm = math.sqrt(sum(item * item for item in fused_quaternion))
    _require(norm > 0, "weighted tag rotation is degenerate")
    qx, qy, qz, qw = [item / norm for item in fused_quaternion]
    fused_transform = _pose_matrix(
        {
            "x_m": translation[0],
            "y_m": translation[1],
            "z_m": translation[2],
            "qx": qx,
            "qy": qy,
            "qz": qz,
            "qw": qw,
        }
    )
    return {
        "T_world_tag": fused_transform,
        "observation_count": len(samples),
        "translation_spread_max_m": maximum_translation,
        "rotation_spread_max_rad": maximum_rotation,
        "translation_weight_sum_m2_inverse": total,
        "observation_event_ids": [sample["event_id"] for sample in samples],
    }


def _checkpoint(
    document: object, candidates: Mapping[int, Mapping[str, object]]
) -> dict[str, object]:
    _require(
        isinstance(document, Mapping)
        and set(document)
        == {
            "schema_version",
            "kind",
            "measurement_kind",
            "tag_ids",
            "measured_distance_m",
            "maximum_error_m",
        },
        "tape checkpoint schema is invalid",
    )
    _require(
        document["schema_version"] == 1
        and document["kind"] == "independent_tape_checkpoint"
        and document["measurement_kind"] == "XYZ_euclidean_distance",
        "tape checkpoint must be an independent XYZ measurement",
    )
    ids = document["tag_ids"]
    _require(
        isinstance(ids, list) and len(ids) == 2 and ids[0] != ids[1],
        "tape checkpoint needs two distinct tag IDs",
    )
    first, second = (_integer(item, "tape checkpoint tag_id", maximum=586) for item in ids)
    measured = _number(document["measured_distance_m"], "tape measured_distance_m", minimum=0)
    maximum = _number(document["maximum_error_m"], "tape maximum_error_m", minimum=0)
    _require(0 < maximum <= MAX_TAPE_ERROR_M, "tape maximum_error_m must be in (0, 0.10]")
    if first not in candidates or second not in candidates:
        return {
            "tag_ids": [first, second],
            "measurement_kind": "XYZ_euclidean_distance",
            "measured_distance_m": measured,
            "maximum_error_m": maximum,
            "status": "not_evaluated",
            "reason": "checkpoint_tags_not_fused",
        }
    actual = math.dist(
        _translation(candidates[first]["T_world_tag"]),
        _translation(candidates[second]["T_world_tag"]),
    )
    error = abs(actual - measured)
    return {
        "tag_ids": [first, second],
        "measurement_kind": "XYZ_euclidean_distance",
        "measured_distance_m": measured,
        "computed_distance_m": actual,
        "absolute_error_m": error,
        "maximum_error_m": maximum,
        "status": "evaluated",
        "passes": error <= maximum,
    }


def fuse_observations(
    observations: Sequence[Observation],
    *,
    request: Mapping[str, object],
    calibration: Mapping[str, object],
    mount: Mapping[str, object],
    registration: Sequence[Sequence[float]],
    input_pins: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    """Fuse only typed, captured canonical observations into an unapproved candidate."""
    _require(
        1 <= len(observations) <= MAX_OBSERVATIONS, "observation count is outside the fusion bound"
    )
    scope = request["source_scope"]
    event_ids = [event.submission.event_id for event in observations]
    _require(len(event_ids) == len(set(event_ids)), "observation event IDs must be unique")
    camera_by_image: dict[tuple[str, str, int], Observation] = {}
    body_samples: list[Observation] = []
    diagnostics: list[dict[str, object]] = []
    for event in observations:
        _require(isinstance(event, Observation), "fusion core accepts typed Observation values")
        if not _same_scope(event, scope):
            diagnostics.append(
                {"event_id": event.submission.event_id, "reason": "source_scope_mismatch"}
            )
            continue
        payload = event.submission.payload
        kind = payload["kind"]
        captured = _capture_time(event)
        if kind == "camera_frame":
            if captured is None:
                diagnostics.append(
                    {"event_id": event.submission.event_id, "reason": "missing_capture_time"}
                )
                continue
            if event.submission.frame != mount["camera_frame"]:
                diagnostics.append(
                    {"event_id": event.submission.event_id, "reason": "camera_frame_mismatch"}
                )
                continue
            if payload["calibration_id"] != request["calibration_id"]:
                diagnostics.append(
                    {"event_id": event.submission.event_id, "reason": "calibration_id_mismatch"}
                )
                continue
            image_key = (payload["image_id"], *captured)
            _require(image_key not in camera_by_image, "camera evidence identity is duplicated")
            camera_by_image[image_key] = event
        elif kind == "pose":
            pose = payload["pose"]
            if (
                captured is not None
                and event.submission.frame == request["odom_frame"]
                and pose["parent_frame"] == request["odom_frame"]
                and pose["child_frame"] == mount["body_frame"]
            ):
                body_samples.append(event)
    fused_samples: dict[int, list[dict[str, object]]] = {}
    for event in observations:
        payload = event.submission.payload
        if payload["kind"] != "tag_observation" or not _same_scope(event, scope):
            continue
        event_id = event.submission.event_id
        captured = _capture_time(event)
        if captured is None:
            diagnostics.append({"event_id": event_id, "reason": "missing_capture_time"})
            continue
        if (
            event.submission.frame != mount["camera_frame"]
            or payload["pixel_frame"] != "rectified_camera"
        ):
            diagnostics.append(
                {"event_id": event_id, "reason": "camera_frame_or_pixel_frame_mismatch"}
            )
            continue
        if payload["pose_accepted"] is not True or payload["tag_pose"] is None:
            diagnostics.append({"event_id": event_id, "reason": "tag_pose_not_accepted"})
            continue
        image = camera_by_image.get((payload["image_id"], *captured))
        if image is None:
            diagnostics.append(
                {"event_id": event_id, "reason": "camera_frame_not_capture_associated"}
            )
            continue
        body = min(
            (
                candidate
                for candidate in body_samples
                if candidate.submission.t_capture.clock_id == captured[0]
            ),
            key=lambda candidate: abs(candidate.submission.t_capture.value - captured[1]),
            default=None,
        )
        if (
            body is None
            or abs(body.submission.t_capture.value - captured[1])
            > request["maximum_association_error_ns"]
        ):
            diagnostics.append({"event_id": event_id, "reason": "body_pose_not_capture_associated"})
            continue
        try:
            weight = _weight(payload)
        except ValueError as error:
            diagnostics.append({"event_id": event_id, "reason": str(error)})
            continue
        tag_pose = payload["tag_pose"]
        world_tag = _multiply(
            _multiply(
                _multiply(registration, _pose_matrix(body.submission.payload["pose"])),
                mount["T_body_camera"],
            ),
            _pose_matrix(tag_pose),
        )
        identifier = payload["tag_id"]
        fused_samples.setdefault(identifier, []).append(
            {
                "event_id": event_id,
                "transform": world_tag,
                "translation": _translation(world_tag),
                "weight": weight,
            }
        )
    _require(len(fused_samples) <= MAX_TAGS, "fused tag count exceeds the global bound")
    candidates: dict[int, dict[str, object]] = {}
    for identifier, samples in sorted(fused_samples.items()):
        try:
            candidates[identifier] = _weighted_pose(samples, request)
        except ValueError as error:
            diagnostics.append({"tag_id": identifier, "reason": str(error)})
    checkpoint = _checkpoint(request["tape_checkpoint"], candidates)
    if checkpoint["status"] == "not_evaluated":
        diagnostics.append({"reason": "checkpoint_not_evaluated"})
        candidates = {}
    candidate = {
        "schema_version": 2,
        "kind": "ohmni_tag_candidate_fusion",
        "approval_status": "unapproved",
        "claim_scope": (
            "Offline XYZ tag-center estimates only. This candidate does not approve flight "
            "or aerial clearance."
        ),
        "source_scope": dict(scope),
        "registration": dict(input_pins["registration"]),
        "calibration": dict(input_pins["calibration"]),
        "mount": dict(input_pins["mount"]),
        "observations": dict(input_pins["observations"]),
        "candidates": [{"tag_id": identifier, **value} for identifier, value in candidates.items()],
        "checkpoint": checkpoint,
        "diagnostics": diagnostics[:MAX_OBSERVATIONS],
        "diagnostic_count": len(diagnostics),
    }
    if checkpoint["status"] == "evaluated":
        _require(checkpoint["passes"], "independent tape checkpoint exceeds its error bound")
    encoded = json.dumps(candidate, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    _require(len(encoded) <= MAX_OUTPUT_BYTES, "fusion output exceeds the global byte limit")
    return candidate


def _read_request(path: Path) -> tuple[dict[str, object], str, int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_REQUEST_BYTES:
            raise ValueError("fusion request must be a bounded regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            payload = stream.read(MAX_REQUEST_BYTES + 1)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("fusion request exceeds its byte limit")
    return (
        _request(parse_document(payload, str(path))),
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    )


def run(request_path: Path, evidence_root: Path, output: Path) -> dict[str, object]:
    request, request_hash, request_bytes = _read_request(request_path)
    snapshots = _Snapshots(evidence_root)
    snapshots.total_bytes = request_bytes
    observation_payload, observation_pin = snapshots.read(
        request["observations"], "observations", maximum=MAX_INPUT_BYTES
    )
    lines = observation_payload.splitlines()
    _require(
        1 <= len(lines) <= MAX_OBSERVATIONS and all(lines), "observations must be bounded JSONL"
    )
    observations = [decode_observation(line) for line in lines]
    calibration_payload, calibration_pin = snapshots.read(
        request["calibration"], "calibration", maximum=1024 * 1024
    )
    calibration = _calibration(
        parse_document(calibration_payload, calibration_pin["path"]), calibration_pin
    )
    mount_payload, mount_pin = snapshots.read(request["mount"], "mount", maximum=1024 * 1024)
    mount = _mount(parse_document(mount_payload, mount_pin["path"]), calibration)
    registration_payload, registration_pin = snapshots.read(
        request["registration"], "registration", maximum=1024 * 1024
    )
    registration = _registration(
        parse_document(registration_payload, registration_pin["path"]),
        request["source_scope"],
        request["odom_frame"],
    )
    candidate = fuse_observations(
        observations,
        request=request,
        calibration=calibration,
        mount=mount,
        registration=registration,
        input_pins={
            "observations": observation_pin,
            "calibration": calibration_pin,
            "mount": mount_pin,
            "registration": registration_pin,
        },
    )
    candidate["request_sha256"] = request_hash
    encoded = json.dumps(candidate, allow_nan=False, sort_keys=True, indent=2).encode() + b"\n"
    _require(len(encoded) <= MAX_OUTPUT_BYTES, "fusion output exceeds the global byte limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = _temporary_output(output)
    try:
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, output, follow_symlinks=False)
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        os.unlink(temporary)
        raise
    os.unlink(temporary)
    return candidate


def _temporary_output(output: Path) -> tuple[int, Path]:
    for index in range(128):
        temporary = output.parent / f".{output.name}.{os.getpid()}.{index}.tmp"
        try:
            return (
                os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
                temporary,
            )
        except FileExistsError:
            continue
    raise FileExistsError("unable to reserve an atomic fusion output path")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("unable to write fusion output")
        offset += written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("evidence_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        candidate = run(args.request, args.evidence_root, args.output)
    except (OSError, ValueError) as error:
        print(json.dumps({"valid": False, "error": str(error)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "valid": True,
                "output": str(args.output),
                "candidate_count": len(candidate["candidates"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

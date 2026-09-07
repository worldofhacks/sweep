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

import numpy as np

from perception.camera_tags import CameraTagDetector
from relay.observations import Observation, decode_observation
from tools.map_common import finite_number, parse_document, validate_transform
from tools.ohmni_world_registration import apply_transform

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
MAX_VERTICAL_ERROR_M = 0.10


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


def _source_scopes(value: object) -> dict[str, dict[str, object]]:
    _require(
        isinstance(value, Mapping) and set(value) == {"pose", "camera", "tag"},
        "source_scopes requires pose, camera, and tag identities",
    )
    scopes = {name: _scope(value[name], f"source_scopes.{name}") for name in value}
    identity = {key: scopes["pose"][key] for key in ("session", "device_id", "connection_epoch")}
    _require(
        all(
            all(scope[key] == expected for key, expected in identity.items())
            for scope in scopes.values()
        ),
        "source scopes must share session, device, and connection epoch",
    )
    _require(
        scopes["pose"]["source_id"] != scopes["camera"]["source_id"],
        "pose and camera sources must be distinct",
    )
    return scopes


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
    document: object, pose_scope: Mapping[str, object], odom_frame: str
) -> dict[str, object]:
    _require(isinstance(document, Mapping), "registration must be an object")
    allowed = {
        "schema_version",
        "kind",
        "approval_status",
        "source",
        "target",
        "T_target_source",
        "fit_tag_ids",
        "held_out_tag_ids",
        "residuals",
        "max_residual_m",
        "rms_residual_m",
        "held_out_residuals",
        "max_held_out_residual_m",
    }
    if "input_provenance" in document:
        allowed.add("input_provenance")
    _require(set(document) == allowed, "registration candidate schema is invalid")
    _require(
        document["schema_version"] == 1
        and document["kind"] == "ohmni_world_registration_candidate"
        and document["approval_status"] == "unapproved",
        "registration must be an unapproved Ohmni world-registration candidate",
    )
    source = document["source"]
    target = document["target"]
    _require(
        isinstance(source, Mapping) and isinstance(target, Mapping),
        "registration source and target are required",
    )
    _require(
        set(source)
        == {"frame", "session", "device_id", "connection_epoch", "source_id", "name", "sha256"}
        and source["frame"] == odom_frame
        and all(source[key] == value for key, value in pose_scope.items()),
        "registration source scope mismatches pose observations",
    )
    _text(source["name"], "registration.source.name")
    _digest(source["sha256"], "registration.source.sha256")
    _require(
        set(target) == {"frame", "map_id", "map_version", "physical_datum", "name", "sha256"}
        and target["frame"] == "world",
        "registration target is invalid",
    )
    for key in ("map_id", "map_version", "physical_datum", "name"):
        _text(target[key], f"registration.target.{key}")
    _digest(target["sha256"], "registration.target.sha256")
    transform = document["T_target_source"]
    _require(
        isinstance(transform, Mapping) and set(transform) == {"dx_m", "dy_m", "yaw_rad"},
        "registration transform is invalid",
    )
    transform = {
        "dx_m": _number(transform["dx_m"], "registration.dx_m"),
        "dy_m": _number(transform["dy_m"], "registration.dy_m"),
        "yaw_rad": _number(transform["yaw_rad"], "registration.yaw_rad"),
    }
    fit_ids = _tag_ids(document["fit_tag_ids"], "registration.fit_tag_ids", minimum=3)
    held_out_ids = _tag_ids(document["held_out_tag_ids"], "registration.held_out_tag_ids")
    _require(not set(fit_ids) & set(held_out_ids), "registration fit and held-out tags overlap")
    residuals = _residual_rows(document["residuals"], fit_ids, transform, "registration.residuals")
    held_out = _residual_rows(
        document["held_out_residuals"], held_out_ids, transform, "registration.held_out_residuals"
    )
    _summary_matches(
        document["max_residual_m"], document["rms_residual_m"], residuals, "registration"
    )
    if held_out:
        maximum = max(row["residual_m"] for row in held_out)
        _require(
            math.isclose(
                _number(
                    document["max_held_out_residual_m"], "registration.max_held_out_residual_m"
                ),
                maximum,
                rel_tol=0,
                abs_tol=1e-9,
            ),
            "registration held-out residual summary is invalid",
        )
    else:
        _require(
            document["max_held_out_residual_m"] is None, "registration held-out summary is invalid"
        )
    if "input_provenance" in document:
        provenance = document["input_provenance"]
        _require(
            isinstance(provenance, Mapping)
            and set(provenance) == {"observed_document_sha256", "known_document_sha256"},
            "registration input provenance is invalid",
        )
        for key in provenance:
            _digest(provenance[key], f"registration.{key}")
    return {"transform": transform, "fit_tag_ids": fit_ids, "target": dict(target)}


def _tag_ids(value: object, name: str, *, minimum: int = 0) -> list[int]:
    _require(isinstance(value, list) and minimum <= len(value) <= MAX_TAGS, f"{name} is invalid")
    ids = [_integer(item, name, maximum=586) for item in value]
    _require(ids == sorted(set(ids)), f"{name} must be sorted unique IDs")
    return ids


def _xy(value: object, name: str) -> tuple[float, float]:
    _require(isinstance(value, list) and len(value) == 2, f"{name} must be XY")
    return (_number(value[0], f"{name}.x"), _number(value[1], f"{name}.y"))


def _residual_rows(
    value: object, expected_ids: Sequence[int], transform: Mapping[str, float], name: str
) -> list[dict[str, object]]:
    _require(isinstance(value, list) and len(value) == len(expected_ids), f"{name} is invalid")
    rows: list[dict[str, object]] = []
    for expected, row in zip(expected_ids, value, strict=True):
        _require(
            isinstance(row, Mapping)
            and set(row)
            == {"tag_id", "source_xy_m", "target_xy_m", "registered_target_xy_m", "residual_m"}
            and row["tag_id"] == expected,
            f"{name} does not match its tie IDs",
        )
        source = _xy(row["source_xy_m"], f"{name}.source")
        target = _xy(row["target_xy_m"], f"{name}.target")
        registered = _xy(row["registered_target_xy_m"], f"{name}.registered")
        recomputed = apply_transform(dict(transform), source)
        _require(
            math.dist(registered, recomputed) <= 1e-9,
            f"{name} registered point does not match its transform",
        )
        residual = math.dist(recomputed, target)
        _require(
            math.isclose(
                _number(row["residual_m"], f"{name}.residual"), residual, rel_tol=0, abs_tol=1e-9
            ),
            f"{name} residual does not match its transform",
        )
        rows.append({"residual_m": residual})
    return rows


def _summary_matches(
    maximum: object, rms: object, rows: Sequence[Mapping[str, object]], name: str
) -> None:
    actual_maximum = max(row["residual_m"] for row in rows)
    actual_rms = math.sqrt(sum(row["residual_m"] ** 2 for row in rows) / len(rows))
    _require(
        math.isclose(
            _number(maximum, f"{name}.max_residual_m"), actual_maximum, rel_tol=0, abs_tol=1e-9
        )
        and math.isclose(
            _number(rms, f"{name}.rms_residual_m"), actual_rms, rel_tol=0, abs_tol=1e-9
        ),
        f"{name} residual summary is invalid",
    )


def _request(document: object) -> dict[str, object]:
    world_fields = {
        "schema_version",
        "kind",
        "source_scopes",
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
        "vertical_datum",
        "tape_checkpoint",
    }
    local_fields = {
        "schema_version",
        "kind",
        "candidate_mode",
        "source_scopes",
        "odom_frame",
        "calibration_id",
        "maximum_association_error_ns",
        "maximum_translation_spread_m",
        "maximum_rotation_spread_rad",
        "minimum_observations_per_tag",
        "observations",
        "calibration",
        "mount",
    }
    _require(
        isinstance(document, Mapping),
        "fusion request schema is invalid",
    )
    if document.get("schema_version") == 3 and set(document) == world_fields:
        candidate_mode = "world_registered"
    elif (
        document.get("schema_version") == 4
        and set(document) == local_fields
        and document.get("candidate_mode") == "local_odom"
    ):
        candidate_mode = "local_odom"
    else:
        raise ValueError("fusion request schema is invalid")
    _require(
        document["kind"] == "ohmni_tag_candidate_fusion_request",
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
    vertical_datum = document.get("vertical_datum")
    if candidate_mode == "world_registered" and vertical_datum is not None:
        _require(
            isinstance(vertical_datum, Mapping) and set(vertical_datum) == {"path", "sha256"},
            "vertical_datum requires a pinned artifact or null",
        )
    return {
        **document,
        "source_scopes": _source_scopes(document["source_scopes"]),
        "odom_frame": _text(document["odom_frame"], "odom_frame"),
        "calibration_id": _text(document["calibration_id"], "calibration_id", maximum=512),
        "maximum_association_error_ns": maximum_association,
        "maximum_translation_spread_m": translation_spread,
        "maximum_rotation_spread_rad": rotation_spread,
        "minimum_observations_per_tag": minimum_observations,
        "vertical_datum": vertical_datum,
        "candidate_mode": candidate_mode,
    }


def _vertical_datum(
    document: object,
    pose_scope: Mapping[str, object],
    odom_frame: str,
    target: Mapping[str, object],
) -> float | None:
    if document is None:
        return None
    _require(
        isinstance(document, Mapping)
        and set(document)
        == {
            "schema_version",
            "kind",
            "measurement_kind",
            "source",
            "target",
            "z_offset_m",
            "maximum_error_m",
            "measured",
        },
        "vertical datum proof schema is invalid",
    )
    _require(
        document["schema_version"] == 1
        and document["kind"] == "ohmni_vertical_datum_measurement"
        and document["measurement_kind"] == "scoped_odom_to_world_height"
        and document["measured"] is True,
        "vertical datum must be a measured scoped height proof",
    )
    source = document["source"]
    target_document = document["target"]
    _require(
        isinstance(source, Mapping)
        and set(source) == {"frame", "session", "device_id", "connection_epoch", "source_id"}
        and source["frame"] == odom_frame
        and all(source[key] == value for key, value in pose_scope.items()),
        "vertical datum source scope mismatches pose observations",
    )
    _require(
        isinstance(target_document, Mapping)
        and set(target_document) == {"frame", "map_id", "map_version", "physical_datum"}
        and all(target_document.get(key) == target[key] for key in target_document),
        "vertical datum target mismatches registration",
    )
    _require(
        0
        < _number(document["maximum_error_m"], "vertical datum maximum_error_m")
        <= MAX_VERTICAL_ERROR_M,
        "vertical datum maximum_error_m must be in (0, 0.10]",
    )
    return _number(document["z_offset_m"], "vertical datum z_offset_m")


def _capture_time(event: Observation) -> tuple[str, int] | None:
    capture = event.submission.t_capture
    if capture is None or capture.unit != "ns":
        return None
    return capture.clock_id, capture.value


def _weight(payload: Mapping[str, object]) -> float:
    covariance = payload.get("covariance_m2")
    if not isinstance(covariance, tuple | list) or len(covariance) != 9:
        raise ValueError("accepted tag observation requires a 3x3 measured covariance")
    try:
        matrix = np.asarray(covariance, dtype=float).reshape(3, 3)
    except (TypeError, ValueError) as error:
        raise ValueError("accepted tag observation requires a 3x3 measured covariance") from error
    if not np.isfinite(matrix).all() or not np.allclose(matrix, matrix.T, rtol=0, atol=1e-12):
        raise ValueError("accepted tag observation covariance must be symmetric PSD")
    try:
        if np.linalg.eigvalsh(matrix).min() < -1e-12:
            raise ValueError("accepted tag observation covariance must be symmetric PSD")
    except np.linalg.LinAlgError as error:
        raise ValueError("accepted tag observation covariance must be symmetric PSD") from error
    trace = float(np.trace(matrix))
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
    samples: Sequence[dict[str, object]], request: Mapping[str, object], pose_key: str
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
        pose_key: fused_transform,
        "observation_count": len(samples),
        "translation_spread_max_m": maximum_translation,
        "rotation_spread_max_rad": maximum_rotation,
        "translation_weight_sum_m2_inverse": total,
        "observation_event_ids": [sample["event_id"] for sample in samples],
    }


def _checkpoint(
    document: object,
    candidates: Mapping[int, Mapping[str, object]],
    pose_key: str,
    fit_tag_ids: Sequence[int],
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
    _require(
        first not in fit_tag_ids and second not in fit_tag_ids,
        "tape checkpoint tags must be independent of registration fit ties",
    )
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
        _translation(candidates[first][pose_key]),
        _translation(candidates[second][pose_key]),
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
    registration: Mapping[str, object] | None,
    input_pins: Mapping[str, Mapping[str, str] | None],
) -> dict[str, object]:
    """Fuse only typed, captured canonical observations into an unapproved candidate."""
    _require(
        1 <= len(observations) <= MAX_OBSERVATIONS, "observation count is outside the fusion bound"
    )
    candidate_mode = request.get("candidate_mode", "world_registered")
    _require(
        candidate_mode in {"world_registered", "local_odom"}, "fusion candidate mode is invalid"
    )
    if candidate_mode == "world_registered":
        _require(
            registration is not None
            and set(registration) == {"transform", "fit_tag_ids", "target", "vertical_offset_m"},
            "registration evidence is invalid",
        )
    else:
        _require(registration is None, "local odometry fusion cannot accept registration evidence")
    scopes = request["source_scopes"]
    event_ids = [event.submission.event_id for event in observations]
    _require(len(event_ids) == len(set(event_ids)), "observation event IDs must be unique")
    camera_by_image: dict[tuple[str, str, int], Observation] = {}
    body_samples: list[Observation] = []
    diagnostics: list[dict[str, object]] = []
    for event in observations:
        _require(isinstance(event, Observation), "fusion core accepts typed Observation values")
        payload = event.submission.payload
        kind = payload["kind"]
        captured = _capture_time(event)
        if kind == "camera_frame":
            if not _same_scope(event, scopes["camera"]):
                diagnostics.append(
                    {"event_id": event.submission.event_id, "reason": "camera_scope_mismatch"}
                )
                continue
            if captured is None:
                diagnostics.append(
                    {"event_id": event.submission.event_id, "reason": "missing_capture_time"}
                )
                continue
            if event.submission.confidence <= 0:
                diagnostics.append(
                    {"event_id": event.submission.event_id, "reason": "nonpositive_confidence"}
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
            if not _same_scope(event, scopes["pose"]):
                diagnostics.append(
                    {"event_id": event.submission.event_id, "reason": "pose_scope_mismatch"}
                )
                continue
            if captured is None:
                diagnostics.append(
                    {"event_id": event.submission.event_id, "reason": "missing_capture_time"}
                )
                continue
            if event.submission.confidence <= 0:
                diagnostics.append(
                    {"event_id": event.submission.event_id, "reason": "nonpositive_confidence"}
                )
                continue
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
        if payload["kind"] != "tag_observation":
            continue
        event_id = event.submission.event_id
        if not _same_scope(event, scopes["tag"]):
            diagnostics.append({"event_id": event_id, "reason": "tag_scope_mismatch"})
            continue
        captured = _capture_time(event)
        if captured is None:
            diagnostics.append({"event_id": event_id, "reason": "missing_capture_time"})
            continue
        if event.submission.confidence <= 0:
            diagnostics.append({"event_id": event_id, "reason": "nonpositive_confidence"})
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
        tag_pose = payload["tag_pose"]
        if (
            tag_pose["parent_frame"] != mount["camera_frame"]
            or tag_pose["child_frame"] != f"tag:{payload['tag_id']}"
        ):
            diagnostics.append({"event_id": event_id, "reason": "tag_pose_frame_mismatch"})
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
        odom_tag = _multiply(
            _multiply(
                _pose_matrix(body.submission.payload["pose"]),
                mount["T_body_camera"],
            ),
            _pose_matrix(tag_pose),
        )
        identifier = payload["tag_id"]
        fused_samples.setdefault(identifier, []).append(
            {
                "event_id": event_id,
                "transform": odom_tag,
                "translation": _translation(odom_tag),
                "weight": weight,
            }
        )
    _require(len(fused_samples) <= MAX_TAGS, "fused tag count exceeds the global bound")
    vertical_offset = None if registration is None else registration["vertical_offset_m"]
    pose_key = "T_world_tag" if vertical_offset is not None else "T_odom_tag"
    candidates: dict[int, dict[str, object]] = {}
    for identifier, samples in sorted(fused_samples.items()):
        try:
            candidate = _weighted_pose(samples, request, "T_odom_tag")
            if vertical_offset is not None:
                candidate["T_world_tag"] = _multiply(
                    _planar_world_odom(registration["transform"], vertical_offset),
                    candidate.pop("T_odom_tag"),
                )
            candidates[identifier] = candidate
        except ValueError as error:
            diagnostics.append({"tag_id": identifier, "reason": str(error)})
    checkpoint: dict[str, object] | None = None
    if candidate_mode == "world_registered":
        checkpoint = _checkpoint(
            request["tape_checkpoint"], candidates, pose_key, registration["fit_tag_ids"]
        )
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
            if candidate_mode == "world_registered"
            else "Offline local-odometry XYZ tag-center estimates only. This candidate is not "
            "world registered and does not approve control, flight, or aerial clearance."
        ),
        "source_scopes": {name: dict(scope) for name, scope in scopes.items()},
        "candidate_frame": "world" if vertical_offset is not None else request["odom_frame"],
        "calibration": dict(input_pins["calibration"]),
        "mount": dict(input_pins["mount"]),
        "observations": dict(input_pins["observations"]),
        "candidates": [{"tag_id": identifier, **value} for identifier, value in candidates.items()],
        "diagnostics": diagnostics[:MAX_OBSERVATIONS],
        "diagnostic_count": len(diagnostics),
    }
    if candidate_mode == "world_registered":
        candidate["registration"] = dict(input_pins["registration"])
        candidate["vertical_datum"] = None
        if input_pins.get("vertical_datum") is not None:
            candidate["vertical_datum"] = dict(input_pins["vertical_datum"])
        candidate["checkpoint"] = checkpoint
    else:
        candidate["candidate_mode"] = "local_odom"
    if checkpoint is not None and checkpoint["status"] == "evaluated":
        _require(checkpoint["passes"], "independent tape checkpoint exceeds its error bound")
    encoded = json.dumps(candidate, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    _require(len(encoded) <= MAX_OUTPUT_BYTES, "fusion output exceeds the global byte limit")
    return candidate


def _planar_world_odom(
    transform: Mapping[str, object], vertical_offset: object
) -> list[list[float]]:
    yaw = _number(transform["yaw_rad"], "registration.yaw_rad")
    cosine, sine = math.cos(yaw), math.sin(yaw)
    return [
        [cosine, -sine, 0.0, _number(transform["dx_m"], "registration.dx_m")],
        [sine, cosine, 0.0, _number(transform["dy_m"], "registration.dy_m")],
        [0.0, 0.0, 1.0, _number(vertical_offset, "vertical datum z_offset_m")],
        [0.0, 0.0, 0.0, 1.0],
    ]


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
    registration: dict[str, object] | None = None
    registration_pin: dict[str, str] | None = None
    vertical_pin: dict[str, str] | None = None
    if request["candidate_mode"] == "world_registered":
        registration_payload, registration_pin = snapshots.read(
            request["registration"], "registration", maximum=1024 * 1024
        )
        registration = _registration(
            parse_document(registration_payload, registration_pin["path"]),
            request["source_scopes"]["pose"],
            request["odom_frame"],
        )
        if request["vertical_datum"] is None:
            registration["vertical_offset_m"] = None
        else:
            vertical_payload, vertical_pin = snapshots.read(
                request["vertical_datum"], "vertical_datum", maximum=1024 * 1024
            )
            registration["vertical_offset_m"] = _vertical_datum(
                parse_document(vertical_payload, vertical_pin["path"]),
                request["source_scopes"]["pose"],
                request["odom_frame"],
                registration["target"],
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
            "vertical_datum": vertical_pin,
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

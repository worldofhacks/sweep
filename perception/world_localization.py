"""Convert accepted world-frame camera evidence into control-localization measurements."""

from __future__ import annotations

import json
import os
import stat
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Literal

import numpy as np

from perception.control_localization import (
    BodyExtrinsics,
    ControlLocalizationSnapshot,
    HeightObservation,
    TagFix,
    VelocityObservation,
)
from relay.observations import ClockMapping, Observation
from tools.map_common import validate_transform
from tools.map_validate import validate_bundle

_MAX_CACHE_ITEMS = 512
_MAX_IDENTIFIER_CHARS = 128
_MAX_CAPTURE_MAPPING_ERROR_MS = 100
_MAX_EVIDENCE_BYTES = 1_048_576


class WorldLocalizationError(ValueError):
    """A bounded refusal for canonical evidence that cannot enter control fusion."""


def _identifier(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value) > _MAX_IDENTIFIER_CHARS
    ):
        raise WorldLocalizationError(f"{name} must be canonical text of at most 128 characters")
    return value


def _digest(value: object, name: str) -> str:
    result = _identifier(value, name)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise WorldLocalizationError(f"{name} must be a lowercase SHA-256 digest")
    return result


def _finite(value: object, name: str) -> float:
    if type(value) not in {int, float} or not isfinite(float(value)):
        raise WorldLocalizationError(f"{name} must be finite")
    return float(value)


def _covariance(value: object, name: str) -> tuple[tuple[float, ...], ...]:
    try:
        matrix = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as error:
        raise WorldLocalizationError(f"{name} must be a 3x3 positive definite matrix") from error
    if (
        matrix.shape != (3, 3)
        or not np.isfinite(matrix).all()
        or not np.allclose(matrix, matrix.T, rtol=0, atol=1e-12)
    ):
        raise WorldLocalizationError(f"{name} must be a 3x3 positive definite matrix")
    try:
        if np.linalg.eigvalsh(matrix).min() <= 0:
            raise WorldLocalizationError(f"{name} must be a 3x3 positive definite matrix")
    except np.linalg.LinAlgError as error:
        raise WorldLocalizationError(f"{name} must be a 3x3 positive definite matrix") from error
    return tuple(tuple(float(item) for item in row) for row in matrix)


def _transform(value: object, name: str) -> np.ndarray:
    try:
        matrix = np.asarray(validate_transform(np.asarray(value).tolist()), dtype=float)
    except (TypeError, ValueError) as error:
        raise WorldLocalizationError(f"{name} must be a rigid 4x4 transform") from error
    matrix.setflags(write=False)
    return matrix


def _pose_matrix(raw: object, name: str) -> np.ndarray:
    if not isinstance(raw, Mapping):
        raise WorldLocalizationError(f"{name} must be a canonical framed pose")
    fields = {"x_m", "y_m", "z_m", "qx", "qy", "qz", "qw"}
    if not fields.issubset(raw):
        raise WorldLocalizationError(f"{name} must be a canonical framed pose")
    x, y, z, qx, qy, qz, qw = (
        _finite(raw[field], name) for field in ("x_m", "y_m", "z_m", "qx", "qy", "qz", "qw")
    )
    quaternion = np.array([qx, qy, qz, qw])
    if not np.isclose(np.linalg.norm(quaternion), 1.0, atol=1e-6):
        raise WorldLocalizationError(f"{name} quaternion must be unit length")
    x2, y2, z2 = quaternion[:3] * 2
    xx, yy, zz = quaternion[:3] * np.array([x2, y2, z2])
    xy, xz, yz = qx * y2, qx * z2, qy * z2
    wx, wy, wz = qw * np.array([x2, y2, z2])
    matrix = np.eye(4)
    matrix[:3, :3] = (
        (1 - yy - zz, xy - wz, xz + wy),
        (xy + wz, 1 - xx - zz, yz - wx),
        (xz - wy, yz + wx, 1 - xx - yy),
    )
    matrix[:3, 3] = (x, y, z)
    return _transform(matrix.tolist(), name)


def _derived_event_id(event_id: str, kind: str) -> str:
    return f"world-{kind}-{sha256(event_id.encode()).hexdigest()}"


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _evidence_document(path: str | Path, name: str, expected_sha256: str) -> Mapping[str, object]:
    source = Path(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_EVIDENCE_BYTES:
            raise WorldLocalizationError(f"{name} evidence must be a bounded regular file")
        chunks: list[bytes] = []
        remaining = _MAX_EVIDENCE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except OSError as error:
        raise WorldLocalizationError(f"{name} evidence could not be opened safely") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                raise WorldLocalizationError(
                    f"{name} evidence could not be closed safely"
                ) from error
    if len(payload) > _MAX_EVIDENCE_BYTES:
        raise WorldLocalizationError(f"{name} evidence exceeds 1 MiB")
    if sha256(payload).hexdigest() != expected_sha256:
        raise WorldLocalizationError(f"{name} evidence hash does not match its host pin")
    try:
        document = json.loads(payload, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError) as error:
        raise WorldLocalizationError(f"{name} evidence must be JSON") from error
    if not isinstance(document, Mapping):
        raise WorldLocalizationError(f"{name} evidence must be a JSON object")
    return document


def _evidence_matches(
    document: Mapping[str, object], name: str, expected: Mapping[str, object]
) -> None:
    if any(document.get(key) != value for key, value in expected.items()):
        raise WorldLocalizationError(f"{name} evidence does not match its pinned scope")


@dataclass(frozen=True, slots=True)
class WorldEnuTransform:
    transform_id: str
    sha256: str
    matrix_world_enu: tuple[tuple[float, ...], ...]
    measured: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "transform_id", _identifier(self.transform_id, "transform_id"))
        object.__setattr__(self, "sha256", _digest(self.sha256, "world ENU transform sha256"))
        if self.measured is not True:
            raise WorldLocalizationError("T_world_enu must be measured")
        matrix = _transform(self.matrix_world_enu, "matrix_world_enu")
        object.__setattr__(
            self,
            "matrix_world_enu",
            tuple(tuple(float(item) for item in row) for row in matrix),
        )

    @property
    def matrix(self) -> np.ndarray:
        return np.asarray(self.matrix_world_enu)


@dataclass(frozen=True, slots=True)
class MeasurementUncertainty:
    artifact_id: str
    sha256: str
    evidence_kind: Literal["recorded_live", "approved_fixture"]
    camera_calibration_id: str
    camera_pipeline_id: str
    position_covariance_world_m2: tuple[tuple[float, ...], ...]
    velocity_covariance_enu_m2ps2: tuple[tuple[float, ...], ...]
    height_variance_enu_m2: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _identifier(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "sha256", _digest(self.sha256, "uncertainty sha256"))
        if self.evidence_kind not in {"recorded_live", "approved_fixture"}:
            raise WorldLocalizationError("uncertainty evidence kind is unsupported")
        for name in ("camera_calibration_id", "camera_pipeline_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(
            self,
            "position_covariance_world_m2",
            _covariance(self.position_covariance_world_m2, "position covariance"),
        )
        object.__setattr__(
            self,
            "velocity_covariance_enu_m2ps2",
            _covariance(self.velocity_covariance_enu_m2ps2, "velocity covariance"),
        )
        height = _finite(self.height_variance_enu_m2, "height variance")
        if height <= 0:
            raise WorldLocalizationError("height variance must be positive")
        object.__setattr__(self, "height_variance_enu_m2", height)


@dataclass(frozen=True, slots=True)
class WorldLocalizationPins:
    drone_id: int
    map_id: str
    map_version: str
    map_content_sha256: str
    geometry_id: str
    geometry_sha256: str
    physical_datum: str
    tag_source_id: str
    body_pose_source_id: str
    telemetry_source_id: str
    telemetry_frame_id: str
    height_datum_id: str
    height_alignment_artifact_id: str
    height_alignment_sha256: str
    height_alignment_measured: bool
    capture_clock_mapping_id: str
    camera_calibration_id: str
    camera_calibration_sha256: str
    camera_pipeline_id: str
    body_extrinsics_id: str
    world_enu: WorldEnuTransform
    uncertainty: MeasurementUncertainty

    def __post_init__(self) -> None:
        if type(self.drone_id) is not int or self.drone_id <= 0:
            raise WorldLocalizationError("drone_id must be positive")
        for name in (
            "map_id",
            "map_version",
            "geometry_id",
            "physical_datum",
            "tag_source_id",
            "body_pose_source_id",
            "telemetry_source_id",
            "telemetry_frame_id",
            "height_datum_id",
            "height_alignment_artifact_id",
            "capture_clock_mapping_id",
            "camera_calibration_id",
            "camera_pipeline_id",
            "body_extrinsics_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        for name in (
            "map_content_sha256",
            "geometry_sha256",
            "camera_calibration_sha256",
            "height_alignment_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if not isinstance(self.world_enu, WorldEnuTransform):
            raise WorldLocalizationError("world_enu must be a measured transform")
        if not isinstance(self.uncertainty, MeasurementUncertainty):
            raise WorldLocalizationError("uncertainty must be measured evidence")
        if (
            self.uncertainty.camera_calibration_id != self.camera_calibration_id
            or self.uncertainty.camera_pipeline_id != self.camera_pipeline_id
        ):
            raise WorldLocalizationError("uncertainty evidence does not match camera pins")
        if self.height_alignment_measured is not True:
            raise WorldLocalizationError("telemetry height datum must be measured against ENU z")


@dataclass(frozen=True, slots=True)
class WorldPose:
    map_id: str
    map_version: str
    map_content_sha256: str
    geometry_id: str
    geometry_sha256: str
    position_world_m: tuple[float, float, float]
    covariance_world_m2: tuple[tuple[float, ...], ...]


@dataclass(frozen=True, slots=True)
class _CameraFrame:
    calibration_id: str


CaptureStamp = tuple[str, str, int]
CameraKey = tuple[str, int, int, str, str, CaptureStamp]
BodyKey = tuple[str, int, int, str, CaptureStamp]


class WorldLocalizationAdapter:
    """Statefully correlate canonical camera evidence before emitting ENU measurements."""

    def __init__(
        self,
        bundle: str | Path,
        accepted_versions: Mapping[str, str],
        pins: WorldLocalizationPins,
        capture_clock_mapping: ClockMapping,
        *,
        evidence_paths: Mapping[str, str | Path],
        body_extrinsics_at_capture: Callable[[Observation, float], BodyExtrinsics | None]
        | None = None,
        allow_fixture_evidence: bool = False,
    ) -> None:
        self.pins = pins
        if capture_clock_mapping.mapping_id != pins.capture_clock_mapping_id:
            raise WorldLocalizationError("capture clock mapping does not match host pins")
        if capture_clock_mapping.max_error_ms > _MAX_CAPTURE_MAPPING_ERROR_MS:
            raise WorldLocalizationError(
                "capture clock mapping error exceeds control admission bound"
            )
        self.capture_clock_mapping = capture_clock_mapping
        self._body_extrinsics_at_capture = body_extrinsics_at_capture
        if pins.uncertainty.evidence_kind == "approved_fixture" and not allow_fixture_evidence:
            raise WorldLocalizationError(
                "live localization requires recorded_live uncertainty evidence"
            )
        try:
            manifest = validate_bundle(bundle, dict(accepted_versions))
        except (OSError, TypeError, ValueError) as error:
            raise WorldLocalizationError("world bundle is not externally approved") from error
        if (
            manifest.get("schema_version") != 2
            or manifest.get("map_id") != pins.map_id
            or manifest.get("bundle_version") != pins.map_version
            or manifest.get("content_sha256") != pins.map_content_sha256
            or manifest.get("frame", {}).get("physical_datum") != pins.physical_datum
        ):
            raise WorldLocalizationError("world bundle does not match host pins")
        expected_paths = {
            "geometry": pins.geometry_sha256,
            "camera_calibration": pins.camera_calibration_sha256,
            "uncertainty": pins.uncertainty.sha256,
            "world_enu": pins.world_enu.sha256,
            "height_alignment": pins.height_alignment_sha256,
        }
        if set(evidence_paths) != set(expected_paths):
            raise WorldLocalizationError(
                "world localization evidence paths do not match the contract"
            )
        evidence = {
            name: _evidence_document(evidence_paths[name], name, digest)
            for name, digest in expected_paths.items()
        }
        _evidence_matches(
            evidence["geometry"],
            "geometry",
            {
                "artifact_id": pins.geometry_id,
                "kind": "geometry",
                "map_id": pins.map_id,
                "map_version": pins.map_version,
            },
        )
        _evidence_matches(
            evidence["camera_calibration"],
            "camera calibration",
            {
                "artifact_id": pins.camera_calibration_id,
                "kind": "camera_calibration",
                "camera_pipeline_id": pins.camera_pipeline_id,
            },
        )
        _evidence_matches(
            evidence["uncertainty"],
            "uncertainty",
            {
                "artifact_id": pins.uncertainty.artifact_id,
                "kind": "uncertainty",
                "camera_calibration_id": pins.camera_calibration_id,
                "camera_pipeline_id": pins.camera_pipeline_id,
            },
        )
        _evidence_matches(
            evidence["world_enu"],
            "world ENU transform",
            {
                "artifact_id": pins.world_enu.transform_id,
                "kind": "world_enu_transform",
                "map_id": pins.map_id,
                "map_version": pins.map_version,
                "physical_datum": pins.physical_datum,
                "matrix_world_enu": [list(row) for row in pins.world_enu.matrix_world_enu],
            },
        )
        _evidence_matches(
            evidence["height_alignment"],
            "height alignment",
            {
                "artifact_id": pins.height_alignment_artifact_id,
                "kind": "height_alignment",
                "map_id": pins.map_id,
                "telemetry_frame_id": pins.telemetry_frame_id,
                "height_datum_id": pins.height_datum_id,
            },
        )
        tags = manifest.document("tags.yaml").get("tags")
        if not isinstance(tags, list):
            raise WorldLocalizationError("world bundle tags are invalid")
        self._tags: dict[int, np.ndarray] = {}
        self._sizes: dict[int, float] = {}
        for tag in tags:
            if not isinstance(tag, Mapping) or tag.get("verified_for_flight") is not True:
                continue
            try:
                tag_id = tag["id"]
                if type(tag_id) is not int:
                    raise TypeError
                self._tags[tag_id] = _transform(tag["T_world_tag"], "T_world_tag")
                self._sizes[tag_id] = _finite(tag["size_m"], "tag size")
            except (KeyError, TypeError, WorldLocalizationError) as error:
                raise WorldLocalizationError("world bundle verified tag is invalid") from error
        if not self._tags:
            raise WorldLocalizationError("world bundle has no tape-verified tag")
        self._camera_frames: OrderedDict[CameraKey, _CameraFrame] = OrderedDict()
        self._body_cameras: OrderedDict[BodyKey, BodyExtrinsics] = OrderedDict()
        self._connection_epoch: int | None = None

    def ingest(self, event: Observation, *, connection_epoch: int) -> tuple[object, ...]:
        """Consume one admitted observation and return ordinary fuser measurements."""
        submission = event.submission
        if (
            submission.device_id != self.pins.drone_id
            or submission.connection_epoch != connection_epoch
            or submission.node_type != "aircraft"
        ):
            raise WorldLocalizationError(
                "canonical observation does not match current aircraft epoch"
            )
        if self._connection_epoch != connection_epoch:
            self._connection_epoch = connection_epoch
            self._camera_frames.clear()
            self._body_cameras.clear()
        payload = submission.payload
        kind = payload["kind"]
        if kind == "camera_frame":
            self._ingest_camera_frame(event)
            return ()
        if kind == "pose":
            self._ingest_body_camera(event)
            return ()
        if kind == "tag_observation":
            return (self._tag_fix(event),)
        if kind == "telemetry":
            return self._telemetry(event)
        return ()

    def project_world(self, snapshot: ControlLocalizationSnapshot) -> WorldPose | None:
        if (
            self._connection_epoch is None
            or snapshot.drone_id != self.pins.drone_id
            or snapshot.connection_epoch != self._connection_epoch
            or snapshot.map_id != self.pins.map_id
            or snapshot.geometry_id != self.pins.geometry_id
            or snapshot.capture_clock_id != self.pins.capture_clock_mapping_id
            or not snapshot.control_eligible
            or snapshot.status != "ready"
            or snapshot.fix_age_s is None
            or snapshot.fix_age_s < 0
            or snapshot.position_map_enu_m is None
            or snapshot.covariance_map_enu_m2 is None
        ):
            return None
        transform = self.pins.world_enu.matrix
        rotation = transform[:3, :3]
        position = rotation @ np.asarray(snapshot.position_map_enu_m) + transform[:3, 3]
        covariance = rotation @ np.asarray(snapshot.covariance_map_enu_m2) @ rotation.T
        return WorldPose(
            self.pins.map_id,
            self.pins.map_version,
            self.pins.map_content_sha256,
            self.pins.geometry_id,
            self.pins.geometry_sha256,
            tuple(float(value) for value in position),
            _covariance(covariance, "world covariance"),
        )

    def _capture_stamp(self, event: Observation) -> CaptureStamp:
        submission = event.submission
        if submission.clock_mapping_id != self.pins.capture_clock_mapping_id:
            raise WorldLocalizationError("canonical observation clock mapping is unpinned")
        if submission.t_capture is None:
            raise WorldLocalizationError("canonical observation requires a capture timestamp")
        return (
            submission.t_capture.clock_id,
            submission.t_capture.unit,
            submission.t_capture.value,
        )

    def _capture_relay_ms(self, event: Observation) -> int:
        self._capture_stamp(event)
        try:
            return self.capture_clock_mapping.relay_ms(event.submission.t_capture)
        except ValueError as error:
            raise WorldLocalizationError("canonical capture timestamp is invalid") from error

    @staticmethod
    def _require_positive_confidence(event: Observation) -> None:
        if event.submission.confidence <= 0:
            raise WorldLocalizationError("canonical evidence requires positive confidence")

    def _camera_key(self, event: Observation, image_id: str) -> CameraKey:
        submission = event.submission
        return (
            submission.session,
            submission.device_id,
            submission.connection_epoch,
            submission.source_id,
            image_id,
            self._capture_stamp(event),
        )

    def _body_key(self, event: Observation, source_id: str | None = None) -> BodyKey:
        submission = event.submission
        return (
            submission.session,
            submission.device_id,
            submission.connection_epoch,
            source_id if source_id is not None else submission.source_id,
            self._capture_stamp(event),
        )

    def _ingest_camera_frame(self, event: Observation) -> None:
        submission = event.submission
        payload = submission.payload
        if submission.source_id != self.pins.tag_source_id or submission.frame != "camera":
            raise WorldLocalizationError("camera frame source is unpinned")
        self._require_positive_confidence(event)
        if payload.get("calibration_id") != self.pins.camera_calibration_id:
            raise WorldLocalizationError("camera frame calibration is unpinned")
        image_id = payload.get("image_id")
        if type(image_id) is not str:
            raise WorldLocalizationError("camera frame image identity is invalid")
        self._remember(
            self._camera_frames,
            self._camera_key(event, image_id),
            _CameraFrame(self.pins.camera_calibration_id),
        )

    def _ingest_body_camera(self, event: Observation) -> None:
        submission = event.submission
        payload = submission.payload
        if submission.source_id != self.pins.body_pose_source_id or submission.frame != "body":
            raise WorldLocalizationError("body-camera source is unpinned")
        self._require_positive_confidence(event)
        pose = payload.get("pose")
        if not isinstance(pose, Mapping) or (pose.get("parent_frame"), pose.get("child_frame")) != (
            "body",
            "camera",
        ):
            raise WorldLocalizationError("dynamic extrinsics must be a body-to-camera pose")
        capture_time = self._control_capture_time(self._capture_relay_ms(event))
        if self._body_extrinsics_at_capture is None:
            raise WorldLocalizationError(
                "dynamic extrinsics lack measured gimbal and attitude timing"
            )
        measured = self._body_extrinsics_at_capture(event, capture_time)
        if not isinstance(measured, BodyExtrinsics):
            raise WorldLocalizationError("dynamic extrinsics evidence is unavailable")
        if (
            measured.extrinsics_id != self.pins.body_extrinsics_id
            or measured.source_id != self.pins.tag_source_id
            or measured.capture_time != capture_time
            or measured.gimbal_time != capture_time
            or measured.attitude_time != capture_time
            or not np.allclose(measured.matrix, _pose_matrix(pose, "body pose"), rtol=0, atol=1e-9)
        ):
            raise WorldLocalizationError("dynamic extrinsics do not prove this capture-time pose")
        self._remember(self._body_cameras, self._body_key(event), measured)

    def _tag_fix(self, event: Observation) -> TagFix:
        submission = event.submission
        payload = submission.payload
        if submission.source_id != self.pins.tag_source_id or submission.frame != "camera":
            raise WorldLocalizationError("tag observation source is unpinned")
        self._require_positive_confidence(event)
        if payload.get("pose_accepted") is not True or payload.get("reason") != "pose":
            raise WorldLocalizationError("tag observation pose was not accepted")
        tag_id = payload.get("tag_id")
        if type(tag_id) is not int or tag_id not in self._tags:
            raise WorldLocalizationError("tag observation does not reference a verified world tag")
        size = _finite(payload.get("size_m"), "tag size")
        if not np.isclose(size, self._sizes[tag_id], rtol=0, atol=1e-6):
            raise WorldLocalizationError("tag observation size does not match the surveyed tag")
        image_id = payload.get("image_id")
        if type(image_id) is not str:
            raise WorldLocalizationError("tag observation image identity is invalid")
        camera_frame = self._camera_frames.get(self._camera_key(event, image_id))
        capture_ms = self._capture_relay_ms(event)
        if camera_frame is None:
            raise WorldLocalizationError("tag observation lacks a matching captured camera frame")
        body_camera = self._body_cameras.get(self._body_key(event, self.pins.body_pose_source_id))
        if body_camera is None:
            raise WorldLocalizationError("tag observation lacks capture-time body extrinsics")
        pose = payload.get("tag_pose")
        if not isinstance(pose, Mapping) or (pose.get("parent_frame"), pose.get("child_frame")) != (
            "camera",
            f"tag:{tag_id}",
        ):
            raise WorldLocalizationError("tag observation pose frame is invalid")
        world_body = (
            self._tags[tag_id]
            @ np.linalg.inv(_pose_matrix(pose, "camera tag pose"))
            @ np.linalg.inv(np.asarray(body_camera.matrix))
        )
        enu_world = np.linalg.inv(self.pins.world_enu.matrix)
        enu_body = enu_world @ world_body
        rotation = enu_world[:3, :3]
        covariance = rotation @ np.asarray(self.pins.uncertainty.position_covariance_world_m2)
        covariance = covariance @ rotation.T
        capture_time = self._control_capture_time(capture_ms)
        return TagFix(
            event_id=submission.event_id,
            drone_id=self.pins.drone_id,
            connection_epoch=submission.connection_epoch,
            map_id=self.pins.map_id,
            geometry_id=self.pins.geometry_id,
            clock_id=self.pins.capture_clock_mapping_id,
            capture_time=capture_time,
            position_map_enu_m=tuple(float(value) for value in enu_body[:3, 3]),
            covariance_map_enu_m2=_covariance(covariance, "ENU position covariance"),
            source_id=self.pins.tag_source_id,
            camera_calibration_id=self.pins.camera_calibration_id,
            source_verified=True,
            timing_verified=True,
            extrinsics=BodyExtrinsics(
                extrinsics_id=self.pins.body_extrinsics_id,
                source_id=self.pins.tag_source_id,
                matrix=body_camera.matrix,
                capture_time=body_camera.capture_time,
                gimbal_time=body_camera.gimbal_time,
                attitude_time=body_camera.attitude_time,
                measured=body_camera.measured,
            ),
        )

    def _telemetry(self, event: Observation) -> tuple[VelocityObservation, HeightObservation]:
        submission = event.submission
        payload = submission.payload
        self._require_positive_confidence(event)
        if (
            submission.source_id != self.pins.telemetry_source_id
            or submission.frame != self.pins.telemetry_frame_id
        ):
            raise WorldLocalizationError("telemetry source or frame is unpinned")
        position = payload.get("position")
        velocity = payload.get("velocity")
        if not isinstance(position, Mapping) or not isinstance(velocity, Mapping):
            raise WorldLocalizationError("telemetry vectors are invalid")
        if (
            position.get("frame") != self.pins.telemetry_frame_id
            or velocity.get("frame") != self.pins.telemetry_frame_id
        ):
            raise WorldLocalizationError("telemetry vectors must use the pinned ENU frame")
        enu_position = np.array(
            [_finite(position.get(axis), "ENU position") for axis in ("x_m", "y_m", "z_m")]
        )
        enu_velocity = np.array(
            [_finite(velocity.get(axis), "ENU velocity") for axis in ("x_m_s", "y_m_s", "z_m_s")]
        )
        capture_time = self._control_capture_time(self._capture_relay_ms(event))
        common = dict(
            event_id=_derived_event_id(submission.event_id, "velocity"),
            drone_id=self.pins.drone_id,
            connection_epoch=submission.connection_epoch,
            map_id=self.pins.map_id,
            geometry_id=self.pins.geometry_id,
            clock_id=self.pins.capture_clock_mapping_id,
            capture_time=capture_time,
            source_verified=True,
            timing_verified=True,
        )
        return (
            VelocityObservation(
                **common,
                velocity_map_enu_mps=tuple(float(value) for value in enu_velocity),
                covariance_m2ps2=self.pins.uncertainty.velocity_covariance_enu_m2ps2,
                source_id=self.pins.telemetry_source_id,
            ),
            HeightObservation(
                **{**common, "event_id": _derived_event_id(submission.event_id, "height")},
                height_map_enu_m=float(enu_position[2]),
                variance_m2=self.pins.uncertainty.height_variance_enu_m2,
                source_id=self.pins.telemetry_source_id,
            ),
        )

    def _control_capture_time(self, relay_ms: int) -> float:
        # `clock_id` is the host-pinned canonical mapping ID; its seconds use relay epoch.
        return relay_ms / 1_000.0

    @staticmethod
    def _remember(cache: OrderedDict[object, object], key: object, value: object) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _MAX_CACHE_ITEMS:
            cache.popitem(last=False)

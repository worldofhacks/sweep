"""Bounded, versioned observations shared by aircraft and ground producers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite, sqrt
from typing import Literal

MAX_EVENT_BYTES = 65_536
MAX_IDENTIFIER_CHARS = 128
MAX_SESSION_CHARS = 512
MAX_COORDINATE_M = 1_000_000.0
MAX_RANGE_SAMPLES = 720
MAX_IMAGE_DIMENSION_PX = 16_384
MAX_CLOCK_ERROR_MS = 10_000
MAX_QUATERNION_ERROR = 1e-6

FrameKind = Literal["world", "odom", "body", "camera", "lidar", "tag", "legacy_map_enu"]
NodeType = Literal["aircraft", "ground"]
ClockUnit = Literal["ms", "ns"]


class ObservationError(ValueError):
    """A bounded reason for rejecting one observation or its host configuration."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _error(code: str, detail: str) -> None:
    raise ObservationError(code, detail)


def _text(value: object, name: str, maximum: int = MAX_IDENTIFIER_CHARS) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value) > maximum
    ):
        _error("invalid_observation", f"{name} must be canonical printable text")
    return value


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _error("invalid_observation", f"{name} is outside its bounded integer range")
    return value


def _number(value: object, name: str, *, maximum: float = MAX_COORDINATE_M) -> float:
    if type(value) not in {int, float} or not isfinite(value) or abs(value) > maximum:
        _error("invalid_observation", f"{name} must be a bounded finite number")
    return float(value)


def _exact(raw: object, fields: frozenset[str], name: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != fields:
        _error("invalid_observation", f"{name} fields do not match the v1 contract")
    return raw


def _canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
    except (TypeError, ValueError) as error:
        _error("invalid_observation", f"observation cannot be encoded: {error}")
    if len(encoded) > MAX_EVENT_BYTES:
        _error("observation_too_large", "encoded observation exceeds the v1 byte ceiling")
    return encoded


@dataclass(frozen=True, slots=True)
class SourceTime:
    clock_id: str
    unit: ClockUnit
    value: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "clock_id", _text(self.clock_id, "clock_id"))
        if self.unit not in {"ms", "ns"}:
            _error("invalid_observation", "source clock unit must be ms or ns")
        object.__setattr__(self, "value", _integer(self.value, "source timestamp"))

    def to_mapping(self) -> dict[str, object]:
        return {"clock_id": self.clock_id, "unit": self.unit, "value": self.value}

    @classmethod
    def parse(cls, raw: object) -> SourceTime:
        value = _exact(raw, frozenset({"clock_id", "unit", "value"}), "source timestamp")
        return cls(value["clock_id"], value["unit"], value["value"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ClockMapping:
    """Host-pinned conversion from one producer clock to relay Unix milliseconds."""

    mapping_id: str
    source_clock_id: str
    source_unit: ClockUnit
    source_reference: int
    relay_reference_ms: int
    relay_ms_numerator: int
    source_units_denominator: int
    max_error_ms: int

    def __post_init__(self) -> None:
        for name in ("mapping_id", "source_clock_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.source_unit not in {"ms", "ns"}:
            _error("invalid_clock_mapping", "source_unit must be ms or ns")
        for name in ("source_reference", "relay_reference_ms"):
            object.__setattr__(self, name, _integer(getattr(self, name), name))
        for name in ("relay_ms_numerator", "source_units_denominator"):
            object.__setattr__(self, name, _integer(getattr(self, name), name, minimum=1))
        object.__setattr__(
            self,
            "max_error_ms",
            _integer(self.max_error_ms, "max_error_ms", maximum=MAX_CLOCK_ERROR_MS),
        )

    def relay_ms(self, timestamp: SourceTime) -> int:
        if timestamp.clock_id != self.source_clock_id or timestamp.unit != self.source_unit:
            _error(
                "clock_mapping_mismatch", "timestamp does not match its referenced clock mapping"
            )
        offset = timestamp.value - self.source_reference
        scaled = offset * self.relay_ms_numerator
        if scaled >= 0:
            delta = (scaled + self.source_units_denominator // 2) // self.source_units_denominator
        else:
            delta = -(
                (-scaled + self.source_units_denominator // 2) // self.source_units_denominator
            )
        return _integer(self.relay_reference_ms + delta, "mapped relay timestamp")


@dataclass(frozen=True, slots=True)
class FrameDeclaration:
    """A host-approved frame name and the scope in which it may be referenced."""

    frame_id: str
    kind: FrameKind
    axis_convention: str
    unit: Literal["m"]
    session: str | None = None
    device_id: int | None = None
    connection_epoch: int | None = None
    source_id: str | None = None
    map_id: str | None = None
    map_version: str | None = None
    physical_datum: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame_id", _text(self.frame_id, "frame_id"))
        if self.kind not in {"world", "odom", "body", "camera", "lidar", "tag", "legacy_map_enu"}:
            _error("invalid_frame_declaration", "frame kind is unknown")
        if self.axis_convention not in {
            "right_handed_z_up",
            "east_north_up",
            "forward_left_up",
            "right_down_forward",
            "right_up_outward",
        }:
            _error("invalid_frame_declaration", "frame axis convention is unknown")
        if self.unit != "m":
            _error("invalid_frame_declaration", "frame unit must be metric metres")
        local = (self.session, self.device_id, self.connection_epoch, self.source_id)
        if self.kind == "world":
            if self.frame_id != "world" or any(value is not None for value in local):
                _error("invalid_frame_declaration", "world has global scope")
            if (
                self.axis_convention != "right_handed_z_up"
                or self.map_id is None
                or self.map_version is None
                or self.physical_datum is None
            ):
                _error(
                    "invalid_frame_declaration",
                    "world requires its convention, map pins, and physical datum",
                )
            object.__setattr__(self, "map_id", _text(self.map_id, "map_id"))
            object.__setattr__(self, "map_version", _text(self.map_version, "map_version"))
            object.__setattr__(self, "physical_datum", _text(self.physical_datum, "physical_datum"))
            return
        if self.frame_id == "world":
            _error("invalid_frame_declaration", "only the global world declaration may use world")
        if (
            any(value is None for value in local)
            or self.map_id is not None
            or self.map_version is not None
            or self.physical_datum is not None
        ):
            _error("invalid_frame_declaration", "local frames require a complete source scope")
        object.__setattr__(self, "session", _text(self.session, "frame session", MAX_SESSION_CHARS))
        object.__setattr__(
            self, "device_id", _integer(self.device_id, "frame device_id", minimum=1)
        )
        object.__setattr__(
            self, "connection_epoch", _integer(self.connection_epoch, "frame epoch", minimum=1)
        )
        object.__setattr__(self, "source_id", _text(self.source_id, "frame source_id"))


@dataclass(frozen=True, slots=True)
class FrameRegistry:
    """Host declarations, including repeated local frame IDs from different sources."""

    declarations: tuple[FrameDeclaration, ...]

    def __post_init__(self) -> None:
        copied = tuple(self.declarations)
        if not copied or len(copied) > 256:
            _error(
                "invalid_frame_registry", "frame registry must contain 1 through 256 declarations"
            )
        if not all(isinstance(item, FrameDeclaration) for item in copied):
            _error("invalid_frame_registry", "frame registry entries must be declarations")
        keys = {
            (
                item.frame_id,
                item.session,
                item.device_id,
                item.connection_epoch,
                item.source_id,
            )
            for item in copied
        }
        if len(keys) != len(copied):
            _error("invalid_frame_registry", "frame declarations must have unique scopes")
        if sum(item.kind == "world" for item in copied) > 1:
            _error("invalid_frame_registry", "frame registry may declare world once")
        object.__setattr__(self, "declarations", copied)

    def require(self, frame_id: str, submission: ObservationSubmission) -> FrameDeclaration:
        candidates = [item for item in self.declarations if item.frame_id == frame_id]
        for declaration in candidates:
            if declaration.kind == "world":
                return declaration
            if (
                declaration.session,
                declaration.device_id,
                declaration.connection_epoch,
                declaration.source_id,
            ) == (
                submission.session,
                submission.device_id,
                submission.connection_epoch,
                submission.source_id,
            ):
                return declaration
        if candidates:
            _error("frame_scope_mismatch", "observation frame is not current for its source epoch")
        _error("unknown_frame", "observation references an undeclared frame")


@dataclass(frozen=True, slots=True)
class SourceBinding:
    """Host-owned authenticated identity, node class, and frame authorization."""

    session: str
    device_id: int
    connection_epoch: int
    source_id: str
    node_type: NodeType
    allowed_frames: tuple[str, ...]
    world_map_id: str | None = None
    world_map_version: str | None = None
    world_physical_datum: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session", _text(self.session, "binding session", MAX_SESSION_CHARS)
        )
        object.__setattr__(
            self, "device_id", _integer(self.device_id, "binding device_id", minimum=1)
        )
        object.__setattr__(
            self,
            "connection_epoch",
            _integer(self.connection_epoch, "binding connection_epoch", minimum=1),
        )
        object.__setattr__(self, "source_id", _text(self.source_id, "binding source_id"))
        if self.node_type not in {"aircraft", "ground"}:
            _error("invalid_source_binding", "binding node_type is unknown")
        frames = tuple(_text(item, "allowed frame") for item in self.allowed_frames)
        if not frames or len(frames) > 32 or len(set(frames)) != len(frames):
            _error("invalid_source_binding", "binding allowed frames must be unique and bounded")
        object.__setattr__(self, "allowed_frames", frames)
        pins = (self.world_map_id, self.world_map_version, self.world_physical_datum)
        if "world" in frames:
            if any(item is None for item in pins):
                _error("invalid_source_binding", "world authorization requires complete map pins")
            object.__setattr__(self, "world_map_id", _text(self.world_map_id, "world_map_id"))
            object.__setattr__(
                self, "world_map_version", _text(self.world_map_version, "world_map_version")
            )
            object.__setattr__(
                self,
                "world_physical_datum",
                _text(self.world_physical_datum, "world_physical_datum"),
            )
        elif any(item is not None for item in pins):
            _error("invalid_source_binding", "local-only binding cannot carry world map pins")

    def validate(self, submission: ObservationSubmission, frames: FrameRegistry) -> None:
        if (
            self.session,
            self.device_id,
            self.connection_epoch,
            self.source_id,
            self.node_type,
        ) != (
            submission.session,
            submission.device_id,
            submission.connection_epoch,
            submission.source_id,
            submission.node_type,
        ):
            _error(
                "source_binding_mismatch", "observation does not match its authenticated binding"
            )
        self.require_frame(submission.frame, submission, frames)

    def require_frame(
        self,
        frame_id: str,
        submission: ObservationSubmission,
        frames: FrameRegistry,
    ) -> FrameDeclaration:
        if frame_id not in self.allowed_frames:
            _error("frame_not_authorized", "source binding does not authorize this frame")
        declaration = frames.require(frame_id, submission)
        if declaration.kind == "world" and (
            declaration.map_id,
            declaration.map_version,
            declaration.physical_datum,
        ) != (
            self.world_map_id,
            self.world_map_version,
            self.world_physical_datum,
        ):
            _error(
                "world_pin_mismatch", "source world authorization does not match the host map pins"
            )
        return declaration


@dataclass(frozen=True, slots=True)
class FramedVector:
    frame: str
    x_m: float
    y_m: float
    z_m: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", _text(self.frame, "vector frame"))
        for name in ("x_m", "y_m", "z_m"):
            object.__setattr__(self, name, _number(getattr(self, name), name))

    def to_mapping(self) -> dict[str, object]:
        return {"frame": self.frame, "x_m": self.x_m, "y_m": self.y_m, "z_m": self.z_m}

    @classmethod
    def parse(cls, raw: object) -> FramedVector:
        value = _exact(raw, frozenset({"frame", "x_m", "y_m", "z_m"}), "framed vector")
        return cls(value["frame"], value["x_m"], value["y_m"], value["z_m"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class FramedPose:
    """The child-frame origin and orientation expressed in its declared parent frame."""

    parent_frame: str
    child_frame: str
    x_m: float
    y_m: float
    z_m: float
    qx: float
    qy: float
    qz: float
    qw: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "parent_frame", _text(self.parent_frame, "parent_frame"))
        object.__setattr__(self, "child_frame", _text(self.child_frame, "child_frame"))
        if self.parent_frame == self.child_frame:
            _error("invalid_observation", "pose parent and child frames must differ")
        for name in ("x_m", "y_m", "z_m", "qx", "qy", "qz", "qw"):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        norm = sqrt(self.qx**2 + self.qy**2 + self.qz**2 + self.qw**2)
        if abs(norm - 1.0) > MAX_QUATERNION_ERROR:
            _error("invalid_observation", "pose quaternion must have unit length")

    def to_mapping(self) -> dict[str, object]:
        return {
            "parent_frame": self.parent_frame,
            "child_frame": self.child_frame,
            "x_m": self.x_m,
            "y_m": self.y_m,
            "z_m": self.z_m,
            "qx": self.qx,
            "qy": self.qy,
            "qz": self.qz,
            "qw": self.qw,
        }

    @classmethod
    def parse(cls, raw: object) -> FramedPose:
        fields = frozenset(
            {"parent_frame", "child_frame", "x_m", "y_m", "z_m", "qx", "qy", "qz", "qw"}
        )
        value = _exact(raw, fields, "framed pose")
        return cls(
            *(
                value[name]
                for name in (
                    "parent_frame",
                    "child_frame",
                    "x_m",
                    "y_m",
                    "z_m",
                    "qx",
                    "qy",
                    "qz",
                    "qw",
                )
            )
        )  # type: ignore[arg-type]


Payload = Mapping[str, object]
_PAYLOAD_KINDS = frozenset(
    {"aircraft_telemetry", "pose", "range_scan", "camera_frame", "tag_observation", "status"}
)


def _payload(raw: object, envelope_frame: str) -> dict[str, object]:
    if (
        not isinstance(raw, Mapping)
        or type(raw.get("kind")) is not str
        or raw["kind"] not in _PAYLOAD_KINDS
    ):
        _error("invalid_payload", "payload kind is unknown")
    kind = raw["kind"]
    if kind == "aircraft_telemetry":
        value = _exact(
            raw,
            frozenset({"kind", "position", "velocity", "battery", "link", "pos_quality", "state"}),
            "aircraft telemetry",
        )
        position = FramedVector.parse(value["position"])
        velocity = FramedVector.parse(value["velocity"])
        if position.frame != envelope_frame or velocity.frame != envelope_frame:
            _error("payload_frame_mismatch", "aircraft payload vectors must use the envelope frame")
        result: dict[str, object] = {
            "kind": kind,
            "position": position.to_mapping(),
            "velocity": velocity.to_mapping(),
        }
        for name in ("battery", "link", "pos_quality"):
            number = _number(value[name], name, maximum=1.0)
            if number < 0:
                _error("invalid_payload", f"{name} must be in [0, 1]")
            result[name] = number
        result["state"] = _text(value["state"], "state")
        return result
    if kind == "pose":
        value = _exact(raw, frozenset({"kind", "pose"}), "pose payload")
        pose = FramedPose.parse(value["pose"])
        if pose.parent_frame != envelope_frame:
            _error("payload_frame_mismatch", "pose parent frame must equal the envelope frame")
        return {"kind": kind, "pose": pose.to_mapping()}
    if kind == "range_scan":
        fields = frozenset(
            {
                "kind",
                "sensor_pose",
                "angle_min_rad",
                "angle_increment_rad",
                "range_min_m",
                "range_max_m",
                "ranges_m",
                "mount_id",
            }
        )
        value = _exact(raw, fields, "range scan")
        pose = FramedPose.parse(value["sensor_pose"])
        if pose.child_frame != envelope_frame:
            _error("payload_frame_mismatch", "range scan frame must equal sensor pose child frame")
        range_min = _number(value["range_min_m"], "range_min_m")
        range_max = _number(value["range_max_m"], "range_max_m")
        increment = _number(value["angle_increment_rad"], "angle_increment_rad")
        if not 0 <= range_min < range_max or increment <= 0:
            _error("invalid_payload", "range scan bounds or increment are invalid")
        ranges = value["ranges_m"]
        if not isinstance(ranges, list | tuple) or not 1 <= len(ranges) <= MAX_RANGE_SAMPLES:
            _error("invalid_payload", "range scan sample count exceeds its bounded envelope")
        parsed_ranges: list[float | None] = []
        for item in ranges:
            if item is None:
                parsed_ranges.append(None)
                continue
            distance = _number(item, "range sample")
            if not range_min <= distance <= range_max:
                _error("invalid_payload", "range sample lies outside declared sensor bounds")
            parsed_ranges.append(distance)
        return {
            "kind": kind,
            "sensor_pose": pose.to_mapping(),
            "angle_min_rad": _number(value["angle_min_rad"], "angle_min_rad"),
            "angle_increment_rad": increment,
            "range_min_m": range_min,
            "range_max_m": range_max,
            "ranges_m": parsed_ranges,
            "mount_id": _text(value["mount_id"], "mount_id"),
        }
    if kind == "camera_frame":
        fields = frozenset(
            {"kind", "image_id", "sha256", "width_px", "height_px", "calibration_id"}
        )
        value = _exact(raw, fields, "camera frame")
        digest = value["sha256"]
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            _error("invalid_payload", "camera frame sha256 must be lowercase hexadecimal")
        return {
            "kind": kind,
            "image_id": _text(value["image_id"], "image_id"),
            "sha256": digest,
            "width_px": _integer(
                value["width_px"], "width_px", minimum=1, maximum=MAX_IMAGE_DIMENSION_PX
            ),
            "height_px": _integer(
                value["height_px"], "height_px", minimum=1, maximum=MAX_IMAGE_DIMENSION_PX
            ),
            "calibration_id": _text(value["calibration_id"], "calibration_id"),
        }
    if kind == "tag_observation":
        fields = frozenset({"kind", "tag_id", "family", "image_id", "tag_pose", "covariance_m2"})
        value = _exact(raw, fields, "tag observation")
        tag_id = _text(value["tag_id"], "tag_id")
        pose = FramedPose.parse(value["tag_pose"])
        if pose.parent_frame != envelope_frame or pose.child_frame != f"tag:{tag_id}":
            _error("payload_frame_mismatch", "tag pose must be camera-to-declared-tag")
        covariance = value["covariance_m2"]
        if not isinstance(covariance, list | tuple) or len(covariance) != 9:
            _error("invalid_payload", "tag covariance must contain nine entries")
        return {
            "kind": kind,
            "tag_id": tag_id,
            "family": _text(value["family"], "family"),
            "image_id": _text(value["image_id"], "image_id"),
            "tag_pose": pose.to_mapping(),
            "covariance_m2": [_number(item, "tag covariance") for item in covariance],
        }
    value = _exact(raw, frozenset({"kind", "code", "detail", "capabilities"}), "status")
    capabilities = value["capabilities"]
    if not isinstance(capabilities, list | tuple) or len(capabilities) > 32:
        _error("invalid_payload", "status capabilities exceed their bounded envelope")
    return {
        "kind": kind,
        "code": _text(value["code"], "status code"),
        "detail": _text(value["detail"], "status detail", 512),
        "capabilities": [_text(item, "capability", 64) for item in capabilities],
    }


_SUBMISSION_FIELDS = frozenset(
    {
        "v",
        "type",
        "event_id",
        "session",
        "device_id",
        "connection_epoch",
        "source_id",
        "node_type",
        "frame",
        "confidence",
        "t_capture",
        "t_source_receipt",
        "clock_mapping_id",
        "payload",
    }
)
_EVENT_FIELDS = _SUBMISSION_FIELDS | frozenset({"t_ingest"})


@dataclass(frozen=True, slots=True)
class ObservationSubmission:
    event_id: str
    session: str
    device_id: int
    connection_epoch: int
    source_id: str
    node_type: NodeType
    frame: str
    confidence: float
    t_capture: SourceTime | None
    t_source_receipt: SourceTime
    clock_mapping_id: str | None
    payload: Payload

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _text(self.event_id, "event_id"))
        object.__setattr__(self, "session", _text(self.session, "session", MAX_SESSION_CHARS))
        for name in ("source_id", "frame"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "device_id", _integer(self.device_id, "device_id", minimum=1))
        object.__setattr__(
            self, "connection_epoch", _integer(self.connection_epoch, "connection_epoch", minimum=1)
        )
        if self.node_type not in {"aircraft", "ground"}:
            _error("invalid_observation", "node_type is unknown")
        confidence = _number(self.confidence, "confidence", maximum=1.0)
        if confidence < 0:
            _error("invalid_observation", "confidence must be in [0, 1]")
        object.__setattr__(self, "confidence", confidence)
        if not isinstance(self.t_source_receipt, SourceTime):
            _error("invalid_observation", "source receipt time is required")
        if self.t_capture is not None:
            if not isinstance(self.t_capture, SourceTime):
                _error("invalid_observation", "capture time must be null or a source timestamp")
            if (self.t_capture.clock_id, self.t_capture.unit) != (
                self.t_source_receipt.clock_id,
                self.t_source_receipt.unit,
            ):
                _error(
                    "capture_clock_mismatch",
                    "capture and source receipt must share a declared clock",
                )
            if self.t_capture.value > self.t_source_receipt.value:
                _error("invalid_time_order", "capture time exceeds source receipt time")
        if self.clock_mapping_id is not None:
            object.__setattr__(
                self, "clock_mapping_id", _text(self.clock_mapping_id, "clock_mapping_id")
            )
        object.__setattr__(self, "payload", _payload(self.payload, self.frame))

    def to_mapping(self) -> dict[str, object]:
        return {
            "v": 1,
            "type": "observation",
            "event_id": self.event_id,
            "session": self.session,
            "device_id": self.device_id,
            "connection_epoch": self.connection_epoch,
            "source_id": self.source_id,
            "node_type": self.node_type,
            "frame": self.frame,
            "confidence": self.confidence,
            "t_capture": None if self.t_capture is None else self.t_capture.to_mapping(),
            "t_source_receipt": self.t_source_receipt.to_mapping(),
            "clock_mapping_id": self.clock_mapping_id,
            "payload": dict(self.payload),
        }

    @classmethod
    def parse(cls, raw: object) -> ObservationSubmission:
        value = _exact(raw, _SUBMISSION_FIELDS, "observation submission")
        if value["v"] != 1 or value["type"] != "observation":
            _error("invalid_observation", "observation v/type is invalid")
        capture = value["t_capture"]
        return cls(
            event_id=value["event_id"],  # type: ignore[arg-type]
            session=value["session"],  # type: ignore[arg-type]
            device_id=value["device_id"],  # type: ignore[arg-type]
            connection_epoch=value["connection_epoch"],  # type: ignore[arg-type]
            source_id=value["source_id"],  # type: ignore[arg-type]
            node_type=value["node_type"],  # type: ignore[arg-type]
            frame=value["frame"],  # type: ignore[arg-type]
            confidence=value["confidence"],  # type: ignore[arg-type]
            t_capture=None if capture is None else SourceTime.parse(capture),
            t_source_receipt=SourceTime.parse(value["t_source_receipt"]),
            clock_mapping_id=value["clock_mapping_id"],  # type: ignore[arg-type]
            payload=value["payload"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class TimingPolicy:
    max_future_skew_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_future_skew_ms",
            _integer(self.max_future_skew_ms, "max_future_skew_ms", maximum=MAX_CLOCK_ERROR_MS),
        )


@dataclass(frozen=True, slots=True)
class RatePolicy:
    minimum_interval_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "minimum_interval_ms",
            _integer(self.minimum_interval_ms, "minimum_interval_ms", minimum=1, maximum=60_000),
        )

    def accepts(self, previous_t_ingest: int | None, t_ingest: int) -> bool:
        now = _integer(t_ingest, "t_ingest")
        return (
            previous_t_ingest is None
            or now < previous_t_ingest
            or now - previous_t_ingest >= self.minimum_interval_ms
        )


@dataclass(frozen=True, slots=True)
class Observation:
    submission: ObservationSubmission
    t_ingest: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "t_ingest", _integer(self.t_ingest, "t_ingest"))

    def to_mapping(self) -> dict[str, object]:
        return {**self.submission.to_mapping(), "t_ingest": self.t_ingest}

    def encode(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @classmethod
    def parse(cls, raw: object) -> Observation:
        value = _exact(raw, _EVENT_FIELDS, "observation")
        submission = ObservationSubmission.parse({key: value[key] for key in _SUBMISSION_FIELDS})
        return cls(submission, value["t_ingest"])


def ingest(
    submission: ObservationSubmission,
    *,
    t_ingest: int,
    frames: FrameRegistry,
    binding: SourceBinding,
    mappings: Mapping[str, ClockMapping],
    timing: TimingPolicy,
) -> Observation:
    """Stamp a producer submission after host-owned frame and clock checks."""
    ingest_time = _integer(t_ingest, "t_ingest")
    binding.validate(submission, frames)
    _validate_payload_frames(submission, frames, binding)
    if submission.clock_mapping_id is None:
        return Observation(submission, ingest_time)
    mapping = mappings.get(submission.clock_mapping_id)
    if mapping is None:
        _error("unknown_clock_mapping", "observation references an unconfigured clock mapping")
    receipt_ms = mapping.relay_ms(submission.t_source_receipt)
    if receipt_ms > ingest_time + mapping.max_error_ms + timing.max_future_skew_ms:
        _error("source_receipt_in_future", "mapped source receipt exceeds the relay ingest bound")
    if submission.t_capture is not None:
        capture_ms = mapping.relay_ms(submission.t_capture)
        if capture_ms > ingest_time + mapping.max_error_ms + timing.max_future_skew_ms:
            _error("capture_in_future", "mapped capture exceeds the relay ingest bound")
    return Observation(submission, ingest_time)


def decode_submission(encoded: bytes | str) -> ObservationSubmission:
    return ObservationSubmission.parse(_decode(encoded))


def decode_observation(encoded: bytes | str) -> Observation:
    return Observation.parse(_decode(encoded))


def _decode(encoded: bytes | str) -> object:
    raw = encoded.encode() if isinstance(encoded, str) else encoded
    if not isinstance(raw, bytes) or len(raw) > MAX_EVENT_BYTES:
        _error("observation_too_large", "encoded observation exceeds the v1 byte ceiling")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _error("duplicate_json_key", "observation JSON contains a duplicate object key")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        _error("invalid_observation", f"observation is not valid JSON: {error.msg}")


def _validate_payload_frames(
    submission: ObservationSubmission, frames: FrameRegistry, binding: SourceBinding
) -> None:
    payload = submission.payload
    kind = payload["kind"]
    if kind == "aircraft_telemetry":
        binding.require_frame(str(payload["position"]["frame"]), submission, frames)  # type: ignore[index]
        binding.require_frame(str(payload["velocity"]["frame"]), submission, frames)  # type: ignore[index]
    elif kind == "pose":
        pose = payload["pose"]  # type: ignore[assignment]
        binding.require_frame(str(pose["parent_frame"]), submission, frames)  # type: ignore[index]
        binding.require_frame(str(pose["child_frame"]), submission, frames)  # type: ignore[index]
    elif kind == "range_scan":
        pose = payload["sensor_pose"]  # type: ignore[assignment]
        binding.require_frame(str(pose["parent_frame"]), submission, frames)  # type: ignore[index]
        binding.require_frame(str(pose["child_frame"]), submission, frames)  # type: ignore[index]
    elif kind == "tag_observation":
        pose = payload["tag_pose"]  # type: ignore[assignment]
        binding.require_frame(str(pose["parent_frame"]), submission, frames)  # type: ignore[index]
        binding.require_frame(str(pose["child_frame"]), submission, frames)  # type: ignore[index]

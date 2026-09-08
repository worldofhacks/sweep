"""One typed observation envelope for live consumers, audit, and replay.

Submissions omit the three relay-owned fields. Accepted observations add them;
producer identity and capture evidence remain unchanged. No type here is a plan.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from math import pi
from types import MappingProxyType

from spatial.contracts import (
    FrameDeclaration,
    FrameKind,
    NodeType,
    ObservationError,
    Position,
    exact,
    identifier,
    integer,
    number,
)

MAX_OBSERVATION_BYTES = 16 * 1024
MAX_SCAN_RANGES = 720
MAX_RANGE_MM = 655_350
MAX_OBSERVATION_SOURCES = 64
MAX_SOURCE_FRAMES = 8
PAYLOAD_MAX_HZ = MappingProxyType({"pose": 20, "lidar_scan": 5})
SUBMISSION_FIELDS = frozenset(
    {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "drone_id",
        "connection_epoch",
        "source_id",
        "node_type",
        "t_capture",
        "frame",
        "confidence",
        "payload",
    }
)
RELAY_FIELDS = frozenset({"t_ingest", "frame_provenance", "authority"})


@dataclass(frozen=True, slots=True)
class PosePayload:
    position: Position
    yaw_rad: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.position, Position):
            raise ObservationError("invalid_observation", "pose requires an explicit position")
        if self.yaw_rad is not None:
            object.__setattr__(self, "yaw_rad", number(self.yaw_rad, "yaw_rad", -pi, pi))

    @property
    def kind(self) -> str:
        return "pose"

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "position": self.position.to_dict(), "yaw_rad": self.yaw_rad}


@dataclass(frozen=True, slots=True)
class LidarScanPayload:
    angle_min_mdeg: int
    angle_increment_mdeg: int
    range_min_mm: int
    range_max_mm: int
    ranges_mm: tuple[int | None, ...]

    def __post_init__(self) -> None:
        integer(self.angle_min_mdeg, "angle_min_mdeg", -180_000, 180_000)
        integer(self.angle_increment_mdeg, "angle_increment_mdeg", 1, 360_000)
        integer(self.range_min_mm, "range_min_mm", 1, MAX_RANGE_MM)
        integer(self.range_max_mm, "range_max_mm", self.range_min_mm, MAX_RANGE_MM)
        if type(self.ranges_mm) is not tuple or not 1 <= len(self.ranges_mm) <= MAX_SCAN_RANGES:
            raise ObservationError(
                "invalid_observation", "scan ranges must be a bounded immutable tuple"
            )
        if self.angle_increment_mdeg * len(self.ranges_mm) > 360_000:
            raise ObservationError(
                "invalid_observation", "scan cannot cover more than one revolution"
            )
        for item in self.ranges_mm:
            if item is not None:
                integer(item, "range_mm", self.range_min_mm, self.range_max_mm)

    @property
    def kind(self) -> str:
        return "lidar_scan"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "angle_min_mdeg": self.angle_min_mdeg,
            "angle_increment_mdeg": self.angle_increment_mdeg,
            "range_min_mm": self.range_min_mm,
            "range_max_mm": self.range_max_mm,
            "ranges_mm": list(self.ranges_mm),
        }


@dataclass(frozen=True, slots=True)
class LegacyAircraftTelemetryPayload:
    position: Position
    vx_m_s: float
    vy_m_s: float
    vz_m_s: float
    battery: float
    state: str
    link: float
    pos_quality: float

    def __post_init__(self) -> None:
        if not isinstance(self.position, Position):
            raise ObservationError(
                "invalid_observation", "legacy pose requires an explicit position"
            )
        for key in ("vx_m_s", "vy_m_s", "vz_m_s"):
            object.__setattr__(self, key, number(getattr(self, key), key, -1_000, 1_000))
        for key in ("battery", "link", "pos_quality"):
            object.__setattr__(self, key, number(getattr(self, key), key, 0, 1))
        identifier(self.state, "state")

    @property
    def kind(self) -> str:
        return "legacy_aircraft_telemetry"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "position": self.position.to_dict(),
            "vx_m_s": self.vx_m_s,
            "vy_m_s": self.vy_m_s,
            "vz_m_s": self.vz_m_s,
            "battery": self.battery,
            "state": self.state,
            "link": self.link,
            "pos_quality": self.pos_quality,
            "capture_time_available": False,
        }


Payload = PosePayload | LidarScanPayload | LegacyAircraftTelemetryPayload


def parse_payload(raw: object, frame: str, *, allow_legacy: bool = False) -> Payload:
    if not isinstance(raw, Mapping):
        raise ObservationError("invalid_observation", "payload must be an object")
    kind = raw.get("kind")
    if kind == "pose":
        value = exact(raw, {"kind", "position", "yaw_rad"}, "pose payload")
        position = Position.parse(value["position"])
        if position.frame != frame:
            raise ObservationError("frame_mismatch", "pose and envelope frames differ")
        yaw = value["yaw_rad"]
        return PosePayload(position, None if yaw is None else number(yaw, "yaw_rad", -pi, pi))
    if kind == "lidar_scan":
        value = exact(
            raw,
            {
                "kind",
                "angle_min_mdeg",
                "angle_increment_mdeg",
                "range_min_mm",
                "range_max_mm",
                "ranges_mm",
            },
            "lidar payload",
        )
        start = integer(value["angle_min_mdeg"], "angle_min_mdeg", -180_000, 180_000)
        step = integer(value["angle_increment_mdeg"], "angle_increment_mdeg", 1, 360_000)
        low = integer(value["range_min_mm"], "range_min_mm", 1, MAX_RANGE_MM)
        high = integer(value["range_max_mm"], "range_max_mm", low, MAX_RANGE_MM)
        ranges = value["ranges_mm"]
        if type(ranges) is not list or not 1 <= len(ranges) <= MAX_SCAN_RANGES:
            raise ObservationError("invalid_observation", "scan must contain 1 through 720 ranges")
        if step * len(ranges) > 360_000:
            raise ObservationError(
                "invalid_observation", "scan cannot cover more than one revolution"
            )
        return LidarScanPayload(
            start,
            step,
            low,
            high,
            tuple(
                None if item is None else integer(item, "range_mm", low, high) for item in ranges
            ),
        )
    if kind == "legacy_aircraft_telemetry" and allow_legacy:
        value = exact(
            raw,
            {
                "kind",
                "position",
                "vx_m_s",
                "vy_m_s",
                "vz_m_s",
                "battery",
                "state",
                "link",
                "pos_quality",
                "capture_time_available",
            },
            "legacy payload",
        )
        position = Position.parse(value["position"])
        if position.frame != frame:
            raise ObservationError("frame_mismatch", "legacy pose and envelope frames differ")
        if value["capture_time_available"] is not False:
            raise ObservationError("invalid_observation", "legacy capture time is unavailable")
        return LegacyAircraftTelemetryPayload(
            position,
            *(number(value[key], key, -1_000, 1_000) for key in ("vx_m_s", "vy_m_s", "vz_m_s")),
            number(value["battery"], "battery", 0, 1),
            identifier(value["state"], "state"),
            number(value["link"], "link", 0, 1),
            number(value["pos_quality"], "pos_quality", 0, 1),
        )
    raise ObservationError("unsupported_observation_payload", "payload kind is not supported")


@dataclass(frozen=True, slots=True)
class ObservationSubmission:
    t: int
    event_id: str
    session: str
    drone_id: int
    connection_epoch: int
    source_id: str
    node_type: NodeType
    t_capture: int | None
    frame: str
    confidence: float
    payload: Payload

    def __post_init__(self) -> None:
        integer(self.t, "t")
        for key in ("event_id", "source_id", "frame"):
            identifier(getattr(self, key), key)
        identifier(self.session, "session", 512)
        integer(self.drone_id, "drone_id", 1, 2**31 - 1)
        integer(self.connection_epoch, "connection_epoch", 1, 2**31 - 1)
        if not isinstance(self.node_type, NodeType):
            raise ObservationError("invalid_observation", "node_type must be explicit")
        number(self.confidence, "confidence", 0, 1)
        if not isinstance(
            self.payload, (PosePayload, LidarScanPayload, LegacyAircraftTelemetryPayload)
        ):
            raise ObservationError("unsupported_observation_payload", "payload must be typed")
        if (
            isinstance(self.payload, (PosePayload, LegacyAircraftTelemetryPayload))
            and self.payload.position.frame != self.frame
        ):
            raise ObservationError("frame_mismatch", "position and envelope frames differ")
        if isinstance(self.payload, LegacyAircraftTelemetryPayload):
            if (
                self.t_capture is not None
                or self.node_type is not NodeType.AIRCRAFT
                or self.confidence != 0
            ):
                raise ObservationError(
                    "invalid_observation", "legacy capture time and confidence are unqualified"
                )
        else:
            integer(self.t_capture, "t_capture", 0, self.t)

    @classmethod
    def parse(cls, raw: object, *, allow_legacy: bool = False) -> ObservationSubmission:
        value = exact(raw, SUBMISSION_FIELDS, "observation submission")
        if type(value["v"]) is not int or value["v"] != 1 or value["type"] != "observation":
            raise ObservationError("invalid_observation", "expected observation v1")
        frame = identifier(value["frame"], "frame")
        payload = parse_payload(value["payload"], frame, allow_legacy=allow_legacy)
        timestamp = integer(value["t"], "t")
        try:
            node_type = NodeType(value["node_type"])
        except (TypeError, ValueError):
            raise ObservationError("invalid_observation", "unknown node_type") from None
        confidence = number(value["confidence"], "confidence", 0, 1)
        legacy = isinstance(payload, LegacyAircraftTelemetryPayload)
        if legacy:
            if (
                value["t_capture"] is not None
                or node_type is not NodeType.AIRCRAFT
                or confidence != 0
            ):
                raise ObservationError(
                    "invalid_observation",
                    "legacy evidence has no capture time or qualified confidence",
                )
            capture = None
        else:
            capture = integer(value["t_capture"], "t_capture", 0, timestamp)
        result = cls(
            timestamp,
            identifier(value["event_id"], "event_id"),
            identifier(value["session"], "session", 512),
            integer(value["drone_id"], "drone_id", 1, 2**31 - 1),
            integer(value["connection_epoch"], "connection_epoch", 1, 2**31 - 1),
            identifier(value["source_id"], "source_id"),
            node_type,
            capture,
            frame,
            confidence,
            payload,
        )
        bounded_json(result.to_dict())
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "v": 1,
            "type": "observation",
            "t": self.t,
            "event_id": self.event_id,
            "session": self.session,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "source_id": self.source_id,
            "node_type": self.node_type.value,
            "t_capture": self.t_capture,
            "frame": self.frame,
            "confidence": self.confidence,
            "payload": self.payload.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Observation:
    submission: ObservationSubmission
    t_ingest: int
    declaration: FrameDeclaration

    def __post_init__(self) -> None:
        integer(self.t_ingest, "t_ingest")
        if not isinstance(self.submission, ObservationSubmission) or not isinstance(
            self.declaration, FrameDeclaration
        ):
            raise ObservationError(
                "invalid_observation", "observation requires typed submission and frame"
            )
        if self.submission.frame != self.declaration.frame_id:
            raise ObservationError("frame_mismatch", "observation uses another declared frame")
        legacy = isinstance(self.submission.payload, LegacyAircraftTelemetryPayload)
        if legacy != (self.declaration.kind is FrameKind.LEGACY_AIRCRAFT):
            raise ObservationError("frame_mismatch", "legacy frames only describe legacy telemetry")
        bounded_json(self.to_dict())

    @classmethod
    def parse(cls, raw: object) -> Observation:
        value = exact(raw, SUBMISSION_FIELDS | RELAY_FIELDS, "accepted observation")
        if value["authority"] != "diagnostic":
            raise ObservationError(
                "invalid_observation", "observations cannot grant motion authority"
            )
        submission = ObservationSubmission.parse(
            {key: value[key] for key in SUBMISSION_FIELDS}, allow_legacy=True
        )
        provenance = value["frame_provenance"]
        if not isinstance(provenance, Mapping):
            raise ObservationError("invalid_frame", "frame provenance must be an object")
        declaration = FrameDeclaration.parse(
            {"id": submission.frame, "kind": provenance.get("kind")}
        )
        if dict(provenance) != declaration.provenance(
            submission.drone_id, submission.connection_epoch
        ):
            raise ObservationError("invalid_frame", "frame provenance differs from its declaration")
        return cls(submission, integer(value["t_ingest"], "t_ingest"), declaration)

    def to_dict(self) -> dict[str, object]:
        return self.submission.to_dict() | {
            "t_ingest": self.t_ingest,
            "frame_provenance": self.declaration.provenance(
                self.submission.drone_id, self.submission.connection_epoch
            ),
            "authority": "diagnostic",
        }


@dataclass(frozen=True, slots=True)
class ObservationSource:
    source_id: str
    principal_source: str
    drone_id: int
    node_type: NodeType
    frames: tuple[FrameDeclaration, ...]
    payload_types: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.source_id, "source_id")
        if self.source_id.startswith("legacy."):
            raise ObservationError(
                "invalid_observation_source", "legacy source IDs are relay reserved"
            )
        if type(self.principal_source) is not str or self.principal_source not in {
            "adapter",
            "localization",
        }:
            raise ObservationError(
                "invalid_observation_source", "observation producer must be device bound"
            )
        integer(self.drone_id, "drone_id", 1, 2**31 - 1)
        if not isinstance(self.node_type, NodeType):
            raise ObservationError("invalid_observation_source", "node_type must be explicit")
        if (
            type(self.frames) is not tuple
            or not 1 <= len(self.frames) <= MAX_SOURCE_FRAMES
            or any(not isinstance(item, FrameDeclaration) for item in self.frames)
        ):
            raise ObservationError(
                "invalid_observation_source", "source frames must be bounded declarations"
            )
        if len({item.frame_id for item in self.frames}) != len(self.frames) or any(
            item.kind is FrameKind.LEGACY_AIRCRAFT for item in self.frames
        ):
            raise ObservationError(
                "invalid_observation_source", "source frames must be unique and non-legacy"
            )
        if (
            type(self.payload_types) is not tuple
            or not 1 <= len(self.payload_types) <= len(PAYLOAD_MAX_HZ)
            or any(type(item) is not str for item in self.payload_types)
            or len(set(self.payload_types)) != len(self.payload_types)
            or any(item not in PAYLOAD_MAX_HZ for item in self.payload_types)
        ):
            raise ObservationError(
                "invalid_observation_source", "source payload types must be supported and unique"
            )

    @classmethod
    def parse(cls, source_id: str, raw: object) -> ObservationSource:
        value = exact(
            raw,
            {"principal_source", "drone_id", "node_type", "frames", "payload_types"},
            "observation source",
        )
        if (
            type(value["frames"]) is not list
            or len(value["frames"]) > MAX_SOURCE_FRAMES
            or type(value["payload_types"]) is not list
            or len(value["payload_types"]) > len(PAYLOAD_MAX_HZ)
        ):
            raise ObservationError(
                "invalid_observation_source", "frames and payload types must be bounded arrays"
            )
        try:
            node_type = NodeType(value["node_type"])
        except (ValueError, TypeError):
            raise ObservationError("invalid_observation_source", "unknown node_type") from None
        return cls(
            source_id,
            value["principal_source"],
            value["drone_id"],
            node_type,
            tuple(FrameDeclaration.parse(item) for item in value["frames"]),
            tuple(value["payload_types"]),
        )  # type: ignore[arg-type]


def source_registry(sources: Mapping[str, ObservationSource]) -> Mapping[str, ObservationSource]:
    if not isinstance(sources, Mapping) or len(sources) > MAX_OBSERVATION_SOURCES:
        raise ObservationError(
            "invalid_observation_source", "at most 64 observation sources are allowed"
        )
    result = dict(sources)
    declarations: dict[str, FrameDeclaration] = {}
    local_owners: dict[str, int] = {}
    node_types: dict[int, NodeType] = {}
    for source_id, source in result.items():
        if not isinstance(source, ObservationSource) or source_id != source.source_id:
            raise ObservationError(
                "invalid_observation_source", "source IDs must match their declarations"
            )
        if node_types.setdefault(source.drone_id, source.node_type) is not source.node_type:
            raise ObservationError(
                "invalid_observation_source", "one device has conflicting node types"
            )
        for declaration in source.frames:
            prior = declarations.setdefault(declaration.frame_id, declaration)
            if prior != declaration:
                raise ObservationError("invalid_frame", "a frame ID has conflicting declarations")
            if declaration.kind not in {FrameKind.WORLD, FrameKind.MAP}:
                owner = local_owners.setdefault(declaration.frame_id, source.drone_id)
                if owner != source.drone_id:
                    raise ObservationError(
                        "invalid_frame", "device-local frame has multiple owners"
                    )
    return MappingProxyType(result)


def bounded_json(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise ObservationError(
            "invalid_observation", "observation must be canonical JSON"
        ) from None
    if len(encoded) > MAX_OBSERVATION_BYTES:
        raise ObservationError("observation_too_large", "observation exceeds 16 KiB")
    return encoded

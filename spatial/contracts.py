"""Relay-neutral spatial vocabulary and explicit frame declarations for #94."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

MAX_IDENTIFIER_BYTES = 128
MAX_TIMESTAMP = 2**53 - 1
MAX_COORDINATE_M = 1_000.0


class ObservationError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


class NodeType(StrEnum):
    AIRCRAFT = "aircraft"
    GROUND_VEHICLE = "ground_vehicle"


class FrameKind(StrEnum):
    WORLD = "world"
    MAP = "map"
    DEVICE_BODY = "device_body"
    DEVICE_ODOMETRY = "device_odometry"
    CAMERA_OPTICAL = "camera_optical"
    LIDAR_SENSOR = "lidar_sensor"
    LEGACY_AIRCRAFT = "legacy_aircraft"


_AXES = {
    FrameKind.WORLD: "right_handed_z_up",
    FrameKind.MAP: "right_handed_z_up",
    FrameKind.DEVICE_BODY: "x_forward_y_left_z_up",
    FrameKind.DEVICE_ODOMETRY: "right_handed_z_up",
    FrameKind.CAMERA_OPTICAL: "x_right_y_down_z_forward",
    FrameKind.LIDAR_SENSOR: "unqualified_sensor_scan_angles",
    FrameKind.LEGACY_AIRCRAFT: "unqualified_legacy_planner_axes",
}
_LOCAL_KINDS = frozenset(
    {
        FrameKind.DEVICE_BODY,
        FrameKind.DEVICE_ODOMETRY,
        FrameKind.CAMERA_OPTICAL,
        FrameKind.LIDAR_SENSOR,
        FrameKind.LEGACY_AIRCRAFT,
    }
)


def exact(raw: object, fields: set[str] | frozenset[str], name: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise ObservationError("invalid_observation", f"{name} fields do not match the contract")
    return raw


def identifier(value: object, name: str, maximum: int = MAX_IDENTIFIER_BYTES) -> str:
    try:
        encoded_size = len(value.encode("utf-8")) if type(value) is str else maximum + 1
    except UnicodeError:
        encoded_size = maximum + 1
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or encoded_size > maximum
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ObservationError("invalid_observation", f"{name} must be bounded printable text")
    return value


def integer(value: object, name: str, minimum: int = 0, maximum: int = MAX_TIMESTAMP) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ObservationError("invalid_observation", f"{name} is outside its integer bounds")
    return value


def number(value: object, name: str, minimum: float, maximum: float) -> float:
    if type(value) not in {int, float} or not minimum <= value <= maximum or not isfinite(value):
        raise ObservationError("invalid_observation", f"{name} must be finite and bounded")
    return float(value)


@dataclass(frozen=True, slots=True)
class FrameDeclaration:
    """Host-owned declaration, not a transform or an assertion of registration."""

    frame_id: str
    kind: FrameKind

    def __post_init__(self) -> None:
        identifier(self.frame_id, "frame.id")
        if not isinstance(self.kind, FrameKind):
            raise ObservationError("invalid_frame", "frame.kind must be a supported frame kind")
        if (self.frame_id == "world") != (self.kind is FrameKind.WORLD):
            raise ObservationError("invalid_frame", "only the canonical world ID has world kind")
        if self.frame_id in {"map_enu", "building"} and self.kind is not FrameKind.MAP:
            raise ObservationError("invalid_frame", "legacy map IDs must retain map kind")

    @classmethod
    def parse(cls, raw: object) -> FrameDeclaration:
        value = exact(raw, {"id", "kind"}, "frame declaration")
        try:
            kind = FrameKind(value["kind"])
        except (ValueError, TypeError):
            raise ObservationError("invalid_frame", "unknown frame kind") from None
        return cls(identifier(value["id"], "frame.id"), kind)

    def to_dict(self) -> dict[str, str]:
        return {"id": self.frame_id, "kind": self.kind.value}

    def provenance(self, drone_id: int, epoch: int) -> dict[str, object]:
        local = self.kind in _LOCAL_KINDS
        return {
            "kind": self.kind.value,
            "units": "m",
            "axes": _AXES[self.kind],
            "origin_drone_id": drone_id if local else None,
            "origin_connection_epoch": epoch if local else None,
            "transform_id": None,
        }


@dataclass(frozen=True, slots=True)
class Position:
    frame: str
    x_m: float
    y_m: float
    z_m: float

    def __post_init__(self) -> None:
        identifier(self.frame, "position.frame")
        for field in ("x_m", "y_m", "z_m"):
            object.__setattr__(
                self,
                field,
                number(getattr(self, field), field, -MAX_COORDINATE_M, MAX_COORDINATE_M),
            )

    @classmethod
    def parse(cls, raw: object) -> Position:
        value = exact(raw, {"frame", "x_m", "y_m", "z_m"}, "position")
        return cls(value["frame"], value["x_m"], value["y_m"], value["z_m"])  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        return {"frame": self.frame, "x_m": self.x_m, "y_m": self.y_m, "z_m": self.z_m}

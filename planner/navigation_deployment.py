from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from planner.navigation import (
    ArrivalSlot,
    MotionConfig,
    NavigationArtifact,
    NavigationPermission,
    Pose,
)
from planner.navigation_authorization import NavigationApproval, content_digest
from planner.navigation_runtime import (
    MAX_AIRCRAFT,
    NavigationExecutionConfig,
    NavigationFrame,
    NavigationRuntime,
    navigation_configuration_digest,
)
from relay.control_localization import (
    ClockMapping,
    ControlLocalizationPins,
    ControlLocalizationProjector,
    ControlPose,
)

if TYPE_CHECKING:
    from relay.navigation_wire import NavigationWireConfig


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"duplicate navigation field: {name}")
        result[name] = value
    return result


def _read_bytes(path: Path, name: str) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1_000_000:
            raise ValueError(f"{name} must be a regular file of at most one megabyte")
        chunks = []
        remaining = 1_000_001
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(payload) > 1_000_000:
        raise ValueError(f"{name} exceeds one megabyte")
    return payload


def read_document(path: Path) -> dict[str, object]:
    payload = _read_bytes(path, "navigation document")
    value = json.loads(payload, object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise ValueError("navigation document must be a JSON object")
    return value


def _fields(value: object, fields: set[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} fields must be exactly {sorted(fields)}")
    return value


@dataclass(frozen=True, slots=True)
class NavigationDeployment:
    path: Path
    config: NavigationExecutionConfig
    permission: NavigationPermission
    home_zone_id: str
    approval: NavigationApproval
    artifact: Callable[[], NavigationArtifact]
    wire_profiles: Mapping[int, NavigationWireConfig] = field(
        default_factory=lambda: MappingProxyType({})
    )
    _validate_flight: Callable[[], None] | None = field(default=None, repr=False)

    def validate_projector(self, projector: ControlLocalizationProjector | None) -> None:
        if self.approval.mode != "flight":
            return
        if projector is None:
            raise ValueError("flight navigation requires an active localization projector")
        expected = {frame.drone_id: frame.control_pins for frame in self.config.frames}
        if any(pin is None for pin in expected.values()) or projector.pins != expected:
            raise ValueError("navigation source pins differ from the active localization projector")
        assert self._validate_flight is not None
        self._validate_flight()

    def for_session(
        self,
        session: str,
        control_pose: Callable[[int], ControlPose | None],
        projector: ControlLocalizationProjector | None,
    ) -> NavigationRuntime:
        self.validate_projector(projector)
        return NavigationRuntime(
            self.artifact,
            self.config,
            self.permission,
            self.approval,
            session=session,
            home_zone_id=self.home_zone_id,
            control_pose=control_pose,
        )


_WIRE_LIMIT_FIELDS = frozenset(
    {
        "max_speed_mm_s",
        "max_acceleration_mm_s2",
        "max_deceleration_mm_s2",
        "max_position_uncertainty_mm",
        "max_cross_track_mm",
        "arrival_horizontal_tolerance_mm",
        "arrival_vertical_tolerance_mm",
        "pose_freshness_ms",
        "tracking_timeout_ms",
    }
)


def _device_mapping(value: object, name: str) -> dict[int, object]:
    if not isinstance(value, dict) or not 1 <= len(value) <= MAX_AIRCRAFT:
        raise ValueError(f"{name} must map one through {MAX_AIRCRAFT} device IDs")
    result: dict[int, object] = {}
    for raw_id, item in value.items():
        if type(raw_id) is not str or not raw_id.isdecimal() or str(int(raw_id)) != raw_id:
            raise ValueError(f"{name} keys must be canonical positive device IDs")
        device_id = int(raw_id)
        if device_id < 1:
            raise ValueError(f"{name} keys must be canonical positive device IDs")
        result[device_id] = item
    return result


def _wire_profiles(value: object, frame_ids: set[int]) -> Mapping[int, NavigationWireConfig]:
    from relay.navigation_wire import NavigationWireConfig

    entries = _device_mapping(value, "wire_profiles")
    if set(entries) != frame_ids:
        raise ValueError("wire profiles must match every configured aircraft")
    result: dict[int, NavigationWireConfig] = {}
    fields = set(NavigationWireConfig.__dataclass_fields__)
    for device_id, item in entries.items():
        profile = dict(_fields(item, fields, "wire profile"))
        sources = profile["control_source_ids"]
        if not isinstance(sources, list):
            raise ValueError("wire profile control sources must be a list")
        profile["control_source_ids"] = tuple(sources)
        result[device_id] = NavigationWireConfig(**profile)
    return MappingProxyType(result)


def _validate_wire_tuning(
    value: object,
    root: Path,
    profiles: Mapping[int, NavigationWireConfig],
) -> None:
    entries = _device_mapping(value, "wire_navigation_files")
    if set(entries) != set(profiles):
        raise ValueError("wire navigation files must match every configured aircraft")
    for device_id, raw_path in entries.items():
        if type(raw_path) is not str or not raw_path or raw_path != raw_path.strip():
            raise ValueError("wire navigation file must be a bounded relative path")
        path = Path(raw_path)
        encoded = _read_bytes(path if path.is_absolute() else root / path, "wire navigation tuning")
        try:
            raw = json.loads(encoded, object_pairs_hook=_unique)
        except json.JSONDecodeError as error:
            raise ValueError("wire navigation tuning must be valid JSON") from error
        value = _fields(
            raw,
            {"v", "navigation_config_id", "device_id", "limits"},
            "wire navigation tuning",
        )
        profile = profiles[device_id]
        if (
            type(value["v"]) is not int
            or value["v"] != 1
            or value["navigation_config_id"] != profile.navigation_config_id
            or type(value["device_id"]) is not int
            or value["device_id"] != device_id
            or hashlib.sha256(encoded).hexdigest() != profile.navigation_config_sha256
        ):
            raise ValueError("wire navigation tuning does not match its approved profile")
        limits = _fields(value["limits"], set(_WIRE_LIMIT_FIELDS), "wire navigation limits")
        if any(
            type(limits[name]) is not int or limits[name] != getattr(profile, name)
            for name in _WIRE_LIMIT_FIELDS
        ):
            raise ValueError("wire navigation limits differ from the approved profile")


def _validate_world_localization(
    path: Path,
    config: NavigationExecutionConfig,
    approval: NavigationApproval,
    profiles: Mapping[int, NavigationWireConfig],
) -> None:
    from perception.world_localization_runtime import WorldLocalizationRuntimeConfig

    world = WorldLocalizationRuntimeConfig.load(path)
    frame_ids = {frame.drone_id for frame in config.frames}
    if world.publisher.session != approval.session or set(world.adapters) != frame_ids:
        raise ValueError("world localization deployment does not match navigation approval")
    for frame in config.frames:
        adapter = world.adapters[frame.drone_id]
        pins = adapter.pins
        publisher = world.publisher.drones[frame.drone_id]
        source_ids = tuple(
            sorted(
                {
                    publisher.fuser.tag_source_id,
                    publisher.fuser.velocity_source_id,
                    publisher.fuser.height_source_id,
                }
            )
        )
        control = frame.control_pins
        profile = profiles[frame.drone_id]
        if (
            control is None
            or frame.transform_id != pins.world_enu.transform_id
            or frame.world_from_enu != pins.world_enu.matrix_world_enu
            or frame.camera_calibration_sha256 != pins.camera_calibration_sha256
            or frame.body_extrinsics_sha256 != pins.capture_alignment_config_sha256
            or frame.world_transform_sha256 != pins.world_enu.sha256
            or control.map_id != pins.map_id
            or control.geometry_id != pins.geometry_id
            or control.camera_calibration_id != pins.camera_calibration_id
            or control.body_extrinsics_id != pins.body_extrinsics_id
            or control.source_ids != source_ids
            or control.clock_mapping != publisher.clock_mapping
            or profile.map_version != pins.map_version
            or profile.map_sha256 != pins.map_content_sha256
            or profile.geometry_sha256 != pins.geometry_sha256
            or profile.camera_calibration_sha256 != pins.camera_calibration_sha256
            or profile.body_extrinsics_sha256 != pins.capture_alignment_config_sha256
            or profile.world_transform_sha256 != pins.world_enu.sha256
            or profile.control_source_ids != source_ids
        ):
            raise ValueError("navigation deployment does not bind world localization evidence")


def load_navigation_deployment(path: str | Path) -> NavigationDeployment:
    path = Path(path).resolve()
    raw = read_document(path)
    base_fields = {
        "schema_version",
        "bundle_directory",
        "geometry_directory",
        "geometry_authoring",
        "accepted_map_versions",
        "arrival_slots",
        "permission_zone_ids",
        "home_zone_id",
        "execution",
        "approval_file",
        "approval_key_file",
    }
    flight_fields = base_fields | {
        "world_localization_file",
        "wire_profiles",
        "wire_navigation_files",
    }
    if not isinstance(raw, dict) or (set(raw) != base_fields and set(raw) != flight_fields):
        raise ValueError("navigation deployment fields do not match the contract")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("navigation deployment schema_version must be 1")
    digest = content_digest(raw)

    def local(name: str) -> Path:
        value = raw[name]
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{name} must name a file or directory")
        return (path.parent / value).resolve()

    slots_raw = raw["arrival_slots"]
    if not isinstance(slots_raw, list) or not 1 <= len(slots_raw) <= 16:
        raise ValueError("navigation deployment needs a bounded arrival slot list")
    slots = []
    for value in slots_raw:
        _fields(value, {"slot_id", "zone_id", "pose", "radius_m", "half_height_m"}, "arrival slot")
        pose = _fields(value["pose"], {"x_m", "y_m", "z_m", "floor_id"}, "arrival pose")
        slots.append(
            ArrivalSlot(
                value["slot_id"],
                value["zone_id"],
                Pose(**pose),
                value["radius_m"],
                value["half_height_m"],
            )
        )
    permission_raw = raw["permission_zone_ids"]
    if (
        not isinstance(permission_raw, list)
        or any(not isinstance(item, str) for item in permission_raw)
        or len(set(permission_raw)) != len(permission_raw)
    ):
        raise ValueError("permission_zone_ids must be a unique string list")
    permission = NavigationPermission(frozenset(permission_raw))
    if raw["home_zone_id"] not in permission.permitted_zone_ids:
        raise ValueError("home zone must have explicit arrival permission")
    execution_fields = set(NavigationExecutionConfig.__dataclass_fields__)
    execution_raw = raw["execution"]
    if isinstance(execution_raw, dict) and set(execution_raw) == execution_fields - {
        "max_aircraft"
    }:
        execution = {**execution_raw, "max_aircraft": 4}
    else:
        execution = dict(_fields(execution_raw, execution_fields, "navigation execution"))
    execution["motion"] = MotionConfig(
        **_fields(execution["motion"], set(MotionConfig.__dataclass_fields__), "navigation motion")
    )
    frames_raw = execution["frames"]
    if not isinstance(frames_raw, list):
        raise ValueError("navigation frames must be a list")
    frames = []
    for value in frames_raw:
        frame = dict(_fields(value, set(NavigationFrame.__dataclass_fields__), "navigation frame"))
        matrix = frame["world_from_enu"]
        if not isinstance(matrix, list) or any(not isinstance(row, list) for row in matrix):
            raise ValueError("world_from_enu must be a 4x4 matrix")
        frame["world_from_enu"] = tuple(tuple(row) for row in matrix)
        if frame["control_pins"] is not None:
            pin = dict(
                _fields(
                    frame["control_pins"],
                    set(ControlLocalizationPins.__dataclass_fields__),
                    "control pins",
                )
            )
            pin["clock_mapping"] = ClockMapping.from_mapping(pin["clock_mapping"])
            frame["control_pins"] = ControlLocalizationPins(**pin)
        frames.append(NavigationFrame(**frame))
    execution["frames"] = tuple(frames)
    config = NavigationExecutionConfig(**execution)
    approval_path = local("approval_file")
    approval_raw = read_document(approval_path)
    key_path = local("approval_key_file")
    if key_path.stat().st_mode & 0o077:
        raise ValueError("navigation approval key must have mode 0600")
    with key_path.open("rb") as stream:
        key = stream.read(4097)
    if not 32 <= len(key) <= 4096:
        raise ValueError("navigation approval key must contain 32 through 4096 raw bytes")
    approval = NavigationApproval.verify(approval_raw, key)
    if approval.mode == "flight":
        _fields(raw, flight_fields, "flight navigation deployment")
    else:
        _fields(raw, base_fields, "simulation navigation deployment")
    bundle, geometry = local("bundle_directory"), local("geometry_directory")
    authoring = None if raw["geometry_authoring"] is None else local("geometry_authoring")
    if approval.mode == "flight" and authoring is None:
        raise ValueError("flight navigation needs measured geometry authoring")

    def artifact() -> NavigationArtifact:
        if (
            content_digest(read_document(path)) != digest
            or read_document(approval_path) != approval_raw
        ):
            raise ValueError("navigation deployment or approval changed; load a new configuration")
        loaded = NavigationArtifact.from_geometry_directory(
            bundle, geometry, raw["accepted_map_versions"], tuple(slots), authoring=authoring
        )
        return replace(
            loaded,
            zones=tuple(
                replace(zone, owner_approved=zone.zone_id in permission.permitted_zone_ids)
                for zone in loaded.zones
            ),
        )

    loaded = artifact()
    if (
        navigation_configuration_digest(loaded, config, permission, raw["home_zone_id"])
        != approval.configuration_sha256
    ):
        raise ValueError("navigation approval does not bind this deployment configuration")
    if approval.mode != "flight":
        return NavigationDeployment(
            path, config, permission, raw["home_zone_id"], approval, artifact
        )

    frame_ids = {frame.drone_id for frame in config.frames}
    profiles = _wire_profiles(raw["wire_profiles"], frame_ids)
    if config.wire_config_sha256 != content_digest(
        {str(device_id): asdict(profiles[device_id]) for device_id in sorted(profiles)}
    ):
        raise ValueError("navigation execution does not bind approved wire profiles")
    if any(
        loaded.map_pin.version != profile.map_version
        or loaded.map_pin.content_sha256 != profile.map_sha256
        or loaded.geometry_pin.content_sha256 != profile.geometry_sha256
        for profile in profiles.values()
    ):
        raise ValueError("wire profiles do not bind the approved navigation artifacts")
    world_path = local("world_localization_file")

    def validate_flight() -> None:
        _validate_wire_tuning(raw["wire_navigation_files"], path.parent, profiles)
        _validate_world_localization(world_path, config, approval, profiles)

    def flight_artifact() -> NavigationArtifact:
        validate_flight()
        return artifact()

    validate_flight()
    return NavigationDeployment(
        path,
        config,
        permission,
        raw["home_zone_id"],
        approval,
        flight_artifact,
        profiles,
        validate_flight,
    )

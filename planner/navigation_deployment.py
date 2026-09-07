from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from planner.navigation import (
    ArrivalSlot,
    MotionConfig,
    NavigationArtifact,
    NavigationPermission,
    Pose,
)
from planner.navigation_authorization import NavigationApproval, content_digest
from planner.navigation_runtime import (
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


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"duplicate navigation field: {name}")
        result[name] = value
    return result


def read_document(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        payload = stream.read(1_000_001)
    if len(payload) > 1_000_000:
        raise ValueError("navigation document exceeds one megabyte")
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

    def for_session(
        self,
        session: str,
        control_pose: Callable[[int], ControlPose | None],
        projector: ControlLocalizationProjector | None,
    ) -> NavigationRuntime:
        if self.approval.mode == "flight" and (
            projector is None
            or any(
                projector.pins.get(frame.drone_id) != frame.control_pins
                for frame in self.config.frames
            )
        ):
            raise ValueError("navigation source pins differ from the active localization projector")
        return NavigationRuntime(
            self.artifact,
            self.config,
            self.permission,
            self.approval,
            session=session,
            home_zone_id=self.home_zone_id,
            control_pose=control_pose,
        )


def load_navigation_deployment(path: str | Path) -> NavigationDeployment:
    path = Path(path).resolve()
    raw = read_document(path)
    _fields(
        raw,
        {
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
        },
        "navigation deployment",
    )
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
    execution = dict(
        _fields(
            raw["execution"],
            set(NavigationExecutionConfig.__dataclass_fields__),
            "navigation execution",
        )
    )
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
    return NavigationDeployment(path, config, permission, raw["home_zone_id"], approval, artifact)

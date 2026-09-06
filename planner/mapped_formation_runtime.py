"""Runtime adapter for explicitly configured mapped formations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from planner.mapped_formations import (
    FormationLayout,
    FormationPermission,
    FormationRefusal,
    FormationSlot,
    FormationZone,
    MappedFormationPlanner,
    MappedFormationRequest,
    SlotAssignment,
    _formation_artifact,
    _slots,
)
from planner.models import Command, FleetSnapshot, Plan, Refusal, RefusalReason
from planner.navigation import NavigationArtifact, NavigationPermission, Pose
from planner.navigation_runtime import NavigationExecutionConfig, NavigationRuntime
from relay.intent_v1 import IntentName, IntentV1


@dataclass(frozen=True, slots=True)
class ConfiguredFormation:
    shape: str
    zone: FormationZone
    layout: FormationLayout


@dataclass(frozen=True, slots=True)
class MappedFormationRuntimeConfig:
    formations: dict[str, ConfiguredFormation]
    navigation: NavigationExecutionConfig

    def __post_init__(self) -> None:
        if not self.formations:
            raise ValueError("mapped formations require explicit configured layouts")
        if any(
            not name or formation.shape not in {"line", "column", "wedge", "diamond"}
            for name, formation in self.formations.items()
        ):
            raise ValueError("formation configuration is invalid")

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], navigation: NavigationExecutionConfig
    ) -> MappedFormationRuntimeConfig:
        if set(value) != {"formations"} or not isinstance(value["formations"], Mapping):
            raise ValueError("formation config must contain only formations")
        formations = {}
        for name, raw in value["formations"].items():
            if (
                not isinstance(name, str)
                or not isinstance(raw, Mapping)
                or set(raw) != {"shape", "zone", "layout"}
            ):
                raise ValueError("formation entry is invalid")
            shape, zone_raw, layout_raw = raw["shape"], raw["zone"], raw["layout"]
            if (
                not isinstance(shape, str)
                or not isinstance(zone_raw, Mapping)
                or not isinstance(layout_raw, Mapping)
            ):
                raise ValueError("formation entry must contain shape, zone, and layout")
            if set(zone_raw) != {
                "zone_id",
                "floor_id",
                "polygon_xy",
                "z_min_m",
                "z_max_m",
                "owner_approved",
                "formation_enabled",
            }:
                raise ValueError("formation zone fields are invalid")
            if set(layout_raw) != {"center", "heading_rad", "spacing_m", "altitude_offsets_m"}:
                raise ValueError("formation layout fields are invalid")
            center = layout_raw["center"]
            polygon_raw = zone_raw["polygon_xy"]
            offsets = layout_raw["altitude_offsets_m"]
            if (
                not isinstance(center, Mapping)
                or set(center) != {"x_m", "y_m", "z_m", "floor_id"}
                or not isinstance(polygon_raw, list)
                or not isinstance(offsets, list)
            ):
                raise ValueError("formation geometry fields are invalid")
            if len(offsets) not in {2, 4} or (shape in {"wedge", "diamond"} and len(offsets) != 4):
                raise ValueError("formation shape and aircraft count are incompatible")
            zone = FormationZone(
                zone_raw["zone_id"],
                zone_raw["floor_id"],
                tuple(tuple(point) for point in polygon_raw),
                zone_raw["z_min_m"],
                zone_raw["z_max_m"],
                zone_raw["owner_approved"],
                zone_raw["formation_enabled"],
            )
            layout = FormationLayout(
                Pose(center["x_m"], center["y_m"], center["z_m"], center["floor_id"]),
                layout_raw["heading_rad"],
                layout_raw["spacing_m"],
                tuple(offsets),
            )
            formations[name] = ConfiguredFormation(shape, zone, layout)
        return cls(formations, navigation)


class MappedFormationRuntime:
    def __init__(
        self,
        artifact: Callable[[], NavigationArtifact],
        config: MappedFormationRuntimeConfig,
        permission: FormationPermission,
    ) -> None:
        self.artifact = artifact
        self.config = config
        self.permission = permission
        self.planner = MappedFormationPlanner()
        self.navigation = NavigationRuntime(
            artifact, config.navigation, NavigationPermission(frozenset())
        )

    def prepare(self, intent: IntentV1, snapshot: FleetSnapshot) -> Plan | Refusal:
        if intent.name is not IntentName.FORMATION_SET:
            return self._refuse(
                intent, snapshot, "mapped formation runtime only accepts formation_set"
            )
        name = intent.args.get("name")
        formation = self.config.formations.get(name) if isinstance(name, str) else None
        if formation is None:
            return self._refuse(intent, snapshot, "formation layout is not explicitly configured")
        try:
            positions = self.navigation._positions(snapshot)
            selected = tuple(item for item in positions if item.drone_id in intent.selection)
            request = MappedFormationRequest(
                formation.shape,
                snapshot.roster_version,
                selected,
                positions,
                frozenset(
                    drone_id
                    for drone_id, aircraft in snapshot.aircraft.items()
                    if aircraft.flight_state.value in {"airborne", "hovering"}
                ),
                self.config.navigation.motion,
                self.permission,
                formation.layout,
            )
            result = self.planner.plan(request, self.artifact(), formation.zone)
        except (KeyError, ValueError) as error:
            return self._refuse(intent, snapshot, str(error))
        if isinstance(result, FormationRefusal):
            return self._refuse(intent, snapshot, f"{result.code}: {result.detail}")
        return replace(
            self.navigation.prepare_route(intent, snapshot, result.navigation_plan),
            formation_update=name,
        )

    def check(
        self,
        plan: Plan,
        command: Command,
        snapshot: FleetSnapshot,
        *,
        completed: bool = False,
        issued_at_ms: int | None = None,
    ) -> Refusal | None:
        execution = plan.navigation
        if execution is None or not execution.route.destination_zone_id.startswith("formation:"):
            return self._refuse_from_plan(plan, snapshot, "formation route is missing")
        name = plan.formation_update
        formation = self.config.formations.get(name) if name is not None else None
        destination = f"formation:{formation.zone.zone_id}" if formation is not None else None
        if (
            formation is None
            or destination != execution.route.destination_zone_id
            or formation.zone.zone_id not in self.permission.permitted_zone_ids
            or not formation.zone.owner_approved
            or not formation.zone.formation_enabled
        ):
            return self._refuse_from_plan(
                plan, snapshot, "formation configuration or permission changed"
            )
        slots = _slots(formation.shape, formation.layout)
        if tuple(slot.pose for slot in slots) != tuple(
            slot.pose for slot in execution.route.arrival_slots
        ):
            return self._refuse_from_plan(plan, snapshot, "formation layout changed")
        artifact = self.artifact()
        if self.config.navigation.motion.swept_radius_m > artifact.grid_clearance_m + 1e-9:
            return self._refuse_from_plan(plan, snapshot, "formation motion clearance changed")
        slot_refusal = self.planner._validate_slots(
            slots, self.config.navigation.motion, artifact, formation.zone
        )
        if slot_refusal is not None:
            return self._refuse_from_plan(
                plan, snapshot, f"{slot_refusal.code}: {slot_refusal.detail}"
            )
        assignments = tuple(
            SlotAssignment(
                route.drone, FormationSlot(route.arrival_slot.slot_id, route.arrival_slot.pose), 0.0
            )
            for route in execution.route.routes
        )
        checker = NavigationRuntime(
            lambda: _formation_artifact(
                artifact, formation.zone, assignments, self.config.navigation.motion
            ),
            self.config.navigation,
            NavigationPermission(frozenset({destination})),
        )
        checker.control_pins = self.navigation.control_pins
        checker.maximum_aircraft = self.navigation.maximum_aircraft
        return checker.check(
            plan, command, snapshot, completed=completed, issued_at_ms=issued_at_ms
        )

    def _refuse(self, intent: IntentV1, snapshot: FleetSnapshot, detail: str) -> Refusal:
        return Refusal(
            intent.intent_id, snapshot.roster_version, None, None, RefusalReason.UNSUPPORTED, detail
        )

    def _refuse_from_plan(self, plan: Plan, snapshot: FleetSnapshot, detail: str) -> Refusal:
        return Refusal(
            plan.intent_id, snapshot.roster_version, None, None, RefusalReason.UNSUPPORTED, detail
        )

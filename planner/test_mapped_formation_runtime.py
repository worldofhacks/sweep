from dataclasses import replace

import pytest

from planner.mapped_formation_runtime import (
    ConfiguredFormation,
    MappedFormationRuntime,
    MappedFormationRuntimeConfig,
)
from planner.mapped_formations import FormationLayout, FormationPermission
from planner.models import LifecycleStatus, Plan, Position
from planner.navigation import NavigationDispatchAcceptance
from planner.navigation_runtime import NavigationExecutionConfig
from planner.test_mapped_formations import MOTION, ZONE, artifact, pose
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import (
    make_intent,
    make_snapshot,
    make_stack,
    replace_aircraft,
)


def runtime(current):
    config = MappedFormationRuntimeConfig(
        {"line": ConfiguredFormation("line", ZONE, FormationLayout(pose(10, 10), 0, 2, (0, 0)))},
        NavigationExecutionConfig("level_1", MOTION, 0.5, 0.05, 500, 0.5, 5_000),
    )
    mapped = MappedFormationRuntime(
        lambda: current[0], config, FormationPermission(frozenset({"lobby"}))
    )
    mapped.navigation.dispatch_acceptance = lambda plan, artifact: NavigationDispatchAcceptance(
        "test-formation-acceptance",
        plan.map_pin,
        plan.geometry_pin,
        plan.navigation_pin,
        plan.plan_revision,
    )
    return mapped


def test_runtime_freezes_mapped_assignments_and_refuses_revoked_permission():
    snapshot = make_snapshot(2)
    snapshot = replace_aircraft(snapshot, 1, pose=replace(snapshot.aircraft[1].pose, x=8, y=8, z=1.5))
    snapshot = replace_aircraft(snapshot, 2, pose=replace(snapshot.aircraft[2].pose, x=12, y=8, z=1.5))
    current = [artifact()]
    mapped = runtime(current)
    intent = make_intent(
        IntentName.FORMATION_SET, selection=(1, 2), args={"name": "line"}, confirm=True
    )
    plan = mapped.prepare(intent, snapshot)
    assert hasattr(plan, "navigation")
    assert plan.navigation.route.destination_zone_id == "formation:lobby"
    mapped.permission = FormationPermission(frozenset())
    assert mapped.check(plan, plan.commands[0], snapshot) is not None


def test_config_parser_rejects_extra_configuration():
    config = runtime([artifact()]).config.navigation
    try:
        MappedFormationRuntimeConfig.from_mapping({"formations": {}, "fallback": "kitchen"}, config)
    except ValueError:
        pass
    else:
        raise AssertionError("parser accepted an undeclared fallback")


@pytest.mark.parametrize("shape,count", (("line", 2), ("column", 2), ("wedge", 4), ("diamond", 4)))
def test_raw_json_parser_accepts_explicit_supported_layouts(shape, count):
    config = runtime([artifact()]).config.navigation
    raw = {
        "formations": {
            "configured": {
                "shape": shape,
                "zone": {
                    "zone_id": "lobby",
                    "floor_id": "level_1",
                    "polygon_xy": [[1, 1], [19, 1], [19, 19], [1, 19], [1, 1]],
                    "z_min_m": 0.5,
                    "z_max_m": 3.5,
                    "owner_approved": True,
                    "formation_enabled": True,
                },
                "layout": {
                    "center": {"x_m": 10, "y_m": 10, "z_m": 1.5, "floor_id": "level_1"},
                    "heading_rad": 0,
                    "spacing_m": 2,
                    "altitude_offsets_m": [0] * count,
                },
            }
        }
    }
    parsed = MappedFormationRuntimeConfig.from_mapping(raw, config)
    assert parsed.formations["configured"].shape == shape


def test_runtime_binds_formation_name_and_revalidates_configuration():
    snapshot = make_snapshot(2)
    snapshot = replace_aircraft(snapshot, 1, pose=replace(snapshot.aircraft[1].pose, x=8, y=8, z=1.5))
    snapshot = replace_aircraft(snapshot, 2, pose=replace(snapshot.aircraft[2].pose, x=12, y=8, z=1.5))
    mapped = runtime([artifact()])
    intent = make_intent(
        IntentName.FORMATION_SET, selection=(1, 2), args={"name": "line"}, confirm=True
    )
    plan = mapped.prepare(intent, snapshot)
    assert isinstance(plan, Plan)
    assert plan.formation_update == "line"
    assert mapped.check(plan, plan.commands[0], snapshot) is None

    configured = mapped.config.formations["line"]
    mapped.config.formations["line"] = replace(
        configured, layout=replace(configured.layout, spacing_m=3)
    )
    layout_refusal = mapped.check(plan, plan.commands[0], snapshot)
    assert layout_refusal is not None
    assert layout_refusal.detail == "formation layout changed"


def test_runtime_revalidates_formation_zone_and_completed_navigation_evidence():
    snapshot = make_snapshot(2)
    snapshot = replace_aircraft(snapshot, 1, pose=replace(snapshot.aircraft[1].pose, x=8, y=8, z=1.5))
    snapshot = replace_aircraft(snapshot, 2, pose=replace(snapshot.aircraft[2].pose, x=12, y=8, z=1.5))
    mapped = runtime([artifact()])
    intent = make_intent(
        IntentName.FORMATION_SET, selection=(1, 2), args={"name": "line"}, confirm=True
    )
    plan = mapped.prepare(intent, snapshot)
    assert isinstance(plan, Plan)
    command = plan.commands[0]
    target = plan.navigation.route.routes[0].swept_segments[0].end
    completed_snapshot = replace_aircraft(
        replace(snapshot, now_ms=snapshot.now_ms + 1),
        command.drone_id,
        pose=Position(*target.xyz),
        position_last_seen_ms=snapshot.now_ms + 1,
    )
    assert (
        mapped.check(
            plan,
            command,
            completed_snapshot,
            completed=True,
            issued_at_ms=snapshot.now_ms,
        )
        is None
    )

    configured = mapped.config.formations["line"]
    mapped.config.formations["line"] = replace(
        configured, zone=replace(configured.zone, formation_enabled=False)
    )
    zone_refusal = mapped.check(plan, command, completed_snapshot)
    assert zone_refusal is not None
    assert zone_refusal.detail == "formation configuration or permission changed"


def test_controller_dispatches_mapped_formation_through_its_runtime():
    snapshot = make_snapshot(2)
    snapshot = replace_aircraft(snapshot, 1, pose=replace(snapshot.aircraft[1].pose, x=7, y=8, z=1.5))
    snapshot = replace_aircraft(snapshot, 2, pose=replace(snapshot.aircraft[2].pose, x=9, y=8, z=1.5))
    mapped = runtime([artifact()])
    configured = mapped.config.formations["line"]
    mapped.config.formations["line"] = replace(
        configured, layout=replace(configured.layout, center=pose(8, 8))
    )
    controller, planner, _, dispatcher, flight, _ = make_stack(snapshot)
    planner.mapped_formations = mapped
    dispatcher.navigation = mapped
    clock = [snapshot.now_ms]

    def current():
        clock[0] += 1
        aircraft = {
            drone_id: replace(
                snapshot.aircraft[drone_id],
                pose=drone.pose,
                flight_state=drone.flight_state,
                position_last_seen_ms=clock[0],
            )
            for drone_id, drone in flight.aircraft.items()
        }
        return replace(snapshot, now_ms=clock[0], aircraft=aircraft)

    result = controller.execute(
        make_intent(
            IntentName.FORMATION_SET,
            selection=(1, 2),
            args={"name": "line"},
            confirm=True,
        ),
        snapshot,
        current_snapshot=current,
    )
    assert result.status is LifecycleStatus.COMPLETED, result.refusal
    assert result.plan.formation_update == "line"
    assert [call.operation for call in flight.calls].count("hover") == 2

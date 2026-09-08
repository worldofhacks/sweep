from __future__ import annotations

from dataclasses import asdict, replace

from planner.mapped_formations import FormationLayout, FormationZone
from planner.models import Plan, Position, Refusal
from planner.navigation import Pose
from planner.navigation_authorization import NavigationApproval
from planner.navigation_runtime import (
    FormationBinding,
    NavigationFrame,
    PrecisionReturnBinding,
    TagDestinationBinding,
    navigation_configuration_digest,
)
from planner.planner import DeterministicPlanner
from planner.test_navigation_runtime import IDENTITY, KEY, setup_runtime
from relay.auth import sign_event
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_intent, make_snapshot, planning_config, replace_aircraft


def _approve(runtime, geometry, epochs: list[list[int]]) -> None:
    raw = asdict(runtime.approval)
    raw.update(
        v=1,
        type="navigation_approval",
        epochs=epochs,
        evidence_sha256=[],
        configuration_sha256=navigation_configuration_digest(
            geometry[0], runtime.config, runtime.permission, runtime.home_zone_id
        ),
    )
    runtime.approval = NavigationApproval.verify({**raw, "signature": sign_event(raw, KEY)}, KEY)


def _complete(runtime, plan: Plan, snapshot):
    for command in plan.commands:
        assert runtime.check(plan, command, snapshot) is None
        issued = snapshot.now_ms
        snapshot = replace(snapshot, now_ms=issued + 10)
        changes = {"position_last_seen_ms": snapshot.now_ms}
        if command.operation.value == "goto":
            changes["pose"] = Position(*(command.parameters[axis] for axis in ("x", "y", "z")))
        snapshot = replace_aircraft(snapshot, command.drone_id, **changes)
        assert runtime.check(plan, command, snapshot, completed=True, issued_at_ms=issued) is None
    return snapshot


def test_simulated_hallway_route_rejects_stale_localization_before_dispatch():
    runtime, snapshot, intent, _ = setup_runtime()
    plan = runtime.prepare(intent, snapshot)

    assert isinstance(plan, Plan)
    assert plan.commands[0].operation.value == "goto"
    completed = _complete(runtime, plan, snapshot)
    stale = replace_aircraft(
        replace(
            completed,
            now_ms=completed.now_ms + runtime.config.position_max_age_ms + 1,
        ),
        1,
        position_last_seen_ms=completed.now_ms,
    )
    assert isinstance(runtime.check(plan, plan.commands[-1], stale), Refusal)


def test_simulated_named_tag_route_rejects_replaced_map_identity():
    runtime, snapshot, _, geometry = setup_runtime()
    runtime.config = replace(
        runtime.config,
        tag_destinations=(TagDestinationBinding(42, "atrium", "atrium-a", 0.5, 0.1, 1.0),),
    )
    _approve(runtime, geometry, [[1, 1]])
    intent = make_intent(
        IntentName.NAVIGATE, selection=(1,), args={"zone_id": "atrium"}, confirm=True
    )
    plan = runtime.prepare(intent, snapshot)

    assert isinstance(plan, Plan)
    assert plan.navigation.route.arrival_slots[0].slot_id == "atrium-a"
    completed = _complete(runtime, plan, snapshot)
    geometry[0] = replace(
        geometry[0],
        map_pin=replace(geometry[0].map_pin, version="map-v3"),
    )
    assert isinstance(runtime.check(plan, plan.commands[-1], completed), Refusal)


def test_simulated_precision_return_requires_its_marked_aircraft_and_fresh_arrival():
    runtime, snapshot, _, geometry = setup_runtime()
    runtime.config = replace(
        runtime.config,
        precision_returns=(PrecisionReturnBinding(1, 1, "atrium", "atrium-a"),),
    )
    _approve(runtime, geometry, [[1, 1]])
    plan = runtime.prepare(
        make_intent(IntentName.NAVIGATE, selection=(1,), args={"zone_id": "atrium"}, confirm=True),
        snapshot,
    )

    assert isinstance(plan, Plan)
    arrived = _complete(runtime, plan, snapshot)
    changed_aircraft = replace_aircraft(arrived, 1, connection_epoch=2)
    assert isinstance(runtime.check(plan, plan.commands[-1], changed_aircraft), Refusal)


def test_simulated_line_and_column_routes_refuse_lost_separation():
    runtime, _, _, geometry = setup_runtime()
    zone = FormationZone(
        "demo-grid",
        "level_1",
        ((0.0, 0.0), (7.5, 0.0), (7.5, 4.5), (0.0, 4.5), (0.0, 0.0)),
        0.5,
        2.0,
        0.2,
        True,
        True,
        geometry[0].map_pin,
        geometry[0].geometry_pin,
    )
    layout = FormationLayout(Pose(5.5, 2.5, 1.0, "level_1"), 0.0, 1.0, (0.0, 0.0))
    runtime.config = replace(
        runtime.config,
        frames=tuple(
            NavigationFrame(drone_id, f"demo-world-{drone_id}", IDENTITY)
            for drone_id in (1, 2)
        ),
        max_aircraft=2,
        formation_bindings=(
            FormationBinding("line", zone, layout),
            FormationBinding("column", zone, layout),
        ),
    )
    _approve(runtime, geometry, [[1, 1], [2, 1]])
    initial = make_snapshot(2, spacing=1.0)
    initial = replace_aircraft(initial, 1, pose=Position(1.5, 1.0, 1.0))
    initial = replace_aircraft(initial, 2, pose=Position(1.5, 4.0, 1.0))
    planner = DeterministicPlanner(planning_config(), navigation_runtime=runtime)

    for shape in ("line", "column"):
        plan = planner.plan(
            make_intent(
                IntentName.FORMATION_SET,
                selection=(1, 2),
                args={"name": shape},
                confirm=True,
            ),
            initial,
        )
        assert isinstance(plan, Plan)
        assert runtime.check(plan, plan.commands[0], initial) is None
        too_close = replace_aircraft(initial, 2, pose=Position(1.5, 1.1, 1.0))
        assert isinstance(runtime.check(plan, plan.commands[0], too_close), Refusal)
        _complete(runtime, plan, initial)

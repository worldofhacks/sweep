from __future__ import annotations

from dataclasses import replace
from random import Random

from adapters.dispatch import AdapterDispatcher
from adapters.sim.flight import SimFlightAdapter
from arbiter.bvc import BvcConfig, filter_velocities
from planner.models import (
    Command,
    CommandOperation,
    FlightState,
    Geofence,
    LifecycleStatus,
    Plan,
    Position,
)
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_snapshot, make_stack, replace_aircraft


def config() -> BvcConfig:
    return BvcConfig(
        min_spacing_m=0.8,
        horizon_s=0.25,
        geofence=Geofence(-10.0, 10.0, -10.0, 10.0, 0.0, 5.0),
        ceiling_m=4.0,
    )


def test_safe_far_velocities_pass_through() -> None:
    positions = {1: Position(-3.0, 0.0, 1.0), 2: Position(3.0, 0.0, 1.0)}
    velocities = {1: Position(0.5, -0.2, 0.0), 2: Position(-0.4, 0.1, 0.0)}

    assert filter_velocities(positions, velocities, config()) == velocities


def test_head_on_commands_are_deflected() -> None:
    positions = {1: Position(-0.5, 0.0, 1.0), 2: Position(0.5, 0.0, 1.0)}
    filtered = filter_velocities(
        positions,
        {1: Position(0.5, 0.0, 0.0), 2: Position(-0.5, 0.0, 0.0)},
        config(),
    )

    assert filtered[1].y < 0.0
    assert filtered[2].y > 0.0
    projected = {
        drone_id: Position(
            positions[drone_id].x + velocity.x * config().horizon_s,
            positions[drone_id].y + velocity.y * config().horizon_s,
            positions[drone_id].z + velocity.z * config().horizon_s,
        )
        for drone_id, velocity in filtered.items()
    }
    assert projected[1].distance_to(projected[2]) >= 0.8 - 1e-8


def test_invalid_or_already_unsafe_state_stops_every_aircraft() -> None:
    positions = {1: Position(0.0, 0.0, 1.0), 2: Position(0.7, 0.0, 1.0)}
    velocities = {1: Position(0.5, 0.0, 0.0), 2: Position(-0.5, 0.0, 0.0)}

    assert filter_velocities(positions, velocities, config()) == {
        1: Position(0.0, 0.0, 0.0),
        2: Position(0.0, 0.0, 0.0),
    }
    assert filter_velocities({1: positions[1]}, velocities, config()) == {
        1: Position(0.0, 0.0, 0.0),
        2: Position(0.0, 0.0, 0.0),
    }


def test_randomized_safe_geometry_keeps_cells_separated_and_bounded() -> None:
    rng = Random(91)
    settings = config()
    for _ in range(300):
        positions = _random_positions(rng)
        velocities = {
            drone_id: Position(
                rng.uniform(-1.0, 1.0), rng.uniform(-1.0, 1.0), rng.uniform(-0.5, 0.5)
            )
            for drone_id in positions
        }

        filtered = filter_velocities(positions, velocities, settings)
        future = {
            drone_id: Position(
                position.x + filtered[drone_id].x * settings.horizon_s,
                position.y + filtered[drone_id].y * settings.horizon_s,
                position.z + filtered[drone_id].z * settings.horizon_s,
            )
            for drone_id, position in positions.items()
        }

        assert all(settings.geofence.contains(position) for position in future.values())
        assert all(position.z <= settings.ceiling_m for position in future.values())
        assert all(
            future[left].distance_to(future[right]) >= settings.min_spacing_m - 1e-8
            for left in future
            for right in future
            if left < right
        )


def test_filter_accounts_for_an_unselected_peer_velocity() -> None:
    positions = {
        1: Position(-0.5, 0.0, 1.0),
        2: Position(0.5, 0.0, 1.0),
        3: Position(0.0, 2.0, 1.0),
    }
    filtered = filter_velocities(
        positions,
        {
            1: Position(0.5, 0.0, 0.0),
            2: Position(-0.5, 0.0, 0.0),
            3: Position(0.0, -1.0, 0.0),
        },
        config(),
    )
    for step in range(101):
        elapsed = config().horizon_s * step / 100
        future = {
            drone_id: Position(
                position.x + filtered[drone_id].x * elapsed,
                position.y + filtered[drone_id].y * elapsed,
                position.z + filtered[drone_id].z * elapsed,
            )
            for drone_id, position in positions.items()
        }
        assert all(
            future[left].distance_to(future[right]) >= 0.8 - 1e-8
            for left in future
            for right in future
            if left < right
        )


def test_kinematic_sim_crossing_path_holds_spacing_over_successive_ticks() -> None:
    snapshot = make_snapshot(2)
    snapshot = replace_aircraft(snapshot, 1, pose=Position(-1.0, 0.0, 1.0))
    snapshot = replace_aircraft(snapshot, 2, pose=Position(1.0, 0.0, 1.0))
    flight = SimFlightAdapter.from_snapshot(snapshot)
    settings = config()
    desired = {1: Position(0.5, 0.0, 0.0), 2: Position(-0.5, 0.0, 0.0)}

    minimum = float("inf")
    for _ in range(24):
        positions = {drone_id: aircraft.pose for drone_id, aircraft in flight.aircraft.items()}
        filtered = filter_velocities(positions, desired, settings)
        for drone_id, velocity in filtered.items():
            position = positions[drone_id]
            flight.goto(
                drone_id,
                position.x + velocity.x * settings.horizon_s,
                position.y + velocity.y * settings.horizon_s,
                position.z + velocity.z * settings.horizon_s,
                0.5,
            )
        current = flight.aircraft
        minimum = min(minimum, current[1].pose.distance_to(current[2].pose))

    assert minimum >= settings.min_spacing_m - 1e-8


def test_dispatch_sends_bvc_projected_gotos_through_the_existing_adapter_route() -> None:
    snapshot = make_snapshot(2, selection=(1, 2))
    snapshot = replace_aircraft(snapshot, 1, pose=Position(-0.5, 0.0, 1.0))
    snapshot = replace_aircraft(snapshot, 2, pose=Position(0.5, 0.0, 1.0))
    _, _, arbiter, dispatcher, flight, _ = make_stack(snapshot)
    plan = Plan(
        plan_id="bvc-crossing",
        intent_id="bvc-crossing",
        intent_name=IntentName.TRANSLATE,
        roster_version=snapshot.roster_version,
        selection=(1, 2),
        confirmed=False,
        commands=(
            _goto("bvc-crossing:1", 1, 0.5, 0.0),
            _goto("bvc-crossing:2", 2, -0.5, 0.0),
        ),
    )

    projected = arbiter.filtered_goto_commands(plan, snapshot)
    assert arbiter.check_plan(plan, snapshot) is None
    _assert_simultaneous_goto_paths_keep_spacing(snapshot, plan, projected)
    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    assert result.deflected_commands == tuple(
        projected[command.command_id] for command in plan.commands
    )
    assert result.completion_detail is not None
    assert "requested targets remain outstanding" in result.completion_detail
    assert result.to_dict()["deflected_commands"] == [
        command.to_dict() for command in result.deflected_commands
    ]
    assert all(
        "BVC deflected goto" in acknowledgement.detail
        for acknowledgement in result.acknowledgements
    )
    calls = [call for call in flight.calls if call.operation is CommandOperation.GOTO]
    assert len(calls) == 2
    for call, command in zip(calls, plan.commands, strict=True):
        parameters = dict(call.parameters)
        expected = projected[command.command_id]
        assert parameters["x"] == expected.parameters["x"]
        assert parameters["y"] == expected.parameters["y"]
        assert parameters["z"] == expected.parameters["z"]
        assert Position.from_mapping(parameters) != Position.from_mapping(command.parameters)
    positions = {drone_id: aircraft.pose for drone_id, aircraft in flight.aircraft.items()}
    assert positions[1].distance_to(positions[2]) >= 0.8 - 1e-8


def test_crossing_routes_are_deflected_when_their_synchronized_positions_are_safe() -> None:
    snapshot = make_snapshot(2, selection=(1, 2))
    snapshot = replace_aircraft(snapshot, 1, pose=Position(0.0, 0.0, 1.0))
    snapshot = replace_aircraft(snapshot, 2, pose=Position(3.0, -3.0, 1.0))
    _, _, arbiter, _, _, _ = make_stack(snapshot)
    plan = Plan(
        plan_id="bvc-asynchronous-crossing",
        intent_id="bvc-asynchronous-crossing",
        intent_name=IntentName.TRANSLATE,
        roster_version=snapshot.roster_version,
        selection=(1, 2),
        confirmed=False,
        commands=(
            _goto("bvc-asynchronous-crossing:1", 1, 4.0, 0.0),
            _goto("bvc-asynchronous-crossing:2", 2, 3.0, 3.0),
        ),
    )

    projected = arbiter.filtered_goto_commands(plan, snapshot)

    assert projected[plan.commands[0].command_id] != plan.commands[0]
    assert projected[plan.commands[1].command_id] != plan.commands[1]


def test_missing_peer_velocity_deflects_to_a_hold_setpoint() -> None:
    snapshot = make_snapshot(2, selection=(1,))
    snapshot = replace_aircraft(snapshot, 2, flight_state=FlightState.AIRBORNE)
    _, _, arbiter, _, _, _ = make_stack(snapshot)
    plan = Plan(
        plan_id="bvc-missing-peer-motion",
        intent_id="bvc-missing-peer-motion",
        intent_name=IntentName.TRANSLATE,
        roster_version=snapshot.roster_version,
        selection=(1,),
        confirmed=False,
        commands=(_goto("bvc-missing-peer-motion:1", 1, 2.0, 0.0),),
    )

    projected = arbiter.filtered_goto_commands(plan, snapshot)[plan.commands[0].command_id]

    assert projected.parameters["x"] == snapshot.aircraft[1].pose.x
    assert projected.parameters["y"] == snapshot.aircraft[1].pose.y


class _ExecutingDeflectionFlight(SimFlightAdapter):
    def goto(self, drone_id: int, x: float, y: float, z: float, speed: float):
        acknowledgement = super().goto(drone_id, x, y, z, speed)
        return replace(acknowledgement, status=LifecycleStatus.EXECUTING)


def test_async_deflection_keeps_the_actual_setpoint_in_the_terminal_result() -> None:
    snapshot = make_snapshot(2, selection=(1,))
    snapshot = replace_aircraft(snapshot, 1, pose=Position(-0.5, 0.0, 1.0))
    snapshot = replace_aircraft(snapshot, 2, pose=Position(0.5, 0.0, 1.0))
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = _ExecutingDeflectionFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)
    plan = Plan(
        plan_id="bvc-async",
        intent_id="bvc-async",
        intent_name=IntentName.TRANSLATE,
        roster_version=snapshot.roster_version,
        selection=(1,),
        confirmed=False,
        commands=(replace(_goto("bvc-async:1", 1, 0.5, 0.0), intent_id="bvc-async"),),
    )

    pending = dispatcher.dispatch(plan, snapshot)
    terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)
    result = dispatcher.resume_after_completion(plan, pending, terminal, snapshot)

    assert pending.status is LifecycleStatus.EXECUTING
    assert result.status is LifecycleStatus.COMPLETED
    assert result.deflected_commands == pending.deflected_commands
    assert result.completion_detail is not None
    assert "requested targets remain outstanding" in result.completion_detail


def _random_positions(rng: Random) -> dict[int, Position]:
    positions: dict[int, Position] = {}
    while len(positions) < 4:
        candidate = Position(rng.uniform(-8.0, 8.0), rng.uniform(-8.0, 8.0), rng.uniform(0.5, 3.5))
        if all(candidate.distance_to(existing) >= 1.0 for existing in positions.values()):
            positions[len(positions) + 1] = candidate
    return positions


def _goto(command_id: str, drone_id: int, x: float, y: float) -> Command:
    return Command(
        command_id=command_id,
        intent_id="bvc-crossing",
        roster_version=7,
        drone_id=drone_id,
        connection_epoch=1,
        operation=CommandOperation.GOTO,
        parameters={"x": x, "y": y, "z": 1.0, "speed": 0.5},
    )


def _assert_simultaneous_goto_paths_keep_spacing(
    snapshot, plan: Plan, projected: dict[str, Command]
) -> None:
    durations = {
        command.drone_id: snapshot.aircraft[command.drone_id].pose.distance_to(
            Position.from_mapping(projected[command.command_id].parameters)
        )
        / float(projected[command.command_id].parameters["speed"])
        for command in plan.commands
    }
    latest = max(durations.values())
    for step in range(101):
        elapsed = latest * step / 100
        positions = {}
        for command in plan.commands:
            start = snapshot.aircraft[command.drone_id].pose
            target = Position.from_mapping(projected[command.command_id].parameters)
            duration = durations[command.drone_id]
            progress = 1.0 if duration == 0 else min(1.0, elapsed / duration)
            positions[command.drone_id] = Position(
                start.x + (target.x - start.x) * progress,
                start.y + (target.y - start.y) * progress,
                start.z + (target.z - start.z) * progress,
            )
        assert positions[1].distance_to(positions[2]) >= 0.8 - 1e-8

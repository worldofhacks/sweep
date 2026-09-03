"""Deterministic kinematic implementation of the frozen flight protocol."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from enum import StrEnum

from adapters.protocols import (
    AdapterAcknowledgement,
    AdapterTimeout,
    NodeWatchdogState,
    Telemetry,
    WatchdogConfig,
)
from planner.models import (
    CommandOperation,
    FleetSnapshot,
    FlightState,
    LifecycleStatus,
    LossBehavior,
    Position,
)


class InjectedFlightFailure(StrEnum):
    FAILURE = "failure"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class AdapterCall:
    operation: CommandOperation
    drone_ids: tuple[int, ...]
    parameters: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class SimAircraft:
    drone_id: int
    connection_epoch: int
    pose: Position
    home: Position
    yaw_deg: float
    flight_state: FlightState
    armed: bool
    battery: float
    link_quality: float
    position_quality: float


class SimFlightAdapter:
    def __init__(self, aircraft: dict[int, SimAircraft], *, timestamp_ms: int) -> None:
        self._aircraft = dict(sorted(aircraft.items()))
        self._timestamp_ms = timestamp_ms
        self.calls: list[AdapterCall] = []
        self._failures: dict[tuple[int, CommandOperation], InjectedFlightFailure] = {}
        self._ack_epoch_overrides: dict[int, int] = {}

    @classmethod
    def from_snapshot(cls, snapshot: FleetSnapshot) -> SimFlightAdapter:
        aircraft: dict[int, SimAircraft] = {}
        for drone_id, state in snapshot.aircraft.items():
            if state.home is None:
                raise ValueError(f"sim aircraft {drone_id} requires a home pose")
            aircraft[drone_id] = SimAircraft(
                drone_id=drone_id,
                connection_epoch=state.connection_epoch,
                pose=state.pose,
                home=state.home,
                yaw_deg=0.0,
                flight_state=state.flight_state,
                armed=state.armed,
                battery=state.battery,
                link_quality=state.link_quality,
                position_quality=state.position_quality,
            )
        return cls(aircraft, timestamp_ms=snapshot.now_ms)

    @property
    def aircraft(self) -> dict[int, SimAircraft]:
        return dict(self._aircraft)

    def camera_pose(self, drone_id: int) -> tuple[Position, float, int]:
        aircraft = self._require_aircraft(drone_id)
        return aircraft.pose, aircraft.yaw_deg, aircraft.connection_epoch

    def inject_failure(
        self,
        drone_id: int,
        operation: CommandOperation,
        failure: InjectedFlightFailure,
    ) -> None:
        self._failures[(drone_id, operation)] = failure

    def override_ack_epoch(self, drone_id: int, connection_epoch: int) -> None:
        self._ack_epoch_overrides[drone_id] = connection_epoch

    def update_connection_epoch(self, drone_id: int, connection_epoch: int) -> None:
        aircraft = self._require_aircraft(drone_id)
        self._aircraft[drone_id] = replace(aircraft, connection_epoch=connection_epoch)

    def takeoff(self, ids: list[int], z: float) -> tuple[AdapterAcknowledgement, ...]:
        self.calls.append(AdapterCall(CommandOperation.TAKEOFF, tuple(ids), (("z", float(z)),)))
        acknowledgements = []
        for drone_id in ids:
            failure = self._take_failure(drone_id, CommandOperation.TAKEOFF)
            if failure is not None:
                acknowledgements.append(failure)
                continue
            aircraft = self._require_aircraft(drone_id)
            self._aircraft[drone_id] = replace(
                aircraft,
                pose=Position(aircraft.pose.x, aircraft.pose.y, float(z)),
                flight_state=FlightState.HOVERING,
                armed=True,
            )
            acknowledgements.append(self._ack(drone_id, CommandOperation.TAKEOFF))
        return tuple(acknowledgements)

    def goto(
        self, drone_id: int, x: float, y: float, z: float, speed: float
    ) -> AdapterAcknowledgement:
        self.calls.append(
            AdapterCall(
                CommandOperation.GOTO,
                (drone_id,),
                tuple(sorted({"speed": speed, "x": x, "y": y, "z": z}.items())),
            )
        )
        failure = self._take_failure(drone_id, CommandOperation.GOTO)
        if failure is not None:
            return failure
        aircraft = self._require_aircraft(drone_id)
        self._aircraft[drone_id] = replace(
            aircraft,
            pose=Position(float(x), float(y), float(z)),
            flight_state=FlightState.HOVERING,
        )
        return self._ack(drone_id, CommandOperation.GOTO)

    def rotate_to(self, drone_id: int, yaw: float, speed: float) -> AdapterAcknowledgement:
        self.calls.append(
            AdapterCall(
                CommandOperation.ROTATE_TO,
                (drone_id,),
                tuple(sorted({"speed": speed, "yaw": yaw}.items())),
            )
        )
        failure = self._take_failure(drone_id, CommandOperation.ROTATE_TO)
        if failure is not None:
            return failure
        aircraft = self._require_aircraft(drone_id)
        self._aircraft[drone_id] = replace(aircraft, yaw_deg=float(yaw))
        return self._ack(drone_id, CommandOperation.ROTATE_TO)

    def hover(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]:
        self.calls.append(AdapterCall(CommandOperation.HOVER, tuple(ids)))
        acknowledgements = []
        for drone_id in ids:
            failure = self._take_failure(drone_id, CommandOperation.HOVER)
            if failure is not None:
                acknowledgements.append(failure)
                continue
            aircraft = self._require_aircraft(drone_id)
            self._aircraft[drone_id] = replace(aircraft, flight_state=FlightState.HOVERING)
            acknowledgements.append(self._ack(drone_id, CommandOperation.HOVER))
        return tuple(acknowledgements)

    def land(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]:
        self.calls.append(AdapterCall(CommandOperation.LAND, tuple(ids)))
        acknowledgements = []
        for drone_id in ids:
            failure = self._take_failure(drone_id, CommandOperation.LAND)
            if failure is not None:
                acknowledgements.append(failure)
                continue
            aircraft = self._require_aircraft(drone_id)
            self._aircraft[drone_id] = replace(
                aircraft,
                pose=Position(aircraft.pose.x, aircraft.pose.y, aircraft.home.z),
                flight_state=FlightState.LANDED,
                armed=False,
            )
            acknowledgements.append(self._ack(drone_id, CommandOperation.LAND))
        return tuple(acknowledgements)

    def estop(self) -> tuple[AdapterAcknowledgement, ...]:
        ids = tuple(sorted(self._aircraft))
        self.calls.append(AdapterCall(CommandOperation.ESTOP, ids))
        acknowledgements = []
        for drone_id in ids:
            failure = self._take_failure(drone_id, CommandOperation.ESTOP)
            if failure is not None:
                acknowledgements.append(failure)
                continue
            aircraft = self._require_aircraft(drone_id)
            if aircraft.flight_state is not FlightState.LANDED:
                self._aircraft[drone_id] = replace(aircraft, flight_state=FlightState.HOVERING)
            acknowledgements.append(self._ack(drone_id, CommandOperation.ESTOP))
        return tuple(acknowledgements)

    def telemetry(self) -> Iterator[Telemetry]:
        for drone_id, aircraft in sorted(self._aircraft.items()):
            yield Telemetry(
                drone_id=drone_id,
                connection_epoch=aircraft.connection_epoch,
                t_ms=self._timestamp_ms,
                pose=aircraft.pose,
                velocity=Position(0.0, 0.0, 0.0),
                yaw_deg=aircraft.yaw_deg,
                battery=aircraft.battery,
                flight_state=aircraft.flight_state.value,
                link_quality=aircraft.link_quality,
                position_quality=aircraft.position_quality,
            )

    def apply_node_watchdog(
        self,
        state: NodeWatchdogState,
        *,
        now_ms: int,
        config: WatchdogConfig,
    ) -> LossBehavior | None:
        """Apply one node's local activity clock without a relay callback."""
        aircraft = self._require_aircraft(state.drone_id)
        if state.connection_epoch != aircraft.connection_epoch:
            raise ValueError("watchdog state belongs to a prior connection epoch")
        action = state.action_at(now_ms, config)
        if action is None:
            return None
        if action is LossBehavior.HOLD:
            if _is_airborne(aircraft.flight_state):
                self._aircraft[state.drone_id] = replace(
                    aircraft, flight_state=FlightState.HOVERING
                )
            return LossBehavior.HOLD

        if _is_airborne(aircraft.flight_state):
            self._aircraft[state.drone_id] = replace(
                aircraft,
                pose=aircraft.home,
                flight_state=FlightState.LANDED,
                armed=False,
            )
        return action

    def _take_failure(
        self, drone_id: int, operation: CommandOperation
    ) -> AdapterAcknowledgement | None:
        failure = self._failures.pop((drone_id, operation), None)
        if failure is InjectedFlightFailure.TIMEOUT:
            raise AdapterTimeout(drone_id, operation)
        if failure is InjectedFlightFailure.FAILURE:
            return self._ack(
                drone_id,
                operation,
                status=LifecycleStatus.FAILED,
                detail="injected simulated adapter failure",
            )
        return None

    def _ack(
        self,
        drone_id: int,
        operation: CommandOperation,
        *,
        status: LifecycleStatus = LifecycleStatus.COMPLETED,
        detail: str = "",
    ) -> AdapterAcknowledgement:
        aircraft = self._require_aircraft(drone_id)
        epoch = self._ack_epoch_overrides.get(drone_id, aircraft.connection_epoch)
        return AdapterAcknowledgement(
            drone_id=drone_id,
            connection_epoch=epoch,
            operation=operation,
            status=status,
            detail=detail,
        )

    def _require_aircraft(self, drone_id: int) -> SimAircraft:
        try:
            return self._aircraft[drone_id]
        except KeyError as error:
            raise ValueError(f"unknown simulated aircraft {drone_id}") from error


def _is_airborne(state: FlightState) -> bool:
    return state in {
        FlightState.TAKING_OFF,
        FlightState.AIRBORNE,
        FlightState.HOVERING,
        FlightState.LANDING,
    }

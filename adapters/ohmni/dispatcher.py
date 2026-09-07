from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping

from adapters.dji_mini3.remote import CommandRequest, NodeLink
from adapters.protocols import AdapterError
from planner.models import (
    Command,
    CommandAcknowledgement,
    CommandOperation,
    ExecutionResult,
    LifecycleStatus,
    Plan,
    Refusal,
    RefusalReason,
)
from relay.intent_v1 import IntentName, IntentV1


class GroundCommandDispatcher:
    """Send one confirmed ground pulse through the relay's signed node link."""

    def __init__(
        self,
        link: NodeLink,
        *,
        acknowledgement_timeout_ms: int,
        command_deadline_ms: int,
        command_ids: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if acknowledgement_timeout_ms <= 0 or command_deadline_ms < acknowledgement_timeout_ms:
            raise ValueError("ground command timeouts are invalid")
        self._link = link
        self._acknowledgement_timeout_ms = acknowledgement_timeout_ms
        self._command_deadline_ms = command_deadline_ms
        self._command_ids = command_ids or (lambda: str(uuid.uuid4()))
        self._monotonic = monotonic

    def dispatch(self, intent: IntentV1, state: Mapping[str, object]) -> ExecutionResult:
        target = self._target(intent, state)
        if isinstance(target, ExecutionResult):
            return target
        drone_id, connection_epoch, roster_version = target
        command = Command(
            command_id=self._command_ids(),
            intent_id=intent.intent_id,
            roster_version=roster_version,
            drone_id=drone_id,
            connection_epoch=connection_epoch,
            operation=CommandOperation.GROUND_VELOCITY,
            parameters=intent.args,
        )
        plan = Plan(
            plan_id=f"plan:{intent.intent_id}",
            intent_id=intent.intent_id,
            intent_name=intent.name,
            roster_version=roster_version,
            selection=intent.selection,
            confirmed=True,
            commands=(command,),
        )
        if self._link.connection_epoch(drone_id) != connection_epoch:
            return self._refused(
                intent,
                roster_version,
                drone_id,
                connection_epoch,
                RefusalReason.STALE_CONNECTION_EPOCH,
                "ground node reconnected before command release",
                plan,
            )
        request = CommandRequest(
            command_id=command.command_id,
            intent_id=intent.intent_id,
            roster_version=roster_version,
            drone_id=drone_id,
            connection_epoch=connection_epoch,
            operation=CommandOperation.GROUND_VELOCITY,
            args={key: value for key, value in intent.args.items() if isinstance(value, int)},
        )
        try:
            self._link.send(request)
        except AdapterError as error:
            return self._failed(
                intent,
                plan,
                (),
                drone_id,
                connection_epoch,
                RefusalReason.ADAPTER_FAILURE,
                str(error),
            )
        acknowledgements = self._collect(request)
        terminal = acknowledgements[-1] if acknowledgements else None
        if terminal is not None and terminal.status is LifecycleStatus.COMPLETED:
            return ExecutionResult(
                intent_id=intent.intent_id,
                roster_version=roster_version,
                status=LifecycleStatus.COMPLETED,
                plan=plan,
                acknowledgements=tuple(acknowledgements),
            )
        if terminal is not None and terminal.status in {
            LifecycleStatus.FAILED,
            LifecycleStatus.INVALIDATED,
        }:
            return self._failed(
                intent,
                plan,
                acknowledgements,
                drone_id,
                connection_epoch,
                _refusal_reason(terminal.reason, RefusalReason.ADAPTER_FAILURE),
                terminal.detail or "ground node rejected the command",
                status=terminal.status,
            )
        return self._failed(
            intent,
            plan,
            acknowledgements,
            drone_id,
            connection_epoch,
            RefusalReason.ADAPTER_TIMEOUT,
            "ground node did not complete the bounded pulse before its command deadline",
        )

    def dispatch_stop(self, intent: IntentV1, state: Mapping[str, object]) -> ExecutionResult:
        """Fan a confirmed hold or global emergency stop to every selected ground node."""
        target = self._stop_targets(intent, state)
        if isinstance(target, ExecutionResult):
            return target
        roster_version, operation, targets = target
        commands = tuple(
            Command(
                command_id=self._command_ids(),
                intent_id=intent.intent_id,
                roster_version=roster_version,
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                operation=operation,
                parameters={},
            )
            for drone_id, connection_epoch in targets
        )
        plan = Plan(
            plan_id=f"plan:{intent.intent_id}:ground-{operation.value}",
            intent_id=intent.intent_id,
            intent_name=intent.name,
            roster_version=roster_version,
            selection=intent.selection,
            confirmed=True,
            commands=commands,
        )
        acknowledgements: list[CommandAcknowledgement] = []
        issued: list[tuple[Command, CommandRequest]] = []
        for command in commands:
            if self._link.connection_epoch(command.drone_id) != command.connection_epoch:
                acknowledgements.append(
                    self._acknowledgement(
                        command,
                        LifecycleStatus.FAILED,
                        RefusalReason.STALE_CONNECTION_EPOCH,
                        "ground node reconnected before stop release",
                    )
                )
                continue
            request = CommandRequest(
                command_id=command.command_id,
                intent_id=command.intent_id,
                roster_version=command.roster_version,
                drone_id=command.drone_id,
                connection_epoch=command.connection_epoch,
                operation=command.operation,
                args={},
            )
            try:
                self._link.send(request)
            except AdapterError as error:
                acknowledgements.append(
                    self._acknowledgement(
                        command,
                        LifecycleStatus.FAILED,
                        RefusalReason.ADAPTER_FAILURE,
                        str(error),
                    )
                )
                continue
            issued.append((command, request))

        for command, request in issued:
            replies = self._collect(request)
            acknowledgements.extend(replies)
            terminal = replies[-1] if replies else None
            if terminal is None or terminal.status not in {
                LifecycleStatus.COMPLETED,
                LifecycleStatus.FAILED,
                LifecycleStatus.INVALIDATED,
            }:
                acknowledgements.append(
                    self._acknowledgement(
                        command,
                        LifecycleStatus.FAILED,
                        RefusalReason.ADAPTER_TIMEOUT,
                        "ground node did not complete the stop before its command deadline",
                    )
                )

        failed = next(
            (
                acknowledgement
                for acknowledgement in acknowledgements
                if acknowledgement.status in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED}
            ),
            None,
        )
        if failed is None:
            return ExecutionResult(
                intent_id=intent.intent_id,
                roster_version=roster_version,
                status=LifecycleStatus.COMPLETED,
                plan=plan,
                acknowledgements=tuple(acknowledgements),
            )
        return ExecutionResult(
            intent_id=intent.intent_id,
            roster_version=roster_version,
            status=LifecycleStatus.FAILED,
            plan=plan,
            acknowledgements=tuple(acknowledgements),
            refusal=Refusal(
                intent_id=intent.intent_id,
                roster_version=roster_version,
                drone_id=failed.drone_id,
                connection_epoch=failed.connection_epoch,
                reason=failed.reason or RefusalReason.ADAPTER_FAILURE,
                detail=failed.detail or "a ground node did not complete the stop",
                status=LifecycleStatus.FAILED,
            ),
            degraded_aircraft=tuple(
                sorted(
                    {
                        acknowledgement.drone_id
                        for acknowledgement in acknowledgements
                        if acknowledgement.status
                        in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED}
                    }
                )
            ),
        )

    def _target(
        self, intent: IntentV1, state: Mapping[str, object]
    ) -> tuple[int, int, int] | ExecutionResult:
        roster_version = state.get("roster_version")
        if not isinstance(roster_version, int) or isinstance(roster_version, bool):
            return self._refused(
                intent,
                0,
                None,
                None,
                RefusalReason.INVALID_ROSTER_TRANSITION,
                "relay state has no current roster version",
            )
        if intent.name is not IntentName.GROUND_VELOCITY or not intent.confirm:
            return self._refused(
                intent,
                roster_version,
                None,
                None,
                RefusalReason.CONFIRMATION_REQUIRED,
                "ground velocity requires a confirmed canonical intent",
            )
        if len(intent.selection) != 1:
            return self._refused(
                intent,
                roster_version,
                None,
                None,
                RefusalReason.INVALID_SELECTION,
                "ground velocity requires exactly one selected ground node",
            )
        if state.get("estop_active") is True:
            return self._refused(
                intent,
                roster_version,
                None,
                None,
                RefusalReason.ESTOP_ACTIVE,
                "the session emergency stop remains active",
            )
        drone_id = intent.selection[0]
        drone = next(
            (
                value
                for value in state.get("drones", ())
                if isinstance(value, Mapping) and value.get("drone_id") == drone_id
            ),
            None,
        )
        if drone is None or drone.get("node_type") != "ground":
            return self._refused(
                intent,
                roster_version,
                drone_id,
                None,
                RefusalReason.INVALID_SELECTION,
                "ground velocity can target only an authenticated ground node",
            )
        connection_epoch = drone.get("connection_epoch")
        capabilities = drone.get("adapter_capabilities")
        if (
            not isinstance(connection_epoch, int)
            or isinstance(connection_epoch, bool)
            or drone.get("membership") != "ready"
            or drone.get("selectable") is not True
            or drone.get("control_authority") is not True
            or not isinstance(capabilities, list)
            or "ground_drive" not in capabilities
        ):
            return self._refused(
                intent,
                roster_version,
                drone_id,
                connection_epoch if isinstance(connection_epoch, int) else None,
                RefusalReason.AIRCRAFT_NOT_READY,
                "ground readiness, pose evidence, and local drive authority are required",
            )
        return drone_id, connection_epoch, roster_version

    def _stop_targets(
        self, intent: IntentV1, state: Mapping[str, object]
    ) -> tuple[int, CommandOperation, tuple[tuple[int, int], ...]] | ExecutionResult:
        roster_version = state.get("roster_version")
        if not isinstance(roster_version, int) or isinstance(roster_version, bool):
            return self._refused(
                intent,
                0,
                None,
                None,
                RefusalReason.INVALID_ROSTER_TRANSITION,
                "relay state has no current roster version",
            )
        operation = (
            CommandOperation.HOVER
            if intent.name is IntentName.HOLD
            else CommandOperation.ESTOP
            if intent.name is IntentName.ESTOP
            else None
        )
        if operation is None:
            return self._refused(
                intent,
                roster_version,
                None,
                None,
                RefusalReason.INVALID_PLAN,
                "only hold and estop are ground stop commands",
            )
        drones = state.get("drones", ())
        if not isinstance(drones, (list, tuple)):
            drones = ()
        selected = set(intent.selection)
        targets: list[tuple[int, int]] = []
        for drone in drones:
            if not isinstance(drone, Mapping) or drone.get("node_type") != "ground":
                continue
            drone_id = drone.get("drone_id")
            connection_epoch = drone.get("connection_epoch")
            if (
                not isinstance(drone_id, int)
                or isinstance(drone_id, bool)
                or not isinstance(connection_epoch, int)
                or isinstance(connection_epoch, bool)
            ):
                continue
            if operation is CommandOperation.HOVER and drone_id not in selected:
                continue
            targets.append((drone_id, connection_epoch))
        if not targets:
            return self._refused(
                intent,
                roster_version,
                None,
                None,
                RefusalReason.INVALID_SELECTION,
                "the stop did not select an authenticated ground node",
            )
        return roster_version, operation, tuple(sorted(targets))

    def _collect(self, request: CommandRequest) -> list[CommandAcknowledgement]:
        deadline = self._monotonic() + self._command_deadline_ms / 1_000
        acknowledgements: list[CommandAcknowledgement] = []
        while remaining := deadline - self._monotonic():
            wire = self._link.await_acknowledgement(
                request.command_id,
                timeout_ms=min(self._acknowledgement_timeout_ms, max(1, int(remaining * 1_000))),
            )
            if wire is None:
                break
            acknowledgement = CommandAcknowledgement(
                command_id=request.command_id,
                intent_id=request.intent_id,
                roster_version=request.roster_version,
                drone_id=request.drone_id,
                connection_epoch=request.connection_epoch,
                status=LifecycleStatus(wire.status.value),
                reason=_refusal_reason(wire.reason, RefusalReason.ADAPTER_FAILURE),
                detail=wire.detail or "",
            )
            acknowledgements.append(acknowledgement)
            if acknowledgement.status in {
                LifecycleStatus.COMPLETED,
                LifecycleStatus.FAILED,
                LifecycleStatus.INVALIDATED,
            }:
                break
        return acknowledgements

    @staticmethod
    def _acknowledgement(
        command: Command, status: LifecycleStatus, reason: RefusalReason, detail: str
    ) -> CommandAcknowledgement:
        return CommandAcknowledgement(
            command_id=command.command_id,
            intent_id=command.intent_id,
            roster_version=command.roster_version,
            drone_id=command.drone_id,
            connection_epoch=command.connection_epoch,
            status=status,
            reason=reason,
            detail=detail,
        )

    @staticmethod
    def _refused(
        intent: IntentV1,
        roster_version: int,
        drone_id: int | None,
        connection_epoch: int | None,
        reason: RefusalReason,
        detail: str,
        plan: Plan | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            intent_id=intent.intent_id,
            roster_version=roster_version,
            status=LifecycleStatus.REFUSED,
            plan=plan,
            refusal=Refusal(
                intent_id=intent.intent_id,
                roster_version=roster_version,
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                reason=reason,
                detail=detail,
            ),
        )

    @staticmethod
    def _failed(
        intent: IntentV1,
        plan: Plan,
        acknowledgements: list[CommandAcknowledgement] | tuple[CommandAcknowledgement, ...],
        drone_id: int,
        connection_epoch: int,
        reason: RefusalReason,
        detail: str,
        *,
        status: LifecycleStatus = LifecycleStatus.FAILED,
    ) -> ExecutionResult:
        return ExecutionResult(
            intent_id=intent.intent_id,
            roster_version=plan.roster_version,
            status=status,
            plan=plan,
            acknowledgements=tuple(acknowledgements),
            refusal=Refusal(
                intent_id=intent.intent_id,
                roster_version=plan.roster_version,
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                reason=reason,
                detail=detail,
                status=status,
            ),
            degraded_aircraft=(drone_id,),
        )


def _refusal_reason(value: str | None, fallback: RefusalReason) -> RefusalReason:
    try:
        return RefusalReason(value)
    except (TypeError, ValueError):
        return fallback

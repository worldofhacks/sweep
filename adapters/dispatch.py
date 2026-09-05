"""Fail-closed dispatch from checked plans to flight and camera protocols."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from math import isfinite
from string import hexdigits

from adapters.protocols import (
    AdapterAcknowledgement,
    AdapterTimeout,
    CameraCapabilities,
    CameraCapture,
    CameraIntrinsics,
    CameraResultStatus,
    CameraState,
    CameraStateCode,
    CaptureBundle,
    CaptureCoverage,
    CapturePattern,
    CaptureResult,
    MediaFile,
    MediaReference,
    MediaResult,
    SwarmAdapter,
)
from arbiter.safety import SafetyArbiter
from planner.models import (
    Command,
    CommandAcknowledgement,
    CommandOperation,
    ExecutionResult,
    FleetSnapshot,
    HoldScope,
    LifecycleStatus,
    MembershipState,
    Plan,
    Position,
    Refusal,
    RefusalReason,
)
from relay.intent_v1 import IntentName

type SnapshotProvider = Callable[[], FleetSnapshot]
type CommandOutcome = (
    CommandAcknowledgement | Refusal | tuple[list[CommandAcknowledgement], list[MediaFile]]
)


class AdapterDispatcher:
    def __init__(
        self,
        *,
        flight: SwarmAdapter,
        camera: CameraCapture,
        arbiter: SafetyArbiter,
    ) -> None:
        self.flight = flight
        self.camera = camera
        self.arbiter = arbiter

    def dispatch(
        self,
        plan: Plan,
        snapshot: FleetSnapshot,
        *,
        current_snapshot: SnapshotProvider | None = None,
    ) -> ExecutionResult:
        """Dispatch after whole-plan and immediate pre-I/O state validation.

        ``current_snapshot`` is read before every call and before accepting every
        acknowledgement, so roster or epoch changes during a multi-step mission
        fail closed before the next adapter operation.
        """
        provider = current_snapshot or (lambda: snapshot)
        initial = provider()
        preflight = self.arbiter.check_plan(plan, initial)
        if preflight is not None:
            return self._refused(plan, initial, preflight)

        if plan.commands and all(
            command.operation is CommandOperation.ESTOP for command in plan.commands
        ):
            return self._dispatch_estop(plan, provider)

        return self._dispatch_checked(plan, provider, initial)

    def _dispatch_checked(
        self,
        plan: Plan,
        provider: SnapshotProvider,
        initial: FleetSnapshot,
        *,
        start_index: int = 0,
        prior_affected: tuple[Command, ...] = (),
    ) -> ExecutionResult:
        """Keep possible I/O owned even when live state fails between adapter calls."""
        last_snapshot = initial
        attempted = list(prior_affected)

        def tracked_snapshot() -> FleetSnapshot:
            nonlocal last_snapshot
            last_snapshot = provider()
            return last_snapshot

        try:
            return self._dispatch_checked_commands(
                plan,
                tracked_snapshot,
                initial,
                start_index=start_index,
                prior_affected=prior_affected,
                attempted=attempted,
            )
        except Exception as error:
            acknowledgements = []
            stop_commands = plan.commands if plan.intent_name is IntentName.HOLD else attempted
            targets = {command.drone_id: command for command in stop_commands}
            for command in targets.values():
                acknowledgements.extend(
                    self._best_effort_hold(plan, command, lambda: last_snapshot)
                )
            return ExecutionResult(
                intent_id=plan.intent_id,
                roster_version=last_snapshot.roster_version,
                status=LifecycleStatus.FAILED,
                plan=plan,
                acknowledgements=tuple(acknowledgements),
                refusal=Refusal(
                    intent_id=plan.intent_id,
                    roster_version=last_snapshot.roster_version,
                    drone_id=None,
                    connection_epoch=None,
                    reason=RefusalReason.ADAPTER_FAILURE,
                    detail=f"dispatch interrupted after possible I/O: {type(error).__name__}",
                    status=LifecycleStatus.FAILED,
                ),
                degraded_aircraft=tuple(sorted(targets)),
            )

    def _dispatch_checked_commands(
        self,
        plan: Plan,
        provider: SnapshotProvider,
        initial: FleetSnapshot,
        *,
        start_index: int,
        prior_affected: tuple[Command, ...],
        attempted: list[Command],
    ) -> ExecutionResult:
        acknowledgements: list[CommandAcknowledgement] = []
        captures: dict[str, CaptureResult] = {}
        media_files: list[MediaFile] = []
        degraded: set[int] = set()
        failures: list[Refusal] = []
        projected = {}
        for completed in prior_affected:
            aircraft = initial.aircraft.get(completed.drone_id)
            if aircraft is None:
                continue
            target = self.arbiter.command_position(completed, aircraft)
            if target is not None:
                projected[completed.drone_id] = target
        affected = {
            command.drone_id: command
            for command in prior_affected
            if command.operation
            not in {
                CommandOperation.HOVER,
                CommandOperation.LAND,
                CommandOperation.ESTOP,
            }
        }

        for command in plan.commands[start_index:]:
            if command.drone_id in degraded:
                continue
            current = provider()
            refusal = self.arbiter.check_command(
                plan,
                command,
                current,
                projected_positions=projected,
            )
            if refusal is not None:
                if plan.intent_name is IntentName.CAPTURE_ROOM:
                    affected[command.drone_id] = command
                acknowledgements.extend(self._hold_affected(plan, affected, provider))
                return self._refused(
                    plan,
                    current,
                    refusal,
                    acknowledgements=acknowledgements,
                    media_files=media_files,
                    degraded=degraded,
                )

            attempted.append(command)
            try:
                outcome = self._execute(command, captures, provider)
            except AdapterTimeout as error:
                failure = self._failure_for(
                    command,
                    current,
                    RefusalReason.ADAPTER_TIMEOUT,
                    error.detail,
                )
                failures.append(failure)
                degraded.add(command.drone_id)
                projected.pop(command.drone_id, None)
                acknowledgements.append(self._failed_ack(command, failure))
                acknowledgements.extend(
                    self._best_effort_hold(plan, command, provider, fallback_snapshot=current)
                )
                continue
            except Exception as error:  # adapter exceptions never cross this boundary
                failure = self._failure_for(
                    command,
                    current,
                    RefusalReason.ADAPTER_FAILURE,
                    f"adapter raised {type(error).__name__}",
                )
                failures.append(failure)
                degraded.add(command.drone_id)
                projected.pop(command.drone_id, None)
                acknowledgements.append(self._failed_ack(command, failure))
                acknowledgements.extend(
                    self._best_effort_hold(plan, command, provider, fallback_snapshot=current)
                )
                continue

            if isinstance(outcome, Refusal):
                if outcome.reason in {
                    RefusalReason.STALE_ROSTER,
                    RefusalReason.STALE_CONNECTION_EPOCH,
                    RefusalReason.CAMERA_UNSUPPORTED,
                }:
                    status = LifecycleStatus.REFUSED
                else:
                    status = LifecycleStatus.FAILED
                    outcome = Refusal(
                        intent_id=outcome.intent_id,
                        roster_version=outcome.roster_version,
                        drone_id=outcome.drone_id,
                        connection_epoch=outcome.connection_epoch,
                        reason=outcome.reason,
                        detail=outcome.detail,
                        status=status,
                    )
                affected[command.drone_id] = command
                acknowledgements.extend(self._hold_affected(plan, affected, provider))
                return ExecutionResult(
                    intent_id=plan.intent_id,
                    roster_version=provider().roster_version,
                    status=status,
                    plan=plan,
                    acknowledgements=tuple(acknowledgements),
                    refusal=outcome,
                    capture_bundle=self._failed_bundle(plan, media_files, outcome),
                    degraded_aircraft=tuple(sorted(degraded)),
                )

            if isinstance(outcome, tuple):
                command_acks, new_media = outcome
                acknowledgements.extend(command_acks)
                media_files.extend(new_media)
                latest = command_acks[-1]
            else:
                acknowledgements.append(outcome)
                latest = outcome

            # Camera work does not intentionally change the aircraft pose or
            # flight state. Re-run the complete live command gate after the I/O
            # result so drift, authority loss, or stale telemetry that occurs
            # during an adapter call cannot be certified as a completed mission.
            # The completed acknowledgement/media stays in the audit trail, but
            # dependent camera work never advances and the target is held.
            if plan.intent_name is IntentName.CAPTURE_ROOM:
                after = provider()
                post_io_refusal = self.arbiter.check_command(
                    plan,
                    command,
                    after,
                    projected_positions=projected,
                )
                if post_io_refusal is not None:
                    affected[command.drone_id] = command
                    acknowledgements.extend(self._hold_affected(plan, affected, provider))
                    return self._refused(
                        plan,
                        after,
                        post_io_refusal,
                        acknowledgements=acknowledgements,
                        media_files=media_files,
                        degraded=degraded,
                    )

            # Dependent work advances only after a terminal completed ack.
            if latest.status in {LifecycleStatus.ACCEPTED, LifecycleStatus.EXECUTING}:
                return ExecutionResult(
                    intent_id=plan.intent_id,
                    roster_version=provider().roster_version,
                    status=LifecycleStatus.EXECUTING,
                    plan=plan,
                    acknowledgements=tuple(acknowledgements),
                )
            if latest.status is not LifecycleStatus.COMPLETED:
                reason = latest.reason or RefusalReason.ADAPTER_FAILURE
                failure = self._failure_for(command, provider(), reason, latest.detail)
                if reason in {
                    RefusalReason.STALE_ROSTER,
                    RefusalReason.STALE_CONNECTION_EPOCH,
                }:
                    failure = Refusal(
                        intent_id=failure.intent_id,
                        roster_version=failure.roster_version,
                        drone_id=failure.drone_id,
                        connection_epoch=failure.connection_epoch,
                        reason=failure.reason,
                        detail=failure.detail,
                        status=LifecycleStatus.REFUSED,
                    )
                    affected[command.drone_id] = command
                    acknowledgements.extend(self._hold_affected(plan, affected, provider))
                    return self._refused(
                        plan,
                        provider(),
                        failure,
                        acknowledgements=acknowledgements,
                        media_files=media_files,
                        degraded=degraded,
                    )
                failures.append(failure)
                degraded.add(command.drone_id)
                projected.pop(command.drone_id, None)
                acknowledgements.extend(self._best_effort_hold(plan, command, provider))
            else:
                target = self.arbiter.command_position(command, current.aircraft[command.drone_id])
                if target is not None:
                    projected[command.drone_id] = target
                if command.operation not in {
                    CommandOperation.HOVER,
                    CommandOperation.LAND,
                    CommandOperation.ESTOP,
                }:
                    affected[command.drone_id] = command

        if failures:
            failure = failures[0]
            current = provider()
            return ExecutionResult(
                intent_id=plan.intent_id,
                roster_version=current.roster_version,
                status=LifecycleStatus.FAILED,
                plan=plan,
                acknowledgements=tuple(acknowledgements),
                refusal=failure,
                capture_bundle=self._failed_bundle(plan, media_files, failure),
                degraded_aircraft=tuple(sorted(degraded)),
            )

        completion_snapshot = None
        if not plan.commands:
            completion_snapshot = provider()
            refusal = self.arbiter.check_plan(plan, completion_snapshot)
            if refusal is not None:
                return self._refused(plan, completion_snapshot, refusal)

        bundle = self._completed_bundle(plan, media_files)
        if isinstance(bundle, Refusal):
            if plan.commands:
                acknowledgements.extend(self._best_effort_hold(plan, plan.commands[-1], provider))
            return ExecutionResult(
                intent_id=plan.intent_id,
                roster_version=provider().roster_version,
                status=LifecycleStatus.FAILED,
                plan=plan,
                acknowledgements=tuple(acknowledgements),
                refusal=bundle,
                capture_bundle=self._failed_bundle(plan, media_files, bundle),
            )
        return ExecutionResult(
            intent_id=plan.intent_id,
            roster_version=(completion_snapshot or provider()).roster_version,
            status=LifecycleStatus.COMPLETED,
            plan=plan,
            acknowledgements=tuple(acknowledgements),
            capture_bundle=bundle,
        )

    def validate_acknowledgement(
        self,
        command: Command,
        acknowledgement: AdapterAcknowledgement,
        snapshot: FleetSnapshot,
    ) -> CommandAcknowledgement:
        """Validate roster, identity, and epoch before accepting an adapter ack."""
        aircraft = snapshot.aircraft.get(command.drone_id)
        if not _valid_adapter_acknowledgement(acknowledgement):
            return CommandAcknowledgement(
                command_id=command.command_id,
                intent_id=command.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=command.drone_id,
                connection_epoch=(
                    aircraft.connection_epoch if aircraft is not None else command.connection_epoch
                ),
                status=LifecycleStatus.FAILED,
                reason=RefusalReason.ADAPTER_FAILURE,
                detail="adapter acknowledgement violates the typed result boundary",
            )
        if command.roster_version != snapshot.roster_version:
            return CommandAcknowledgement(
                command_id=command.command_id,
                intent_id=command.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=command.drone_id,
                connection_epoch=(
                    aircraft.connection_epoch if aircraft is not None else command.connection_epoch
                ),
                status=LifecycleStatus.REFUSED,
                reason=RefusalReason.STALE_ROSTER,
                detail="acknowledgement belongs to a command from a prior roster",
            )
        if (
            aircraft is None
            or acknowledgement.drone_id != command.drone_id
            or acknowledgement.connection_epoch != command.connection_epoch
            or acknowledgement.connection_epoch != aircraft.connection_epoch
        ):
            return CommandAcknowledgement(
                command_id=command.command_id,
                intent_id=command.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=command.drone_id,
                connection_epoch=(
                    aircraft.connection_epoch if aircraft is not None else command.connection_epoch
                ),
                status=LifecycleStatus.REFUSED,
                reason=RefusalReason.STALE_CONNECTION_EPOCH,
                detail="acknowledgement identity or connection epoch is stale",
            )
        if acknowledgement.operation is not command.operation:
            return CommandAcknowledgement(
                command_id=command.command_id,
                intent_id=command.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=command.drone_id,
                connection_epoch=aircraft.connection_epoch,
                status=LifecycleStatus.FAILED,
                reason=RefusalReason.ADAPTER_FAILURE,
                detail="acknowledgement operation does not match command",
            )
        failed = acknowledgement.status not in {
            LifecycleStatus.ACCEPTED,
            LifecycleStatus.EXECUTING,
            LifecycleStatus.COMPLETED,
        }
        return CommandAcknowledgement(
            command_id=command.command_id,
            intent_id=command.intent_id,
            roster_version=snapshot.roster_version,
            drone_id=command.drone_id,
            connection_epoch=aircraft.connection_epoch,
            status=acknowledgement.status,
            reason=RefusalReason.ADAPTER_FAILURE if failed else None,
            detail=acknowledgement.detail,
        )

    def resume_after_completion(
        self,
        plan: Plan,
        pending: ExecutionResult,
        terminal_ack: CommandAcknowledgement,
        snapshot: FleetSnapshot,
        *,
        current_snapshot: SnapshotProvider | None = None,
    ) -> ExecutionResult:
        """Resume after an accepted/executing command receives a terminal ack.

        The original plan's complete target structure and its acknowledged prefix are
        proven before advancing an explicit cursor, so the waiting command is never
        resent and a partial safety plan cannot masquerade as a complete one.
        Captured-media context cannot be reconstructed from a wire acknowledgement,
        so resumption after completed capture/retrieval work is refused; camera
        implementations used by M1.2 return terminal typed results.

        The integration must authenticate ``terminal_ack`` before calling this
        method. Domain correlation is then rechecked here before any safety action.
        """
        provider = current_snapshot or (lambda: snapshot)
        current = provider()
        if (
            not isinstance(pending, ExecutionResult)
            or not isinstance(pending.acknowledgements, tuple)
            or not all(self._valid_domain_acknowledgement(ack) for ack in pending.acknowledgements)
            or not self._valid_domain_acknowledgement(terminal_ack)
        ):
            return self._invalid_resume(
                plan,
                current,
                "result or acknowledgement violates the typed resume boundary",
            )
        if pending.status is not LifecycleStatus.EXECUTING or pending.plan != plan:
            return self._invalid_resume(plan, current, "result has no waiting command")
        if plan.intent_name is IntentName.ESTOP:
            if plan.roster_version != current.roster_version:
                command_index = self._estop_completion_index(plan, pending, terminal_ack)
                if command_index is None:
                    return self._invalid_resume(
                        plan, current, "estop completion does not match a waiting command"
                    )
                acknowledgements = list(pending.acknowledgements)
                acknowledgements[command_index] = terminal_ack
                return self._invalidated_resume(
                    plan,
                    current,
                    RefusalReason.STALE_ROSTER,
                    "estop plan was invalidated by a roster change while awaiting completion",
                    acknowledgements=acknowledgements,
                )
            authorization = self.arbiter.check_plan_authorization(plan, current)
            if authorization is not None:
                return self._invalid_resume(plan, current, authorization.detail)
            return self._resume_estop(plan, pending, terminal_ack, current)

        acknowledged_count = len(pending.acknowledgements)
        if acknowledged_count == 0 or acknowledged_count > len(plan.commands):
            return self._invalid_resume(plan, current, "acknowledged prefix is invalid")
        command_index = acknowledged_count - 1
        acknowledged_commands = plan.commands[:acknowledged_count]
        if any(
            not self._ack_matches_command(ack, command)
            for ack, command in zip(pending.acknowledgements, acknowledged_commands, strict=True)
        ):
            return self._invalid_resume(plan, current, "acknowledgements do not match plan prefix")
        if any(
            ack.status is not LifecycleStatus.COMPLETED for ack in pending.acknowledgements[:-1]
        ) or pending.acknowledgements[-1].status not in {
            LifecycleStatus.ACCEPTED,
            LifecycleStatus.EXECUTING,
        }:
            return self._invalid_resume(plan, current, "plan prefix has no final waiting command")

        waiting = pending.acknowledgements[-1]
        command = plan.commands[command_index]
        if (
            terminal_ack.command_id != waiting.command_id
            or not self._ack_matches_command(terminal_ack, command)
            or terminal_ack.status
            not in {LifecycleStatus.COMPLETED, LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED}
        ):
            return self._invalid_resume(plan, current, "terminal acknowledgement is mismatched")

        prior_acks = [*pending.acknowledgements[:-1], terminal_ack]
        completed_prefix = plan.commands[: command_index + 1]
        if (
            plan.roster_version != current.roster_version
            and command_index + 1 == len(plan.commands)
            and terminal_ack.status is LifecycleStatus.COMPLETED
            and not any(
                completed.operation
                in {
                    CommandOperation.CAPTURE_PANORAMA,
                    CommandOperation.CAPTURE_PHOTO,
                    CommandOperation.RETRIEVE_MEDIA,
                }
                for completed in completed_prefix
            )
            and all(
                (aircraft := current.aircraft.get(completed.drone_id)) is not None
                and aircraft.connection_epoch == completed.connection_epoch
                and (
                    aircraft.membership is MembershipState.READY
                    or (
                        plan.intent_name in {IntentName.HOLD, IntentName.LAND, IntentName.LAND_ALL}
                        and aircraft.membership is MembershipState.DEGRADED
                        and snapshot.aircraft.get(completed.drone_id) is not None
                        and snapshot.aircraft[completed.drone_id].membership
                        is MembershipState.DEGRADED
                    )
                )
                for completed in completed_prefix
            )
        ):
            return ExecutionResult(
                intent_id=plan.intent_id,
                roster_version=current.roster_version,
                status=LifecycleStatus.COMPLETED,
                plan=plan,
                acknowledgements=tuple(prior_acks),
            )
        if plan.roster_version != current.roster_version:
            holds = self._hold_affected(
                plan,
                {completed.drone_id: completed for completed in completed_prefix},
                provider,
            )
            latest = provider()
            return self._invalidated_resume(
                plan,
                latest,
                RefusalReason.STALE_ROSTER,
                "plan was invalidated by a roster change while awaiting completion",
                acknowledgements=(*prior_acks, *holds),
            )
        if (
            current.aircraft.get(command.drone_id) is None
            or current.aircraft[command.drone_id].connection_epoch != command.connection_epoch
        ):
            holds = self._hold_affected(
                plan,
                {completed.drone_id: completed for completed in completed_prefix},
                provider,
            )
            latest = provider()
            return self._invalidated_resume(
                plan,
                latest,
                RefusalReason.STALE_CONNECTION_EPOCH,
                "plan was invalidated by an aircraft connection-epoch change",
                acknowledgements=(*prior_acks, *holds),
            )
        if terminal_ack.status in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED}:
            holds = self._hold_affected(
                plan,
                {affected.drone_id: affected for affected in completed_prefix},
                provider,
            )
            latest = provider()
            reason = terminal_ack.reason or RefusalReason.ADAPTER_FAILURE
            failure = Refusal(
                intent_id=plan.intent_id,
                roster_version=latest.roster_version,
                drone_id=command.drone_id,
                connection_epoch=command.connection_epoch,
                reason=reason,
                detail=terminal_ack.detail or reason.value,
                status=terminal_ack.status,
            )
            return ExecutionResult(
                intent_id=plan.intent_id,
                roster_version=latest.roster_version,
                status=terminal_ack.status,
                plan=plan,
                acknowledgements=(*prior_acks, *holds),
                refusal=failure,
                degraded_aircraft=(command.drone_id,),
            )
        completed_ids = frozenset(
            ack.command_id for ack in (*pending.acknowledgements[:-1], terminal_ack)
        )
        authorization = self.arbiter.check_plan_authorization(
            plan,
            current,
            completed_command_ids=completed_ids,
        )
        if authorization is not None:
            holds = self._hold_affected(
                plan,
                {completed.drone_id: completed for completed in completed_prefix},
                provider,
            )
            latest = provider()
            return self._invalidated_resume(
                plan,
                latest,
                authorization.reason,
                authorization.detail,
                acknowledgements=(*prior_acks, *holds),
            )
        if any(
            prior.operation
            in {
                CommandOperation.CAPTURE_PANORAMA,
                CommandOperation.CAPTURE_PHOTO,
                CommandOperation.RETRIEVE_MEDIA,
            }
            for prior in plan.commands[:command_index]
        ):
            return self._invalid_resume(
                plan, current, "camera media context cannot be reconstructed for resume"
            )

        if command_index + 1 == len(plan.commands):
            return ExecutionResult(
                intent_id=plan.intent_id,
                roster_version=current.roster_version,
                status=LifecycleStatus.COMPLETED,
                plan=plan,
                acknowledgements=tuple(prior_acks),
            )
        resumed = self._dispatch_checked(
            plan,
            provider,
            current,
            start_index=command_index + 1,
            prior_affected=plan.commands[: command_index + 1],
        )
        return ExecutionResult(
            intent_id=plan.intent_id,
            roster_version=resumed.roster_version,
            status=resumed.status,
            plan=plan,
            acknowledgements=(*prior_acks, *resumed.acknowledgements),
            refusal=resumed.refusal,
            capture_bundle=resumed.capture_bundle,
            degraded_aircraft=resumed.degraded_aircraft,
        )

    def _resume_estop(
        self,
        plan: Plan,
        pending: ExecutionResult,
        terminal_ack: CommandAcknowledgement,
        current: FleetSnapshot,
    ) -> ExecutionResult:
        if len(pending.acknowledgements) != len(plan.commands) or any(
            not self._ack_matches_command(ack, command)
            for ack, command in zip(pending.acknowledgements, plan.commands, strict=True)
        ):
            return self._invalid_resume(plan, current, "estop acknowledgements are incomplete")
        waiting_indexes = [
            index
            for index, ack in enumerate(pending.acknowledgements)
            if ack.status in {LifecycleStatus.ACCEPTED, LifecycleStatus.EXECUTING}
        ]
        if any(
            ack.status
            not in {
                LifecycleStatus.ACCEPTED,
                LifecycleStatus.EXECUTING,
                LifecycleStatus.COMPLETED,
            }
            for ack in pending.acknowledgements
        ):
            return self._invalid_resume(plan, current, "estop acknowledgement set has a failure")
        command_index = next(
            (
                index
                for index in waiting_indexes
                if pending.acknowledgements[index].command_id == terminal_ack.command_id
            ),
            None,
        )
        if command_index is None:
            return self._invalid_resume(plan, current, "estop completion is not pending")
        command = plan.commands[command_index]
        if (
            terminal_ack.status
            not in {LifecycleStatus.COMPLETED, LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED}
            or not self._ack_matches_command(terminal_ack, command)
            or current.aircraft.get(command.drone_id) is None
            or current.aircraft[command.drone_id].connection_epoch != command.connection_epoch
        ):
            return self._invalid_resume(plan, current, "estop completion is stale or mismatched")
        acknowledgements = list(pending.acknowledgements)
        acknowledgements[command_index] = terminal_ack
        if terminal_ack.status in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED}:
            reason = terminal_ack.reason or RefusalReason.ADAPTER_FAILURE
            return ExecutionResult(
                intent_id=plan.intent_id,
                roster_version=current.roster_version,
                status=terminal_ack.status,
                plan=plan,
                acknowledgements=tuple(acknowledgements),
                refusal=Refusal(
                    intent_id=plan.intent_id,
                    roster_version=current.roster_version,
                    drone_id=command.drone_id,
                    connection_epoch=command.connection_epoch,
                    reason=reason,
                    detail=terminal_ack.detail or reason.value,
                    status=terminal_ack.status,
                ),
                degraded_aircraft=(command.drone_id,),
            )
        status = (
            LifecycleStatus.EXECUTING
            if any(
                ack.status in {LifecycleStatus.ACCEPTED, LifecycleStatus.EXECUTING}
                for ack in acknowledgements
            )
            else LifecycleStatus.COMPLETED
        )
        return ExecutionResult(
            intent_id=plan.intent_id,
            roster_version=current.roster_version,
            status=status,
            plan=plan,
            acknowledgements=tuple(acknowledgements),
        )

    @staticmethod
    def _estop_completion_index(
        plan: Plan,
        pending: ExecutionResult,
        terminal_ack: CommandAcknowledgement,
    ) -> int | None:
        """Correlate a terminal stop ack without trusting current roster state."""
        if len(pending.acknowledgements) != len(plan.commands) or any(
            not AdapterDispatcher._ack_matches_command(ack, command)
            for ack, command in zip(pending.acknowledgements, plan.commands, strict=True)
        ):
            return None
        if any(
            ack.status
            not in {
                LifecycleStatus.ACCEPTED,
                LifecycleStatus.EXECUTING,
                LifecycleStatus.COMPLETED,
            }
            for ack in pending.acknowledgements
        ):
            return None
        command_index = next(
            (
                index
                for index, ack in enumerate(pending.acknowledgements)
                if ack.status in {LifecycleStatus.ACCEPTED, LifecycleStatus.EXECUTING}
                and ack.command_id == terminal_ack.command_id
            ),
            None,
        )
        if command_index is None:
            return None
        command = plan.commands[command_index]
        if terminal_ack.status not in {
            LifecycleStatus.COMPLETED,
            LifecycleStatus.FAILED,
            LifecycleStatus.INVALIDATED,
        } or not AdapterDispatcher._ack_matches_command(terminal_ack, command):
            return None
        return command_index

    @staticmethod
    def _ack_matches_command(acknowledgement: CommandAcknowledgement, command: Command) -> bool:
        return (
            AdapterDispatcher._valid_domain_acknowledgement(acknowledgement)
            and acknowledgement.command_id == command.command_id
            and acknowledgement.intent_id == command.intent_id
            and acknowledgement.roster_version == command.roster_version
            and acknowledgement.drone_id == command.drone_id
            and acknowledgement.connection_epoch == command.connection_epoch
        )

    @staticmethod
    def _valid_domain_acknowledgement(value: object) -> bool:
        return (
            isinstance(value, CommandAcknowledgement)
            and isinstance(value.command_id, str)
            and bool(value.command_id)
            and isinstance(value.intent_id, str)
            and bool(value.intent_id)
            and _is_nonnegative_int(value.roster_version)
            and _is_positive_int(value.drone_id)
            and _is_nonnegative_int(value.connection_epoch)
            and isinstance(value.status, LifecycleStatus)
            and (value.reason is None or isinstance(value.reason, RefusalReason))
            and isinstance(value.detail, str)
        )

    def _execute(
        self,
        command: Command,
        captures: dict[str, CaptureResult],
        provider: SnapshotProvider,
    ) -> CommandOutcome:
        operation = command.operation
        if operation is CommandOperation.TAKEOFF:
            raw = self.flight.takeoff([command.drone_id], float(command.parameters["z"]))[0]
            return self.validate_acknowledgement(command, raw, provider())
        if operation is CommandOperation.GOTO:
            raw = self.flight.goto(
                command.drone_id,
                float(command.parameters["x"]),
                float(command.parameters["y"]),
                float(command.parameters["z"]),
                float(command.parameters["speed"]),
            )
            return self.validate_acknowledgement(command, raw, provider())
        if operation is CommandOperation.ROTATE_TO:
            raw = self.flight.rotate_to(
                command.drone_id,
                float(command.parameters["yaw"]),
                float(command.parameters["speed"]),
            )
            return self.validate_acknowledgement(command, raw, provider())
        if operation is CommandOperation.HOVER:
            raw = self.flight.hover([command.drone_id])[0]
            return self.validate_acknowledgement(command, raw, provider())
        if operation is CommandOperation.LAND:
            raw = self.flight.land([command.drone_id])[0]
            return self.validate_acknowledgement(command, raw, provider())
        if operation is CommandOperation.CAMERA_CAPABILITIES:
            capabilities = self.camera.capabilities(command.drone_id)
            current = provider()
            if not _valid_camera_capabilities(capabilities):
                return self._command_refusal(
                    command,
                    current,
                    RefusalReason.ADAPTER_FAILURE,
                    "camera capabilities violate the typed result boundary",
                )
            refusal = self._camera_identity_refusal(
                command,
                actual_drone_id=capabilities.drone_id,
                actual_epoch=capabilities.connection_epoch,
                snapshot=current,
            )
            if refusal is not None:
                return refusal
            pattern = CapturePattern(str(command.parameters["pattern"]))
            if not capabilities.supports(pattern):
                return self._command_refusal(
                    command,
                    provider(),
                    RefusalReason.CAMERA_UNSUPPORTED,
                    f"camera does not support {pattern.value}",
                )
            return self._completed_ack(command, provider())
        if operation is CommandOperation.SET_GIMBAL_PITCH:
            raw = self.camera.set_gimbal_pitch(command.drone_id, float(command.parameters["pitch"]))
            return self.validate_acknowledgement(command, raw, provider())
        if operation is CommandOperation.CAMERA_READY:
            state = self.camera.ready(command.drone_id)
            current = provider()
            if not _valid_camera_state(state):
                return self._command_refusal(
                    command,
                    current,
                    RefusalReason.ADAPTER_FAILURE,
                    "camera readiness violates the typed result boundary",
                )
            refusal = self._camera_identity_refusal(
                command,
                actual_drone_id=state.drone_id,
                actual_epoch=state.connection_epoch,
                snapshot=current,
            )
            if refusal is not None:
                return refusal
            if state.state is not CameraStateCode.READY:
                reason = (
                    RefusalReason.CAMERA_UNSUPPORTED
                    if state.state is CameraStateCode.UNSUPPORTED
                    else RefusalReason.CAMERA_NOT_READY
                )
                return self._command_refusal(
                    command, provider(), reason, state.detail or state.state.value
                )
            return self._completed_ack(command, provider())
        if operation in {CommandOperation.CAPTURE_PANORAMA, CommandOperation.CAPTURE_PHOTO}:
            capture_id = str(command.parameters["capture_id"])
            if operation is CommandOperation.CAPTURE_PANORAMA:
                result = self.camera.capture_panorama(command.drone_id, capture_id)
            else:
                result = self.camera.capture_photo(command.drone_id, capture_id)
            current = provider()
            if not _valid_capture_result(result):
                return self._command_refusal(
                    command,
                    current,
                    RefusalReason.ADAPTER_FAILURE,
                    "camera capture violates the typed result boundary",
                )
            refusal = self._camera_identity_refusal(
                command,
                actual_drone_id=result.drone_id,
                actual_epoch=result.connection_epoch,
                snapshot=current,
            )
            if refusal is not None:
                return refusal
            if result.capture_id != capture_id:
                return self._command_refusal(
                    command,
                    provider(),
                    RefusalReason.ADAPTER_FAILURE,
                    "camera result capture_id does not match the request",
                )
            if result.status is not CameraResultStatus.COMPLETED:
                return self._command_refusal(
                    command,
                    provider(),
                    result.reason or RefusalReason.CAMERA_FAILURE,
                    result.detail or "camera capture failed",
                )
            captures[command.command_id] = result
            return self._completed_ack(command, provider())
        if operation is CommandOperation.RETRIEVE_MEDIA:
            source_id = str(command.parameters["source_command_id"])
            result = captures.get(source_id)
            if result is None:
                return self._command_refusal(
                    command,
                    provider(),
                    RefusalReason.DOWNLOAD_FAILURE,
                    "capture result is missing for retrieval",
                )
            media_files: list[MediaFile] = []
            for reference in result.media:
                media_result = self.camera.retrieve(command.drone_id, reference.file_id)
                current = provider()
                if not _valid_media_result(media_result):
                    return self._command_refusal(
                        command,
                        current,
                        RefusalReason.ADAPTER_FAILURE,
                        "media retrieval violates the typed result boundary",
                    )
                refusal = self._camera_identity_refusal(
                    command,
                    actual_drone_id=media_result.drone_id,
                    actual_epoch=media_result.connection_epoch,
                    snapshot=current,
                )
                if refusal is not None:
                    return refusal
                if (
                    media_result.capture_id != reference.capture_id
                    or media_result.capture_id != result.capture_id
                    or media_result.file_id != reference.file_id
                ):
                    return self._command_refusal(
                        command,
                        provider(),
                        RefusalReason.ADAPTER_FAILURE,
                        "media result does not correlate to the requested capture and file",
                    )
                if (
                    media_result.status is not CameraResultStatus.COMPLETED
                    or media_result.media_file is None
                ):
                    return self._command_refusal(
                        command,
                        provider(),
                        media_result.reason or RefusalReason.DOWNLOAD_FAILURE,
                        media_result.detail or "media retrieval failed",
                    )
                media_file = media_result.media_file
                if not _valid_media_file_boundary(media_file):
                    return self._command_refusal(
                        command,
                        provider(),
                        RefusalReason.ADAPTER_FAILURE,
                        "retrieved media violates the typed result boundary",
                    )
                if (
                    media_file.drone_id != command.drone_id
                    or media_file.connection_epoch != command.connection_epoch
                    or media_file.capture_id != reference.capture_id
                    or media_file.file_id != reference.file_id
                    or media_file.retrieval_status is not CameraResultStatus.COMPLETED
                ):
                    return self._command_refusal(
                        command,
                        provider(),
                        RefusalReason.ADAPTER_FAILURE,
                        "retrieved media identity or status does not match the requested file",
                    )
                media_files.append(media_file)
            return [self._completed_ack(command, provider())], media_files
        raise ValueError(f"dispatcher has no implementation for {operation.value}")

    def _dispatch_estop(self, plan: Plan, provider: SnapshotProvider) -> ExecutionResult:
        current = provider()
        preflight = self.arbiter.check_plan(plan, current)
        if preflight is not None:
            return self._refused(plan, current, preflight)
        try:
            raw_by_id = {ack.drone_id: ack for ack in self.flight.estop()}
        except Exception as error:
            refusal = Refusal(
                intent_id=plan.intent_id,
                roster_version=current.roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=RefusalReason.ADAPTER_FAILURE,
                detail=f"estop adapter raised {type(error).__name__}",
                status=LifecycleStatus.FAILED,
            )
            return ExecutionResult(
                intent_id=plan.intent_id,
                roster_version=current.roster_version,
                status=LifecycleStatus.FAILED,
                plan=plan,
                refusal=refusal,
                degraded_aircraft=tuple(command.drone_id for command in plan.commands),
            )

        acknowledgements = []
        for command in plan.commands:
            raw = raw_by_id.get(command.drone_id)
            after = provider()
            if raw is None:
                acknowledgement = CommandAcknowledgement(
                    command_id=command.command_id,
                    intent_id=command.intent_id,
                    roster_version=after.roster_version,
                    drone_id=command.drone_id,
                    connection_epoch=command.connection_epoch,
                    status=LifecycleStatus.FAILED,
                    reason=RefusalReason.ADAPTER_FAILURE,
                    detail="estop returned no acknowledgement for target aircraft",
                )
            else:
                acknowledgement = self.validate_acknowledgement(command, raw, after)
            acknowledgements.append(acknowledgement)
        failed = next(
            (ack for ack in acknowledgements if ack.status is not LifecycleStatus.COMPLETED),
            None,
        )
        terminal_failure = next(
            (
                ack
                for ack in acknowledgements
                if ack.status
                not in {
                    LifecycleStatus.ACCEPTED,
                    LifecycleStatus.EXECUTING,
                    LifecycleStatus.COMPLETED,
                }
            ),
            None,
        )
        if terminal_failure is not None:
            failed = terminal_failure
            refusal = Refusal(
                intent_id=plan.intent_id,
                roster_version=provider().roster_version,
                drone_id=failed.drone_id,
                connection_epoch=failed.connection_epoch,
                reason=failed.reason or RefusalReason.ADAPTER_FAILURE,
                detail=failed.detail,
                status=(
                    LifecycleStatus.REFUSED
                    if failed.reason
                    in {RefusalReason.STALE_ROSTER, RefusalReason.STALE_CONNECTION_EPOCH}
                    else LifecycleStatus.FAILED
                ),
            )
            return ExecutionResult(
                intent_id=plan.intent_id,
                roster_version=provider().roster_version,
                status=refusal.status,
                plan=plan,
                acknowledgements=tuple(acknowledgements),
                refusal=refusal,
                degraded_aircraft=(failed.drone_id,)
                if refusal.status is LifecycleStatus.FAILED
                else (),
            )
        if failed is not None:
            return ExecutionResult(
                intent_id=plan.intent_id,
                roster_version=provider().roster_version,
                status=LifecycleStatus.EXECUTING,
                plan=plan,
                acknowledgements=tuple(acknowledgements),
            )
        return ExecutionResult(
            intent_id=plan.intent_id,
            roster_version=provider().roster_version,
            status=LifecycleStatus.COMPLETED,
            plan=plan,
            acknowledgements=tuple(acknowledgements),
        )

    def _best_effort_hold(
        self,
        plan: Plan,
        failed_command: Command,
        provider: SnapshotProvider,
        *,
        fallback_snapshot: FleetSnapshot | None = None,
    ) -> list[CommandAcknowledgement]:
        try:
            current = provider()
        except Exception:
            if fallback_snapshot is None:
                raise
            # Enrichment failure after I/O must not prevent stopping that same aircraft.
            current = fallback_snapshot
        aircraft = current.aircraft.get(failed_command.drone_id)
        if aircraft is None or not aircraft.airborne:
            return []
        hold = Command(
            command_id=f"{failed_command.command_id}:safety-hold",
            intent_id=failed_command.intent_id,
            roster_version=current.roster_version,
            drone_id=failed_command.drone_id,
            connection_epoch=aircraft.connection_epoch,
            operation=CommandOperation.HOVER,
            safety_action=True,
        )
        safety_plan = Plan(
            plan_id=f"{plan.plan_id}:safety-hold:{failed_command.drone_id}",
            intent_id=plan.intent_id,
            intent_name=IntentName.HOLD,
            roster_version=current.roster_version,
            selection=(failed_command.drone_id,),
            confirmed=True,
            commands=(hold,),
            hold_scope=HoldScope.TARGETED_SAFETY,
        )
        if (
            self.arbiter.check_targeted_hold(
                safety_plan,
                current,
                required_targets=(failed_command.drone_id,),
            )
            is not None
        ):
            return []
        try:
            raw = self.flight.hover([failed_command.drone_id])[0]
        except Exception:
            return [
                CommandAcknowledgement(
                    command_id=hold.command_id,
                    intent_id=hold.intent_id,
                    roster_version=current.roster_version,
                    drone_id=hold.drone_id,
                    connection_epoch=hold.connection_epoch,
                    status=LifecycleStatus.FAILED,
                    reason=RefusalReason.ADAPTER_FAILURE,
                    detail="best-effort safety hold failed",
                )
            ]
        try:
            after = provider()
        except Exception:
            if fallback_snapshot is None:
                raise
            after = current
        return [self.validate_acknowledgement(hold, raw, after)]

    def _hold_affected(
        self,
        plan: Plan,
        affected: dict[int, Command],
        provider: SnapshotProvider,
    ) -> list[CommandAcknowledgement]:
        acknowledgements: list[CommandAcknowledgement] = []
        for drone_id in sorted(affected):
            command = affected[drone_id]
            if command.operation in {
                CommandOperation.HOVER,
                CommandOperation.LAND,
                CommandOperation.ESTOP,
            }:
                continue
            acknowledgements.extend(self._best_effort_hold(plan, command, provider))
        return acknowledgements

    @staticmethod
    def _failure_for(
        command: Command,
        snapshot: FleetSnapshot,
        reason: RefusalReason,
        detail: str,
    ) -> Refusal:
        aircraft = snapshot.aircraft.get(command.drone_id)
        return Refusal(
            intent_id=command.intent_id,
            roster_version=snapshot.roster_version,
            drone_id=command.drone_id,
            connection_epoch=(
                aircraft.connection_epoch if aircraft is not None else command.connection_epoch
            ),
            reason=reason,
            detail=detail or reason.value,
            status=LifecycleStatus.FAILED,
        )

    @staticmethod
    def _failed_ack(command: Command, failure: Refusal) -> CommandAcknowledgement:
        return CommandAcknowledgement(
            command_id=command.command_id,
            intent_id=command.intent_id,
            roster_version=failure.roster_version,
            drone_id=command.drone_id,
            connection_epoch=(
                failure.connection_epoch
                if failure.connection_epoch is not None
                else command.connection_epoch
            ),
            status=LifecycleStatus.FAILED,
            reason=failure.reason,
            detail=failure.detail,
        )

    @staticmethod
    def _completed_ack(command: Command, snapshot: FleetSnapshot) -> CommandAcknowledgement:
        aircraft = snapshot.aircraft.get(command.drone_id)
        if aircraft is None or aircraft.connection_epoch != command.connection_epoch:
            return CommandAcknowledgement(
                command_id=command.command_id,
                intent_id=command.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=command.drone_id,
                connection_epoch=(
                    aircraft.connection_epoch if aircraft is not None else command.connection_epoch
                ),
                status=LifecycleStatus.REFUSED,
                reason=RefusalReason.STALE_CONNECTION_EPOCH,
                detail="camera acknowledgement belongs to a prior connection epoch",
            )
        if snapshot.roster_version != command.roster_version:
            return CommandAcknowledgement(
                command_id=command.command_id,
                intent_id=command.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=command.drone_id,
                connection_epoch=aircraft.connection_epoch,
                status=LifecycleStatus.REFUSED,
                reason=RefusalReason.STALE_ROSTER,
                detail="camera acknowledgement belongs to a prior roster",
            )
        return CommandAcknowledgement(
            command_id=command.command_id,
            intent_id=command.intent_id,
            roster_version=snapshot.roster_version,
            drone_id=command.drone_id,
            connection_epoch=aircraft.connection_epoch,
            status=LifecycleStatus.COMPLETED,
        )

    def _camera_identity_refusal(
        self,
        command: Command,
        *,
        actual_drone_id: int,
        actual_epoch: int,
        snapshot: FleetSnapshot,
    ) -> Refusal | None:
        if not _is_positive_int(actual_drone_id) or not _is_nonnegative_int(actual_epoch):
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.ADAPTER_FAILURE,
                "camera result identity violates the typed result boundary",
            )
        if snapshot.roster_version != command.roster_version:
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.STALE_ROSTER,
                "camera result belongs to a prior roster",
            )
        aircraft = snapshot.aircraft.get(command.drone_id)
        if actual_drone_id != command.drone_id:
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.ADAPTER_FAILURE,
                "camera result carries the wrong aircraft identity",
            )
        if (
            aircraft is None
            or actual_epoch != command.connection_epoch
            or actual_epoch != aircraft.connection_epoch
        ):
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.STALE_CONNECTION_EPOCH,
                "camera result carries a prior connection epoch",
            )
        return None

    @staticmethod
    def _command_refusal(
        command: Command,
        snapshot: FleetSnapshot,
        reason: RefusalReason,
        detail: str,
    ) -> Refusal:
        aircraft = snapshot.aircraft.get(command.drone_id)
        return Refusal(
            intent_id=command.intent_id,
            roster_version=snapshot.roster_version,
            drone_id=command.drone_id,
            connection_epoch=(
                aircraft.connection_epoch if aircraft is not None else command.connection_epoch
            ),
            reason=reason,
            detail=detail,
        )

    def _refused(
        self,
        plan: Plan,
        snapshot: FleetSnapshot,
        refusal: Refusal,
        *,
        acknowledgements: Iterable[CommandAcknowledgement] = (),
        media_files: Iterable[MediaFile] = (),
        degraded: Iterable[int] = (),
    ) -> ExecutionResult:
        return ExecutionResult(
            intent_id=plan.intent_id,
            roster_version=snapshot.roster_version,
            status=LifecycleStatus.REFUSED,
            plan=plan,
            acknowledgements=tuple(acknowledgements),
            refusal=refusal,
            capture_bundle=self._failed_bundle(plan, media_files, refusal),
            degraded_aircraft=tuple(sorted(degraded)),
        )

    @staticmethod
    def _invalid_resume(plan: Plan, snapshot: FleetSnapshot, detail: str) -> ExecutionResult:
        refusal = Refusal(
            intent_id=plan.intent_id,
            roster_version=snapshot.roster_version,
            drone_id=None,
            connection_epoch=None,
            reason=RefusalReason.INVALID_RESUME,
            detail=detail,
        )
        return ExecutionResult(
            intent_id=plan.intent_id,
            roster_version=snapshot.roster_version,
            status=LifecycleStatus.REFUSED,
            plan=plan,
            refusal=refusal,
        )

    @staticmethod
    def _invalidated_resume(
        plan: Plan,
        snapshot: FleetSnapshot,
        reason: RefusalReason,
        detail: str,
        *,
        acknowledgements: Iterable[CommandAcknowledgement] = (),
    ) -> ExecutionResult:
        refusal = Refusal(
            intent_id=plan.intent_id,
            roster_version=snapshot.roster_version,
            drone_id=None,
            connection_epoch=None,
            reason=reason,
            detail=detail,
            status=LifecycleStatus.INVALIDATED,
        )
        return ExecutionResult(
            intent_id=plan.intent_id,
            roster_version=snapshot.roster_version,
            status=LifecycleStatus.INVALIDATED,
            plan=plan,
            acknowledgements=tuple(acknowledgements),
            refusal=refusal,
        )

    @staticmethod
    def _capture_metadata(plan: Plan) -> tuple[str, str, CapturePattern, int, int] | None:
        capture_command = next(
            (
                command
                for command in plan.commands
                if command.operation
                in {CommandOperation.CAPTURE_PANORAMA, CommandOperation.CAPTURE_PHOTO}
            ),
            None,
        )
        if capture_command is None:
            return None
        return (
            str(capture_command.parameters["room_id"]),
            str(capture_command.parameters["capture_id"]),
            CapturePattern(str(capture_command.parameters["pattern"])),
            capture_command.drone_id,
            capture_command.connection_epoch,
        )

    def _completed_bundle(
        self, plan: Plan, media_files: list[MediaFile]
    ) -> CaptureBundle | Refusal | None:
        metadata = self._capture_metadata(plan)
        if metadata is None:
            return None
        room_id, capture_id, pattern, drone_id, epoch = metadata
        capture_commands = tuple(
            command
            for command in plan.commands
            if command.operation
            in {CommandOperation.CAPTURE_PANORAMA, CommandOperation.CAPTURE_PHOTO}
        )
        evidence_context = _capture_evidence_context(plan, capture_commands)
        evidence_valid = evidence_context is not None and _valid_media_evidence(
            media_files,
            capture_id=capture_id,
            drone_id=drone_id,
            connection_epoch=epoch,
            approved_pose=evidence_context[0],
            pose_tolerance_m=evidence_context[1],
            expected_gimbal_pitch_deg=evidence_context[2],
            gimbal_tolerance_deg=self.arbiter.config.max_capture_gimbal_error_deg,
        )
        if pattern is CapturePattern.PANO_360:
            valid = (
                evidence_valid
                and len(media_files) == 1
                and media_files[0].intrinsics.projection == "equirectangular"
                and media_files[0].intrinsics.width_px == media_files[0].intrinsics.height_px * 2
                and media_files[0].intrinsics.horizontal_fov_deg == 360.0
            )
            coverage = CaptureCoverage.FULL_EQUIRECTANGULAR
        else:
            rotations = tuple(
                command
                for command in plan.commands
                if command.operation is CommandOperation.ROTATE_TO
            )
            yaw_order_valid = len(rotations) == len(media_files) == 8 and all(
                _yaw_matches_rotation(media_file.actual_yaw_deg, rotation.parameters)
                for media_file, rotation in zip(media_files, rotations, strict=True)
            )
            valid = (
                evidence_valid
                and yaw_order_valid
                and _has_measured_yaw_overlap(media_files, rotations)
                and all(
                    media_file.intrinsics.projection == "rectilinear" for media_file in media_files
                )
            )
            coverage = CaptureCoverage.INCOMPLETE_VERTICAL
        if not valid:
            return Refusal(
                intent_id=plan.intent_id,
                roster_version=plan.roster_version,
                drone_id=drone_id,
                connection_epoch=epoch,
                reason=RefusalReason.CAMERA_FAILURE,
                detail=f"{pattern.value} returned an invalid deterministic capture bundle",
                status=LifecycleStatus.FAILED,
            )
        return CaptureBundle(
            room_id=room_id,
            capture_id=capture_id,
            drone_id=drone_id,
            connection_epoch=epoch,
            pattern=pattern,
            coverage=coverage,
            status=CameraResultStatus.COMPLETED,
            media=tuple(media_files),
        )

    def _failed_bundle(
        self, plan: Plan, media_files: Iterable[MediaFile], refusal: Refusal
    ) -> CaptureBundle | None:
        metadata = self._capture_metadata(plan)
        if metadata is None:
            return None
        room_id, capture_id, pattern, drone_id, epoch = metadata
        coverage = (
            CaptureCoverage.FULL_EQUIRECTANGULAR
            if pattern is CapturePattern.PANO_360
            else CaptureCoverage.INCOMPLETE_VERTICAL
        )
        return CaptureBundle(
            room_id=room_id,
            capture_id=capture_id,
            drone_id=drone_id,
            connection_epoch=epoch,
            pattern=pattern,
            coverage=coverage,
            status=CameraResultStatus.FAILED,
            media=tuple(media_files),
            reason=refusal.reason,
            detail=refusal.detail,
        )


def _yaw_matches_rotation(actual_yaw: object, parameters: object) -> bool:
    if not isinstance(parameters, Mapping):
        return False
    expected = parameters.get("yaw")
    tolerance = parameters.get("tolerance")
    values = (actual_yaw, expected, tolerance)
    if any(
        isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value)
        for value in values
    ):
        return False
    if not 0 <= tolerance < 180:
        return False
    distance = abs((float(actual_yaw) - float(expected) + 180.0) % 360.0 - 180.0)
    return distance <= float(tolerance)


def _capture_evidence_context(
    plan: Plan,
    capture_commands: tuple[Command, ...],
) -> tuple[Position, float, float] | None:
    if not capture_commands:
        return None
    parameters = capture_commands[0].parameters
    approved_raw = parameters.get("approved_pose")
    tolerance = parameters.get("pose_tolerance")
    if not isinstance(approved_raw, Mapping) or not _finite_nonnegative_number(tolerance):
        return None
    try:
        approved = Position.from_mapping(approved_raw)
    except (TypeError, ValueError):
        return None
    if any(
        command.parameters.get("approved_pose") != approved_raw
        or command.parameters.get("pose_tolerance") != tolerance
        for command in capture_commands
    ):
        return None
    gimbal_commands = tuple(
        command
        for command in plan.commands
        if command.operation is CommandOperation.SET_GIMBAL_PITCH
    )
    if len(gimbal_commands) != 1:
        return None
    expected_gimbal = gimbal_commands[0].parameters.get("pitch")
    if not _finite_number(expected_gimbal):
        return None
    return approved, float(tolerance), float(expected_gimbal)


def _valid_media_evidence(
    media_files: list[MediaFile],
    *,
    capture_id: str,
    drone_id: int,
    connection_epoch: int,
    approved_pose: Position,
    pose_tolerance_m: float,
    expected_gimbal_pitch_deg: float,
    gimbal_tolerance_deg: float,
) -> bool:
    file_ids = [media_file.file_id for media_file in media_files]
    timestamps = [media_file.timestamp_ms for media_file in media_files]
    if (
        any(not isinstance(file_id, str) or not file_id for file_id in file_ids)
        or len(set(file_ids)) != len(file_ids)
        or any(
            not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp <= 0
            for timestamp in timestamps
        )
        or any(later <= earlier for earlier, later in zip(timestamps, timestamps[1:], strict=False))
    ):
        return False
    for media_file in media_files:
        intrinsics = media_file.intrinsics
        if (
            media_file.capture_id != capture_id
            or media_file.drone_id != drone_id
            or media_file.connection_epoch != connection_epoch
            or media_file.retrieval_status is not CameraResultStatus.COMPLETED
            or not isinstance(media_file.pose, Position)
            or media_file.pose.distance_to(approved_pose) > pose_tolerance_m
            or not _finite_number(media_file.actual_yaw_deg)
            or not _finite_number(media_file.gimbal_pitch_deg)
            or abs(float(media_file.gimbal_pitch_deg) - expected_gimbal_pitch_deg)
            > gimbal_tolerance_deg
            or not isinstance(media_file.checksum_sha256, str)
            or len(media_file.checksum_sha256) != 64
            or any(character not in hexdigits for character in media_file.checksum_sha256)
            or not isinstance(media_file.storage_ref, str)
            or not media_file.storage_ref
            or not isinstance(intrinsics, CameraIntrinsics)
            or not isinstance(intrinsics.width_px, int)
            or isinstance(intrinsics.width_px, bool)
            or intrinsics.width_px <= 0
            or not isinstance(intrinsics.height_px, int)
            or isinstance(intrinsics.height_px, bool)
            or intrinsics.height_px <= 0
            or not _finite_number(intrinsics.horizontal_fov_deg)
            or not 0 < intrinsics.horizontal_fov_deg <= 360
            or not isinstance(intrinsics.projection, str)
            or not intrinsics.projection
        ):
            return False
    return True


def _has_measured_yaw_overlap(
    media_files: list[MediaFile],
    rotations: tuple[Command, ...],
) -> bool:
    if len(media_files) != len(rotations) or not media_files:
        return False
    samples: list[tuple[float, float, float]] = []
    for media_file, rotation in zip(media_files, rotations, strict=True):
        minimum_overlap = rotation.parameters.get("min_overlap")
        field_of_view = media_file.intrinsics.horizontal_fov_deg
        if (
            not _finite_positive_number(minimum_overlap)
            or minimum_overlap >= 180
            or not _finite_number(media_file.actual_yaw_deg)
            or not _finite_number(field_of_view)
            or not 0 < field_of_view < 180
        ):
            return False
        samples.append(
            (
                float(media_file.actual_yaw_deg) % 360.0,
                float(field_of_view),
                float(minimum_overlap),
            )
        )
    samples.sort(key=lambda sample: sample[0])
    if len({sample[0] for sample in samples}) != len(samples):
        return False
    for index, (yaw, field_of_view, minimum_overlap) in enumerate(samples):
        next_yaw, next_field_of_view, next_minimum_overlap = samples[(index + 1) % len(samples)]
        gap = (next_yaw - yaw) % 360.0
        measured_overlap = (field_of_view + next_field_of_view) / 2.0 - gap
        if measured_overlap < max(minimum_overlap, next_minimum_overlap):
            return False
    return True


def _finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value)


def _finite_nonnegative_number(value: object) -> bool:
    return _finite_number(value) and value >= 0


def _finite_positive_number(value: object) -> bool:
    return _finite_number(value) and value > 0


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_adapter_acknowledgement(value: object) -> bool:
    return (
        isinstance(value, AdapterAcknowledgement)
        and _is_positive_int(value.drone_id)
        and _is_nonnegative_int(value.connection_epoch)
        and isinstance(value.operation, CommandOperation)
        and isinstance(value.status, LifecycleStatus)
        and isinstance(value.detail, str)
    )


def _valid_camera_capabilities(value: object) -> bool:
    return (
        isinstance(value, CameraCapabilities)
        and _is_positive_int(value.drone_id)
        and _is_nonnegative_int(value.connection_epoch)
        and isinstance(value.native_panorama_modes, tuple)
        and all(isinstance(mode, str) and bool(mode) for mode in value.native_panorama_modes)
        and isinstance(value.photo_capture, bool)
        and _finite_number(value.gimbal_pitch_min_deg)
        and _finite_number(value.gimbal_pitch_max_deg)
        and value.gimbal_pitch_min_deg < value.gimbal_pitch_max_deg
        and _finite_positive_number(value.horizontal_fov_deg)
        and value.horizontal_fov_deg <= 360
        and _is_nonnegative_int(value.storage_remaining_bytes)
        and isinstance(value.media_retrieval, bool)
    )


def _valid_camera_state(value: object) -> bool:
    return (
        isinstance(value, CameraState)
        and _is_positive_int(value.drone_id)
        and _is_nonnegative_int(value.connection_epoch)
        and isinstance(value.state, CameraStateCode)
        and isinstance(value.detail, str)
    )


def _valid_media_reference(value: object) -> bool:
    return (
        isinstance(value, MediaReference)
        and isinstance(value.capture_id, str)
        and bool(value.capture_id)
        and isinstance(value.file_id, str)
        and bool(value.file_id)
    )


def _valid_capture_result(value: object) -> bool:
    return (
        isinstance(value, CaptureResult)
        and _is_positive_int(value.drone_id)
        and _is_nonnegative_int(value.connection_epoch)
        and isinstance(value.capture_id, str)
        and bool(value.capture_id)
        and isinstance(value.status, CameraResultStatus)
        and isinstance(value.media, tuple)
        and all(
            _valid_media_reference(reference) and reference.capture_id == value.capture_id
            for reference in value.media
        )
        and (value.reason is None or isinstance(value.reason, RefusalReason))
        and isinstance(value.detail, str)
    )


def _valid_camera_intrinsics(value: object) -> bool:
    return (
        isinstance(value, CameraIntrinsics)
        and _is_positive_int(value.width_px)
        and _is_positive_int(value.height_px)
        and _finite_positive_number(value.horizontal_fov_deg)
        and value.horizontal_fov_deg <= 360
        and isinstance(value.projection, str)
        and bool(value.projection)
    )


def _valid_media_file_boundary(value: object) -> bool:
    return (
        isinstance(value, MediaFile)
        and isinstance(value.capture_id, str)
        and bool(value.capture_id)
        and isinstance(value.file_id, str)
        and bool(value.file_id)
        and _is_nonnegative_int(value.timestamp_ms)
        and _is_positive_int(value.drone_id)
        and _is_nonnegative_int(value.connection_epoch)
        and isinstance(value.pose, Position)
        and _finite_number(value.actual_yaw_deg)
        and _finite_number(value.gimbal_pitch_deg)
        and _valid_camera_intrinsics(value.intrinsics)
        and isinstance(value.checksum_sha256, str)
        and isinstance(value.storage_ref, str)
        and isinstance(value.retrieval_status, CameraResultStatus)
    )


def _valid_media_result(value: object) -> bool:
    return (
        isinstance(value, MediaResult)
        and _is_positive_int(value.drone_id)
        and _is_nonnegative_int(value.connection_epoch)
        and isinstance(value.capture_id, str)
        and bool(value.capture_id)
        and isinstance(value.file_id, str)
        and bool(value.file_id)
        and isinstance(value.status, CameraResultStatus)
        and (value.media_file is None or _valid_media_file_boundary(value.media_file))
        and (value.reason is None or isinstance(value.reason, RefusalReason))
        and isinstance(value.detail, str)
    )

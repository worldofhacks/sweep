from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

from adapters.dji_mini3.remote import CommandRequest, NodeLink
from adapters.ohmni.dispatcher import GroundCommandDispatcher
from adapters.protocols import AdapterError
from planner.ground_navigation import (
    GroundNavigationDeployment,
    GroundNavigationPlan,
    GroundNavigationPose,
)
from planner.models import CommandOperation, ExecutionResult, LifecycleStatus, Plan
from planner.navigation_authorization import content_digest
from relay.ground_navigation_identity import GroundNavigationIdentityStore
from relay.intent_v1 import IntentName, IntentV1, Mode
from relay.navigation_service import state_projection

if TYPE_CHECKING:
    from relay.app import RelayRuntime
    from relay.session import RelaySession


@dataclass(frozen=True, slots=True)
class PreparedGroundNavigation:
    intent: IntentV1
    plan: GroundNavigationPlan
    execution: dict
    expires_at: int
    identity_generations: tuple[tuple[int, int], ...]


class GroundPlatformNavigation:
    def __init__(
        self, runtime: RelayRuntime, session_id: str, deployment: GroundNavigationDeployment
    ) -> None:
        self.runtime, self.session_id, self.deployment = runtime, session_id, deployment
        self.identities = GroundNavigationIdentityStore(session_id)
        self._reviews: dict[str, PreparedGroundNavigation] = {}
        self._lock = Lock()

    @property
    def session(self) -> RelaySession:
        return self.runtime.sessions[self.session_id]

    def _state(self) -> dict:
        return self.session.current_state()

    def _poses(
        self, state: dict, expected_generations: Mapping[int, int] | None = None
    ) -> tuple[tuple[GroundNavigationPose, ...], tuple[tuple[int, int], ...]]:
        platform = self.runtime.platform_services
        if platform is None:
            raise ValueError("qualified ground world observations are unavailable")
        platform.require_current()
        deployment = self.deployment
        records = platform.observations.positions(
            self.session.session_id,
            {
                "reference": dict(deployment.map_reference),
                "mapVersion": deployment.map_version,
                "floorId": deployment.floor_id,
            },
            state,
        )["observations"]
        by_device = {record["deviceId"]: record for record in records}
        poses = []
        generations = []
        for node in state["drones"]:
            if node.get("node_type") != "ground":
                continue
            device_id, epoch = node["drone_id"], node["connection_epoch"]
            device = deployment.device(device_id)
            record = by_device.get(device_id)
            if record is None or record["connectionEpoch"] != epoch:
                raise ValueError("every admitted ground node needs a current qualified world pose")
            registration = platform.observations.registrations[record["sourceId"]]
            if (
                record["sourceId"] != device.world_pose_source_id
                or registration.transform_id != device.registration_id
            ):
                raise ValueError("ground world registration differs from its approved deployment")
            readiness = node.get("ground_readiness")
            if not isinstance(readiness, Mapping) or not isinstance(
                readiness.get("source_id"), str
            ):
                raise ValueError("ground local readiness identity is unavailable")
            generation = self.identities.require(
                device_id,
                epoch,
                device.identity_source_id,
                {
                    "odom_origin_id": device.odom_origin_id,
                    "pose_source_id": device.pose_source_id,
                    "registration_id": device.registration_id,
                    "configuration_sha256": deployment.configuration_sha256,
                },
                now_ms=self.runtime.clock(),
                max_age_ms=device.limits.pose_max_age_ms,
                expected_generation=(
                    None if expected_generations is None else expected_generations.get(device_id)
                ),
            )
            generations.append((device_id, generation))
            poses.append(
                GroundNavigationPose(
                    device_id,
                    epoch,
                    record["position"]["x"],
                    record["position"]["y"],
                    record["tCapture"],
                    record["sourceId"],
                    registration.transform_id,
                    device.odom_origin_id,
                )
            )
        return tuple(poses), tuple(generations)

    def preview(self, preview: Mapping[str, object]) -> dict:
        state = self._state()
        projection = state_projection(state)
        selected = preview.get("selected")
        if (
            not isinstance(selected, list)
            or not selected
            or selected != projection["selected"]
            or any(target["deviceClass"] != "ground_vehicle" for target in selected)
        ):
            raise ValueError("ground navigation requires the current ground-only selection")
        now = self.runtime.clock()
        expires_at = preview.get("expiresAt")
        if type(expires_at) is not int or expires_at <= now:
            raise ValueError("ground navigation review expired")
        destination = preview.get("destination")
        if not isinstance(destination, Mapping) or not isinstance(destination.get("zoneId"), str):
            raise ValueError("ground navigation requires a named destination")
        map_ref = preview.get("map")
        expected_pin = {
            "version": self.deployment.map_version,
            "contentSha256": self.deployment.map_reference["contentHash"],
        }
        if (
            not isinstance(map_ref, Mapping)
            or map_ref.get("mapPin") != expected_pin
            or map_ref.get("floorId") != self.deployment.floor_id
            or map_ref.get("mapId") != self.deployment.map_reference["bundleId"]
        ):
            raise ValueError("ground deployment differs from the active approved map")
        self._check_state(state, tuple(target["id"] for target in selected))
        poses, generations = self._poses(state)
        plan = self.deployment.prepare(
            destination["zoneId"],
            poses,
            tuple(target["id"] for target in selected),
            session=self.session.session_id,
            roster_version=state["roster_version"],
            now_ms=now,
        )
        targets = {target["id"]: target for target in selected}
        routes = []
        for route in plan.routes:
            points = [
                {
                    "xM": point.x_m,
                    "yM": point.y_m,
                    "zM": 0.0,
                    "floorId": self.deployment.floor_id,
                    "frame": "world",
                }
                for point in route.points
            ]
            routes.append(
                {
                    "target": targets[route.device_id],
                    "waypoints": points,
                    "arrivalSlot": {
                        "slotId": f"{plan.zone_id}-ground-{route.device_id}",
                        "zoneId": plan.zone_id,
                        "position": points[-1],
                    },
                    "holdBehavior": "stop",
                }
            )
        execution = {
            "planHash": content_digest(
                {
                    "planId": plan.plan_id,
                    "routes": routes,
                    "rosterVersion": plan.roster_version,
                    "session": plan.session,
                    "configurationSha256": plan.configuration_sha256,
                }
            ),
            "mapPin": expected_pin,
            "geometryPin": dict(map_ref["geometryPin"]),
            "navigationPin": {
                "version": f"ground-{plan.configuration_sha256[:16]}",
                "contentSha256": plan.configuration_sha256,
            },
            "approvalId": self.deployment.approval_id,
            "configurationSha256": plan.configuration_sha256,
            "permissionZoneIds": [plan.zone_id],
        }
        intent = IntentV1(
            v=1,
            t=now,
            type="intent",
            intent_id=f"platform:{preview['previewId']}",
            retry_of=None,
            source="platform",
            session=self.session.session_id,
            name=IntentName.NAVIGATE,
            args={"zone_id": plan.zone_id},
            selection=plan.selected,
            mode=Mode.INDOOR,
            confirm=True,
        )
        with self._lock:
            self._prune(now)
            if len(self._reviews) >= 128:
                raise ValueError("ground navigation review capacity reached")
            prepared = PreparedGroundNavigation(
                intent,
                plan,
                json.loads(json.dumps(execution)),
                min(expires_at, plan.expires_at),
                generations,
            )
            self.revalidate(prepared)
            self._reviews[preview["previewId"]] = prepared
        return {
            "routes": routes,
            "execution": execution,
            "outcomes": [
                {
                    "target": target,
                    "status": "planned",
                    "code": "route_qualified",
                    "detail": "The signed ground deployment qualified this route.",
                }
                for target in selected
            ],
        }

    def take(self, preview: Mapping[str, object]) -> PreparedGroundNavigation:
        with self._lock:
            self._prune(self.runtime.clock())
            prepared = self._reviews.pop(preview.get("previewId"), None)
        if prepared is None or preview.get("execution") != prepared.execution:
            raise ValueError("retained ground navigation plan is unavailable")
        self.revalidate(prepared)
        return replace(prepared, intent=replace(prepared.intent, t=self.runtime.clock()))

    def _prune(self, now: int) -> None:
        for identity, prepared in tuple(self._reviews.items()):
            if prepared.expires_at <= now:
                del self._reviews[identity]

    @staticmethod
    def _check_state(state: dict, selected: tuple[int, ...]) -> None:
        projected = state_projection(state)
        if (
            state.get("estop") is True
            or state.get("mode") != "indoor"
            or tuple(target["id"] for target in projected["selected"]) != selected
        ):
            raise ValueError("ground navigation control state changed")
        targets = {node["target"]["id"]: node for node in projected["nodes"]}
        if any(
            device not in targets
            or not targets[device]["ready"]
            or not targets[device]["authority"]
            or "navigate" not in targets[device]["capabilities"]
            for device in selected
        ):
            reasons = {
                device: targets.get(device, {}).get("readinessReasons") for device in selected
            }
            raise ValueError(f"selected ground navigation targets are no longer ready: {reasons}")

    def revalidate(
        self,
        prepared: PreparedGroundNavigation,
        *,
        completed: tuple[int, ...] = (),
        active_device_id: int | None = None,
    ) -> None:
        state = self._state()
        try:
            self._check_state(state, prepared.plan.selected)
            self.deployment.revalidate(
                prepared.plan,
                self._poses(state, dict(prepared.identity_generations))[0],
                prepared.plan.selected,
                session=self.session.session_id,
                roster_version=state["roster_version"],
                now_ms=self.runtime.clock(),
                completed=completed,
                active_device_id=active_device_id,
            )
        except (ValueError, OSError):
            self.deployment.cancel(prepared.plan)
            raise

    def dispatch(
        self,
        prepared: PreparedGroundNavigation,
        link: NodeLink,
        check_cancelled: Callable[[], None],
        *,
        send_stop: Callable[[CommandRequest], None],
    ) -> ExecutionResult:
        completed: tuple[int, ...] = ()
        results = []
        for route in prepared.plan.routes:
            check_cancelled()
            self.revalidate(prepared, completed=completed)

            def guard(moving: bool, *, completed=completed, device_id=route.device_id) -> None:
                check_cancelled()
                self.revalidate(
                    prepared,
                    completed=completed,
                    active_device_id=device_id if moving else None,
                )

            device = self.deployment.device(route.device_id)
            guarded = _GroundGuardedLink(link, self.session, guard, send_stop)
            result = GroundCommandDispatcher(
                guarded,
                acknowledgement_timeout_ms=device.limits.route_timeout_ms,
                command_deadline_ms=device.limits.route_timeout_ms,
            ).dispatch_navigation(
                replace(prepared.intent, selection=(route.device_id,)),
                self._state(),
                route_id=f"{prepared.plan.plan_id}:{route.device_id}",
                navigation_route=self.deployment.route_record(
                    prepared.plan, route.device_id, now_ms=self.runtime.clock()
                ),
            )
            results.append(result)
            if result.status is not LifecycleStatus.COMPLETED:
                self.deployment.cancel(prepared.plan)
                break
            completed += (route.device_id,)
            self.revalidate(prepared, completed=completed)
        last = results[-1]
        return replace(
            last,
            plan=Plan(
                plan_id=prepared.plan.plan_id,
                intent_id=prepared.intent.intent_id,
                intent_name=IntentName.NAVIGATE,
                roster_version=prepared.plan.roster_version,
                selection=prepared.plan.selected,
                confirmed=True,
                commands=tuple(
                    command
                    for result in results
                    if result.plan is not None
                    for command in result.plan.commands
                ),
            ),
            acknowledgements=tuple(ack for result in results for ack in result.acknowledgements),
        )


class _GroundGuardedLink:
    def __init__(
        self,
        inner: NodeLink,
        session: RelaySession,
        guard: Callable[[bool], None],
        send_stop: Callable[[CommandRequest], None],
    ) -> None:
        self.inner, self.session, self.guard = inner, session, guard
        self.send_stop = send_stop
        self.request: CommandRequest | None = None

    def connection_epoch(self, device_id: int) -> int | None:
        return self.inner.connection_epoch(device_id)

    def send(self, request: CommandRequest) -> None:
        self.guard(False)
        self.request = request
        try:
            self.inner.send(request)
        except BaseException:
            try:
                self._stop()
            finally:
                self.session.discard_command_waiter(request.command_id)
            raise

    def await_acknowledgement(self, command_id: str, *, timeout_ms: int):
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            while (remaining := deadline - time.monotonic()) > 0:
                self.guard(True)
                acknowledgement = self.session.await_command_acknowledgement(
                    command_id,
                    timeout_ms=min(100, max(1, int(remaining * 1000))),
                    retain_on_timeout=True,
                )
                if acknowledgement is not None:
                    return acknowledgement
            try:
                self._stop()
            finally:
                self.session.discard_command_waiter(command_id)
            return None
        except BaseException as error:
            try:
                self._stop()
            finally:
                self.session.discard_command_waiter(command_id)
            if isinstance(error, (ValueError, OSError)):
                raise AdapterError(str(error)) from error
            raise

    def _stop(self) -> None:
        if self.request is not None:
            request, self.request = self.request, None
            identity = f"ground-stop-{uuid4().hex}"
            stop = replace(
                request,
                command_id=identity,
                intent_id=identity,
                operation=CommandOperation.HOVER,
                roster_version=self.session.registry.roster_version,
                args={},
            )
            try:
                self.send_stop(stop)
            finally:
                self.session.discard_command_waiter(stop.command_id)

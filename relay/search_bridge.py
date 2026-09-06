from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import asdict
from threading import RLock

from perception.object_detection import DetectionCandidate, FrameIdentity, SightingEvent
from planner.models import AircraftState, FleetSnapshot
from planner.navigation import Pose
from relay.auth import Principal
from relay.control_runtime import ControlRuntimeConfig
from relay.perception_ingress import (
    DetectionDroneState,
    DetectionIngress,
    DetectionIngressConfig,
    DetectionSourcePin,
    TrustedCapturePose,
)
from relay.search_runtime import SearchRuntime


class SearchBridge:
    def __init__(
        self,
        session_id: str,
        search: SearchRuntime,
        control: ControlRuntimeConfig,
        camera_ids: Mapping[int, str],
    ) -> None:
        self.session_id = session_id
        self.search = search
        self.control = control
        self.camera_ids = dict(camera_ids)
        self._poses: dict[int, deque[tuple[int, AircraftState]]] = {}
        self._ingresses: dict[str, DetectionIngress] = {}
        self._accepted_frame_keys: dict[str, set[tuple[str, str, str]]] = {}
        self._accepted_frame_order: dict[str, deque[tuple[str, str, str]]] = {}
        self._lock = RLock()

    def observe_snapshot(self, snapshot: FleetSnapshot) -> None:
        with self._lock:
            for aircraft in snapshot.aircraft.values():
                provenance = aircraft.control_provenance
                if provenance is None or provenance.reason != "ready":
                    continue
                history = self._poses.setdefault(aircraft.drone_id, deque(maxlen=64))
                if history and history[-1][0] == snapshot.now_ms:
                    continue
                history.append((snapshot.now_ms, aircraft))

    def consume(
        self, raw: Mapping[str, object], principal: Principal, now_ms: int
    ) -> dict[str, object]:
        with self._lock:
            drone_id = raw.get("drone_id")
            if type(drone_id) is not int:
                return {"type": "perception_result", "accepted": False, "reason": "invalid_drone"}
            active = self.search.active_mission(drone_id)
            if active is None:
                return {
                    "type": "perception_result",
                    "accepted": False,
                    "reason": "no_active_search",
                }
            intent_id, preview = active
            if raw.get("type") == "perception.frame_processed":
                capture_ms = raw.get("capture_timestamp_ms")
                if type(capture_ms) is int:
                    height_reason = self._capture_height_reason(drone_id, capture_ms)
                    if height_reason is not None:
                        return {
                            "type": "perception_result",
                            "intent_id": intent_id,
                            "accepted": False,
                            "reason": height_reason,
                        }
            ingress = self._ingresses.get(intent_id)
            if ingress is None:
                pins = {}
                for assignment in preview.search.assignments:
                    target = assignment.drone.drone.drone_id
                    control = self.control.pins.get(target)
                    camera_id = self.camera_ids.get(target)
                    if control is None or camera_id is None:
                        return {
                            "type": "perception_result",
                            "accepted": False,
                            "reason": "camera_unconfigured",
                        }
                    pins[target] = DetectionSourcePin(
                        target,
                        self.search.config.source_by_drone[target],
                        camera_id,
                        control.camera_calibration_id,
                        intent_id,
                        preview.search.mission.frame_mission_id,
                        control.clock_mapping,
                    )
                ingress = DetectionIngress(
                    DetectionIngressConfig(self.session_id, pins),
                    self.search,
                    self._current_drone,
                    self._capture_pose,
                )
                if len(self._ingresses) >= 32:
                    self._ingresses.pop(next(iter(self._ingresses)))
                self._ingresses[intent_id] = ingress
            if (
                raw.get("type") == "perception.sighting"
                and raw.get("label") != preview.search.target_class
            ):
                return {
                    "type": "perception_result",
                    "intent_id": intent_id,
                    "accepted": False,
                    "reason": "target_class_mismatch",
                }
            result = ingress.consume(raw, principal, now_ms)
            payload = {
                "type": "perception_result",
                "intent_id": intent_id,
                "accepted": result.accepted,
                "reason": result.reason,
            }
            if result.observation is not None:
                payload["coverage"] = result.observation.payload()
            if raw.get("type") == "perception.frame_processed" and result.accepted:
                self._remember_frame(intent_id, self._frame_key(raw))
            if raw.get("type") == "perception.sighting" and result.accepted:
                frame_key = self._frame_key(raw)
                if frame_key not in self._accepted_frame_keys.get(intent_id, set()):
                    payload["accepted"] = False
                    payload["reason"] = "unverified_frame"
                    return payload
                candidate = self.search.observe_sighting(intent_id, self._sighting(raw, frame_key))
                if candidate is None:
                    payload["accepted"] = False
                    payload["reason"] = "unverified_frame"
                else:
                    payload["candidate"] = candidate.payload()
            return payload

    def _remember_frame(self, intent_id: str, frame_key: tuple[str, str, str]) -> None:
        keys = self._accepted_frame_keys.setdefault(intent_id, set())
        order = self._accepted_frame_order.setdefault(intent_id, deque())
        if frame_key in keys:
            return
        if len(order) == 8_192:
            keys.remove(order.popleft())
        keys.add(frame_key)
        order.append(frame_key)

    @staticmethod
    def _frame_key(raw: Mapping[str, object]) -> tuple[str, str, str]:
        source_id = raw.get("source_id")
        frame_id = raw.get("frame_id")
        mission_id = raw.get("mission_id")
        if not all(isinstance(value, str) and value for value in (source_id, frame_id, mission_id)):
            raise ValueError("detector frame identity is invalid")
        return source_id, frame_id, mission_id

    @staticmethod
    def _sighting(raw: Mapping[str, object], frame_key: tuple[str, str, str]) -> SightingEvent:
        source_id, frame_id, mission_id = frame_key
        worker_run_id = raw["worker_run_id"]
        frame_sequence = raw["frame_sequence"]
        identity = (
            FrameIdentity(source_id, frame_id, mission_id)
            if worker_run_id == "legacy" and frame_sequence == 1
            else FrameIdentity(source_id, mission_id, worker_run_id, frame_sequence)
        )
        return SightingEvent(
            raw["sighting_id"],
            identity,
            raw["first_frame_decoded_at_monotonic_ms"] / 1_000,
            raw["last_frame_decoded_at_monotonic_ms"] / 1_000,
            raw["evaluation_started_at_monotonic_ms"] / 1_000,
            raw["evaluation_completed_at_monotonic_ms"] / 1_000,
            DetectionCandidate(
                raw["label"], raw["class_id"], raw["confidence"], tuple(raw["bbox_xyxy"])
            ),
            raw["observation_count"],
            raw["detector_config_sha256"],
        )

    def _current_drone(self, drone_id: int) -> DetectionDroneState | None:
        active = self.search.active_mission(drone_id)
        history = self._poses.get(drone_id)
        if active is None or not history:
            return None
        return DetectionDroneState(
            drone_id, history[-1][1].connection_epoch, active[1].search.mission.frame_mission_id
        )

    def _capture_height_reason(self, drone_id: int, capture_ms: int) -> str | None:
        history = self._poses.get(drone_id, ())
        if not history:
            return "camera_height_unverified"
        candidates = self._capture_candidates(drone_id, history[-1][1].connection_epoch)
        if not candidates:
            return "camera_height_unverified"
        _, aircraft = min(
            candidates,
            key=lambda item: abs(item[1].control_provenance.evaluated_at_relay_ms - capture_ms),
        )
        return self.search.camera_height_reason(aircraft.pose.z)

    def _capture_candidates(
        self, drone_id: int, connection_epoch: int
    ) -> list[tuple[int, AircraftState]]:
        return [
            (observed, aircraft)
            for observed, aircraft in self._poses.get(drone_id, ())
            if aircraft.connection_epoch == connection_epoch
            and aircraft.control_provenance is not None
            and aircraft.control_provenance.evaluated_at_relay_ms is not None
        ]

    def _capture_pose(
        self, state: DetectionDroneState, identity: FrameIdentity, capture_ms: int
    ) -> TrustedCapturePose | None:
        candidates = [
            (observed, aircraft)
            for observed, aircraft in self._capture_candidates(
                state.drone_id, state.connection_epoch
            )
            if self.search.camera_height_reason(aircraft.pose.z) is None
        ]
        if not candidates:
            return None
        observed, aircraft = min(
            candidates,
            key=lambda item: abs(item[1].control_provenance.evaluated_at_relay_ms - capture_ms),
        )
        provenance = aircraft.control_provenance
        return TrustedCapturePose(
            identity,
            state.connection_epoch,
            Pose(
                aircraft.pose.x,
                aircraft.pose.y,
                aircraft.pose.z,
                self.search.navigation.config.floor_id,
            ),
            provenance.evaluated_at_relay_ms,
            observed,
            provenance,
        )


def search_progress(search: SearchRuntime, intent_id: str) -> dict[str, object]:
    status = search.status(intent_id)
    return {
        "type": "search_progress",
        "intent_id": intent_id,
        "state": status.state,
        "tasks": [asdict(task) for task in status.tasks],
    }

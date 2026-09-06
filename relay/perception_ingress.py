"""Verify detector frames and bind coverage to relay-owned control pose evidence."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType

from perception.object_detection import (
    DetectionCandidate,
    FrameIdentity,
    ProcessedFrameEvent,
    SightingEvent,
)
from perception.search_events import CoverageObservation, FramePoseEvidence
from planner.control_provenance import ControlProvenance
from planner.navigation import Pose
from relay.auth import Principal, verify_event_signature
from relay.control_localization import ClockMapping
from relay.search_runtime import SearchRuntime


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def _integer(value: object, name: str, *, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise ValueError(f"{name} must be an integer")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


@dataclass(frozen=True, slots=True)
class DetectionDroneState:
    drone_id: int
    connection_epoch: int
    mission_id: str

    def __post_init__(self) -> None:
        _integer(self.drone_id, "drone_id", allow_zero=False)
        _integer(self.connection_epoch, "connection_epoch")
        _text(self.mission_id, "mission_id")


@dataclass(frozen=True, slots=True)
class DetectionSourcePin:
    drone_id: int
    source_id: str
    camera_id: str
    camera_calibration_id: str
    intent_id: str
    mission_id: str
    clock_mapping: ClockMapping

    def __post_init__(self) -> None:
        _integer(self.drone_id, "drone_id", allow_zero=False)
        for name in (
            "source_id",
            "camera_id",
            "camera_calibration_id",
            "intent_id",
            "mission_id",
        ):
            _text(getattr(self, name), name)
        if not isinstance(self.clock_mapping, ClockMapping) or not self.clock_mapping.measured:
            raise ValueError("detection source pin requires a measured clock mapping")


@dataclass(frozen=True, slots=True)
class DetectionIngressConfig:
    session: str
    sources: Mapping[int, DetectionSourcePin]
    max_pose_skew_ms: int = 200
    max_frame_age_ms: int = 500
    max_seen_events: int = 8192

    def __post_init__(self) -> None:
        _text(self.session, "session")
        _integer(self.max_pose_skew_ms, "max_pose_skew_ms")
        _integer(self.max_frame_age_ms, "max_frame_age_ms", allow_zero=False)
        _integer(self.max_seen_events, "max_seen_events", allow_zero=False)
        sources = dict(self.sources)
        if not sources or any(drone_id != pin.drone_id for drone_id, pin in sources.items()):
            raise ValueError("detection source pins must be keyed by drone id")
        if len({pin.source_id for pin in sources.values()}) != len(sources):
            raise ValueError("detection source ids must be unique")
        object.__setattr__(self, "sources", MappingProxyType(sources))


@dataclass(frozen=True, slots=True)
class TrustedCapturePose:
    """Relay-owned pose matched to a capture timestamp by control-localization evidence."""

    identity: FrameIdentity
    connection_epoch: int
    pose: Pose
    pose_timestamp_ms: int
    observed_at_ms: int
    provenance: ControlProvenance

    def __post_init__(self) -> None:
        _integer(self.connection_epoch, "connection_epoch")
        _integer(self.pose_timestamp_ms, "pose_timestamp_ms")
        _integer(self.observed_at_ms, "observed_at_ms")
        if not isinstance(self.provenance, ControlProvenance):
            raise ValueError("trusted capture pose requires control provenance")

    def frame_evidence(self) -> FramePoseEvidence:
        return FramePoseEvidence(
            self.identity,
            self.connection_epoch,
            self.pose,
            self.pose_timestamp_ms / 1000,
            self.observed_at_ms / 1000,
        )


@dataclass(frozen=True, slots=True)
class DetectionIngressResult:
    accepted: bool
    reason: str
    observation: CoverageObservation | None = None


class DetectionIngress:
    """Turns authenticated perception frames into coverage observations.

    The callback supplies relay-owned control-localization evidence, never client pose fields.
    """

    def __init__(
        self,
        config: DetectionIngressConfig,
        runtime: SearchRuntime,
        current_drone: Callable[[int], DetectionDroneState | None],
        capture_pose: Callable[
            [DetectionDroneState, FrameIdentity, int], TrustedCapturePose | None
        ],
    ) -> None:
        self.config = config
        self._runtime = runtime
        self._current_drone = current_drone
        self._capture_pose = capture_pose
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._last_processed_capture_ms: dict[int, int] = {}

    def consume(self, raw: object, principal: Principal, now_ms: int) -> DetectionIngressResult:
        try:
            now_ms = _integer(now_ms, "now_ms")
            unsigned, signature = self._unsigned(raw)
            if principal.source != "perception" or principal.drone_id is not None:
                return DetectionIngressResult(False, "authentication_mismatch")
            if not verify_event_signature(unsigned, signature, principal.signing_key):
                return DetectionIngressResult(False, "invalid_signature")
            return self._consume_verified(unsigned, now_ms)
        except (KeyError, TypeError, ValueError):
            return DetectionIngressResult(False, "invalid_payload")

    def _unsigned(self, raw: object) -> tuple[dict[str, object], str]:
        if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
            raise ValueError("perception frame must be an object")
        signature = raw.get("signature")
        if not isinstance(signature, str):
            raise ValueError("perception frame has no signature")
        unsigned = dict(raw)
        del unsigned["signature"]
        event_type = unsigned.get("type")
        required = (
            _PROCESSED_FIELDS if event_type == "perception.frame_processed" else _SIGHTING_FIELDS
        )
        if (
            event_type not in {"perception.frame_processed", "perception.sighting"}
            or set(unsigned) != required
        ):
            raise ValueError("perception frame fields are invalid")
        return unsigned, signature

    def _consume_verified(self, raw: Mapping[str, object], now_ms: int) -> DetectionIngressResult:
        pin, state, identity = self._membership(raw)
        event_id = _text(raw["event_id"], "event_id")
        if event_id in self._seen:
            return DetectionIngressResult(False, "duplicate_event")
        if raw["type"] == "perception.frame_processed":
            event = self._processed(raw, identity, now_ms)
            capture_ms = _integer(raw["capture_timestamp_ms"], "capture_timestamp_ms")
            if capture_ms <= self._last_processed_capture_ms.get(pin.drone_id, -1):
                return DetectionIngressResult(False, "stale_capture_timestamp")
            self._last_processed_capture_ms[pin.drone_id] = capture_ms
            self._remember(event_id)
            if not event.capture_time_verified:
                return DetectionIngressResult(False, "capture_time_unverified")
            pose = self._capture_pose(
                state, identity, _integer(raw["capture_timestamp_ms"], "capture_timestamp_ms")
            )
            if pose is None or not self._valid_pose(pose, state, pin, identity, event, now_ms):
                return DetectionIngressResult(False, "trusted_pose_unavailable")
            observation = self._runtime.observe_processed_frame(
                pin.intent_id, event, pose.frame_evidence()
            )
            return DetectionIngressResult(observation.accepted, observation.reason, observation)
        self._sighting(raw, identity, now_ms)
        self._remember(event_id)
        return DetectionIngressResult(True, "accepted")

    def _remember(self, event_id: str) -> None:
        if len(self._seen_order) == self.config.max_seen_events:
            self._seen.remove(self._seen_order.popleft())
        self._seen.add(event_id)
        self._seen_order.append(event_id)

    def _membership(
        self, raw: Mapping[str, object]
    ) -> tuple[DetectionSourcePin, DetectionDroneState, FrameIdentity]:
        if raw["v"] != 1 or raw["source"] != "perception" or raw["session"] != self.config.session:
            raise ValueError("perception frame envelope is invalid")
        drone_id = _integer(raw["drone_id"], "drone_id", allow_zero=False)
        pin = self.config.sources.get(drone_id)
        if pin is None:
            raise ValueError("detector drone is not configured")
        state = self._current_drone(drone_id)
        if state is None or state.drone_id != drone_id:
            raise ValueError("detector drone is unavailable")
        if (
            _integer(raw["connection_epoch"], "connection_epoch") != state.connection_epoch
            or raw["mission_id"] != pin.mission_id
            or state.mission_id != pin.mission_id
            or raw["intent_id"] != pin.intent_id
            or raw["source_id"] != pin.source_id
            or raw["camera_id"] != pin.camera_id
            or raw["capture_clock_id"] != pin.clock_mapping.capture_clock_id
            or raw["relay_clock_id"] != pin.clock_mapping.relay_clock_id
            or ClockMapping.from_mapping(raw["clock_mapping"]) != pin.clock_mapping
        ):
            raise ValueError("perception frame membership is invalid")
        worker_run_id = _text(raw["worker_run_id"], "worker_run_id")
        frame_sequence = _integer(raw["frame_sequence"], "frame_sequence", allow_zero=False)
        frame_id = _text(raw["frame_id"], "frame_id")
        identity = FrameIdentity(pin.source_id, pin.mission_id, worker_run_id, frame_sequence)
        if identity.frame_id != frame_id:
            raise ValueError("perception frame identity is invalid")
        return pin, state, identity

    def _processed(
        self, raw: Mapping[str, object], identity: FrameIdentity, now_ms: int
    ) -> ProcessedFrameEvent:
        capture_ms = _integer(raw["capture_timestamp_ms"], "capture_timestamp_ms")
        frame_decoded_ms = _integer(
            raw["frame_decoded_at_monotonic_ms"], "frame_decoded_at_monotonic_ms"
        )
        evaluation_started_ms = _integer(
            raw["evaluation_started_at_monotonic_ms"], "evaluation_started_at_monotonic_ms"
        )
        evaluation_completed_ms = _integer(
            raw["evaluation_completed_at_monotonic_ms"], "evaluation_completed_at_monotonic_ms"
        )
        if capture_ms != frame_decoded_ms or raw["processed_at_ms"] != evaluation_completed_ms:
            raise ValueError("processed frame timestamps disagree")
        if (
            evaluation_completed_ms < frame_decoded_ms
            or now_ms - evaluation_completed_ms > self.config.max_frame_age_ms
            or evaluation_completed_ms > now_ms + self.config.max_frame_age_ms
        ):
            raise ValueError("processed frame is stale")
        received_ms = raw["received_at_ms"]
        if received_ms is not None:
            received_ms = _integer(received_ms, "received_at_ms")
            if not capture_ms <= received_ms <= evaluation_completed_ms:
                raise ValueError("frame receipt is outside capture and processing times")
        outcome = raw["outcome"]
        if outcome not in _PROCESSED_OUTCOMES:
            raise ValueError("processed frame outcome is invalid")
        event = ProcessedFrameEvent(
            identity,
            frame_decoded_ms / 1000,
            evaluation_started_ms / 1000,
            evaluation_completed_ms / 1000,
            outcome,
            _integer(raw["candidate_count"], "candidate_count"),
            tuple(raw["target_labels"]),
            _text(raw["detector_config_sha256"], "detector_config_sha256"),
            capture_time_verified=raw["capture_time_verified"] is True,
            received_at_s=None if received_ms is None else received_ms / 1000,
        )
        if raw["event_id"] != event.event_id or not isinstance(raw["capture_time_verified"], bool):
            raise ValueError("processed event identity is invalid")
        return event

    def _sighting(
        self, raw: Mapping[str, object], identity: FrameIdentity, now_ms: int
    ) -> SightingEvent:
        first_ms = _integer(raw["first_capture_timestamp_ms"], "first_capture_timestamp_ms")
        last_ms = _integer(raw["last_capture_timestamp_ms"], "last_capture_timestamp_ms")
        first_decoded_ms = _integer(
            raw["first_frame_decoded_at_monotonic_ms"], "first_frame_decoded_at_monotonic_ms"
        )
        last_decoded_ms = _integer(
            raw["last_frame_decoded_at_monotonic_ms"], "last_frame_decoded_at_monotonic_ms"
        )
        evaluation_started_ms = _integer(
            raw["evaluation_started_at_monotonic_ms"], "evaluation_started_at_monotonic_ms"
        )
        evaluation_completed_ms = _integer(
            raw["evaluation_completed_at_monotonic_ms"], "evaluation_completed_at_monotonic_ms"
        )
        if (
            first_ms != first_decoded_ms
            or last_ms != last_decoded_ms
            or raw["processed_at_ms"] != evaluation_completed_ms
            or last_decoded_ms < first_decoded_ms
            or evaluation_started_ms < last_decoded_ms
            or evaluation_completed_ms < evaluation_started_ms
            or now_ms - evaluation_completed_ms > self.config.max_frame_age_ms
            or evaluation_completed_ms > now_ms + self.config.max_frame_age_ms
        ):
            raise ValueError("sighting timestamps are invalid")
        candidate = DetectionCandidate(
            _text(raw["label"], "label"),
            _integer(raw["class_id"], "class_id"),
            _number(raw["confidence"], "confidence"),
            tuple(_number(value, "bbox_xyxy") for value in raw["bbox_xyxy"]),  # type: ignore[arg-type]
        )
        event = SightingEvent(
            _text(raw["sighting_id"], "sighting_id"),
            identity,
            first_decoded_ms / 1000,
            last_decoded_ms / 1000,
            evaluation_started_ms / 1000,
            evaluation_completed_ms / 1000,
            candidate,
            _integer(raw["observation_count"], "observation_count", allow_zero=False),
            _text(raw["detector_config_sha256"], "detector_config_sha256"),
        )
        if raw["event_id"] != event.event_id:
            raise ValueError("sighting event identity is invalid")
        return event

    def _valid_pose(
        self,
        pose: TrustedCapturePose,
        state: DetectionDroneState,
        pin: DetectionSourcePin,
        identity: FrameIdentity,
        event: ProcessedFrameEvent,
        now_ms: int,
    ) -> bool:
        provenance = pose.provenance
        capture_ms = round(event.frame_decoded_at_monotonic_s * 1000)
        return (
            pose.identity == identity
            and pose.connection_epoch == state.connection_epoch
            and abs(pose.pose_timestamp_ms - capture_ms) <= self.config.max_pose_skew_ms
            and 0 <= now_ms - pose.observed_at_ms <= self.config.max_frame_age_ms
            and provenance.camera_calibration_id == pin.camera_calibration_id
            and provenance.capture_clock_id == pin.clock_mapping.capture_clock_id
            and provenance.relay_clock_id == pin.clock_mapping.relay_clock_id
            and provenance.capture_time_s is not None
            and provenance.reason == "ready"
            and provenance.conversion_error_ms <= pin.clock_mapping.max_error_ms
            and abs(pin.clock_mapping.to_relay_ms(provenance.capture_time_s) - capture_ms)
            <= self.config.max_pose_skew_ms + provenance.conversion_error_ms
        )


_PROCESSED_OUTCOMES = frozenset(
    {
        "detections",
        "empty",
        "dropped_stale",
        "dropped_future",
        "dropped_regressive",
        "detector_error",
    }
)
_COMMON_FIELDS = frozenset(
    {
        "v",
        "source",
        "session",
        "intent_id",
        "mission_id",
        "drone_id",
        "connection_epoch",
        "source_id",
        "camera_id",
        "capture_clock_id",
        "relay_clock_id",
        "clock_mapping",
        "event_id",
        "frame_id",
        "worker_run_id",
        "frame_sequence",
    }
)
_PROCESSED_FIELDS = _COMMON_FIELDS | frozenset(
    {
        "type",
        "capture_timestamp_ms",
        "processed_at_ms",
        "outcome",
        "candidate_count",
        "capture_time_verified",
        "received_at_ms",
        "frame_decoded_at_monotonic_ms",
        "evaluation_started_at_monotonic_ms",
        "evaluation_completed_at_monotonic_ms",
        "target_labels",
        "detector_config_sha256",
    }
)
_SIGHTING_FIELDS = _COMMON_FIELDS | frozenset(
    {
        "type",
        "sighting_id",
        "first_capture_timestamp_ms",
        "last_capture_timestamp_ms",
        "processed_at_ms",
        "observation_count",
        "label",
        "class_id",
        "confidence",
        "bbox_xyxy",
        "first_frame_decoded_at_monotonic_ms",
        "last_frame_decoded_at_monotonic_ms",
        "evaluation_started_at_monotonic_ms",
        "evaluation_completed_at_monotonic_ms",
        "detector_config_sha256",
    }
)

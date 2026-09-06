"""Immutable contracts for bounded object-detection events."""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import quote

import numpy as np

COCO_LABELS = (
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
)
DEFAULT_TARGET_LABELS = frozenset({"person", "backpack", "bottle", "suitcase"})
FRAME_CLOCK_DOMAIN = "host_monotonic"
FRAME_TIME_PROVENANCE = "decoder_completion"
FrameOutcome = Literal[
    "detections",
    "empty",
    "dropped_stale",
    "dropped_future",
    "dropped_regressed",
    "dropped_regressive",
    "invalid_frame",
    "detector_error",
    "aggregation_error",
]
_EVENT_OUTCOMES = frozenset(
    {
        "detections",
        "empty",
        "dropped_stale",
        "dropped_future",
        "dropped_regressed",
        "dropped_regressive",
        "invalid_frame",
        "detector_error",
        "aggregation_error",
    }
)


def _finite_nonnegative(value: float, name: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite nonnegative number")


def _finite_positive(value: float, name: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a finite positive number")


def _probability(value: float, name: str) -> None:
    _finite_positive(value, name)
    if value > 1:
        raise ValueError(f"{name} must be at most one")


def _identifier(value: str, name: str, *, max_length: int = 256) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or not value.isprintable()
    ):
        raise ValueError(
            f"{name} must be a trimmed printable string of at most {max_length} characters"
        )


def _identity_component(value: str, name: str) -> None:
    _identifier(value, name, max_length=64)
    if ":" in value:
        raise ValueError(f"{name} must not contain the reserved ':' delimiter")


def _identity_token(value: str) -> str:
    return quote(value, safe="-_.~")


def _sha256_digest(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _target_labels(value: Collection[str], name: str = "target_labels") -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Collection):
        raise ValueError(f"{name} must be a nonempty collection of unique COCO labels")
    labels = tuple(value)
    if (
        not labels
        or any(not isinstance(label, str) for label in labels)
        or len(labels) != len(frozenset(labels))
        or not frozenset(labels) <= frozenset(COCO_LABELS)
    ):
        raise ValueError(f"{name} must be a nonempty collection of unique COCO labels")
    selected = frozenset(labels)
    return tuple(label for label in COCO_LABELS if label in selected)


@dataclass(frozen=True, slots=True, init=False)
class FrameIdentity:
    source_id: str
    mission_id: str
    worker_run_id: str
    frame_sequence: int
    _legacy_frame_id: str | None = field(default=None, repr=False, compare=False)

    def __init__(
        self,
        source_id: str,
        mission_id: str | None = None,
        worker_run_id: str | None = None,
        frame_sequence: int | None = None,
        *,
        frame_id: str | None = None,
    ) -> None:
        legacy_frame_id = frame_id
        if frame_sequence is None:
            if legacy_frame_id is not None:
                if mission_id is None or worker_run_id is not None:
                    raise ValueError("legacy frame identity needs source, frame, and mission ids")
                worker_run_id, frame_sequence = "legacy", 1
            else:
                if mission_id is None or worker_run_id is None:
                    raise ValueError("legacy frame identity needs source, frame, and mission ids")
                legacy_frame_id = mission_id
                mission_id, worker_run_id, frame_sequence = worker_run_id, "legacy", 1
        if legacy_frame_id is not None:
            _identity_component(legacy_frame_id, "frame_id")
        _identity_component(source_id, "source_id")
        if legacy_frame_id is None:
            _identifier(mission_id, "mission_id", max_length=64)
            _identity_component(worker_run_id, "worker_run_id")
        else:
            _identifier(mission_id, "mission_id")
            _identifier(worker_run_id, "worker_run_id")
        if (
            isinstance(frame_sequence, bool)
            or not isinstance(frame_sequence, int)
            or frame_sequence < 1
        ):
            raise ValueError("frame_sequence must be a positive integer")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "mission_id", mission_id)
        object.__setattr__(self, "worker_run_id", worker_run_id)
        object.__setattr__(self, "frame_sequence", frame_sequence)
        object.__setattr__(self, "_legacy_frame_id", legacy_frame_id)

    @property
    def is_legacy(self) -> bool:
        return self._legacy_frame_id is not None

    @property
    def frame_id(self) -> str:
        if self._legacy_frame_id is not None:
            return self._legacy_frame_id
        return (
            f"frame:{_identity_token(self.mission_id)}:{_identity_token(self.source_id)}:"
            f"{_identity_token(self.worker_run_id)}:{self.frame_sequence}"
        )

    def payload(self) -> dict[str, str | int]:
        return {
            "source_id": self.source_id,
            "mission_id": self.mission_id,
            "worker_run_id": self.worker_run_id,
            "frame_id": self.frame_id,
            "frame_sequence": self.frame_sequence,
        }


@dataclass(frozen=True, slots=True)
class DetectionCandidate:
    label: str
    class_id: int
    confidence: float
    bbox_xyxy: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        if self.label not in COCO_LABELS:
            raise ValueError("label must be a COCO class")
        if (
            isinstance(self.class_id, bool)
            or not isinstance(self.class_id, int)
            or self.class_id != COCO_LABELS.index(self.label)
        ):
            raise ValueError("class_id does not match label")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be between zero and one")
        if (
            not isinstance(self.bbox_xyxy, tuple)
            or len(self.bbox_xyxy) != 4
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in self.bbox_xyxy
            )
        ):
            raise ValueError("bbox_xyxy must contain four finite coordinates")
        left, top, right, bottom = self.bbox_xyxy
        if left < 0 or top < 0 or right <= left or bottom <= top:
            raise ValueError("bbox_xyxy must be nonnegative and have positive area")

    def payload(self) -> dict[str, object]:
        return {
            "label": self.label,
            "class_id": self.class_id,
            "confidence": self.confidence,
            "bbox_xyxy": list(self.bbox_xyxy),
        }


@dataclass(frozen=True, slots=True, init=False)
class ProcessedFrameEvent:
    identity: FrameIdentity
    frame_decoded_at_monotonic_s: float
    evaluation_started_at_monotonic_s: float
    evaluation_completed_at_monotonic_s: float
    outcome: FrameOutcome
    candidate_count: int
    target_labels: tuple[str, ...]
    detector_config_sha256: str
    capture_time_verified: bool
    received_at_s: float | None

    def __init__(
        self,
        identity: FrameIdentity,
        frame_decoded_at_monotonic_s: float | None = None,
        evaluation_started_at_monotonic_s: float | None = None,
        evaluation_completed_at_monotonic_s: float | str | None = None,
        outcome: FrameOutcome | int | None = None,
        candidate_count: int | bool | None = None,
        target_labels: tuple[str, ...] | bool | float | None = None,
        detector_config_sha256: str | None = None,
        *,
        frame_timestamp_s: float | None = None,
        processed_at_s: float | None = None,
        capture_time_verified: bool = False,
        received_at_s: float | None = None,
    ) -> None:
        if isinstance(evaluation_completed_at_monotonic_s, str):
            legacy_outcome = evaluation_completed_at_monotonic_s
            legacy_count = outcome
            legacy_verified = candidate_count
            legacy_received = target_labels
            frame_timestamp_s = frame_decoded_at_monotonic_s
            processed_at_s = evaluation_started_at_monotonic_s
            frame_decoded_at_monotonic_s = frame_timestamp_s
            evaluation_started_at_monotonic_s = processed_at_s
            evaluation_completed_at_monotonic_s = processed_at_s
            outcome = legacy_outcome
            candidate_count = legacy_count
            target_labels = _target_labels(DEFAULT_TARGET_LABELS)
            detector_config_sha256 = "0" * 64
            capture_time_verified = (
                capture_time_verified if legacy_verified is None else legacy_verified
            )
            received_at_s = legacy_received if received_at_s is None else received_at_s
        elif frame_timestamp_s is not None or processed_at_s is not None:
            if frame_timestamp_s is None or processed_at_s is None:
                raise ValueError("legacy frame timestamps must be supplied together")
            frame_decoded_at_monotonic_s = frame_timestamp_s
            evaluation_started_at_monotonic_s = processed_at_s
            evaluation_completed_at_monotonic_s = processed_at_s
            target_labels = (
                _target_labels(DEFAULT_TARGET_LABELS) if target_labels is None else target_labels
            )
            detector_config_sha256 = (
                "0" * 64 if detector_config_sha256 is None else detector_config_sha256
            )
        if (
            frame_decoded_at_monotonic_s is None
            or evaluation_started_at_monotonic_s is None
            or evaluation_completed_at_monotonic_s is None
            or outcome is None
            or candidate_count is None
            or target_labels is None
            or detector_config_sha256 is None
        ):
            raise ValueError("processed frame event fields are required")
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "frame_decoded_at_monotonic_s", frame_decoded_at_monotonic_s)
        object.__setattr__(
            self, "evaluation_started_at_monotonic_s", evaluation_started_at_monotonic_s
        )
        object.__setattr__(
            self, "evaluation_completed_at_monotonic_s", evaluation_completed_at_monotonic_s
        )
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "candidate_count", candidate_count)
        object.__setattr__(self, "target_labels", target_labels)
        object.__setattr__(self, "detector_config_sha256", detector_config_sha256)
        object.__setattr__(self, "capture_time_verified", capture_time_verified)
        object.__setattr__(self, "received_at_s", received_at_s)
        self.__post_init__()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, FrameIdentity):
            raise ValueError("identity must be a FrameIdentity")
        _finite_nonnegative(self.frame_decoded_at_monotonic_s, "frame_decoded_at_monotonic_s")
        _finite_nonnegative(
            self.evaluation_started_at_monotonic_s, "evaluation_started_at_monotonic_s"
        )
        _finite_nonnegative(
            self.evaluation_completed_at_monotonic_s, "evaluation_completed_at_monotonic_s"
        )
        if self.evaluation_completed_at_monotonic_s < self.evaluation_started_at_monotonic_s:
            raise ValueError("evaluation completion must not precede its start")
        if not isinstance(self.outcome, str) or self.outcome not in _EVENT_OUTCOMES:
            raise ValueError("outcome must be a supported frame outcome")
        if (
            isinstance(self.candidate_count, bool)
            or not isinstance(self.candidate_count, int)
            or self.candidate_count < 0
        ):
            raise ValueError("candidate_count must be a nonnegative integer")
        if self.outcome == "detections" and self.candidate_count == 0:
            raise ValueError("candidate_count must agree with outcome")
        if self.outcome not in {"detections", "aggregation_error"} and self.candidate_count:
            raise ValueError("candidate_count must agree with outcome")
        if self.target_labels != _target_labels(self.target_labels):
            raise ValueError("target_labels must use canonical COCO order")
        _sha256_digest(self.detector_config_sha256, "detector_config_sha256")
        if not isinstance(self.capture_time_verified, bool):
            raise ValueError("capture_time_verified must be a boolean")
        if self.received_at_s is not None:
            _finite_nonnegative(self.received_at_s, "received_at_s")
        if (
            self.outcome == "dropped_future"
            and self.frame_decoded_at_monotonic_s <= self.evaluation_started_at_monotonic_s
        ) or (
            self.outcome != "dropped_future"
            and self.frame_decoded_at_monotonic_s > self.evaluation_started_at_monotonic_s
        ):
            raise ValueError("frame timing must agree with outcome")

    @property
    def frame_timestamp_s(self) -> float:
        return self.frame_decoded_at_monotonic_s

    @property
    def processed_at_s(self) -> float:
        return self.evaluation_completed_at_monotonic_s

    @property
    def event_id(self) -> str:
        if self.identity.is_legacy:
            return (
                f"processed:{self.identity.mission_id}:{self.identity.source_id}:"
                f"{self.identity.frame_id}"
            )
        return f"event:{self.identity.frame_id}:processed"

    def payload(self) -> dict[str, object]:
        return {
            "type": "perception.frame_processed",
            "event_id": self.event_id,
            **self.identity.payload(),
            "frame_decoded_at_monotonic_s": self.frame_decoded_at_monotonic_s,
            "evaluation_started_at_monotonic_s": self.evaluation_started_at_monotonic_s,
            "evaluation_completed_at_monotonic_s": self.evaluation_completed_at_monotonic_s,
            "received_at_s": self.received_at_s,
            "processed_at_s": self.processed_at_s,
            "clock_domain": FRAME_CLOCK_DOMAIN,
            "frame_time_provenance": FRAME_TIME_PROVENANCE,
            "outcome": self.outcome,
            "candidate_count": self.candidate_count,
            "target_labels": list(self.target_labels),
            "detector_config_sha256": self.detector_config_sha256,
        }


@dataclass(frozen=True, slots=True)
class SightingEvent:
    sighting_id: str
    identity: FrameIdentity
    first_frame_decoded_at_monotonic_s: float
    last_frame_decoded_at_monotonic_s: float
    evaluation_started_at_monotonic_s: float
    evaluation_completed_at_monotonic_s: float
    candidate: DetectionCandidate
    observation_count: int
    detector_config_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.sighting_id, "sighting_id")
        if not isinstance(self.identity, FrameIdentity):
            raise ValueError("identity must be a FrameIdentity")
        if not isinstance(self.candidate, DetectionCandidate):
            raise ValueError("candidate must be a DetectionCandidate")
        _finite_nonnegative(
            self.first_frame_decoded_at_monotonic_s,
            "first_frame_decoded_at_monotonic_s",
        )
        _finite_nonnegative(
            self.last_frame_decoded_at_monotonic_s,
            "last_frame_decoded_at_monotonic_s",
        )
        _finite_nonnegative(
            self.evaluation_started_at_monotonic_s, "evaluation_started_at_monotonic_s"
        )
        _finite_nonnegative(
            self.evaluation_completed_at_monotonic_s, "evaluation_completed_at_monotonic_s"
        )
        if self.last_frame_decoded_at_monotonic_s < self.first_frame_decoded_at_monotonic_s:
            raise ValueError("last decoded time must not precede first decoded time")
        if self.evaluation_started_at_monotonic_s < self.last_frame_decoded_at_monotonic_s:
            raise ValueError("evaluation start must not precede last decoded time")
        if self.evaluation_completed_at_monotonic_s < self.evaluation_started_at_monotonic_s:
            raise ValueError("evaluation completion must not precede its start")
        if (
            isinstance(self.observation_count, bool)
            or not isinstance(self.observation_count, int)
            or self.observation_count < 1
        ):
            raise ValueError("observation_count must be a positive integer")
        _sha256_digest(self.detector_config_sha256, "detector_config_sha256")

    @property
    def event_id(self) -> str:
        return f"event:{self.sighting_id}:observation:{self.observation_count}"

    @property
    def first_frame_timestamp_s(self) -> float:
        return self.first_frame_decoded_at_monotonic_s

    @property
    def last_frame_timestamp_s(self) -> float:
        return self.last_frame_decoded_at_monotonic_s

    @property
    def processed_at_s(self) -> float:
        return self.evaluation_completed_at_monotonic_s

    def payload(self) -> dict[str, object]:
        return {
            "type": "perception.sighting",
            "event_id": self.event_id,
            "sighting_id": self.sighting_id,
            **self.identity.payload(),
            "first_frame_decoded_at_monotonic_s": self.first_frame_decoded_at_monotonic_s,
            "last_frame_decoded_at_monotonic_s": self.last_frame_decoded_at_monotonic_s,
            "evaluation_started_at_monotonic_s": self.evaluation_started_at_monotonic_s,
            "evaluation_completed_at_monotonic_s": self.evaluation_completed_at_monotonic_s,
            "clock_domain": FRAME_CLOCK_DOMAIN,
            "frame_time_provenance": FRAME_TIME_PROVENANCE,
            "observation_count": self.observation_count,
            "detector_config_sha256": self.detector_config_sha256,
            **self.candidate.payload(),
        }


PerceptionEvent = ProcessedFrameEvent | SightingEvent


@dataclass(frozen=True, slots=True)
class DecodedFrame:
    image: np.ndarray
    captured_at_s: float
    received_at_s: float
    capture_time_verified: bool = False

    def __post_init__(self) -> None:
        _finite_nonnegative(self.captured_at_s, "captured_at_s")
        _finite_nonnegative(self.received_at_s, "received_at_s")
        if not isinstance(self.capture_time_verified, bool):
            raise ValueError("capture_time_verified must be a boolean")


type FrameRead = DecodedFrame | tuple[np.ndarray, float] | None


class FrameReader(Protocol):
    """Supplies a frame and its host-monotonic decoder-completion time."""

    def read(self, timeout: float = 0.1) -> FrameRead: ...


class Detector(Protocol):
    @property
    def target_labels(self) -> tuple[str, ...]: ...

    @property
    def detector_config_sha256(self) -> str: ...

    def detect(self, frame: np.ndarray) -> Sequence[DetectionCandidate]: ...


def _iou(
    first: tuple[float, float, float, float], second: tuple[float, float, float, float]
) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    if not intersection:
        return 0.0
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    return intersection / (first_area + second_area - intersection)

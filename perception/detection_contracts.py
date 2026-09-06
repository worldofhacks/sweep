"""Immutable contracts for bounded object-detection events."""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

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


@dataclass(frozen=True, slots=True)
class FrameIdentity:
    source_id: str
    mission_id: str
    worker_run_id: str
    frame_sequence: int

    def __post_init__(self) -> None:
        _identity_component(self.source_id, "source_id")
        _identity_component(self.mission_id, "mission_id")
        _identity_component(self.worker_run_id, "worker_run_id")
        if (
            isinstance(self.frame_sequence, bool)
            or not isinstance(self.frame_sequence, int)
            or self.frame_sequence < 1
        ):
            raise ValueError("frame_sequence must be a positive integer")

    @property
    def frame_id(self) -> str:
        return (
            f"frame:{self.mission_id}:{self.source_id}:{self.worker_run_id}:{self.frame_sequence}"
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


@dataclass(frozen=True, slots=True)
class ProcessedFrameEvent:
    identity: FrameIdentity
    frame_decoded_at_monotonic_s: float
    evaluation_started_at_monotonic_s: float
    evaluation_completed_at_monotonic_s: float
    outcome: FrameOutcome
    candidate_count: int
    target_labels: tuple[str, ...]
    detector_config_sha256: str

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
        if (
            self.outcome == "dropped_future"
            and self.frame_decoded_at_monotonic_s <= self.evaluation_started_at_monotonic_s
        ) or (
            self.outcome != "dropped_future"
            and self.frame_decoded_at_monotonic_s > self.evaluation_started_at_monotonic_s
        ):
            raise ValueError("frame timing must agree with outcome")

    @property
    def event_id(self) -> str:
        return f"event:{self.identity.frame_id}:processed"

    def payload(self) -> dict[str, object]:
        return {
            "type": "perception.frame_processed",
            "event_id": self.event_id,
            **self.identity.payload(),
            "frame_decoded_at_monotonic_s": self.frame_decoded_at_monotonic_s,
            "evaluation_started_at_monotonic_s": self.evaluation_started_at_monotonic_s,
            "evaluation_completed_at_monotonic_s": self.evaluation_completed_at_monotonic_s,
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


class FrameReader(Protocol):
    """Supplies a frame and its host-monotonic decoder-completion time."""

    def read(self, timeout: float = 0.1) -> tuple[np.ndarray, float] | None: ...


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

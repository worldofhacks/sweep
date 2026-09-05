"""Bounded COCO object detections from the latest decoded webcam frame."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import cv2
import numpy as np

YOLOX_S_ONNX_URL = (
    "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx"
)

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
DEFAULT_TARGET_LABELS = frozenset({"backpack", "bottle", "suitcase"})


def _finite_nonnegative(value: float, name: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite nonnegative number")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


@dataclass(frozen=True, slots=True)
class FrameIdentity:
    source_id: str
    frame_id: str
    mission_id: str

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id")
        _identifier(self.frame_id, "frame_id")
        _identifier(self.mission_id, "mission_id")

    def payload(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "frame_id": self.frame_id,
            "mission_id": self.mission_id,
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
        if self.class_id != COCO_LABELS.index(self.label):
            raise ValueError("class_id does not match label")
        if not isinstance(self.confidence, (int, float)) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between zero and one")
        if len(self.bbox_xyxy) != 4 or not all(math.isfinite(value) for value in self.bbox_xyxy):
            raise ValueError("bbox_xyxy must contain four finite coordinates")
        left, top, right, bottom = self.bbox_xyxy
        if right <= left or bottom <= top:
            raise ValueError("bbox_xyxy must have positive area")

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
    frame_timestamp_s: float
    processed_at_s: float
    outcome: Literal["detections", "empty", "dropped_stale", "dropped_future", "detector_error"]
    candidate_count: int

    def __post_init__(self) -> None:
        _finite_nonnegative(self.frame_timestamp_s, "frame_timestamp_s")
        _finite_nonnegative(self.processed_at_s, "processed_at_s")
        if (
            isinstance(self.candidate_count, bool)
            or not isinstance(self.candidate_count, int)
            or self.candidate_count < 0
        ):
            raise ValueError("candidate_count must be a nonnegative integer")

    @property
    def event_id(self) -> str:
        return (
            f"processed:{self.identity.mission_id}:{self.identity.source_id}:"
            f"{self.identity.frame_id}"
        )

    def payload(self) -> dict[str, object]:
        return {
            "type": "perception.frame_processed",
            "event_id": self.event_id,
            **self.identity.payload(),
            "frame_timestamp_s": self.frame_timestamp_s,
            "processed_at_s": self.processed_at_s,
            "outcome": self.outcome,
            "candidate_count": self.candidate_count,
        }


@dataclass(frozen=True, slots=True)
class SightingEvent:
    sighting_id: str
    identity: FrameIdentity
    first_frame_timestamp_s: float
    last_frame_timestamp_s: float
    processed_at_s: float
    candidate: DetectionCandidate
    observation_count: int

    def __post_init__(self) -> None:
        _identifier(self.sighting_id, "sighting_id")
        _finite_nonnegative(self.first_frame_timestamp_s, "first_frame_timestamp_s")
        _finite_nonnegative(self.last_frame_timestamp_s, "last_frame_timestamp_s")
        _finite_nonnegative(self.processed_at_s, "processed_at_s")
        if self.last_frame_timestamp_s < self.first_frame_timestamp_s:
            raise ValueError("last_frame_timestamp_s must not precede first_frame_timestamp_s")
        if self.processed_at_s < self.last_frame_timestamp_s:
            raise ValueError("processed_at_s must not precede last_frame_timestamp_s")
        if (
            isinstance(self.observation_count, bool)
            or not isinstance(self.observation_count, int)
            or self.observation_count < 1
        ):
            raise ValueError("observation_count must be a positive integer")

    @property
    def event_id(self) -> str:
        return f"sighting:{self.sighting_id}:{self.observation_count}"

    def payload(self) -> dict[str, object]:
        return {
            "type": "perception.sighting",
            "event_id": self.event_id,
            "sighting_id": self.sighting_id,
            **self.identity.payload(),
            "first_frame_timestamp_s": self.first_frame_timestamp_s,
            "last_frame_timestamp_s": self.last_frame_timestamp_s,
            "processed_at_s": self.processed_at_s,
            "observation_count": self.observation_count,
            **self.candidate.payload(),
        }


PerceptionEvent = ProcessedFrameEvent | SightingEvent


class FrameReader(Protocol):
    def read(self, timeout: float = 0.1) -> tuple[np.ndarray, float] | None: ...


class Detector(Protocol):
    def detect(self, frame: np.ndarray) -> Sequence[DetectionCandidate]: ...


@dataclass(slots=True)
class _Aggregate:
    sighting_id: str
    identity: FrameIdentity
    first_timestamp_s: float
    last_timestamp_s: float
    candidate: DetectionCandidate
    observation_count: int = 1


class SightingAggregator:
    """Keeps a bounded per-source, per-mission IoU deduplication window."""

    def __init__(
        self,
        *,
        dedup_window_s: float = 2.0,
        iou_threshold: float = 0.5,
        max_sightings: int = 256,
    ) -> None:
        if (
            not math.isfinite(dedup_window_s)
            or dedup_window_s <= 0
            or not math.isfinite(iou_threshold)
            or not 0 < iou_threshold <= 1
            or isinstance(max_sightings, bool)
            or not isinstance(max_sightings, int)
            or max_sightings < 1
        ):
            raise ValueError("invalid sighting aggregation limits")
        self._dedup_window_s = dedup_window_s
        self._iou_threshold = iou_threshold
        self._max_sightings = max_sightings
        self._sightings: list[_Aggregate] = []
        self._next_id = 0

    def observe(
        self,
        identity: FrameIdentity,
        frame_timestamp_s: float,
        processed_at_s: float,
        candidate: DetectionCandidate,
    ) -> SightingEvent:
        _finite_nonnegative(frame_timestamp_s, "frame_timestamp_s")
        _finite_nonnegative(processed_at_s, "processed_at_s")
        self._sightings = [
            sighting
            for sighting in self._sightings
            if frame_timestamp_s - sighting.last_timestamp_s <= self._dedup_window_s
        ]
        matching = [
            sighting
            for sighting in self._sightings
            if sighting.identity.source_id == identity.source_id
            and sighting.identity.mission_id == identity.mission_id
            and sighting.candidate.class_id == candidate.class_id
            and _iou(sighting.candidate.bbox_xyxy, candidate.bbox_xyxy) >= self._iou_threshold
        ]
        if matching:
            sighting = max(matching, key=lambda item: item.last_timestamp_s)
            sighting.last_timestamp_s = frame_timestamp_s
            sighting.identity = identity
            sighting.observation_count += 1
            if candidate.confidence > sighting.candidate.confidence:
                sighting.candidate = candidate
        else:
            self._next_id += 1
            sighting = _Aggregate(
                sighting_id=f"{identity.mission_id}:{identity.source_id}:{self._next_id}",
                identity=identity,
                first_timestamp_s=frame_timestamp_s,
                last_timestamp_s=frame_timestamp_s,
                candidate=candidate,
            )
            self._sightings.append(sighting)
            if len(self._sightings) > self._max_sightings:
                self._sightings.sort(key=lambda item: item.last_timestamp_s, reverse=True)
                del self._sightings[self._max_sightings :]
        return SightingEvent(
            sighting_id=sighting.sighting_id,
            identity=identity,
            first_frame_timestamp_s=sighting.first_timestamp_s,
            last_frame_timestamp_s=sighting.last_timestamp_s,
            processed_at_s=processed_at_s,
            candidate=sighting.candidate,
            observation_count=sighting.observation_count,
        )


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


class YoloXOnnxDetector:
    """COCO YOLOX-s ONNX inference using OpenCV's local DNN runtime."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        confidence_threshold: float = 0.6,
        nms_iou_threshold: float = 0.45,
        target_labels: frozenset[str] = DEFAULT_TARGET_LABELS,
        max_candidates: int = 32,
        net: object | None = None,
    ) -> None:
        if (
            not math.isfinite(confidence_threshold)
            or not 0 < confidence_threshold <= 1
            or not math.isfinite(nms_iou_threshold)
            or not 0 < nms_iou_threshold <= 1
            or not target_labels
            or not target_labels <= set(COCO_LABELS)
            or isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int)
            or not 1 <= max_candidates <= 256
        ):
            raise ValueError("invalid detector configuration")
        path = Path(model_path)
        if net is None and not path.is_file():
            raise ValueError("YOLOX ONNX model path must name a file")
        try:
            self._net = net or cv2.dnn.readNetFromONNX(str(path))
        except cv2.error as error:
            raise ValueError("cannot load YOLOX ONNX model") from error
        self._confidence_threshold = confidence_threshold
        self._nms_iou_threshold = nms_iou_threshold
        self._target_class_ids = frozenset(COCO_LABELS.index(label) for label in target_labels)
        self._max_candidates = max_candidates

    def detect(self, frame: np.ndarray) -> tuple[DetectionCandidate, ...]:
        if (
            not isinstance(frame, np.ndarray)
            or frame.ndim != 3
            or frame.shape[2] != 3
            or frame.dtype != np.uint8
            or not frame.shape[0]
            or not frame.shape[1]
        ):
            raise ValueError("detector frame must be a nonempty uint8 BGR image")
        input_image, scale, padding = _letterbox(frame)
        blob = cv2.dnn.blobFromImage(input_image, 1.0, swapRB=True)
        self._net.setInput(blob)
        predictions = np.asarray(self._net.forward())
        return _postprocess_yolox(
            predictions,
            scale=scale,
            padding=padding,
            frame_width=frame.shape[1],
            frame_height=frame.shape[0],
            confidence_threshold=self._confidence_threshold,
            nms_iou_threshold=self._nms_iou_threshold,
            target_class_ids=self._target_class_ids,
            max_candidates=self._max_candidates,
        )


def _letterbox(frame: np.ndarray) -> tuple[np.ndarray, float, tuple[int, int]]:
    height, width = frame.shape[:2]
    scale = min(640 / height, 640 / width)
    resized_width = round(width * scale)
    resized_height = round(height * scale)
    resized = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((640, 640, 3), 114, dtype=np.uint8)
    canvas[:resized_height, :resized_width] = resized
    return canvas, scale, (0, 0)


def _postprocess_yolox(
    predictions: np.ndarray,
    *,
    scale: float,
    padding: tuple[int, int],
    frame_width: int,
    frame_height: int,
    confidence_threshold: float,
    nms_iou_threshold: float,
    target_class_ids: frozenset[int],
    max_candidates: int,
) -> tuple[DetectionCandidate, ...]:
    if predictions.ndim == 3 and predictions.shape[0] == 1:
        predictions = predictions[0]
    if predictions.shape != (8400, 85) or not np.isfinite(predictions).all():
        raise ValueError("unexpected YOLOX-s ONNX output")
    decoded = predictions.copy()
    grids, strides = _yolox_grid()
    decoded[:, :2] = (decoded[:, :2] + grids) * strides
    decoded[:, 2:4] = np.exp(np.clip(decoded[:, 2:4], -20, 20)) * strides
    class_ids = np.argmax(decoded[:, 5:], axis=1)
    confidence = decoded[:, 4] * decoded[np.arange(len(decoded)), class_ids + 5]
    valid = (
        (confidence >= confidence_threshold)
        & np.isin(class_ids, tuple(target_class_ids))
        & np.isfinite(confidence)
    )
    indices = np.flatnonzero(valid)
    candidates: list[DetectionCandidate] = []
    for index in indices:
        center_x, center_y, width, height = decoded[index, :4]
        left = (center_x - width / 2 - padding[0]) / scale
        top = (center_y - height / 2 - padding[1]) / scale
        right = (center_x + width / 2 - padding[0]) / scale
        bottom = (center_y + height / 2 - padding[1]) / scale
        left, right = max(0.0, left), min(float(frame_width), right)
        top, bottom = max(0.0, top), min(float(frame_height), bottom)
        if right <= left or bottom <= top:
            continue
        class_id = int(class_ids[index])
        candidates.append(
            DetectionCandidate(
                label=COCO_LABELS[class_id],
                class_id=class_id,
                confidence=float(confidence[index]),
                bbox_xyxy=(float(left), float(top), float(right), float(bottom)),
            )
        )
    candidates.sort(key=lambda candidate: candidate.confidence, reverse=True)
    selected: list[DetectionCandidate] = []
    for candidate in candidates:
        if all(
            candidate.class_id != existing.class_id
            or _iou(candidate.bbox_xyxy, existing.bbox_xyxy) < nms_iou_threshold
            for existing in selected
        ):
            selected.append(candidate)
        if len(selected) == max_candidates:
            break
    return tuple(selected)


_GRID: tuple[np.ndarray, np.ndarray] | None = None


def _yolox_grid() -> tuple[np.ndarray, np.ndarray]:
    global _GRID
    if _GRID is None:
        grids = []
        strides = []
        for stride in (8, 16, 32):
            size = 640 // stride
            grid_y, grid_x = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
            grids.append(np.stack((grid_x, grid_y), axis=-1).reshape(-1, 2))
            strides.append(np.full((size * size, 1), stride))
        _GRID = np.concatenate(grids), np.concatenate(strides)
    return _GRID


class LiveDetectionWorker:
    """Samples the latest frame only and emits bounded observation events."""

    def __init__(
        self,
        stream: FrameReader,
        detector: Detector,
        *,
        source_id: str,
        mission_id: str,
        on_event: Callable[[PerceptionEvent], None] | None = None,
        max_frame_age_s: float = 0.5,
        sample_interval_s: float = 0.1,
        retained_events: int = 512,
        aggregator: SightingAggregator | None = None,
    ) -> None:
        _identifier(source_id, "source_id")
        _identifier(mission_id, "mission_id")
        if (
            not math.isfinite(max_frame_age_s)
            or max_frame_age_s <= 0
            or not math.isfinite(sample_interval_s)
            or sample_interval_s <= 0
            or isinstance(retained_events, bool)
            or not isinstance(retained_events, int)
            or retained_events < 1
        ):
            raise ValueError("invalid live detection worker limits")
        self._stream = stream
        self._detector = detector
        self._source_id = source_id
        self._mission_id = mission_id
        self._on_event = on_event
        self._max_frame_age_s = max_frame_age_s
        self._sample_interval_s = sample_interval_s
        self._events: deque[PerceptionEvent] = deque(maxlen=retained_events)
        self._aggregator = aggregator or SightingAggregator()
        self._frame_sequence = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll(self, now: float | None = None) -> tuple[PerceptionEvent, ...]:
        processed_at_s = time.monotonic() if now is None else now
        _finite_nonnegative(processed_at_s, "now")
        frame = self._stream.read(0)
        if frame is None:
            return ()
        image, frame_timestamp_s = frame
        _finite_nonnegative(frame_timestamp_s, "frame timestamp")
        self._frame_sequence += 1
        identity = FrameIdentity(
            source_id=self._source_id,
            frame_id=str(self._frame_sequence),
            mission_id=self._mission_id,
        )
        if frame_timestamp_s > processed_at_s:
            events: tuple[PerceptionEvent, ...] = (
                ProcessedFrameEvent(
                    identity, frame_timestamp_s, processed_at_s, "dropped_future", 0
                ),
            )
        elif processed_at_s - frame_timestamp_s > self._max_frame_age_s:
            events = (
                ProcessedFrameEvent(
                    identity, frame_timestamp_s, processed_at_s, "dropped_stale", 0
                ),
            )
        else:
            try:
                candidates = tuple(self._detector.detect(image))
            except Exception:
                events = (
                    ProcessedFrameEvent(
                        identity, frame_timestamp_s, processed_at_s, "detector_error", 0
                    ),
                )
            else:
                sightings = tuple(
                    self._aggregator.observe(identity, frame_timestamp_s, processed_at_s, candidate)
                    for candidate in candidates
                )
                events = (
                    ProcessedFrameEvent(
                        identity,
                        frame_timestamp_s,
                        processed_at_s,
                        "detections" if candidates else "empty",
                        len(candidates),
                    ),
                    *sightings,
                )
        for event in events:
            self._events.append(event)
            if self._on_event is not None:
                self._on_event(event)
        return events

    def start(self) -> LiveDetectionWorker:
        if self._thread is not None:
            raise RuntimeError("live detection worker is already running")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live-detection", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self._sample_interval_s + 0.2)
            self._thread = None

    def events(self) -> tuple[PerceptionEvent, ...]:
        return tuple(self._events)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self._sample_interval_s)

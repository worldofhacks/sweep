"""YOLOX-s ONNX inference through OpenCV's local DNN runtime."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from pathlib import Path

import cv2
import numpy as np

from perception.detection_contracts import (
    COCO_LABELS,
    DEFAULT_TARGET_LABELS,
    DetectionCandidate,
    _iou,
    _probability,
    _sha256_digest,
    _target_labels,
)

YOLOX_S_ONNX_URL = (
    "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx"
)
YOLOX_S_ONNX_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
MAX_MODEL_BYTES = 128 * 1024 * 1024


class YoloXOnnxDetector:
    """COCO YOLOX-s ONNX inference using OpenCV's local DNN runtime."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        confidence_threshold: float = 0.6,
        nms_iou_threshold: float = 0.45,
        target_labels: Collection[str] = DEFAULT_TARGET_LABELS,
        max_candidates: int = 32,
        net: object | None = None,
        expected_model_sha256: str = YOLOX_S_ONNX_SHA256,
        injected_model_sha256: str | None = None,
    ) -> None:
        canonical_target_labels = _target_labels(target_labels)
        _probability(confidence_threshold, "confidence_threshold")
        _probability(nms_iou_threshold, "nms_iou_threshold")
        if (
            isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int)
            or not 1 <= max_candidates <= 256
        ):
            raise ValueError("invalid detector configuration")
        path = Path(model_path)
        if net is None:
            _sha256_digest(expected_model_sha256, "expected_model_sha256")
            if injected_model_sha256 is not None:
                raise ValueError("injected_model_sha256 requires an injected net")
            try:
                with path.open("rb") as model_file:
                    model_bytes = model_file.read(MAX_MODEL_BYTES + 1)
            except OSError:
                raise ValueError("YOLOX ONNX model path must name a readable file") from None
            if len(model_bytes) > MAX_MODEL_BYTES:
                raise ValueError("YOLOX ONNX model exceeds the 128 MiB limit")
            model_sha256 = hashlib.sha256(model_bytes).hexdigest()
            if model_sha256 != expected_model_sha256:
                raise ValueError("YOLOX ONNX model hash mismatch")
            try:
                self._net = cv2.dnn.readNetFromONNX(np.frombuffer(model_bytes, dtype=np.uint8))
            except cv2.error as error:
                raise ValueError("cannot load YOLOX ONNX model") from error
            implementation = "opencv_dnn_yolox_s_onnx_v1"
        else:
            if injected_model_sha256 is None:
                raise ValueError("an injected net requires injected_model_sha256")
            _sha256_digest(injected_model_sha256, "injected_model_sha256")
            self._net = net
            model_sha256 = injected_model_sha256
            implementation = "opencv_dnn_yolox_s_injected_v1"
        self._confidence_threshold = confidence_threshold
        self._nms_iou_threshold = nms_iou_threshold
        self._target_labels = canonical_target_labels
        self._target_class_ids = frozenset(
            COCO_LABELS.index(label) for label in canonical_target_labels
        )
        self._max_candidates = max_candidates
        configuration = {
            "implementation": implementation,
            "model_sha256": model_sha256,
            "confidence_threshold": float(confidence_threshold),
            "nms_iou_threshold": float(nms_iou_threshold),
            "target_labels": list(canonical_target_labels),
            "max_candidates": max_candidates,
        }
        canonical = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        self._detector_config_sha256 = hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def target_labels(self) -> tuple[str, ...]:
        return self._target_labels

    @property
    def detector_config_sha256(self) -> str:
        return self._detector_config_sha256

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
        # Official YOLOX exports consume OpenCV's native BGR channel order.
        blob = cv2.dnn.blobFromImage(input_image, 1.0, swapRB=False)
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

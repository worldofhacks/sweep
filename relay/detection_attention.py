"""Turn verified perception observations into operator-only attention events."""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from threading import RLock

import cv2
import numpy as np

from perception.detection_contracts import (
    Detector,
    PerceptionEvent,
    ProcessedFrameEvent,
    SightingEvent,
)
from perception.object_detection import LiveDetectionWorker, YoloXOnnxDetector
from relay.session import RelaySession
from relay.settings import DetectionRecording, RelaySettings

MAX_RETAINED_DETECTIONS_PER_SESSION = 128


@dataclass(frozen=True, slots=True)
class DetectionRecord:
    detection_id: str
    drone_id: int
    connection_epoch: int
    acknowledged: bool


class _RecordedFrame:
    def __init__(self, image: np.ndarray) -> None:
        self.image = image

    def read(self, _timeout: float = 0.1) -> tuple[np.ndarray, float]:
        return self.image, time.monotonic()


class _LockedDetector:
    def __init__(self, detector: Detector) -> None:
        self._detector = detector
        self._lock = RLock()
        self.target_labels = detector.target_labels
        self.detector_config_sha256 = detector.detector_config_sha256

    def detect(self, frame: np.ndarray):
        with self._lock:
            return self._detector.detect(frame)


class HostRecordedFrameProcessor:
    """Runs the pinned, host-configured detector over approved recorded frames."""

    def __init__(self, settings: RelaySettings) -> None:
        if settings.detection_model_path is None:
            raise ValueError("recorded frame detector is not configured")
        self._detector = _LockedDetector(YoloXOnnxDetector(settings.detection_model_path))
        self._recordings = {
            item.recording_id: (item, self._load_image(item))
            for item in settings.detection_recordings
        }
        self._workers: dict[tuple[str, str, int], LiveDetectionWorker] = {}
        self._lock = RLock()

    def __call__(
        self, session_id: str, drone_id: int, recording_id: str, connection_epoch: int
    ) -> tuple[PerceptionEvent, ...]:
        configured = self._recordings.get(recording_id)
        if configured is None:
            raise ValueError("recorded frame is unavailable")
        recording, image = configured
        if drone_id != recording.drone_id:
            raise ValueError("recording is not bound to this aircraft")
        key = (session_id, recording_id, connection_epoch)
        with self._lock:
            worker = self._workers.get(key)
            if worker is None:
                worker = LiveDetectionWorker(
                    _RecordedFrame(image),
                    self._detector,
                    source_id=recording.source_id,
                    mission_id=recording.mission_id,
                    worker_run_id=hashlib.sha256(
                        f"{session_id}:{recording_id}:{connection_epoch}".encode()
                    ).hexdigest(),
                )
                self._workers[key] = worker
            return worker.poll()

    @staticmethod
    def _load_image(recording: DetectionRecording) -> np.ndarray:
        try:
            payload = recording.image_path.read_bytes()
        except OSError as error:
            raise ValueError("recorded frame cannot be read") from error
        if hashlib.sha256(payload).hexdigest() != recording.image_sha256:
            raise ValueError("recorded frame digest does not match configuration")
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("recorded frame is not a supported image")
        return image


class DetectionAttention:
    """Keep detection acknowledgement state separate from the intent and command paths."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: OrderedDict[tuple[str, str], DetectionRecord] = OrderedDict()
        self._sightings: OrderedDict[tuple[str, int, int, str], str] = OrderedDict()

    def record(
        self,
        session: RelaySession,
        drone_id: int,
        connection_epoch: int,
        events: Iterable[PerceptionEvent],
    ) -> list[dict[str, object]]:
        if type(drone_id) is not int or drone_id <= 0:
            raise ValueError("detection drone_id must be positive")
        identity = session.registry.active_connection_identity(drone_id)
        if identity is None:
            raise ValueError("detection aircraft is not active in this session")
        if identity[0] != connection_epoch:
            raise ValueError("detection aircraft connection changed before processing")
        recorded: list[dict[str, object]] = []
        proposals: list[tuple[tuple[str, int, int, str], DetectionRecord]] = []
        with self._lock:
            for event in events:
                if isinstance(event, ProcessedFrameEvent):
                    recorded.append(self._frame_event(session, drone_id, event))
                elif isinstance(event, SightingEvent):
                    rendered, proposal = self._sighting_event(
                        session, drone_id, connection_epoch, event
                    )
                    recorded.append(rendered)
                    if proposal is not None:
                        proposals.append(proposal)
                else:
                    raise ValueError("detection event must come from LiveDetectionWorker")
            if recorded:
                session.record_operator_events(recorded)
                for sighting_key, record in proposals:
                    self._sightings[sighting_key] = record.detection_id
                    self._records[(session.session_id, record.detection_id)] = record
                self._trim(session.session_id)
        return recorded

    def acknowledge(
        self, session: RelaySession, detection_id: str, operator_source: str
    ) -> list[dict[str, object]]:
        if operator_source != "console":
            raise ValueError("only the console operator can acknowledge a detection")
        key = (session.session_id, detection_id)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                raise ValueError("unknown detection_id")
            if record.acknowledged:
                raise ValueError("detection is already acknowledged")
            identity = session.registry.active_connection_identity(record.drone_id)
            if identity is None or identity[0] != record.connection_epoch:
                raise ValueError("detection belongs to a previous aircraft connection")
            event = {
                "v": 1,
                "t": session.clock(),
                "type": "detection_acknowledgement",
                "event_id": session.event_ids(),
                "session": session.session_id,
                "detection_id": detection_id,
                "drone_id": record.drone_id,
                "operator_source": operator_source,
            }
            session.record_operator_events([event])
            self._records[key] = DetectionRecord(
                detection_id, record.drone_id, record.connection_epoch, True
            )
        return [event]

    def _frame_event(
        self, session: RelaySession, drone_id: int, event: ProcessedFrameEvent
    ) -> dict[str, object]:
        return {
            "v": 1,
            "t": session.clock(),
            "type": "detection_frame",
            "event_id": event.event_id,
            "session": session.session_id,
            "drone_id": drone_id,
            "source_id": event.identity.source_id,
            "frame_id": event.identity.frame_id,
            "frame_decoded_at_monotonic_s": event.frame_decoded_at_monotonic_s,
            "evaluation_completed_at_monotonic_s": event.evaluation_completed_at_monotonic_s,
            "outcome": event.outcome,
            "candidate_count": event.candidate_count,
        }

    def _sighting_event(
        self, session: RelaySession, drone_id: int, connection_epoch: int, event: SightingEvent
    ) -> tuple[dict[str, object], tuple[tuple[str, int, int, str], DetectionRecord] | None]:
        sighting_key = (session.session_id, drone_id, connection_epoch, event.sighting_id)
        detection_id = self._sightings.get(sighting_key, event.event_id)
        existing = self._records.get((session.session_id, detection_id))
        attention = "promoted" if existing is None else "suppressed_duplicate"
        proposal = (
            None
            if existing is not None
            else (
                sighting_key,
                DetectionRecord(detection_id, drone_id, connection_epoch, False),
            )
        )
        return {
            "v": 1,
            "t": session.clock(),
            "type": "detection",
            "event_id": event.event_id,
            "session": session.session_id,
            "detection_id": detection_id,
            "drone_id": drone_id,
            "source_id": event.identity.source_id,
            "sighting_id": event.sighting_id,
            "frame_id": event.identity.frame_id,
            "label": event.candidate.label,
            "confidence": event.candidate.confidence,
            "bbox_xyxy": list(event.candidate.bbox_xyxy),
            "frame_decoded_at_monotonic_s": event.last_frame_decoded_at_monotonic_s,
            "evaluation_completed_at_monotonic_s": event.evaluation_completed_at_monotonic_s,
            "observation_count": event.observation_count,
            "attention": attention,
            "acknowledged": existing.acknowledged if existing is not None else False,
        }, proposal

    def _trim(self, session_id: str) -> None:
        keys = [key for key in self._records if key[0] == session_id]
        while len(keys) > MAX_RETAINED_DETECTIONS_PER_SESSION:
            key = keys.pop(0)
            record = self._records.pop(key)
            for sighting_key, stored_detection_id in tuple(self._sightings.items()):
                if stored_detection_id == record.detection_id:
                    del self._sightings[sighting_key]

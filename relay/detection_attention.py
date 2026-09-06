"""Turn verified perception observations into operator-only attention events."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from threading import RLock

from perception.detection_contracts import PerceptionEvent, ProcessedFrameEvent, SightingEvent
from relay.session import RelaySession


@dataclass(frozen=True, slots=True)
class DetectionRecord:
    detection_id: str
    drone_id: int
    acknowledged: bool


class DetectionAttention:
    """Keep detection acknowledgement state separate from the intent and command paths."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[tuple[str, str], DetectionRecord] = {}

    def record(
        self,
        session: RelaySession,
        drone_id: int,
        events: Iterable[PerceptionEvent],
    ) -> list[dict[str, object]]:
        if type(drone_id) is not int or drone_id <= 0:
            raise ValueError("detection drone_id must be positive")
        recorded: list[dict[str, object]] = []
        with self._lock:
            for event in events:
                if isinstance(event, ProcessedFrameEvent):
                    recorded.append(self._frame_event(session, drone_id, event))
                elif isinstance(event, SightingEvent):
                    recorded.append(self._sighting_event(session, drone_id, event))
                else:
                    raise ValueError("detection event must come from LiveDetectionWorker")
            if recorded:
                session.record_operator_events(recorded)
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
            self._records[key] = DetectionRecord(detection_id, record.drone_id, True)
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
        self, session: RelaySession, drone_id: int, event: SightingEvent
    ) -> dict[str, object]:
        detection_id = event.event_id
        attention = "promoted" if event.observation_count == 1 else "suppressed_duplicate"
        self._records[(session.session_id, detection_id)] = DetectionRecord(
            detection_id, drone_id, False
        )
        return {
            "v": 1,
            "t": session.clock(),
            "type": "detection",
            "event_id": detection_id,
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
            "acknowledged": False,
        }

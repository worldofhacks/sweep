"""Run a bounded, signed detector producer against a measured camera clock."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Protocol

from perception.object_detection import (
    DecodedFrame,
    FrameRead,
    FrameReader,
    LiveDetectionWorker,
    PerceptionEvent,
    ProcessedFrameEvent,
    SightingEvent,
    YoloXOnnxDetector,
)
from perception.webcam_stream import WebcamStream
from relay.auth import sign_event
from relay.control_localization import ClockMapping


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def _positive_int(value: object, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise ValueError(f"{name} must be a {'non-negative' if allow_zero else 'positive'} integer")
    return value


def _positive_number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


@dataclass(frozen=True, slots=True)
class DetectionPublisherConfig:
    session: str
    intent_id: str
    mission_id: str
    drone_id: int
    connection_epoch: int
    source_id: str
    camera_id: str
    clock_mapping: ClockMapping
    processing_clock_mapping: ClockMapping
    queue_limit: int = 64

    def __post_init__(self) -> None:
        for name in ("session", "intent_id", "mission_id", "source_id", "camera_id"):
            _text(getattr(self, name), name)
        _positive_int(self.drone_id, "drone_id")
        _positive_int(self.connection_epoch, "connection_epoch", allow_zero=True)
        _positive_int(self.queue_limit, "queue_limit")
        for mapping in (self.clock_mapping, self.processing_clock_mapping):
            if not isinstance(mapping, ClockMapping) or not mapping.measured:
                raise ValueError("detection publisher requires measured clock mappings")
        if self.clock_mapping.relay_clock_id != self.processing_clock_mapping.relay_clock_id:
            raise ValueError("detection clocks must map to the same relay clock")

    @classmethod
    def from_mapping(cls, raw: object) -> DetectionPublisherConfig:
        if not isinstance(raw, Mapping):
            raise ValueError("detection publisher configuration must be an object")
        return cls(
            _text(raw.get("session"), "session"),
            _text(raw.get("intent_id"), "intent_id"),
            _text(raw.get("mission_id"), "mission_id"),
            _positive_int(raw.get("drone_id"), "drone_id"),
            _positive_int(raw.get("connection_epoch"), "connection_epoch", allow_zero=True),
            _text(raw.get("source_id"), "source_id"),
            _text(raw.get("camera_id"), "camera_id"),
            ClockMapping.from_mapping(raw.get("pts_clock_mapping")),
            ClockMapping.from_mapping(raw.get("processing_clock_mapping")),
            _positive_int(raw.get("queue_limit", 64), "queue_limit"),
        )


class ClockMappedFrameReader:
    """Projects source PTS and local processing receipt time into the relay clock."""

    def __init__(
        self,
        reader: FrameReader,
        pts_clock_mapping: ClockMapping,
        processing_clock_mapping: ClockMapping,
    ) -> None:
        if (
            pts_clock_mapping.relay_clock_id != processing_clock_mapping.relay_clock_id
            or not pts_clock_mapping.measured
            or not processing_clock_mapping.measured
        ):
            raise ValueError("frame reader requires compatible measured clock mappings")
        self._reader = reader
        self._pts_clock_mapping = pts_clock_mapping
        self._processing_clock_mapping = processing_clock_mapping

    def read(self, timeout: float = 0.1) -> FrameRead:
        return self._map(self._reader.read(timeout))

    def read_timed(self, timeout: float = 0.1) -> FrameRead:
        timed_read = getattr(self._reader, "read_timed", None)
        raw = timed_read(timeout) if callable(timed_read) else self._reader.read(timeout)
        return self._map(raw)

    def _map(self, raw: FrameRead) -> FrameRead:
        if raw is None:
            return None
        if isinstance(raw, DecodedFrame):
            image = raw.image
            captured_at_s = raw.captured_at_s
            received_at_s = raw.received_at_s
            verified = raw.capture_time_verified
        elif isinstance(raw, tuple) and len(raw) == 2:
            image, captured_at_s = raw
            received_at_s = captured_at_s
            verified = False
        else:
            raise ValueError("frame reader returned an invalid frame sample")
        if (
            isinstance(captured_at_s, bool)
            or not isinstance(captured_at_s, int | float)
            or isinstance(received_at_s, bool)
            or not isinstance(received_at_s, int | float)
        ):
            raise ValueError("frame reader timestamps are invalid")
        return DecodedFrame(
            image,
            self._pts_clock_mapping.to_relay_ms(float(captured_at_s)) / 1000,
            self._processing_clock_mapping.to_relay_ms(float(received_at_s)) / 1000,
            bool(verified),
        )


class DetectionPublisher:
    """Signs relay-clock detector events and retains only the latest unsent frames."""

    def __init__(self, config: DetectionPublisherConfig, signing_key: bytes | str) -> None:
        if not isinstance(signing_key, bytes | str) or not signing_key:
            raise ValueError("detection publisher requires a signing key")
        self.config = config
        self._signing_key = signing_key
        self._queue: deque[dict[str, object]] = deque(maxlen=config.queue_limit)

    @property
    def pending(self) -> int:
        return len(self._queue)

    def poll_worker(self, worker: LiveDetectionWorker) -> tuple[dict[str, object], ...]:
        """Poll a clock-mapped worker once, retaining all outputs through reconnects."""
        return tuple(self.enqueue(event) for event in worker.poll())

    def enqueue(self, event: PerceptionEvent) -> dict[str, object]:
        frame = self._frame(event)
        self._queue.append(frame)
        return frame

    def next_frame(self) -> Mapping[str, object] | None:
        return self._queue[0] if self._queue else None

    def acknowledge_sent(self) -> None:
        if not self._queue:
            raise ValueError("no detection frame is pending")
        self._queue.popleft()

    def drain(self) -> tuple[dict[str, object], ...]:
        frames = tuple(self._queue)
        self._queue.clear()
        return frames

    def _frame(self, event: PerceptionEvent) -> dict[str, object]:
        if event.identity.mission_id != self.config.mission_id:
            raise ValueError("detector event mission does not match publisher configuration")
        if event.identity.source_id != self.config.source_id:
            raise ValueError("detector event source does not match publisher configuration")
        if isinstance(event, ProcessedFrameEvent):
            unsigned = self._processed_frame(event)
        elif isinstance(event, SightingEvent):
            unsigned = self._sighting_frame(event)
        else:
            raise ValueError("detector emitted an unknown event")
        return {**unsigned, "signature": sign_event(unsigned, self._signing_key)}

    def _common(self, event: PerceptionEvent) -> dict[str, object]:
        return {
            "v": 1,
            "source": "perception",
            "session": self.config.session,
            "intent_id": self.config.intent_id,
            "mission_id": self.config.mission_id,
            "drone_id": self.config.drone_id,
            "connection_epoch": self.config.connection_epoch,
            "source_id": self.config.source_id,
            "camera_id": self.config.camera_id,
            "capture_clock_id": self.config.clock_mapping.capture_clock_id,
            "relay_clock_id": self.config.clock_mapping.relay_clock_id,
            "clock_mapping": self.config.clock_mapping.to_mapping(),
            "event_id": event.event_id,
            "frame_id": event.identity.frame_id,
            "worker_run_id": event.identity.worker_run_id,
            "frame_sequence": event.identity.frame_sequence,
        }

    @staticmethod
    def _relay_ms(timestamp_s: float) -> int:
        if (
            isinstance(timestamp_s, bool)
            or not isinstance(timestamp_s, int | float)
            or not isfinite(timestamp_s)
        ):
            raise ValueError("detector event timestamp is invalid")
        return round(float(timestamp_s) * 1000)

    def _processed_frame(self, event: ProcessedFrameEvent) -> dict[str, object]:
        frame_decoded_ms = self._relay_ms(event.frame_decoded_at_monotonic_s)
        evaluation_started_ms = self._relay_ms(event.evaluation_started_at_monotonic_s)
        evaluation_completed_ms = self._relay_ms(event.evaluation_completed_at_monotonic_s)
        received_ms = None if event.received_at_s is None else self._relay_ms(event.received_at_s)
        return {
            **self._common(event),
            "type": "perception.frame_processed",
            "capture_timestamp_ms": frame_decoded_ms,
            "frame_decoded_at_monotonic_ms": frame_decoded_ms,
            "received_at_ms": received_ms,
            "processed_at_ms": evaluation_completed_ms,
            "evaluation_started_at_monotonic_ms": evaluation_started_ms,
            "evaluation_completed_at_monotonic_ms": evaluation_completed_ms,
            "outcome": event.outcome,
            "candidate_count": event.candidate_count,
            "capture_time_verified": event.capture_time_verified,
            "target_labels": list(event.target_labels),
            "detector_config_sha256": event.detector_config_sha256,
        }

    def _sighting_frame(self, event: SightingEvent) -> dict[str, object]:
        first_decoded_ms = self._relay_ms(event.first_frame_decoded_at_monotonic_s)
        last_decoded_ms = self._relay_ms(event.last_frame_decoded_at_monotonic_s)
        evaluation_started_ms = self._relay_ms(event.evaluation_started_at_monotonic_s)
        evaluation_completed_ms = self._relay_ms(event.evaluation_completed_at_monotonic_s)
        return {
            **self._common(event),
            "type": "perception.sighting",
            "sighting_id": event.sighting_id,
            "first_capture_timestamp_ms": first_decoded_ms,
            "last_capture_timestamp_ms": last_decoded_ms,
            "first_frame_decoded_at_monotonic_ms": first_decoded_ms,
            "last_frame_decoded_at_monotonic_ms": last_decoded_ms,
            "processed_at_ms": evaluation_completed_ms,
            "evaluation_started_at_monotonic_ms": evaluation_started_ms,
            "evaluation_completed_at_monotonic_ms": evaluation_completed_ms,
            "observation_count": event.observation_count,
            "detector_config_sha256": event.detector_config_sha256,
            **event.candidate.payload(),
        }


class AsyncSocket(Protocol):
    async def send(self, message: str) -> None: ...


class AsyncDetectionTransport:
    """Authenticates a perception connection and keeps the newest unsent frames on failure."""

    def __init__(
        self,
        websocket_url: str,
        producer_key: str,
        *,
        socket_factory: Callable[[], AbstractAsyncContextManager[AsyncSocket]] | None = None,
    ) -> None:
        self.websocket_url = _text(websocket_url, "websocket_url")
        self.producer_key = _text(producer_key, "producer_key")
        self._socket_factory = socket_factory

    async def publish_pending(self, publisher: DetectionPublisher) -> int:
        async with self._connect() as socket:
            await socket.send(
                json.dumps(
                    {"v": 1, "type": "auth", "source": "perception", "token": self.producer_key},
                    separators=(",", ":"),
                )
            )
            sent = 0
            while (frame := publisher.next_frame()) is not None:
                await socket.send(json.dumps(dict(frame), allow_nan=False, separators=(",", ":")))
                publisher.acknowledge_sent()
                sent += 1
            return sent

    def _connect(self) -> AbstractAsyncContextManager[AsyncSocket]:
        if self._socket_factory is not None:
            return self._socket_factory()
        from websockets.asyncio.client import connect

        return connect(self.websocket_url)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class DetectionProducerConfig:
    publisher: DetectionPublisherConfig
    websocket_url: str
    source_url: str
    model_path: str
    key_environment: str
    confidence_threshold: float = 0.5
    max_frame_age_s: float = 0.5
    sample_interval_s: float = 0.1
    reconnect_delay_s: float = 1.0

    def __post_init__(self) -> None:
        for name in ("websocket_url", "source_url", "model_path", "key_environment"):
            _text(getattr(self, name), name)
        for name in (
            "confidence_threshold",
            "max_frame_age_s",
            "sample_interval_s",
            "reconnect_delay_s",
        ):
            _positive_number(getattr(self, name), name)
        if not 0 < self.confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be between zero and one")

    @classmethod
    def from_mapping(cls, raw: object) -> DetectionProducerConfig:
        if not isinstance(raw, Mapping):
            raise ValueError("detection producer configuration must be an object")
        publisher = DetectionPublisherConfig.from_mapping(raw.get("publisher"))
        return cls(
            publisher,
            _text(raw.get("websocket_url"), "websocket_url"),
            _text(raw.get("source_url", raw.get("source_video")), "source_url"),
            _text(raw.get("model_path", raw.get("yolox_model")), "model_path"),
            _text(raw.get("key_environment"), "key_environment"),
            float(raw.get("confidence_threshold", 0.5)),
            float(raw.get("max_frame_age_s", 0.5)),
            float(raw.get("sample_interval_s", 0.1)),
            float(raw.get("reconnect_delay_s", 1.0)),
        )


async def run_producer(config: DetectionProducerConfig, producer_key: str) -> None:
    """Run the live RTSP + YOLOX producer until cancelled."""
    stream = WebcamStream(config.source_url).start()
    try:
        processing_clock = config.publisher.processing_clock_mapping

        def relay_clock() -> float:
            return processing_clock.to_relay_ms(time.monotonic()) / 1000

        worker = LiveDetectionWorker(
            ClockMappedFrameReader(
                stream,
                config.publisher.clock_mapping,
                config.publisher.processing_clock_mapping,
            ),
            YoloXOnnxDetector(config.model_path, confidence_threshold=config.confidence_threshold),
            source_id=config.publisher.source_id,
            mission_id=config.publisher.mission_id,
            max_frame_age_s=config.max_frame_age_s,
            sample_interval_s=config.sample_interval_s,
            retained_events=config.publisher.queue_limit,
            monotonic_clock=relay_clock,
        )
        publisher = DetectionPublisher(config.publisher, producer_key)
        transport = AsyncDetectionTransport(config.websocket_url, producer_key)
        while True:
            publisher.poll_worker(worker, now_s=None)
            try:
                await transport.publish_pending(publisher)
            except OSError:
                pass
            except Exception as error:
                from websockets.exceptions import WebSocketException

                if not isinstance(error, WebSocketException):
                    raise
            await asyncio.sleep(config.sample_interval_s)
            if publisher.pending:
                await asyncio.sleep(config.reconnect_delay_s)
    finally:
        stream.close()


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = DetectionProducerConfig.from_mapping(json.loads(args.config.read_text()))
    producer_key = os.environ.get(config.key_environment)
    if not producer_key:
        raise ValueError(f"missing producer key in {config.key_environment}")
    asyncio.run(run_producer(config, producer_key))


if __name__ == "__main__":
    _main()

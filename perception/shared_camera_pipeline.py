"""Run one RTSP decoder for localization, keyframe selection, and object detection."""

from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import cv2
import numpy as np

from perception.localization_lease import LocalizationLeaseStatus, ManagedWebcamLocalizer
from perception.object_detection import LiveDetectionWorker
from perception.webcam_localization import WebcamLocalization, load_config
from perception.webcam_stream import WebcamStream
from perception.yolox_onnx import YoloXOnnxDetector


def _monotonic(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _nonnegative(value: object) -> bool:
    return _monotonic(value)


def _positive(value: object) -> bool:
    return _monotonic(value) and value > 0


class _Stream(Protocol):
    def start(self) -> _Stream: ...

    def read(self, timeout: float = 0.0) -> tuple[np.ndarray, float] | None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CameraFrame:
    """A decoded frame whose exposure time is unavailable from this RTSP reader."""

    image: np.ndarray
    source_id: str
    sequence: int
    decoded_at_monotonic_s: float
    capture_time_monotonic_s: float | None = None
    capture_time_provenance: str = "unavailable"
    alignment_evidence: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.image, np.ndarray):
            raise ValueError("camera frame image must be an array")
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("camera source ID must be nonempty text")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("camera frame sequence must be positive")
        if not _monotonic(self.decoded_at_monotonic_s):
            raise ValueError("decoded time must be a monotonic timestamp")
        if self.capture_time_monotonic_s is not None:
            raise ValueError("WebcamStream does not provide camera capture timestamps")
        if self.capture_time_provenance != "unavailable":
            raise ValueError("capture-time provenance must remain unavailable")


@dataclass(frozen=True, slots=True)
class CameraPipelineConfig:
    detector_sample_interval_s: float = 0.2
    detector_max_frame_age_s: float = 0.5
    keyframe_sample_interval_s: float = 1.0
    keyframe_max_frame_age_s: float = 0.5
    decoder_read_timeout_s: float = 0.05

    def __post_init__(self) -> None:
        for name in (
            "detector_sample_interval_s",
            "detector_max_frame_age_s",
            "keyframe_sample_interval_s",
            "keyframe_max_frame_age_s",
            "decoder_read_timeout_s",
        ):
            value = getattr(self, name)
            if not _positive(value) or value > 1:
                raise ValueError(f"{name} must be a finite value from zero to one second")


class LatestFrameSubscription:
    """A latest-only view of the shared decoder for one consumer."""

    def __init__(self, owner: _SharedDecoder, name: str, seen_sequence: int) -> None:
        self._owner = owner
        self._name = name
        self._seen_sequence = seen_sequence
        self._closed = False

    def start(self) -> LatestFrameSubscription:
        return self

    def read(self, timeout: float = 0.0) -> tuple[np.ndarray, float] | None:
        record = self.read_record(timeout)
        if record is None:
            return None
        return record.image, record.decoded_at_monotonic_s

    def read_record(self, timeout: float = 0.0) -> CameraFrame | None:
        if self._closed:
            raise RuntimeError("camera subscription is closed")
        record = self._owner.next_after(self._seen_sequence, timeout)
        if record is not None:
            self._seen_sequence = record.sequence
        return record

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._owner.release(self._name)


class SharedCameraPipelineConstructionError(RuntimeError):
    """Construction failed after a decoder subscription was acquired."""

    def __init__(
        self, owner: SharedCameraPipeline, cause: Exception, cleanup_error: Exception
    ) -> None:
        super().__init__("shared camera pipeline could not release its decoder after setup failed")
        self.owner = owner
        self.cause = cause
        self.cleanup_error = cleanup_error


class _NoFrameReader:
    def read(self, timeout: float = 0.0) -> None:
        raise AssertionError("detector validation must not read a camera frame")


class _SharedDecoder:
    def __init__(
        self,
        stream_factory: Callable[[], _Stream],
        *,
        source_id: str,
        alignment_evidence: Mapping[str, object] | None,
        read_timeout_s: float,
    ) -> None:
        self._stream_factory = stream_factory
        self._source_id = source_id
        self._alignment_evidence = alignment_evidence
        self._read_timeout_s = read_timeout_s
        self._condition = threading.Condition()
        self._stream: _Stream | None = None
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._subscriptions: set[str] = set()
        self._latest: CameraFrame | None = None
        self._sequence = 0
        self._closing = False
        self._shutdown_failed = False
        self._pump_failure: str | None = None

    def subscribe(self, name: str) -> LatestFrameSubscription:
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError("consumer name must be nonempty text")
        with self._condition:
            while self._closing:
                self._condition.wait()
            if name in self._subscriptions:
                raise RuntimeError("camera consumer is already subscribed")
            if self._shutdown_failed:
                raise RuntimeError("camera decoder shutdown failed; reopen is blocked")
            if not self._subscriptions:
                self._start_locked()
            self._subscriptions.add(name)
            return LatestFrameSubscription(self, name, self._sequence)

    def _start_locked(self) -> None:
        if self._stream is not None:
            return
        try:
            stream = self._stream_factory().start()
        except Exception:
            raise RuntimeError("cannot start shared camera decoder") from None
        self._stream = stream
        self._pump_failure = None
        stop = threading.Event()
        self._stop = stop
        thread = threading.Thread(
            target=self._pump, args=(stream, stop), name="shared-camera", daemon=True
        )
        self._thread = thread
        try:
            thread.start()
        except Exception:
            self._thread = None
            self._stop = None
            try:
                stream.close()
            except Exception:
                self._shutdown_failed = True
            self._stream = None
            raise RuntimeError("cannot start shared camera pump") from None

    def _pump(self, stream: _Stream, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                received = stream.read(self._read_timeout_s)
            except Exception:
                with self._condition:
                    self._pump_failure = "decoder_read_failed"
                    self._condition.notify_all()
                return
            if received is None:
                continue
            image, decoded_at = received
            if not _monotonic(decoded_at):
                with self._condition:
                    self._pump_failure = "decoder_timestamp_invalid"
                    self._condition.notify_all()
                return
            with self._condition:
                self._sequence += 1
                self._latest = CameraFrame(
                    image=image,
                    source_id=self._source_id,
                    sequence=self._sequence,
                    decoded_at_monotonic_s=decoded_at,
                    alignment_evidence=self._alignment_evidence,
                )
                self._condition.notify_all()

    def next_after(self, sequence: int, timeout: float) -> CameraFrame | None:
        if not _nonnegative(timeout) or timeout > 1:
            raise ValueError("consumer timeout must be a finite value from zero to one second")
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._latest is None or self._latest.sequence <= sequence:
                if self._pump_failure is not None or timeout == 0:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._latest

    def release(self, name: str) -> None:
        with self._condition:
            self._subscriptions.discard(name)
            if self._subscriptions:
                return
            self._closing = True
            stream = self._stream
            thread = self._thread
            stop = self._stop
            if stream is None:
                self._closing = False
                self._condition.notify_all()
                return
            if stop is not None:
                stop.set()
        if thread is not None:
            thread.join(self._read_timeout_s + 0.2)
        try:
            stream.close()
        except Exception:
            with self._condition:
                self._shutdown_failed = True
                self._closing = False
                self._condition.notify_all()
            raise RuntimeError("shared camera decoder did not stop") from None
        with self._condition:
            self._stream = None
            self._thread = None
            self._stop = None
            self._latest = None
            self._closing = False
            self._condition.notify_all()

    @property
    def decoder_status(self) -> str:
        with self._condition:
            if self._shutdown_failed:
                return "shutdown_failed"
            if self._pump_failure is not None:
                return self._pump_failure
            if self._stream is None:
                return "stopped"
            status = getattr(self._stream, "status", None)
            return status if isinstance(status, str) else "running"

    @property
    def subscriber_count(self) -> int:
        with self._condition:
            return len(self._subscriptions)


class SelectedKeyframeWorker:
    """Delivers fresh, rate-limited frames to an existing map builder callback."""

    def __init__(
        self,
        subscription: LatestFrameSubscription,
        consumer: Callable[[CameraFrame], None],
        *,
        sample_interval_s: float,
        max_frame_age_s: float,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(consumer):
            raise ValueError("map builder must be callable")
        self._subscription = subscription
        self._consumer = consumer
        self._sample_interval_s = sample_interval_s
        self._max_frame_age_s = max_frame_age_s
        self._clock = monotonic_clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_selected_at: float | None = None
        self.failure_reason: str | None = None

    def start(self) -> SelectedKeyframeWorker:
        if self._thread is not None:
            raise RuntimeError("keyframe worker is already running")
        self._thread = threading.Thread(target=self._run, name="camera-keyframes", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            if self._thread is threading.current_thread():
                self._subscription.close()
                return
            self._thread.join(self._sample_interval_s + 0.2)
        self._subscription.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            frame = self._subscription.read_record(0)
            now = self._clock()
            if frame is not None and now - frame.decoded_at_monotonic_s <= self._max_frame_age_s:
                if self._last_selected_at is None or (
                    frame.decoded_at_monotonic_s - self._last_selected_at >= self._sample_interval_s
                ):
                    self._last_selected_at = frame.decoded_at_monotonic_s
                    try:
                        self._consumer(frame)
                    except Exception:
                        self.failure_reason = "map_builder_failed"
                        return
            self._stop.wait(self._sample_interval_s)


class SharedCameraPipeline:
    """Own one WebcamStream and schedule independent latest-frame consumers."""

    def __init__(
        self,
        url: str,
        localizer: object,
        *,
        source_id: str,
        stream_factory: Callable[[str], _Stream] = WebcamStream,
        detector: object | None = None,
        mission_id: str | None = None,
        on_detection: Callable[[object], None] | None = None,
        map_builder: Callable[[CameraFrame], None] | None = None,
        alignment_evidence: Mapping[str, object] | None = None,
        config: CameraPipelineConfig | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(url, str) or not url:
            raise ValueError("camera stream URL is invalid")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("camera source ID is invalid")
        if detector is not None and (not isinstance(mission_id, str) or not mission_id):
            raise ValueError("detector requires a mission ID")
        if config is None:
            config = CameraPipelineConfig()
        self._clock = monotonic_clock
        self._decoder = _SharedDecoder(
            lambda: stream_factory(url),
            source_id=source_id,
            alignment_evidence=alignment_evidence,
            read_timeout_s=config.decoder_read_timeout_s,
        )
        self._localization = ManagedWebcamLocalizer(
            lambda: self._decoder.subscribe("localization"), localizer
        )
        self._detector: LiveDetectionWorker | None = None
        self._detector_subscription: LatestFrameSubscription | None = None
        self._keyframes: SelectedKeyframeWorker | None = None
        self._keyframe_subscription: LatestFrameSubscription | None = None
        if on_detection is not None and not callable(on_detection):
            raise ValueError("on_detection must be callable")
        if map_builder is not None and not callable(map_builder):
            raise ValueError("map_builder must be callable")
        if detector is not None:
            LiveDetectionWorker(
                _NoFrameReader(),
                detector,
                source_id=source_id,
                mission_id=mission_id,
                max_frame_age_s=config.detector_max_frame_age_s,
                sample_interval_s=config.detector_sample_interval_s,
                monotonic_clock=monotonic_clock,
            )
        self._on_detection = on_detection
        self._map_builder = map_builder
        self._callback_lock = threading.RLock()
        self._callback_generation = 0
        self._active_callback_generation: int | None = None
        self._started = False
        try:
            if detector is not None:
                self._detector_subscription = self._decoder.subscribe("detector")
                self._detector = LiveDetectionWorker(
                    self._detector_subscription,
                    detector,
                    source_id=source_id,
                    mission_id=mission_id,
                    on_event=self._detection_callback,
                    max_frame_age_s=config.detector_max_frame_age_s,
                    sample_interval_s=config.detector_sample_interval_s,
                    monotonic_clock=monotonic_clock,
                )
            if map_builder is not None:
                self._keyframe_subscription = self._decoder.subscribe("map_builder")
                self._keyframes = SelectedKeyframeWorker(
                    self._keyframe_subscription,
                    self._map_callback,
                    sample_interval_s=config.keyframe_sample_interval_s,
                    max_frame_age_s=config.keyframe_max_frame_age_s,
                    monotonic_clock=monotonic_clock,
                )
        except Exception as error:
            cleanup_error = self._release_constructor_subscriptions()
            if cleanup_error is not None:
                raise SharedCameraPipelineConstructionError(self, error, cleanup_error) from None
            raise

    def _release_constructor_subscriptions(self) -> Exception | None:
        cleanup_error = None
        for subscription in (self._keyframe_subscription, self._detector_subscription):
            if subscription is None:
                continue
            try:
                subscription.close()
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = error
        return cleanup_error

    def _detection_callback(self, event: object) -> None:
        with self._callback_lock:
            callback = self._on_detection if self._active_callback_generation is not None else None
            if callback is not None:
                callback(event)

    def _map_callback(self, frame: CameraFrame) -> None:
        with self._callback_lock:
            callback = self._map_builder if self._active_callback_generation is not None else None
            if callback is not None:
                callback(frame)

    def _activate_callbacks(self) -> None:
        with self._callback_lock:
            self._callback_generation += 1
            self._active_callback_generation = self._callback_generation

    def _deactivate_callbacks(self) -> None:
        with self._callback_lock:
            self._active_callback_generation = None

    def start(self) -> SharedCameraPipeline:
        if self._started:
            raise RuntimeError("shared camera pipeline is already running")
        self._activate_callbacks()
        try:
            if self._detector is not None:
                self._detector.start()
            if self._keyframes is not None:
                self._keyframes.start()
        except Exception:
            self._deactivate_callbacks()
            try:
                self.close()
            except Exception:
                pass
            raise
        self._started = True
        return self

    def resume_localization(self, now: float) -> LocalizationLeaseStatus:
        return self._localization.resume(now)

    def pause_localization(self) -> LocalizationLeaseStatus:
        return self._localization.pause()

    def poll_localization(self, now: float) -> LocalizationLeaseStatus:
        return self._localization.poll(now)

    @property
    def localization_status(self) -> LocalizationLeaseStatus:
        return self._localization.status

    @property
    def decoder_status(self) -> str:
        return self._decoder.decoder_status

    @property
    def subscriber_count(self) -> int:
        return self._decoder.subscriber_count

    @property
    def detector_events(self) -> tuple[object, ...]:
        return () if self._detector is None else self._detector.events()

    def close(self) -> None:
        errors: list[Exception] = []
        self._deactivate_callbacks()
        if self._detector is not None:
            try:
                self._detector.close()
            except Exception as error:
                errors.append(error)
            if self._detector.failure_reason == "shutdown_timeout":
                errors.append(RuntimeError("camera detector did not stop"))
        if self._detector_subscription is not None:
            try:
                self._detector_subscription.close()
            except Exception as error:
                errors.append(error)
        if self._keyframes is not None:
            try:
                self._keyframes.close()
            except Exception as error:
                errors.append(error)
        try:
            self._localization.pause()
        except Exception as error:
            errors.append(error)
        if errors:
            raise errors[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--url-env", default="SWEEP_LOCALIZATION_RTSP_URL")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--allow-synthetic", action="store_true")
    parser.add_argument("--detector-model", type=Path)
    parser.add_argument("--detector-model-sha256")
    parser.add_argument("--mission-id")
    args = parser.parse_args()
    if not _positive(args.duration):
        raise SystemExit("duration must be positive seconds")
    url = os.environ.get(args.url_env, "")
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in ("rtsp", "rtsps") or not parsed_url.netloc:
        raise SystemExit("URL environment variable must contain an RTSP read URL")
    if bool(args.detector_model) != bool(args.detector_model_sha256):
        raise SystemExit("detector model and SHA-256 must be provided together")
    if args.detector_model and not args.mission_id:
        raise SystemExit("detector requires a mission ID")
    loop = WebcamLocalization(load_config(args.config), allow_synthetic=args.allow_synthetic)
    if parsed_url.path != "/" + loop.provenance["stream_path"]:
        raise SystemExit("RTSP path does not match the pinned source configuration")
    detector = (
        None
        if args.detector_model is None
        else YoloXOnnxDetector(
            args.detector_model, expected_model_sha256=args.detector_model_sha256
        )
    )
    pipeline = SharedCameraPipeline(
        url,
        loop,
        source_id=args.source_id,
        detector=detector,
        mission_id=args.mission_id,
        alignment_evidence=loop.provenance,
    )
    started = time.monotonic()
    pipeline.start()
    pipeline.resume_localization(started)
    try:
        with args.output.open("x", encoding="utf-8") as output:
            while time.monotonic() - started < args.duration:
                now = time.monotonic()
                status = pipeline.poll_localization(now)
                payload = status.payload() | {
                    "decoder_status": pipeline.decoder_status,
                    "subscriber_count": pipeline.subscriber_count,
                    "run_elapsed_s": now - started,
                }
                output.write(json.dumps(payload, allow_nan=False) + "\n")
                output.flush()
                time.sleep(0.01)
    except (OSError, RuntimeError, ValueError, TypeError, cv2.error) as error:
        raise SystemExit(f"shared camera pipeline failed ({type(error).__name__})") from None
    finally:
        pipeline.close()


if __name__ == "__main__":
    main()

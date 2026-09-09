"""Read-only, demand-driven camera detections, paired with the exact analyzed image."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, RLock, Thread
from urllib.parse import urlsplit

import cv2
import numpy as np

from perception.object_detection import COCO_LABELS, DetectionCandidate, YoloXOnnxDetector
from perception.webcam_stream import WebcamStream
from relay.search_detection_deployment import _model_path
from relay.settings import SettingsError

MAX_FRAME_AGE_S = 1.5
IDLE_SECONDS = 30


@dataclass(frozen=True)
class LiveDetectionSource:
    device_id: int
    camera_id: str
    stream: str
    stream_url: str = field(repr=False)
    resolution: tuple[int, int]
    model_path: Path
    model_sha256: str
    target_labels: tuple[str, ...] = COCO_LABELS


def load_live_detection_sources(path: Path | None) -> tuple[LiveDetectionSource, ...]:
    if path is None:
        return ()
    try:
        if path.stat().st_size > 65536:
            raise ValueError("configuration exceeds 64 KiB")
        raw = json.loads(path.read_bytes())
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "sources"}
            or raw["schema_version"] != 1
            or not isinstance(raw["sources"], list)
            or not 1 <= len(raw["sources"]) <= 8
        ):
            raise ValueError("expected schema_version 1 and one to eight camera sources")
        sources = []
        keys = set()
        for source in raw["sources"]:
            required = {
                "device_id",
                "camera_id",
                "stream",
                "stream_url",
                "resolution",
                "model_path",
                "model_sha256",
            }
            if not isinstance(source, dict) or not required <= source.keys() <= required | {
                "target_labels"
            }:
                raise ValueError("camera source fields are invalid")
            if type(source["device_id"]) is not int or source["device_id"] <= 0:
                raise ValueError("device_id must be positive")
            for name in ("camera_id", "stream", "stream_url", "model_path", "model_sha256"):
                if not isinstance(source[name], str) or not 1 <= len(source[name]) <= 2048:
                    raise ValueError("camera source text is invalid")
            url = urlsplit(source["stream_url"])
            if url.scheme not in {"rtsp", "rtsps"} or not url.hostname:
                raise ValueError("camera stream_url must be RTSP")
            resolution = source["resolution"]
            if (
                not isinstance(resolution, list)
                or len(resolution) != 2
                or any(type(v) is not int or not 1 <= v <= 1920 for v in resolution)
                or resolution[0] * resolution[1] > 1920 * 1080
            ):
                raise ValueError("camera resolution exceeds 1920x1080 pixels")
            labels = source.get("target_labels", list(COCO_LABELS))
            if (
                not isinstance(labels, list)
                or not labels
                or not all(isinstance(v, str) for v in labels)
                or not set(labels) <= set(COCO_LABELS)
                or len(set(labels)) != len(labels)
            ):
                raise ValueError("target_labels must be distinct COCO classes")
            key = (source["device_id"], source["camera_id"])
            if key in keys:
                raise ValueError("duplicate device camera")
            keys.add(key)
            model = _model_path(path.parent, source["model_path"], source["model_sha256"])
            sources.append(
                LiveDetectionSource(
                    source["device_id"],
                    source["camera_id"],
                    source["stream"],
                    source["stream_url"],
                    tuple(resolution),
                    model,
                    source["model_sha256"],
                    tuple(labels),
                )
            )
        return tuple(sources)
    except (OSError, TypeError, ValueError) as error:
        # Configuration can contain authenticated camera URLs. Never echo its contents.
        raise SettingsError("invalid SWEEP_LIVE_DETECTION_CONFIG") from error


def _detector(source: LiveDetectionSource):
    return YoloXOnnxDetector(
        source.model_path,
        expected_model_sha256=source.model_sha256,
        target_labels=source.target_labels,
    )


def _stream(source: LiveDetectionSource):
    return WebcamStream(source.stream_url, resolution=source.resolution)


class LiveDetectionService:
    def __init__(
        self,
        sources: tuple[LiveDetectionSource, ...],
        *,
        stream_factory: Callable = _stream,
        detector_factory: Callable = _detector,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.sources = {(s.device_id, s.camera_id): s for s in sources}
        self._stream_factory, self._detector_factory, self._clock = (
            stream_factory,
            detector_factory,
            clock,
        )
        self._workers: dict[tuple[int, str], _Worker] = {}
        self._lock = RLock()
        self._closed = False

    def snapshot(self, device_id: int, camera_id: str, epoch: int, stream: str) -> dict:
        key = device_id, camera_id
        source = self.sources.get(key)
        if source is None or source.stream != stream:
            return {"state": "unconfigured", "frame": None}
        with self._lock:
            if self._closed:
                return {"state": "stopped", "frame": None}
            worker = self._workers.get(key)
            if worker is not None and worker.epoch == epoch and worker.state == "failed":
                return worker.snapshot()
            if worker is None or not worker.thread.is_alive() or worker.epoch != epoch:
                if worker is not None:
                    worker.stop.set()
                    # A previous connection cannot keep a decoder beside its replacement.
                    worker.thread.join(3)
                    if worker.thread.is_alive():
                        return {"state": "stopping", "frame": None}
                worker = _Worker(
                    source, epoch, self._stream_factory, self._detector_factory, self._clock
                )
                self._workers[key] = worker
                worker.thread.start()
            return worker.snapshot()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            workers = tuple(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop.set()
        for worker in workers:
            worker.thread.join(3)


class _Worker:
    def __init__(self, source, epoch, stream_factory, detector_factory, clock):
        self.source, self.epoch = source, epoch
        self.stream_factory, self.detector_factory, self.clock = (
            stream_factory,
            detector_factory,
            clock,
        )
        self.stop = Event()
        self.lock = RLock()
        self.last_request = clock()
        self.state = "starting"
        self.frame = None
        self.decoded_at = 0.0
        self.thread = Thread(
            target=self.run,
            name=f"live-detection-{source.device_id}-{source.camera_id}",
            daemon=True,
        )

    def snapshot(self):
        with self.lock:
            self.last_request = self.clock()
            age = self.last_request - self.decoded_at
            fresh = self.frame is not None and 0 <= age <= MAX_FRAME_AGE_S
            return {
                "state": "live" if fresh else self.state if self.frame is None else "stale",
                "frame": {**self.frame, "age_ms": round(age * 1000)} if fresh else None,
            }

    def run(self):
        stream = None
        try:
            detector = self.detector_factory(self.source)
            stream = self.stream_factory(self.source)
            stream.start()
            sequence = 0
            previous = -1.0
            while not self.stop.is_set() and self.clock() - self.last_request < IDLE_SECONDS:
                item = stream.read(timeout=0.1)
                if item is None:
                    with self.lock:
                        self.state = "waiting_for_frame"
                    self.stop.wait(0.1)
                    continue
                image, decoded_at = item
                if decoded_at <= previous or not 0 <= self.clock() - decoded_at <= MAX_FRAME_AGE_S:
                    continue
                previous = decoded_at
                if (
                    not isinstance(image, np.ndarray)
                    or image.dtype != np.uint8
                    or image.shape != (self.source.resolution[1], self.source.resolution[0], 3)
                ):
                    raise ValueError("invalid frame")
                candidates = tuple(detector.detect(image))
                width, height = self.source.resolution
                if len(candidates) > 256 or any(
                    not isinstance(c, DetectionCandidate)
                    or c.label not in self.source.target_labels
                    or not (
                        0 <= c.bbox_xyxy[0] < c.bbox_xyxy[2] <= width
                        and 0 <= c.bbox_xyxy[1] < c.bbox_xyxy[3] <= height
                    )
                    for c in candidates
                ):
                    raise ValueError("invalid detections")
                ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if not ok or jpeg.nbytes > 1024 * 1024:
                    raise ValueError("invalid encoded frame")
                if not 0 <= self.clock() - decoded_at <= MAX_FRAME_AGE_S:
                    continue
                sequence += 1
                with self.lock:
                    self.frame = {
                        "sequence": sequence,
                        "width": width,
                        "height": height,
                        "jpeg_base64": base64.b64encode(jpeg).decode("ascii"),
                        "detections": [
                            {
                                "label": c.label,
                                "confidence": c.confidence,
                                "bbox_xyxy": list(c.bbox_xyxy),
                            }
                            for c in candidates
                        ],
                    }
                    self.decoded_at = decoded_at
                    self.state = "live"
                self.stop.wait(0.2)
        except Exception:
            with self.lock:
                self.state, self.frame = "failed", None
        finally:
            if stream is not None:
                stream.close()


def install_live_detection_routes(application, authorized_runtime):
    from fastapi import Depends, HTTPException
    from fastapi.responses import JSONResponse

    runtime_dependency = Depends(authorized_runtime)

    @application.get("/api/sessions/{session_id}/live-detections/{device_id}/{camera_id}/{epoch}")
    async def live_detections(
        session_id: str,
        device_id: int,
        camera_id: str,
        epoch: int,
        runtime=runtime_dependency,
    ):
        import asyncio

        session = runtime.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session unavailable.")
        node = next(
            (d for d in session.current_state()["drones"] if d["drone_id"] == device_id), None
        )
        if (
            node is None
            or node["connection_epoch"] != epoch
            or node["membership"] in {"disconnected", "leaving"}
        ):
            raise HTTPException(status_code=409, detail="The device connection changed.")
        cameras = node.get("cameras", [])
        camera = next((c for c in cameras if c["camera_id"] == camera_id), None)
        if camera is None:
            raise HTTPException(
                status_code=409, detail="Configure this camera in the relay media roster."
            )
        result = await asyncio.to_thread(
            application.state.live_detection.snapshot, device_id, camera_id, epoch, camera["stream"]
        )
        return JSONResponse(
            {
                "session": session_id,
                "device_id": device_id,
                "camera_id": camera_id,
                "connection_epoch": epoch,
                "stream": camera["stream"],
                **result,
            },
            headers={"Cache-Control": "no-store"},
        )

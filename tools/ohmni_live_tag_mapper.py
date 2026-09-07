"""Publish capture-timestamped, local-only Ohmni camera and tag evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from websockets.asyncio.client import connect

from perception.camera_tags import CameraTagDetector, read_calibration
from perception.ohmni_clock_probe import ClockProbeError, clock_mapping, probe_clock
from perception.ohmni_pts_capture import CapturedFrame, NutCaptureReader
from relay.observations import FramedPose, Observation, ObservationSubmission, decode_submission


class LiveMapperError(ValueError):
    pass


ARCHIVE_FORMAT = "ohmni.accepted-mapping-observation-archive.v1"
MAX_ARCHIVE_RECORDS = 1_024
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_DURATION_S = 120.0


@dataclass(frozen=True, slots=True)
class LiveScope:
    session: str
    device_id: int
    connection_epoch: int


@dataclass(frozen=True, slots=True)
class ArchiveConfig:
    pose_source_id: str
    lidar_source_id: str
    odom_frame: str
    body_frame: str
    lidar_frame: str
    max_records: int = MAX_ARCHIVE_RECORDS
    max_bytes: int = MAX_ARCHIVE_BYTES
    duration_s: float = MAX_ARCHIVE_DURATION_S

    def __post_init__(self) -> None:
        for name in (
            "pose_source_id",
            "lidar_source_id",
            "odom_frame",
            "body_frame",
            "lidar_frame",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or not value.isprintable()
            ):
                raise LiveMapperError(f"archive {name} must be bounded non-empty text")
        if (
            "world" in {self.odom_frame, self.body_frame, self.lidar_frame}
            or len({self.odom_frame, self.body_frame, self.lidar_frame}) != 3
        ):
            raise LiveMapperError("archive frames must describe distinct local frames")
        if type(self.max_records) is not int or not 1 <= self.max_records <= MAX_ARCHIVE_RECORDS:
            raise LiveMapperError(
                f"archive max records must be from one through {MAX_ARCHIVE_RECORDS}"
            )
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= MAX_ARCHIVE_BYTES:
            raise LiveMapperError(f"archive max bytes must be from one through {MAX_ARCHIVE_BYTES}")
        if (
            isinstance(self.duration_s, bool)
            or not isinstance(self.duration_s, int | float)
            or not math.isfinite(self.duration_s)
            or not 0 < self.duration_s <= MAX_ARCHIVE_DURATION_S
        ):
            raise LiveMapperError(
                "archive duration must be greater than zero through "
                f"{MAX_ARCHIVE_DURATION_S:g} seconds"
            )


class AcceptedObservationArchive:
    """Persist the relay's canonical acceptance echo for one mapper scope."""

    def __init__(
        self,
        output: Path,
        *,
        scope: LiveScope,
        mapper: MapperConfig,
        config: ArchiveConfig,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.output = output.absolute()
        self.scope = scope
        self.mapper = mapper
        self.config = config
        if (
            len(
                {
                    mapper.camera_source_id,
                    mapper.tag_source_id,
                    config.pose_source_id,
                    config.lidar_source_id,
                }
            )
            != 4
        ):
            raise LiveMapperError("archive camera, tag, pose, and lidar sources must be distinct")
        self.monotonic = monotonic
        self.started_at = monotonic()
        self.count = 0
        self.byte_count = 0
        self.digest = hashlib.sha256()
        self.kinds = {name: 0 for name in ("camera_frame", "tag_observation", "pose", "range_scan")}
        self._event_ids: set[tuple[str, str]] = set()
        self._reserved_output = False
        self._finished = False
        self.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(self.output)
        except FileExistsError as error:
            raise LiveMapperError(f"archive output already exists: {self.output}") from error
        self._reserved_output = True
        try:
            self._temporary = Path(
                tempfile.mkdtemp(prefix=f".{self.output.name}.", dir=self.output.parent)
            )
            self._stream = (self._temporary / "observations.jsonl").open("xb")
        except BaseException:
            shutil.rmtree(self.output, ignore_errors=True)
            raise

    def observe(self, raw: Mapping[str, object]) -> bool:
        if self._finished:
            raise LiveMapperError("archive is already complete")
        if self.monotonic() - self.started_at > self.config.duration_s:
            raise LiveMapperError("archive duration expired before the mapper completed")
        try:
            observation = Observation.parse(raw)
        except ValueError as error:
            raise LiveMapperError("relay sent an invalid accepted observation") from error
        if not self._selected(observation):
            return False
        event_key = (observation.submission.source_id, observation.submission.event_id)
        if event_key in self._event_ids:
            return False
        encoded = observation.encode() + b"\n"
        if self.count >= self.config.max_records:
            raise LiveMapperError("archive record bound reached before the mapper completed")
        if self.byte_count + len(encoded) > self.config.max_bytes:
            raise LiveMapperError("archive byte bound reached before the mapper completed")
        self._stream.write(encoded)
        self.digest.update(encoded)
        self._event_ids.add(event_key)
        self.count += 1
        self.byte_count += len(encoded)
        self.kinds[observation.submission.payload["kind"]] += 1
        return True

    def finish(self) -> dict[str, object]:
        if self._finished:
            raise LiveMapperError("archive is already complete")
        if self.monotonic() - self.started_at > self.config.duration_s:
            raise LiveMapperError("archive duration expired before the mapper completed")
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        manifest = {
            "format": ARCHIVE_FORMAT,
            "scope": {
                "session": self.scope.session,
                "device_id": self.scope.device_id,
                "connection_epoch": self.scope.connection_epoch,
            },
            "sources": {
                "camera": self.mapper.camera_source_id,
                "tag": self.mapper.tag_source_id,
                "pose": self.config.pose_source_id,
                "lidar": self.config.lidar_source_id,
            },
            "frames": {
                "odom": self.config.odom_frame,
                "body": self.config.body_frame,
                "camera": self.mapper.camera_frame,
                "lidar": self.config.lidar_frame,
            },
            "observations": {
                "path": "observations.jsonl",
                "count": self.count,
                "bytes": self.byte_count,
                "sha256": self.digest.hexdigest(),
                "kinds": dict(self.kinds),
            },
            "limits": {
                "max_records": self.config.max_records,
                "max_bytes": self.config.max_bytes,
                "duration_s": self.config.duration_s,
            },
        }
        with (self._temporary / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, allow_nan=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for name in ("observations.jsonl", "manifest.json"):
            os.replace(self._temporary / name, self.output / name)
        os.rmdir(self._temporary)
        self._reserved_output = False
        self._finished = True
        return manifest

    def abort(self) -> None:
        if self._finished:
            return
        if not self._stream.closed:
            self._stream.close()
        shutil.rmtree(self._temporary, ignore_errors=True)
        if self._reserved_output:
            shutil.rmtree(self.output, ignore_errors=True)
        self._reserved_output = False

    def _selected(self, observation: Observation) -> bool:
        submission = observation.submission
        if (
            submission.session != self.scope.session
            or submission.device_id != self.scope.device_id
            or submission.connection_epoch != self.scope.connection_epoch
            or submission.node_type != "ground"
        ):
            return False
        payload = submission.payload
        kind = payload["kind"]
        if (
            kind == "camera_frame"
            and submission.source_id == self.mapper.camera_source_id
            and submission.frame == self.mapper.camera_frame
        ):
            return True
        if (
            kind == "tag_observation"
            and submission.source_id == self.mapper.tag_source_id
            and submission.frame == self.mapper.camera_frame
        ):
            return True
        if kind == "pose" and submission.source_id == self.config.pose_source_id:
            pose = payload["pose"]
            return (
                submission.frame == self.config.odom_frame
                and pose["parent_frame"] == self.config.odom_frame
                and pose["child_frame"] == self.config.body_frame
            )
        if kind == "range_scan" and submission.source_id == self.config.lidar_source_id:
            pose = payload["sensor_pose"]
            return (
                submission.frame == self.config.lidar_frame
                and pose["parent_frame"] == self.config.odom_frame
                and pose["child_frame"] == self.config.lidar_frame
            )
        return False


@dataclass(frozen=True, slots=True)
class MapperConfig:
    session: str
    device_id: int
    camera_source_id: str
    tag_source_id: str
    camera_frame: str
    camera_serial: str
    calibration_id: str
    clock_id: str
    clock_mapping_id: str
    maximum_capture_lag_ns: int
    confidence: float
    covariance_m2: tuple[float, ...]

    def __post_init__(self) -> None:
        for name in (
            "session",
            "camera_source_id",
            "tag_source_id",
            "camera_frame",
            "camera_serial",
            "calibration_id",
            "clock_id",
            "clock_mapping_id",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or not value.isprintable()
            ):
                raise LiveMapperError(f"{name} must be bounded non-empty text")
        if self.camera_source_id == self.tag_source_id:
            raise LiveMapperError("camera and tag evidence require separate rate-limited sources")
        if type(self.device_id) is not int or not 1 <= self.device_id <= 2**31 - 1:
            raise LiveMapperError("device_id must be a positive signed 32-bit integer")
        if (
            type(self.maximum_capture_lag_ns) is not int
            or not 0 <= self.maximum_capture_lag_ns <= 60_000_000_000
        ):
            raise LiveMapperError("maximum capture lag must be from zero through sixty seconds")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not math.isfinite(self.confidence)
            or not 0 < self.confidence <= 1
        ):
            raise LiveMapperError("confidence must be finite and in (0, 1]")
        if len(self.covariance_m2) != 9 or any(
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            for value in self.covariance_m2
        ):
            raise LiveMapperError("tag covariance must contain nine finite values")
        covariance = np.asarray(self.covariance_m2, dtype=float).reshape(3, 3)
        if (
            not np.allclose(covariance, covariance.T, atol=1e-9)
            or np.linalg.eigvalsh(covariance).min() < -1e-9
        ):
            raise LiveMapperError("tag covariance must be positive semidefinite")


def scope_from_state(state: object, *, session: str, device_id: int) -> LiveScope:
    if (
        not isinstance(state, Mapping)
        or state.get("type") != "state"
        or state.get("session") != session
    ):
        raise LiveMapperError("relay did not provide the requested session state")
    drones = state.get("drones")
    if not isinstance(drones, list):
        raise LiveMapperError("relay state has no drone roster")
    matches = [
        drone
        for drone in drones
        if isinstance(drone, Mapping) and drone.get("drone_id") == device_id
    ]
    if len(matches) != 1:
        raise LiveMapperError("target ground node is not uniquely present in the relay roster")
    drone = matches[0]
    epoch = drone.get("connection_epoch")
    if (
        drone.get("node_type") != "ground"
        or drone.get("membership") != "joined"
        or type(epoch) is not int
        or epoch <= 0
    ):
        raise LiveMapperError("target ground node has no current joined epoch")
    return LiveScope(session=session, device_id=device_id, connection_epoch=epoch)


def _quaternion(rotation: np.ndarray) -> tuple[float, float, float, float]:
    trace = float(np.trace(rotation))
    if trace > 0:
        scale = math.sqrt(trace + 1.0) * 2
        values = (
            (rotation[2, 1] - rotation[1, 2]) / scale,
            (rotation[0, 2] - rotation[2, 0]) / scale,
            (rotation[1, 0] - rotation[0, 1]) / scale,
            0.25 * scale,
        )
    else:
        index = int(np.argmax(np.diag(rotation)))
        next_index, final_index = (index + 1) % 3, (index + 2) % 3
        scale = (
            math.sqrt(
                1.0
                + rotation[index, index]
                - rotation[next_index, next_index]
                - rotation[final_index, final_index]
            )
            * 2
        )
        xyz = [0.0, 0.0, 0.0]
        xyz[index] = 0.25 * scale
        xyz[next_index] = (rotation[index, next_index] + rotation[next_index, index]) / scale
        xyz[final_index] = (rotation[index, final_index] + rotation[final_index, index]) / scale
        values = (
            *xyz,
            (rotation[final_index, next_index] - rotation[next_index, final_index]) / scale,
        )
    if not np.isfinite(values).all():
        raise LiveMapperError("detector returned a non-finite tag orientation")
    return tuple(float(value) for value in values)


class LiveTagMapper:
    def __init__(
        self,
        config: MapperConfig,
        detector: CameraTagDetector,
        *,
        receipt_time_ns: Callable[[], int],
        event_ids: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        if (detector.camera_serial, detector.width, detector.height) != (
            config.camera_serial,
            detector.width,
            detector.height,
        ):
            raise LiveMapperError("detector camera identity does not match mapper configuration")
        self.config = config
        self.detector = detector
        self._receipt_time_ns = receipt_time_ns
        self._event_ids = event_ids

    def observations(
        self, scope: LiveScope, frame: CapturedFrame
    ) -> tuple[ObservationSubmission, ...]:
        if (scope.session, scope.device_id) != (self.config.session, self.config.device_id):
            raise LiveMapperError("relay scope does not match mapper identity")
        image = frame.image_bgr8
        if image.shape[:2] != (self.detector.height, self.detector.width):
            raise LiveMapperError("captured frame resolution does not match the pinned calibration")
        receipt_ns = self._receipt_time_ns()
        if type(receipt_ns) is not int or receipt_ns < frame.capture_time_ns:
            raise LiveMapperError("mapped robot receipt time precedes the V4L2 capture timestamp")
        if receipt_ns - frame.capture_time_ns > self.config.maximum_capture_lag_ns:
            raise LiveMapperError("V4L2 PTS is older than the qualified capture-lag bound")
        image_id = (
            f"{self.config.camera_source_id}:{scope.connection_epoch}:{frame.capture_time_ns}"
        )
        camera = self._submission(
            scope=scope,
            source_id=self.config.camera_source_id,
            event_id=f"camera:{self._event_ids()}",
            capture_ns=frame.capture_time_ns,
            receipt_ns=receipt_ns,
            payload={
                "kind": "camera_frame",
                "image_id": image_id,
                "sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                "width_px": int(image.shape[1]),
                "height_px": int(image.shape[0]),
                "calibration_id": self.config.calibration_id,
            },
        )
        events: list[ObservationSubmission] = [camera]
        detections = self.detector.detect(image)
        if not isinstance(detections, list):
            raise LiveMapperError("detector must return a bounded detection list")
        for detection in detections:
            if not isinstance(detection, Mapping):
                raise LiveMapperError("detector returned an invalid tag observation")
            events.append(
                self._tag_submission(
                    scope=scope,
                    image_id=image_id,
                    capture_ns=frame.capture_time_ns,
                    receipt_ns=self._receipt_time_ns(),
                    detection=detection,
                )
            )
        return tuple(events)

    def _tag_submission(
        self,
        *,
        scope: LiveScope,
        image_id: str,
        capture_ns: int,
        receipt_ns: int,
        detection: Mapping[str, object],
    ) -> ObservationSubmission:
        if type(receipt_ns) is not int or receipt_ns < capture_ns:
            raise LiveMapperError("mapped robot receipt time precedes the V4L2 capture timestamp")
        if receipt_ns - capture_ns > self.config.maximum_capture_lag_ns:
            raise LiveMapperError("V4L2 PTS is older than the qualified capture-lag bound")
        tag_id = detection.get("tag_id")
        accepted = detection.get("pose_accepted")
        if type(tag_id) is not int or type(accepted) is not bool:
            raise LiveMapperError("detector returned an invalid tag observation")
        pose = None
        if accepted:
            transform = np.asarray(detection.get("T_camera_tag"), dtype=float)
            if transform.shape != (4, 4) or not np.isfinite(transform).all():
                raise LiveMapperError("detector returned an invalid tag transform")
            pose = FramedPose(
                self.config.camera_frame,
                f"tag:{tag_id}",
                *[float(value) for value in transform[:3, 3]],
                *_quaternion(transform[:3, :3]),
            ).to_mapping()
        payload = {
            "kind": "tag_observation",
            "family": "tag36h11",
            "tag_id": tag_id,
            "image_id": image_id,
            "pose_accepted": accepted,
            "tag_pose": pose,
            "covariance_m2": list(self.config.covariance_m2) if accepted else None,
            "reason": detection.get("reason"),
            "size_m": detection.get("size_m"),
            "corners_px": detection.get("corners_px"),
            "pixel_frame": detection.get("pixel_frame"),
            "reprojection_rms_px": detection.get("reprojection_rms_px"),
        }
        return self._submission(
            scope=scope,
            source_id=self.config.tag_source_id,
            event_id=f"tag:{self._event_ids()}",
            capture_ns=capture_ns,
            receipt_ns=receipt_ns,
            payload=payload,
        )

    def _submission(
        self,
        *,
        scope: LiveScope,
        source_id: str,
        event_id: str,
        capture_ns: int,
        receipt_ns: int,
        payload: Mapping[str, object],
    ) -> ObservationSubmission:
        raw = {
            "v": 1,
            "type": "observation",
            "event_id": event_id,
            "session": scope.session,
            "device_id": scope.device_id,
            "connection_epoch": scope.connection_epoch,
            "source_id": source_id,
            "node_type": "ground",
            "frame": self.config.camera_frame,
            "confidence": self.config.confidence,
            "t_capture": {"clock_id": self.config.clock_id, "unit": "ns", "value": capture_ns},
            "t_source_receipt": {
                "clock_id": self.config.clock_id,
                "unit": "ns",
                "value": receipt_ns,
            },
            "clock_mapping_id": self.config.clock_mapping_id,
            "payload": dict(payload),
        }
        return decode_submission(json.dumps(raw, allow_nan=False, separators=(",", ":")))


class _Socket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str: ...


async def publish_observations(
    socket: _Socket,
    mapper: LiveTagMapper,
    frames: Sequence[CapturedFrame],
    *,
    tag_submit_interval_ms: int = 0,
    receive_timeout_s: float = 30.0,
    archive: AcceptedObservationArchive | None = None,
) -> int:
    scope = await _authenticated_scope(socket, mapper, receive_timeout_s)
    count, _ = await _publish_frames(
        socket,
        mapper,
        scope,
        frames,
        tag_submit_interval_ms,
        receive_timeout_s,
        archive=archive,
    )
    return count


def _receive_timeout(value: float) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise LiveMapperError("relay receive timeout must be a finite number")
    if not 1 <= value <= 120:
        raise LiveMapperError("relay receive timeout must be from one through 120 seconds")
    return float(value)


async def _receive_until(socket: _Socket, deadline: float) -> str:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise LiveMapperError("timed out waiting for relay confirmation")
    try:
        return await asyncio.wait_for(socket.recv(), remaining)
    except TimeoutError as error:
        raise LiveMapperError("timed out waiting for relay confirmation") from error


async def _authenticated_scope(
    socket: _Socket, mapper: LiveTagMapper, receive_timeout_s: float
) -> LiveScope:
    deadline = asyncio.get_running_loop().time() + _receive_timeout(receive_timeout_s)
    accepted = json.loads(await _receive_until(socket, deadline))
    if not isinstance(accepted, Mapping) or accepted.get("type") != "auth.accepted":
        raise LiveMapperError("relay did not accept localization authentication")
    state = json.loads(await _receive_until(socket, deadline))
    return scope_from_state(state, session=mapper.config.session, device_id=mapper.config.device_id)


async def _confirm_submission(
    socket: _Socket,
    scope: LiveScope,
    event: ObservationSubmission,
    receive_timeout_s: float,
    archive: AcceptedObservationArchive | None = None,
) -> None:
    deadline = asyncio.get_running_loop().time() + _receive_timeout(receive_timeout_s)
    while True:
        raw = json.loads(await _receive_until(socket, deadline))
        if not isinstance(raw, Mapping):
            continue
        if raw.get("type") == "observation" and archive is not None:
            archive.observe(raw)
        if raw.get("type") == "state":
            if scope_from_state(raw, session=scope.session, device_id=scope.device_id) != scope:
                raise LiveMapperError("target ground epoch changed while publishing")
            continue
        if raw.get("type") == "protocol_refusal":
            raise LiveMapperError(f"relay rejected map evidence: {raw.get('reason')}")
        if raw.get("type") == "observation" and raw.get("event_id") == event.event_id:
            return


async def _publish_frames(
    socket: _Socket,
    mapper: LiveTagMapper,
    scope: LiveScope,
    frames: Sequence[CapturedFrame],
    tag_submit_interval_ms: int,
    receive_timeout_s: float,
    last_tag_sent_at: float | None = None,
    archive: AcceptedObservationArchive | None = None,
) -> tuple[int, float | None]:
    if type(tag_submit_interval_ms) is not int or not 0 <= tag_submit_interval_ms <= 1_000:
        raise LiveMapperError("tag submit interval must be from zero through one thousand ms")
    count = 0
    interval_s = tag_submit_interval_ms / 1_000
    for frame in frames:
        for event in mapper.observations(scope, frame):
            is_tag = event.source_id == mapper.config.tag_source_id
            if is_tag and last_tag_sent_at is not None and interval_s:
                remaining = interval_s - (asyncio.get_running_loop().time() - last_tag_sent_at)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            await socket.send(
                json.dumps(event.to_mapping(), allow_nan=False, separators=(",", ":"))
            )
            if is_tag:
                last_tag_sent_at = asyncio.get_running_loop().time()
            await _confirm_submission(socket, scope, event, receive_timeout_s, archive)
            count += 1
    return count, last_tag_sent_at


async def _publish_reader(
    socket: _Socket,
    mapper: LiveTagMapper,
    scope: LiveScope,
    frames: Iterator[CapturedFrame],
    tag_submit_interval_ms: int,
    receive_timeout_s: float,
    archive: AcceptedObservationArchive | None = None,
) -> int:
    count = 0
    last_tag_sent_at: float | None = None
    while True:
        try:
            frame = next(frames)
        except StopIteration:
            return count
        published, last_tag_sent_at = await _publish_frames(
            socket,
            mapper,
            scope,
            (frame,),
            tag_submit_interval_ms,
            receive_timeout_s,
            last_tag_sent_at,
            archive,
        )
        count += published


async def _drain_archive(
    socket: _Socket,
    scope: LiveScope,
    archive: AcceptedObservationArchive,
    duration_s: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + duration_s
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        try:
            received = await asyncio.wait_for(socket.recv(), remaining)
        except TimeoutError:
            return
        raw = json.loads(received)
        if not isinstance(raw, Mapping):
            continue
        if raw.get("type") == "state":
            if scope_from_state(raw, session=scope.session, device_id=scope.device_id) != scope:
                raise LiveMapperError("target ground epoch changed while archiving")
        elif raw.get("type") == "protocol_refusal":
            raise LiveMapperError(f"relay rejected map evidence: {raw.get('reason')}")
        elif raw.get("type") == "observation":
            archive.observe(raw)


def _tag_sizes(value: str) -> dict[int, float]:
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError("tag sizes must be a JSON object") from error
    if not isinstance(raw, dict):
        raise argparse.ArgumentTypeError("tag sizes must be a JSON object")
    result: dict[int, float] = {}
    for key, size in raw.items():
        try:
            identifier = int(key)
        except (TypeError, ValueError) as error:
            raise argparse.ArgumentTypeError("tag IDs must be integers") from error
        result[identifier] = float(size)
    return result


def _covariance(value: str) -> tuple[float, ...]:
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError("covariance must be a JSON array") from error
    if not isinstance(raw, list) or len(raw) != 9:
        raise argparse.ArgumentTypeError("covariance must have nine values")
    return tuple(float(item) for item in raw)


async def _serve_one(port: int, timeout_s: float) -> socket.socket:
    received: asyncio.Future[socket.socket] = asyncio.get_running_loop().create_future()

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if received.done():
            writer.close()
            await writer.wait_closed()
            return
        sock = writer.get_extra_info("socket")
        if not isinstance(sock, socket.socket):
            raise LiveMapperError("PTS sidecar did not provide a socket")
        received.set_result(sock.dup())
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(accept, "127.0.0.1", port)
    try:
        try:
            return await asyncio.wait_for(received, timeout_s)
        except TimeoutError as error:
            raise LiveMapperError("timed out waiting for the robot PTS sidecar") from error
    finally:
        server.close()
        await server.wait_closed()


def _adb_clock_query(adb: str, serial: str) -> str:
    result = subprocess.run(
        [
            adb,
            "-s",
            serial,
            "shell",
            "cat /proc/sys/kernel/random/boot_id; cat /proc/uptime",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise LiveMapperError("ADB clock probe failed")
    return result.stdout


def _adb_reverse(adb: str, serial: str, port: int) -> None:
    result = subprocess.run(
        [adb, "-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise LiveMapperError("ADB PTS sidecar reverse tunnel setup failed")


def _remove_adb_reverse(adb: str, serial: str, port: int) -> None:
    subprocess.run(
        [adb, "-s", serial, "reverse", "--remove", f"tcp:{port}"],
        check=False,
        capture_output=True,
        text=True,
    )


def _qualify_clock(args: argparse.Namespace):
    try:
        mapping = clock_mapping(
            tuple(
                probe_clock(
                    lambda: _adb_clock_query(args.adb, args.adb_serial),
                    monotonic_ns=time.monotonic_ns,
                )
                for _ in range(args.clock_probes)
            ),
            maximum_error_ns=args.maximum_clock_error_ms * 1_000_000,
        )
    except ClockProbeError as error:
        raise LiveMapperError(f"robot monotonic clock qualification failed: {error}") from error
    if mapping.boot_id != args.boot_id:
        raise LiveMapperError("robot boot ID does not match the pinned clock configuration")
    return mapping


async def _main_async(args: argparse.Namespace) -> int:
    calibration = read_calibration(args.calibration, args.calibration_sha256)
    detector = CameraTagDetector(
        calibration,
        camera_serial=args.camera_serial,
        tag_sizes_m=args.tag_sizes,
    )
    config = MapperConfig(
        session=args.session,
        device_id=args.device_id,
        camera_source_id=args.camera_source_id,
        tag_source_id=args.tag_source_id,
        camera_frame=args.camera_frame,
        camera_serial=args.camera_serial,
        calibration_id=f"sha256:{args.calibration_sha256}",
        clock_id=args.clock_id,
        clock_mapping_id=args.clock_mapping_id,
        maximum_capture_lag_ns=args.maximum_capture_lag_ms * 1_000_000,
        confidence=args.confidence,
        covariance_m2=args.covariance,
    )
    mapping = _qualify_clock(args)
    archive_output = getattr(args, "archive_output", None)
    archive_config = (
        None
        if archive_output is None
        else ArchiveConfig(
            pose_source_id=getattr(args, "archive_pose_source_id", "ohmni-pose"),
            lidar_source_id=getattr(args, "archive_lidar_source_id", "ohmni-lidar"),
            odom_frame=getattr(args, "archive_odom_frame", "odom"),
            body_frame=getattr(args, "archive_body_frame", "body"),
            lidar_frame=getattr(args, "archive_lidar_frame", "lidar"),
            max_records=getattr(args, "archive_max_records", MAX_ARCHIVE_RECORDS),
            max_bytes=getattr(args, "archive_max_bytes", MAX_ARCHIVE_BYTES),
            duration_s=getattr(args, "archive_duration_s", MAX_ARCHIVE_DURATION_S),
        )
    )
    _adb_reverse(args.adb, args.adb_serial, args.pts_port)
    source: socket.socket | None = None
    archive: AcceptedObservationArchive | None = None
    archive_manifest: dict[str, object] | None = None
    try:
        mapper = LiveTagMapper(
            config, detector, receipt_time_ns=lambda: mapping.robot_time_ns(time.monotonic_ns())
        )
        source = await _serve_one(args.pts_port, args.sidecar_connect_timeout_s)
        with source.makefile("rb") as stream:
            frames = NutCaptureReader(stream).frames()
            async with connect(f"{args.relay_url.rstrip('/')}/ws/{args.session}") as relay:
                await relay.send(
                    json.dumps(
                        {
                            "v": 1,
                            "type": "auth",
                            "source": "localization",
                            "drone_id": args.device_id,
                            "token": args.token,
                        }
                    )
                )
                scope = await _authenticated_scope(relay, mapper, args.relay_receive_timeout_s)
                if archive_config is not None:
                    archive = AcceptedObservationArchive(
                        archive_output,
                        scope=scope,
                        mapper=config,
                        config=archive_config,
                    )
                count = await _publish_reader(
                    relay,
                    mapper,
                    scope,
                    frames,
                    args.tag_submit_interval_ms,
                    args.relay_receive_timeout_s,
                    archive,
                )
                if archive is not None:
                    await _drain_archive(
                        relay, scope, archive, getattr(args, "archive_drain_s", 2.0)
                    )
                    archive_manifest = archive.finish()
    finally:
        if archive is not None:
            archive.abort()
        if source is not None:
            source.close()
        _remove_adb_reverse(args.adb, args.adb_serial, args.pts_port)
    result = {"published": count}
    if archive_manifest is not None:
        result["archive"] = str(archive_output)
        result["archived_observations"] = archive_manifest["observations"]["count"]
    print(json.dumps(result))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relay-url", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--device-id", required=True, type=int)
    parser.add_argument("--token", required=True)
    parser.add_argument("--pts-port", required=True, type=int)
    parser.add_argument("--sidecar-connect-timeout-s", required=True, type=float)
    parser.add_argument("--tag-submit-interval-ms", required=True, type=int)
    parser.add_argument("--relay-receive-timeout-s", required=True, type=float)
    parser.add_argument("--camera-source-id", default="ohmni-live-camera")
    parser.add_argument("--tag-source-id", default="ohmni-live-tag")
    parser.add_argument("--camera-frame", default="camera")
    parser.add_argument("--camera-serial", required=True)
    parser.add_argument("--calibration", required=True, type=Path)
    parser.add_argument("--calibration-sha256", required=True)
    parser.add_argument("--clock-id", required=True)
    parser.add_argument("--clock-mapping-id", required=True)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--adb-serial", required=True)
    parser.add_argument("--boot-id", required=True)
    parser.add_argument("--clock-probes", required=True, type=int)
    parser.add_argument("--maximum-clock-error-ms", required=True, type=int)
    parser.add_argument("--maximum-capture-lag-ms", required=True, type=int)
    parser.add_argument("--confidence", required=True, type=float)
    parser.add_argument("--covariance", required=True, type=_covariance)
    parser.add_argument("--tag-sizes", required=True, type=_tag_sizes)
    parser.add_argument("--archive-output", type=Path)
    parser.add_argument("--archive-pose-source-id", default="ohmni-pose")
    parser.add_argument("--archive-lidar-source-id", default="ohmni-lidar")
    parser.add_argument("--archive-odom-frame", default="odom")
    parser.add_argument("--archive-body-frame", default="body")
    parser.add_argument("--archive-lidar-frame", default="lidar")
    parser.add_argument("--archive-max-records", type=int, default=MAX_ARCHIVE_RECORDS)
    parser.add_argument("--archive-max-bytes", type=int, default=MAX_ARCHIVE_BYTES)
    parser.add_argument("--archive-duration-s", type=float, default=MAX_ARCHIVE_DURATION_S)
    parser.add_argument("--archive-drain-s", type=float, default=2.0)
    args = parser.parse_args(argv)
    if (
        args.clock_probes < 1
        or args.maximum_clock_error_ms < 0
        or args.maximum_capture_lag_ms < 0
        or not math.isfinite(args.sidecar_connect_timeout_s)
        or not 1 <= args.sidecar_connect_timeout_s <= 120
        or not 1024 <= args.pts_port <= 65_535
        or not 0 <= args.tag_submit_interval_ms <= 1_000
        or not 1 <= args.relay_receive_timeout_s <= 120
        or not math.isfinite(args.relay_receive_timeout_s)
        or not isinstance(args.archive_drain_s, int | float)
        or isinstance(args.archive_drain_s, bool)
        or not math.isfinite(args.archive_drain_s)
        or not 0 <= args.archive_drain_s <= 10
    ):
        parser.error("clock qualification bounds must be nonnegative with at least one probe")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

"""Publish verified control-localization snapshots from bounded local sensor records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from math import isfinite
from pathlib import Path
from typing import Literal, Protocol, TextIO

from perception.control_localization import (
    BodyExtrinsics,
    ControlLocalization,
    ControlLocalizationConfig,
    HeightObservation,
    TagFix,
    VelocityObservation,
)
from relay.control_frames import sign_localization_frame
from relay.control_localization import ClockMapping, ControlLocalizationWire, to_wire_payload
from relay.control_localization_contracts import position_uncertainty_p95_m

LIVE_PUBLISH_INTERVAL_S = 0.1


def _host_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError("live capture clock requires the current Linux boot ID") from error
    if not value:
        raise ValueError("live capture clock requires the current Linux boot ID")
    return value


class PublisherTransport(Protocol):
    def authenticate(self, drone_id: int, token: str) -> None: ...

    def send(self, frame: Mapping[str, object]) -> None: ...


class WebSocketPublisherTransport:
    """One authenticated localization socket per aircraft; reconnects send only the new frame."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._sockets: dict[int, object] = {}
        self._tokens: dict[int, str] = {}

    def authenticate(self, drone_id: int, token: str) -> None:
        from websockets.sync.client import connect

        previous = self._sockets.pop(drone_id, None)
        if previous is not None:
            previous.close()
        socket = connect(self.url)
        socket.send(
            json.dumps(
                {
                    "v": 1,
                    "type": "auth",
                    "source": "localization",
                    "drone_id": drone_id,
                    "token": token,
                }
            )
        )
        self._sockets[drone_id] = socket
        self._tokens[drone_id] = token

    def send(self, frame: Mapping[str, object]) -> None:
        drone_id = frame.get("drone_id")
        if isinstance(drone_id, bool) or not isinstance(drone_id, int):
            raise ValueError("control localization frame lacks a drone id")
        socket = self._sockets.get(drone_id)
        if socket is None:
            self.authenticate(drone_id, self._tokens[drone_id])
            socket = self._sockets[drone_id]
        try:
            socket.send(json.dumps(dict(frame), allow_nan=False))
        except OSError:
            self.authenticate(drone_id, self._tokens[drone_id])
            self._sockets[drone_id].send(json.dumps(dict(frame), allow_nan=False))


@dataclass(frozen=True, slots=True)
class MonotonicCaptureClock:
    source: Literal["process_monotonic"]
    boot_id: str
    monotonic_reference_s: float
    capture_reference_s: float

    def __post_init__(self) -> None:
        if (
            self.source != "process_monotonic"
            or not self.boot_id
            or not isfinite(self.monotonic_reference_s)
            or not isfinite(self.capture_reference_s)
        ):
            raise ValueError("live capture clock is invalid")

    @classmethod
    def from_mapping(cls, raw: object) -> MonotonicCaptureClock:
        if not isinstance(raw, Mapping) or set(raw) != {
            "source",
            "boot_id",
            "monotonic_reference_s",
            "capture_reference_s",
        }:
            raise ValueError("live capture clock must be an exact object")
        return cls(
            source=raw["source"],
            boot_id=_text(raw["boot_id"], "boot_id"),
            monotonic_reference_s=_number(raw["monotonic_reference_s"], "monotonic_reference_s"),
            capture_reference_s=_number(raw["capture_reference_s"], "capture_reference_s"),
        )

    def capture_time(self, monotonic_s: float) -> float:
        elapsed_s = _number(monotonic_s, "monotonic_s") - self.monotonic_reference_s
        return self.capture_reference_s + elapsed_s

    def verify_current_boot(self) -> None:
        if self.boot_id != _host_boot_id():
            raise ValueError("live capture clock boot ID differs from the measured reference")


@dataclass(frozen=True, slots=True)
class PublisherDroneConfig:
    fuser: ControlLocalizationConfig
    clock_mapping: ClockMapping
    key_environment: str
    max_position_uncertainty_m: float
    live_capture_clock: MonotonicCaptureClock | None

    def __post_init__(self) -> None:
        if (
            not self.key_environment
            or self.clock_mapping.capture_clock_id != self.fuser.clock_id
            or self.max_position_uncertainty_m <= 0
            or self.live_capture_clock is not None
            and self.live_capture_clock.capture_reference_s
            != self.clock_mapping.capture_reference_s
        ):
            raise ValueError("publisher drone configuration is invalid")


@dataclass(frozen=True, slots=True)
class ControlPublisherConfig:
    mode: Literal["live", "replay"]
    session: str
    websocket_url: str | None
    drones: Mapping[int, PublisherDroneConfig]
    queue_limit: int = 64

    def __post_init__(self) -> None:
        if (
            self.mode not in {"live", "replay"}
            or not self.session
            or (self.mode == "live" and not self.websocket_url)
            or (self.mode == "replay" and self.websocket_url is not None)
            or (
                self.mode == "live"
                and any(item.live_capture_clock is None for item in self.drones.values())
            )
            or self.queue_limit < 1
            or any(drone_id != config.fuser.drone_id for drone_id, config in self.drones.items())
        ):
            raise ValueError("publisher configuration is invalid")

    @classmethod
    def from_mapping(cls, raw: object) -> ControlPublisherConfig:
        if not isinstance(raw, Mapping):
            raise ValueError("publisher configuration must be an object")
        mode = raw.get("mode")
        session = raw.get("session")
        url = raw.get("websocket_url")
        entries = raw.get("drones")
        if (
            mode not in {"live", "replay"}
            or not isinstance(session, str)
            or not session
            or not isinstance(entries, list)
            or not entries
            or url is not None
            and not isinstance(url, str)
        ):
            raise ValueError("publisher configuration fields are invalid")
        drones: dict[int, PublisherDroneConfig] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("publisher drones must be objects")
            fuser_raw = entry.get("fuser")
            if not isinstance(fuser_raw, Mapping):
                raise ValueError("publisher drone requires fuser configuration")
            try:
                fuser = ControlLocalizationConfig(**dict(fuser_raw))
            except TypeError as error:
                raise ValueError("publisher fuser configuration is invalid") from error
            config = PublisherDroneConfig(
                fuser=fuser,
                clock_mapping=ClockMapping.from_mapping(entry.get("clock_mapping")),
                key_environment=_text(entry.get("key_environment"), "key_environment"),
                max_position_uncertainty_m=_number(
                    entry.get("max_position_uncertainty_m"), "max_position_uncertainty_m"
                ),
                live_capture_clock=(
                    None
                    if entry.get("live_capture_clock") is None
                    else MonotonicCaptureClock.from_mapping(entry["live_capture_clock"])
                ),
            )
            if fuser.drone_id in drones:
                raise ValueError("publisher drone ids must be unique")
            drones[fuser.drone_id] = config
        queue_limit = raw.get("queue_limit", 64)
        if isinstance(queue_limit, bool) or not isinstance(queue_limit, int):
            raise ValueError("queue_limit must be an integer")
        return cls(mode, session, url, drones, queue_limit)

    @property
    def identity_sha256(self) -> str:
        payload = {
            "mode": self.mode,
            "session": self.session,
            "websocket_url": self.websocket_url,
            "queue_limit": self.queue_limit,
            "drones": [
                {
                    "fuser": asdict(config.fuser),
                    "clock_mapping": config.clock_mapping.to_mapping(),
                    "key_environment": config.key_environment,
                    "max_position_uncertainty_m": config.max_position_uncertainty_m,
                    "live_capture_clock": (
                        None
                        if config.live_capture_clock is None
                        else asdict(config.live_capture_clock)
                    ),
                }
                for _, config in sorted(self.drones.items())
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class ControlPublisher:
    def __init__(
        self, config: ControlPublisherConfig, transport: PublisherTransport | None = None
    ) -> None:
        if config.mode == "live" and transport is None:
            raise ValueError("live publisher requires a transport")
        self.config = config
        self.transport = transport
        self.fusers = {
            drone_id: ControlLocalization(item.fuser) for drone_id, item in config.drones.items()
        }
        self.queues = {drone_id: deque(maxlen=config.queue_limit) for drone_id in config.drones}
        self.keys: dict[int, str] = {}
        self.invalid_reasons: dict[int, str] = {}
        self.sequence = 0

    def bind_live_credentials(self, environment: Mapping[str, str] | None = None) -> None:
        if self.config.mode != "live" or self.transport is None:
            raise ValueError("replay mode cannot authenticate or connect")
        environment = os.environ if environment is None else environment
        for drone_id, config in self.config.drones.items():
            assert config.live_capture_clock is not None
            config.live_capture_clock.verify_current_boot()
            token = environment.get(config.key_environment)
            if not token:
                raise ValueError(f"missing localization key for drone {drone_id}")
            self.keys[drone_id] = token
            self.transport.authenticate(drone_id, token)

    def enqueue(self, raw: object) -> None:
        if not isinstance(raw, Mapping):
            raise ValueError("sensor record must be an object")
        drone_id = raw.get("drone_id")
        if (
            isinstance(drone_id, bool)
            or not isinstance(drone_id, int)
            or drone_id not in self.queues
        ):
            raise ValueError("sensor record drone is not configured")
        self.queues[drone_id].append(dict(raw))

    def process(self, drone_id: int, now_s: float) -> None:
        queue = self.queues[drone_id]
        while queue:
            raw = queue.popleft()
            try:
                self._ingest(raw, now_s)
            except ValueError:
                self.invalid_reasons[drone_id] = "invalid_sensor_evidence"

    def publish(self, drone_id: int, now_s: float) -> dict[str, object]:
        if self.config.mode != "replay":
            raise ValueError("live publisher must use the calibrated capture clock")
        return self._publish(drone_id, now_s)

    def publish_live(self, drone_id: int, monotonic_s: float) -> dict[str, object]:
        if self.config.mode != "live":
            raise ValueError("replay publisher cannot use the live capture clock")
        clock = self.config.drones[drone_id].live_capture_clock
        assert clock is not None
        return self._publish(drone_id, clock.capture_time(monotonic_s))

    def _publish(self, drone_id: int, now_s: float) -> dict[str, object]:
        self.process(drone_id, now_s)
        config = self.config.drones[drone_id]
        snapshot = self.fusers[drone_id].snapshot(now_s)
        if drone_id in self.invalid_reasons:
            snapshot = replace(
                snapshot,
                status="hold",
                control_eligible=False,
                reason=self.invalid_reasons[drone_id],
            )
        if snapshot.covariance_map_enu_m2 is not None:
            uncertainty = position_uncertainty_p95_m(snapshot.covariance_map_enu_m2)
            if uncertainty > config.max_position_uncertainty_m:
                snapshot = replace(
                    snapshot,
                    status="hold",
                    control_eligible=False,
                    reason="position_uncertainty_exceeded",
                )
        self.sequence += 1
        event_id = f"localization-{drone_id}-{self.sequence}"
        wire = ControlLocalizationWire.from_mapping(to_wire_payload(snapshot, config.clock_mapping))
        frame = sign_localization_frame(
            wire,
            timestamp_ms=config.clock_mapping.to_relay_ms(now_s),
            event_id=event_id,
            session=self.config.session,
            signing_key=self.keys.get(drone_id, "replay").encode(),
        )
        if self.config.mode == "live":
            assert self.transport is not None
            self.transport.send(frame)
        return frame

    def _ingest(self, raw: Mapping[str, object], now_s: float) -> None:
        kind = raw.get("kind")
        drone_id = raw["drone_id"]
        if kind == "tag":
            extrinsics_raw = raw.get("extrinsics")
            if not isinstance(extrinsics_raw, Mapping):
                raise ValueError("tag record requires body extrinsics")
            observation = TagFix(
                event_id=_text(raw.get("event_id"), "event_id"),
                drone_id=drone_id,
                connection_epoch=raw.get("connection_epoch"),
                map_id=_text(raw.get("map_id"), "map_id"),
                geometry_id=_text(raw.get("geometry_id"), "geometry_id"),
                clock_id=_text(raw.get("clock_id"), "clock_id"),
                capture_time=_number(raw.get("capture_time"), "capture_time"),
                position_map_enu_m=tuple(raw.get("position_map_enu_m", ())),
                covariance_map_enu_m2=tuple(
                    tuple(row) for row in raw.get("covariance_map_enu_m2", ())
                ),
                source_id=_text(raw.get("source_id"), "source_id"),
                camera_calibration_id=_text(
                    raw.get("camera_calibration_id"), "camera_calibration_id"
                ),
                source_verified=raw.get("source_verified") is True,
                timing_verified=raw.get("timing_verified") is True,
                extrinsics=BodyExtrinsics(**dict(extrinsics_raw)),
            )
            self.fusers[drone_id].ingest_tag_fix(observation, now_s)
        elif kind == "velocity":
            observation = VelocityObservation(
                _text(raw.get("event_id"), "event_id"),
                drone_id,
                raw.get("connection_epoch"),
                _text(raw.get("map_id"), "map_id"),
                _text(raw.get("geometry_id"), "geometry_id"),
                _text(raw.get("clock_id"), "clock_id"),
                _number(raw.get("capture_time"), "capture_time"),
                tuple(raw.get("velocity_map_enu_mps", ())),
                tuple(tuple(row) for row in raw.get("covariance_m2ps2", ())),
                _text(raw.get("source_id"), "source_id"),
                raw.get("source_verified") is True,
                raw.get("timing_verified") is True,
            )
            self.fusers[drone_id].ingest_velocity(observation, now_s)
        elif kind == "height":
            observation = HeightObservation(
                _text(raw.get("event_id"), "event_id"),
                drone_id,
                raw.get("connection_epoch"),
                _text(raw.get("map_id"), "map_id"),
                _text(raw.get("geometry_id"), "geometry_id"),
                _text(raw.get("clock_id"), "clock_id"),
                _number(raw.get("capture_time"), "capture_time"),
                _number(raw.get("height_map_enu_m"), "height_map_enu_m"),
                _number(raw.get("variance_m2"), "variance_m2"),
                _text(raw.get("source_id"), "source_id"),
                raw.get("source_verified") is True,
                raw.get("timing_verified") is True,
            )
            self.fusers[drone_id].ingest_height(observation, now_s)
        else:
            raise ValueError("sensor record kind is unsupported")
        self.invalid_reasons.pop(drone_id, None)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be nonempty text")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _run_live(
    publisher: ControlPublisher,
    lines: Iterable[str],
    *,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> None:
    incoming: queue.Queue[object] = queue.Queue(maxsize=publisher.config.queue_limit)
    end = object()

    def read() -> None:
        try:
            for line in lines:
                incoming.put(line)
        except BaseException as error:
            incoming.put(error)
        finally:
            incoming.put(end)

    thread = threading.Thread(target=read, name="control-publisher-input", daemon=True)
    thread.start()
    finished = False
    while not finished:
        cycle_started = _number(clock(), "monotonic_s")
        while True:
            try:
                item = incoming.get_nowait()
            except queue.Empty:
                break
            if item is end:
                finished = True
                break
            if isinstance(item, BaseException):
                raise item
            if not isinstance(item, str):
                raise ValueError("publisher input must contain text lines")
            record = json.loads(item)
            publisher.enqueue(record)
        now_s = _number(clock(), "monotonic_s")
        for drone_id in publisher.config.drones:
            publisher.publish_live(drone_id, now_s)
        if not finished:
            elapsed_s = _number(clock(), "monotonic_s") - cycle_started
            wait(max(0.0, LIVE_PUBLISH_INTERVAL_S - elapsed_s))


def _run_replay(publisher: ControlPublisher, lines: Iterable[str], output: TextIO) -> None:
    for line in lines:
        record = json.loads(line)
        publisher.enqueue(record)
        now_s = _number(record.get("now_s"), "now_s")
        frame = publisher.publish(record["drone_id"], now_s)
        output.write(json.dumps(frame, allow_nan=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--replay-output", type=Path)
    args = parser.parse_args()
    config = ControlPublisherConfig.from_mapping(json.loads(args.config.read_text()))
    if config.mode == "replay" and args.replay_output is None:
        raise SystemExit("replay mode requires --replay-output")
    transport = (
        None if config.mode == "replay" else WebSocketPublisherTransport(config.websocket_url)
    )
    publisher = ControlPublisher(config, transport)
    if config.mode == "live":
        publisher.bind_live_credentials()
    if config.mode == "live":
        _run_live(publisher, os.sys.stdin)
    else:
        assert args.replay_output is not None
        with args.replay_output.open("x", encoding="utf-8") as output:
            _run_replay(publisher, os.sys.stdin, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

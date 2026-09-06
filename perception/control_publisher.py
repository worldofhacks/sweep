"""Bounded sensor-record publisher for diagnostic control localization.

The publisher owns input decoding, per-aircraft fuser instances, capture-clock
translation, and transport delivery.  It deliberately does not project safety
state, modify relay state, or create control-pose packets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import queue
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from dataclasses import MISSING, asdict, dataclass, fields, replace
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, TextIO
from urllib.parse import quote, urlsplit

from perception.control_localization import (
    BodyExtrinsics,
    ControlLocalization,
    ControlLocalizationConfig,
    HeightObservation,
    TagFix,
    VelocityObservation,
)
from relay.audit import AuditLogError, SessionAuditLog
from relay.control_frames import sign_localization_frame
from relay.control_localization import ClockMapping, ControlLocalizationWire, to_wire_payload
from relay.control_localization_contracts import (
    MAX_INT64,
    identifier,
    nonnegative_int64,
    positive_int32,
    session_identifier,
)

LIVE_PUBLISH_INTERVAL_S = 0.1
LIVE_RECONNECT_BACKOFF_S = 1.0
LIVE_SHUTDOWN_TIMEOUT_S = 6.0
MAX_DRONES = 4
MAX_QUEUE_LIMIT = 4_096
MAX_JSON_BYTES = 1_048_576
MAX_URL_CHARS = 2_048
_ACTIVE_MEMBERSHIPS = frozenset({"registered", "ready", "degraded"})
_MEMBERSHIPS = _ACTIVE_MEMBERSHIPS | {"leaving", "disconnected"}

_COMMON_SENSOR_FIELDS = frozenset(
    {
        "kind",
        "drone_id",
        "event_id",
        "connection_epoch",
        "map_id",
        "geometry_id",
        "clock_id",
        "capture_time",
        "source_id",
        "source_verified",
        "timing_verified",
    }
)
_TAG_FIELDS = _COMMON_SENSOR_FIELDS | {
    "position_map_enu_m",
    "covariance_map_enu_m2",
    "camera_calibration_id",
    "extrinsics",
}
_VELOCITY_FIELDS = _COMMON_SENSOR_FIELDS | {
    "velocity_map_enu_mps",
    "covariance_m2ps2",
}
_HEIGHT_FIELDS = _COMMON_SENSOR_FIELDS | {"height_map_enu_m", "variance_m2"}
_EXTRINSICS_FIELDS = frozenset(field.name for field in fields(BodyExtrinsics))
_FUSER_FIELDS = frozenset(field.name for field in fields(ControlLocalizationConfig))
_FUSER_REQUIRED_FIELDS = frozenset(
    field.name
    for field in fields(ControlLocalizationConfig)
    if field.default is MISSING and field.default_factory is MISSING
)


class PublisherError(ValueError):
    """Base class for bounded publisher refusals."""


class PublisherTransportError(PublisherError):
    """The isolated aircraft transport is unavailable or invalid."""


class PublisherOverflowError(PublisherError):
    """A sensor record was durably refused because its queue was full."""


class PublisherAuditError(PublisherError):
    """The durable publisher journal is unavailable; publication must stop."""


class PublisherAudit(Protocol):
    def append(self, event: Mapping[str, object]) -> None: ...


@dataclass(frozen=True, slots=True)
class LiveBinding:
    """Relay-authenticated current identity for one localization producer."""

    session: str
    drone_id: int
    connection_epoch: int
    roster_version: int
    membership: Literal["registered", "ready", "degraded"]

    def __post_init__(self) -> None:
        object.__setattr__(self, "session", session_identifier(self.session))
        object.__setattr__(self, "drone_id", positive_int32(self.drone_id, "drone_id"))
        object.__setattr__(
            self,
            "connection_epoch",
            positive_int32(self.connection_epoch, "connection_epoch"),
        )
        object.__setattr__(
            self,
            "roster_version",
            nonnegative_int64(self.roster_version, "roster_version"),
        )
        if type(self.membership) is not str or self.membership not in _ACTIVE_MEMBERSHIPS:
            raise PublisherTransportError("aircraft is not active in the relay registry")


class PublisherTransport(Protocol):
    def authenticate(self, drone_id: int, token: str, session: str) -> LiveBinding: ...

    def current_binding(self, drone_id: int) -> LiveBinding: ...

    def send(self, drone_id: int, frame: Mapping[str, object]) -> None: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class _SocketState:
    socket: object
    binding: LiveBinding
    failure: PublisherTransportError | None = None


class WebSocketPublisherTransport:
    """Independent socket and state-drain loop for each configured aircraft."""

    def __init__(self, base_url: str) -> None:
        self.base_url = _websocket_base_url(base_url)
        self._states: dict[int, _SocketState] = {}
        self._lock = threading.RLock()
        self._closed = False

    def authenticate(self, drone_id: int, token: str, session: str) -> LiveBinding:
        drone = positive_int32(drone_id, "drone_id")
        session_id = session_identifier(session)
        if type(token) is not str or not 1 <= len(token) <= 4_096:
            raise PublisherTransportError("localization credential is not bounded text")
        with self._lock:
            if self._closed:
                raise PublisherTransportError("localization transport is closed")
        socket: object | None = None
        try:
            from websockets.sync.client import connect

            socket = connect(
                f"{self.base_url}/{quote(session_id, safe='')}",
                open_timeout=5,
                close_timeout=1,
                max_size=MAX_JSON_BYTES,
            )
            socket.send(
                json.dumps(
                    {
                        "v": 1,
                        "type": "auth",
                        "source": "localization",
                        "drone_id": drone,
                        "token": token,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
            accepted = _decode_message(socket.recv(timeout=5))
            _validate_auth_accepted(accepted, drone, session_id)
            state_event = _decode_message(socket.recv(timeout=5))
            binding = _binding_from_state(state_event, drone, session_id)
        except PublisherTransportError:
            if socket is not None:
                _close_socket(socket)
            raise
        except Exception as error:
            if socket is not None:
                _close_socket(socket)
            raise PublisherTransportError("localization handshake failed") from error

        state = _SocketState(socket=socket, binding=binding)
        with self._lock:
            if self._closed:
                _close_socket(socket)
                raise PublisherTransportError("localization transport is closed")
            previous = self._states.get(drone)
            self._states[drone] = state
        if previous is not None:
            _close_socket(previous.socket)
        reader = threading.Thread(
            target=self._drain,
            args=(drone, state),
            name=f"control-localization-recv-{drone}",
            daemon=True,
        )
        reader.start()
        return binding

    def current_binding(self, drone_id: int) -> LiveBinding:
        with self._lock:
            if self._closed:
                raise PublisherTransportError("localization transport is closed")
            state = self._states.get(drone_id)
            if state is None:
                raise PublisherTransportError("localization transport is not authenticated")
            if state.failure is not None:
                raise state.failure
            return state.binding

    def send(self, drone_id: int, frame: Mapping[str, object]) -> None:
        with self._lock:
            if self._closed:
                raise PublisherTransportError("localization transport is closed")
            state = self._states.get(drone_id)
            if state is None or state.failure is not None:
                raise PublisherTransportError("localization transport is unavailable")
            socket = state.socket
        try:
            socket.send(_canonical_json(frame).decode("utf-8"))
        except Exception as error:
            failure = PublisherTransportError("localization frame delivery failed")
            self._fail(drone_id, state, failure)
            raise failure from error

    def close(self) -> None:
        with self._lock:
            self._closed = True
            states = tuple(self._states.values())
            self._states.clear()
        for state in states:
            _close_socket(state.socket)

    def _drain(self, drone_id: int, state: _SocketState) -> None:
        try:
            while True:
                event = _decode_message(state.socket.recv())
                if event.get("type") == "state":
                    binding = _binding_from_state(event, drone_id, state.binding.session)
                    with self._lock:
                        if self._states.get(drone_id) is state:
                            state.binding = binding
        except Exception:
            self._fail(
                drone_id,
                state,
                PublisherTransportError("localization state stream ended"),
            )

    def _fail(
        self,
        drone_id: int,
        state: _SocketState,
        failure: PublisherTransportError,
    ) -> None:
        with self._lock:
            if self._states.get(drone_id) is state:
                state.failure = failure
        _close_socket(state.socket)


@dataclass(frozen=True, slots=True)
class MonotonicCaptureClock:
    """Measured mapping from this system boot's monotonic clock to capture time."""

    source: Literal["process_monotonic"]
    boot_id: str
    monotonic_reference_s: float
    capture_reference_s: float

    def __post_init__(self) -> None:
        if self.source != "process_monotonic":
            raise PublisherError("live capture clock source is invalid")
        object.__setattr__(self, "boot_id", identifier(self.boot_id, "boot_id"))
        monotonic_reference = _nonnegative_number(
            self.monotonic_reference_s, "monotonic_reference_s"
        )
        capture_reference = _nonnegative_number(self.capture_reference_s, "capture_reference_s")
        object.__setattr__(self, "monotonic_reference_s", monotonic_reference)
        object.__setattr__(self, "capture_reference_s", capture_reference)

    @classmethod
    def from_mapping(cls, raw: object) -> MonotonicCaptureClock:
        expected = {
            "source",
            "boot_id",
            "monotonic_reference_s",
            "capture_reference_s",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise PublisherError("live capture clock fields do not match the contract")
        return cls(
            source=raw["source"],  # type: ignore[arg-type]
            boot_id=raw["boot_id"],  # type: ignore[arg-type]
            monotonic_reference_s=raw["monotonic_reference_s"],  # type: ignore[arg-type]
            capture_reference_s=raw["capture_reference_s"],  # type: ignore[arg-type]
        )

    def capture_time(self, monotonic_s: object) -> float:
        monotonic = _nonnegative_number(monotonic_s, "monotonic_s")
        if monotonic < self.monotonic_reference_s:
            raise PublisherError("monotonic clock precedes its measured reference")
        capture = self.capture_reference_s + monotonic - self.monotonic_reference_s
        if not isfinite(capture):
            raise PublisherError("capture clock conversion overflowed")
        return capture

    def verify_boot(self, current_boot_id: Callable[[], str]) -> None:
        if self.boot_id != identifier(current_boot_id(), "current_boot_id"):
            raise PublisherError("live capture clock belongs to a different system boot")


@dataclass(frozen=True, slots=True)
class PublisherDroneConfig:
    fuser: ControlLocalizationConfig
    clock_mapping: ClockMapping
    key_environment: str
    live_capture_clock: MonotonicCaptureClock | None

    def __post_init__(self) -> None:
        if not isinstance(self.fuser, ControlLocalizationConfig):
            raise PublisherError("publisher requires a control-localization fuser config")
        positive_int32(self.fuser.drone_id, "fuser drone_id")
        if self.fuser.connection_epoch != 0:
            positive_int32(self.fuser.connection_epoch, "fuser connection_epoch")
        if not isinstance(self.clock_mapping, ClockMapping):
            raise PublisherError("publisher requires a measured clock mapping")
        object.__setattr__(
            self,
            "key_environment",
            identifier(self.key_environment, "key_environment"),
        )
        if self.clock_mapping.capture_clock_id != self.fuser.clock_id:
            raise PublisherError("fuser and transport capture clocks differ")
        if (
            self.live_capture_clock is not None
            and self.live_capture_clock.capture_reference_s
            != self.clock_mapping.capture_reference_s
        ):
            raise PublisherError("live capture clock does not match the measured mapping")


@dataclass(frozen=True, slots=True, init=False)
class ControlPublisherConfig:
    mode: Literal["live", "replay"]
    session: str
    websocket_url: str | None
    audit_dir: Path
    drones: Mapping[int, PublisherDroneConfig]
    queue_limit: int

    def __init__(
        self,
        mode: Literal["live", "replay"],
        session: str,
        websocket_url: str | None,
        audit_dir: Path,
        drones: Mapping[int, PublisherDroneConfig],
        queue_limit: int = 64,
    ) -> None:
        if mode not in {"live", "replay"}:
            raise PublisherError("publisher mode is invalid")
        session_id = session_identifier(session)
        if type(queue_limit) is not int or not 1 <= queue_limit <= MAX_QUEUE_LIMIT:
            raise PublisherError("publisher queue_limit is outside its bounded range")
        copied = dict(drones)
        if not 1 <= len(copied) <= MAX_DRONES or any(
            type(drone_id) is not int
            or not isinstance(config, PublisherDroneConfig)
            or drone_id != config.fuser.drone_id
            for drone_id, config in copied.items()
        ):
            raise PublisherError("publisher drones are invalid")
        audit_root = Path(audit_dir)
        if not audit_root.name:
            raise PublisherError("publisher audit_dir must identify a bounded directory")
        if mode == "live":
            url = _websocket_base_url(websocket_url)
            if any(
                config.live_capture_clock is None or config.fuser.connection_epoch != 0
                for config in copied.values()
            ):
                raise PublisherError(
                    "live fusers require an unbound epoch and a measured capture clock"
                )
        else:
            if websocket_url is not None:
                raise PublisherError("replay mode cannot configure a network transport")
            if any(
                config.live_capture_clock is not None or config.fuser.connection_epoch <= 0
                for config in copied.values()
            ):
                raise PublisherError(
                    "replay fusers require a recorded positive epoch and no live clock"
                )
            url = None
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "session", session_id)
        object.__setattr__(self, "websocket_url", url)
        object.__setattr__(self, "audit_dir", audit_root)
        object.__setattr__(self, "drones", MappingProxyType(copied))
        object.__setattr__(self, "queue_limit", queue_limit)

    @classmethod
    def from_mapping(cls, raw: object) -> ControlPublisherConfig:
        expected = {"mode", "session", "websocket_url", "audit_dir", "drones", "queue_limit"}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise PublisherError("publisher configuration fields do not match the contract")
        entries = raw["drones"]
        if not isinstance(entries, list):
            raise PublisherError("publisher drones must be an array")
        drones: dict[int, PublisherDroneConfig] = {}
        for entry in entries:
            expected_entry = {
                "fuser",
                "clock_mapping",
                "key_environment",
                "live_capture_clock",
            }
            if not isinstance(entry, Mapping) or set(entry) != expected_entry:
                raise PublisherError("publisher drone fields do not match the contract")
            fuser_raw = entry["fuser"]
            if (
                not isinstance(fuser_raw, Mapping)
                or not _FUSER_REQUIRED_FIELDS.issubset(fuser_raw)
                or not set(fuser_raw).issubset(_FUSER_FIELDS)
            ):
                raise PublisherError("fuser configuration fields do not match the contract")
            try:
                fuser = ControlLocalizationConfig(**dict(fuser_raw))
                clock_mapping = ClockMapping.from_mapping(entry["clock_mapping"])
                live_clock = (
                    None
                    if entry["live_capture_clock"] is None
                    else MonotonicCaptureClock.from_mapping(entry["live_capture_clock"])
                )
                item = PublisherDroneConfig(
                    fuser=fuser,
                    clock_mapping=clock_mapping,
                    key_environment=entry["key_environment"],  # type: ignore[arg-type]
                    live_capture_clock=live_clock,
                )
            except (TypeError, ValueError, OverflowError) as error:
                raise PublisherError("publisher drone configuration is invalid") from error
            if fuser.drone_id in drones:
                raise PublisherError("publisher drone IDs must be unique")
            drones[fuser.drone_id] = item
        audit_dir = raw["audit_dir"]
        if type(audit_dir) is not str or not audit_dir or len(audit_dir) > 4_096:
            raise PublisherError("audit_dir must be bounded non-empty text")
        return cls(
            mode=raw["mode"],  # type: ignore[arg-type]
            session=raw["session"],  # type: ignore[arg-type]
            websocket_url=raw["websocket_url"],  # type: ignore[arg-type]
            audit_dir=Path(audit_dir),
            drones=drones,
            queue_limit=raw["queue_limit"],  # type: ignore[arg-type]
        )

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
                    "live_capture_clock": (
                        None
                        if config.live_capture_clock is None
                        else asdict(config.live_capture_clock)
                    ),
                }
                for _, config in sorted(self.drones.items())
            ],
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class _QueuedRecord:
    kind: Literal["tag", "velocity", "height"]
    event_id: str
    observation: TagFix | VelocityObservation | HeightObservation
    digest: str


class ControlPublisher:
    """Per-aircraft fuser and signed-wire coordinator with no safety projection."""

    def __init__(
        self,
        config: ControlPublisherConfig,
        transport: PublisherTransport | None = None,
        *,
        audit: PublisherAudit | None = None,
        boot_identity: Callable[[], str] = lambda: system_boot_id(),
        run_id: str | None = None,
    ) -> None:
        if config.mode == "live" and transport is None:
            raise PublisherError("live publisher requires a transport")
        if config.mode == "replay" and transport is not None:
            raise PublisherError("replay publisher cannot use a transport")
        self.config = config
        self.transport = transport
        self._boot_identity = boot_identity
        self._run_id = identifier(run_id or uuid.uuid4().hex, "publisher_run_id")
        if len(self._run_id) > 64:
            raise PublisherError("publisher_run_id must contain at most 64 characters")
        run_digest = hashlib.sha256(self._run_id.encode()).hexdigest()[:16]
        self._audit_session = f"publisher-{config.identity_sha256[:16]}-{run_digest}"
        try:
            self.audit = (
                audit
                if audit is not None
                else SessionAuditLog(config.audit_dir, self._audit_session)
            )
        except (AuditLogError, OSError, ValueError) as error:
            raise PublisherAuditError("publisher audit could not be initialized") from error
        self._audit_sequence = 0
        self._audit_lock = threading.RLock()
        self._fusers = {
            drone_id: ControlLocalization(item.fuser) for drone_id, item in config.drones.items()
        }
        self._queues: dict[int, deque[_QueuedRecord]] = {
            drone_id: deque() for drone_id in config.drones
        }
        self._queue_locks = {drone_id: threading.Lock() for drone_id in config.drones}
        self._keys: dict[int, bytes] = {}
        self._tokens: dict[int, str] = {}
        self._bindings: dict[int, LiveBinding] = {}
        self._sequence = {drone_id: 0 for drone_id in config.drones}
        self._last_live_monotonic: dict[int, float] = {}
        self._closed = False
        self._audit("publisher_started", run_id=self._run_id)

    def bind_credentials(self, environment: Mapping[str, str] | None = None) -> None:
        """Load bounded per-aircraft signing keys and establish live bindings."""
        self._require_open()
        source = os.environ if environment is None else environment
        keys: dict[int, bytes] = {}
        tokens: dict[int, str] = {}
        for drone_id, config in self.config.drones.items():
            token = source.get(config.key_environment)
            if type(token) is not str or not 1 <= len(token) <= 4_096:
                raise PublisherError(f"localization credential is missing for drone {drone_id}")
            try:
                encoded = token.encode("utf-8")
            except UnicodeEncodeError as error:
                raise PublisherError(
                    f"localization credential is not valid UTF-8 for drone {drone_id}"
                ) from error
            if not 32 <= len(encoded) <= 4_096:
                raise PublisherError(
                    f"localization credential has an invalid length for drone {drone_id}"
                )
            keys[drone_id] = encoded
            tokens[drone_id] = token
            if self.config.mode == "live":
                assert config.live_capture_clock is not None
                config.live_capture_clock.verify_boot(self._boot_identity)
        if len(set(keys.values())) != len(keys):
            raise PublisherError("localization credentials must be distinct per aircraft")
        if self.config.mode == "live":
            assert self.transport is not None
            established: dict[int, LiveBinding] = {}
            try:
                for drone_id in self.config.drones:
                    binding = self.transport.authenticate(
                        drone_id, tokens[drone_id], self.config.session
                    )
                    established[drone_id] = self._adopt_binding(drone_id, binding)
            except Exception:
                self.transport.close()
                self._bindings.clear()
                self._keys.clear()
                self._tokens.clear()
                raise
            self._bindings = established
        self._keys = keys
        self._tokens = tokens

    def enqueue(self, raw: object) -> None:
        """Validate and immutably queue one exact sensor record.

        Queue overflow never evicts evidence.  The refusal is fsynced before this
        method raises, so callers may backpressure or retry from their durable source.
        """
        self._require_open()
        try:
            record = _parse_sensor_record(raw)
        except PublisherError:
            try:
                encoded = _canonical_json(raw)
            except (TypeError, ValueError, OverflowError):
                encoded = repr(raw).encode("utf-8", errors="replace")
            self.refuse_input(encoded, "invalid_sensor_record")
            raise
        drone_id = record.observation.drone_id
        queue_for_drone = self._queues.get(drone_id)
        if queue_for_drone is None:
            self._audit(
                "sensor_refused",
                drone_id=drone_id,
                sensor_event_id=record.event_id,
                sensor_kind=record.kind,
                record_sha256=record.digest,
                reason="drone_not_configured",
            )
            raise PublisherError("sensor record drone is not configured")
        with self._queue_locks[drone_id]:
            if len(queue_for_drone) >= self.config.queue_limit:
                self._audit(
                    "sensor_refused",
                    drone_id=drone_id,
                    sensor_event_id=record.event_id,
                    sensor_kind=record.kind,
                    record_sha256=record.digest,
                    reason="queue_full",
                )
                raise PublisherOverflowError("sensor queue is full; no evidence was evicted")
            queue_for_drone.append(record)

    def refuse_input(self, raw: str | bytes, reason: str) -> None:
        """Durably record an undecodable input line without poisoning any fuser."""
        self._require_open()
        encoded = raw.encode("utf-8", errors="replace") if isinstance(raw, str) else bytes(raw)
        self._audit(
            "input_refused",
            record_sha256=hashlib.sha256(encoded).hexdigest(),
            reason=identifier(reason, "input_refusal_reason"),
        )

    def publish(self, drone_id: int, now_s: object) -> dict[str, object]:
        self._require_open()
        if self.config.mode != "replay":
            raise PublisherError("live publisher must use its measured monotonic clock")
        self._require_key(drone_id)
        return self._build_and_deliver(drone_id, _nonnegative_number(now_s, "now_s"))

    def publish_live(self, drone_id: int, monotonic_s: object) -> dict[str, object]:
        self._require_open()
        if self.config.mode != "live" or self.transport is None:
            raise PublisherError("replay publisher cannot publish live")
        self._require_key(drone_id)
        config = self.config.drones[drone_id]
        assert config.live_capture_clock is not None
        monotonic = _nonnegative_number(monotonic_s, "monotonic_s")
        previous = self._last_live_monotonic.get(drone_id)
        if previous is not None and monotonic < previous:
            raise PublisherError("live monotonic clock regressed")
        self._last_live_monotonic[drone_id] = monotonic
        now_s = config.live_capture_clock.capture_time(monotonic)
        binding = self._current_binding(drone_id)
        frame = self._build_and_deliver(drone_id, now_s)
        try:
            self.transport.send(drone_id, frame)
        except Exception as error:
            self._audit(
                "frame_refused",
                drone_id=drone_id,
                event_id=frame["event_id"],
                connection_epoch=binding.connection_epoch,
                reason="transport_unavailable",
            )
            raise PublisherTransportError("localization frame was not delivered") from error
        return frame

    def bound_epoch(self, drone_id: int) -> int:
        self._require_open()
        if self.config.mode != "live":
            raise PublisherError("replay publisher has no live epoch")
        self._require_key(drone_id)
        return self._current_binding(drone_id).connection_epoch

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._audit("publisher_stopped", run_id=self._run_id)
        finally:
            if self.transport is not None:
                self.transport.close()
            self._closed = True

    def _current_binding(self, drone_id: int) -> LiveBinding:
        assert self.transport is not None
        try:
            binding = self.transport.current_binding(drone_id)
        except PublisherTransportError:
            token = self._tokens.get(drone_id)
            if token is None:
                raise PublisherError("publisher credentials are not bound") from None
            binding = self.transport.authenticate(drone_id, token, self.config.session)
        return self._adopt_binding(drone_id, binding)

    def _adopt_binding(self, drone_id: int, binding: LiveBinding) -> LiveBinding:
        if binding.drone_id != drone_id or binding.session != self.config.session:
            raise PublisherTransportError("relay binding does not match publisher identity")
        prior = self._bindings.get(drone_id)
        if prior is None or binding.connection_epoch != prior.connection_epoch:
            template = self.config.drones[drone_id].fuser
            self._fusers[drone_id] = ControlLocalization(
                replace(template, connection_epoch=binding.connection_epoch)
            )
            self._audit(
                "epoch_bound",
                drone_id=drone_id,
                connection_epoch=binding.connection_epoch,
                roster_version=binding.roster_version,
            )
        self._bindings[drone_id] = binding
        return binding

    def _build_and_deliver(self, drone_id: int, now_s: float) -> dict[str, object]:
        config = self.config.drones.get(drone_id)
        if config is None:
            raise PublisherError("publisher drone is not configured")
        self._process(drone_id, now_s)
        snapshot = self._fusers[drone_id].snapshot(now_s)
        body = to_wire_payload(snapshot, config.clock_mapping)
        wire = ControlLocalizationWire.from_mapping(body)
        event_id = self._next_event_id(drone_id)
        frame = sign_localization_frame(
            wire,
            timestamp_ms=config.clock_mapping.to_relay_ms(now_s),
            event_id=event_id,
            session=self.config.session,
            signing_key=self._keys[drone_id],
        )
        return frame

    def _process(self, drone_id: int, now_s: float) -> None:
        queue_for_drone = self._queues[drone_id]
        while True:
            with self._queue_locks[drone_id]:
                if not queue_for_drone:
                    return
                record = queue_for_drone.popleft()
            try:
                fuser = self._fusers[drone_id]
                if record.kind == "tag":
                    assert isinstance(record.observation, TagFix)
                    result = fuser.ingest_tag_fix(record.observation, now_s)
                elif record.kind == "velocity":
                    assert isinstance(record.observation, VelocityObservation)
                    result = fuser.ingest_velocity(record.observation, now_s)
                else:
                    assert isinstance(record.observation, HeightObservation)
                    result = fuser.ingest_height(record.observation, now_s)
            except (TypeError, ValueError, OverflowError):
                self._audit(
                    "sensor_processed",
                    drone_id=drone_id,
                    sensor_event_id=record.event_id,
                    sensor_kind=record.kind,
                    record_sha256=record.digest,
                    outcome="refused",
                    reason="invalid_sensor_evidence",
                )
                continue
            refused = result.last_rejection
            if refused is not None:
                self._audit(
                    "sensor_processed",
                    drone_id=drone_id,
                    sensor_event_id=record.event_id,
                    sensor_kind=record.kind,
                    record_sha256=record.digest,
                    outcome="refused",
                    reason=refused,
                )

    def _next_event_id(self, drone_id: int) -> str:
        sequence = self._sequence[drone_id]
        if sequence >= MAX_INT64:
            raise PublisherError("publisher event sequence is exhausted")
        sequence += 1
        self._sequence[drone_id] = sequence
        if self.config.mode == "live":
            value = f"localization-{drone_id}-{self._run_id}-{sequence:x}"
        else:
            value = f"localization-{drone_id}-{self.config.identity_sha256[:16]}-{sequence:x}"
        return identifier(value, "event_id")

    def _require_key(self, drone_id: int) -> None:
        if drone_id not in self.config.drones:
            raise PublisherError("publisher drone is not configured")
        if drone_id not in self._keys:
            raise PublisherError("publisher credentials are not bound")

    def _require_open(self) -> None:
        if self._closed:
            raise PublisherError("publisher is closed")

    def _audit(self, event_type: str, **fields_: object) -> None:
        with self._audit_lock:
            if self._audit_sequence >= MAX_INT64:
                raise PublisherAuditError("publisher audit sequence is exhausted")
            self._audit_sequence += 1
            event = {
                "v": 1,
                "type": identifier(event_type, "publisher_audit_type"),
                "session": self._audit_session,
                "event_id": f"publisher-audit-{self._audit_sequence:x}",
                "publisher_identity": self.config.identity_sha256,
                **fields_,
            }
            try:
                self.audit.append(event)
            except (AuditLogError, PublisherAuditError) as error:
                raise PublisherAuditError("publisher audit append failed") from error
            except Exception as error:
                raise PublisherAuditError("publisher audit append failed") from error


def system_boot_id() -> str:
    """Return a stable, bounded identifier for the current OS boot."""
    linux_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        if linux_path.is_file():
            value = linux_path.read_text(encoding="utf-8").strip()
            return identifier(value, "system_boot_id")
        if platform.system() == "Darwin":
            completed = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            raw = completed.stdout.strip()
            if raw:
                return f"darwin-{hashlib.sha256(raw.encode()).hexdigest()}"
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise PublisherError("current system boot identity is unavailable") from error
    raise PublisherError("current system boot identity is unavailable")


def _parse_sensor_record(raw: object) -> _QueuedRecord:
    if not isinstance(raw, Mapping) or not all(type(key) is str for key in raw):
        raise PublisherError("sensor record must be an object with text keys")
    kind = raw.get("kind")
    expected = {
        "tag": _TAG_FIELDS,
        "velocity": _VELOCITY_FIELDS,
        "height": _HEIGHT_FIELDS,
    }.get(kind)
    if expected is None or set(raw) != expected:
        raise PublisherError("sensor record fields do not match the kind contract")
    try:
        drone_id = positive_int32(raw["drone_id"], "drone_id")
        connection_epoch = positive_int32(raw["connection_epoch"], "connection_epoch")
        if kind == "tag":
            extrinsics_raw = raw["extrinsics"]
            if not isinstance(extrinsics_raw, Mapping) or set(extrinsics_raw) != _EXTRINSICS_FIELDS:
                raise PublisherError("tag extrinsics fields do not match the contract")
            observation: TagFix | VelocityObservation | HeightObservation = TagFix(
                event_id=raw["event_id"],  # type: ignore[arg-type]
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                map_id=raw["map_id"],  # type: ignore[arg-type]
                geometry_id=raw["geometry_id"],  # type: ignore[arg-type]
                clock_id=raw["clock_id"],  # type: ignore[arg-type]
                capture_time=raw["capture_time"],  # type: ignore[arg-type]
                position_map_enu_m=raw["position_map_enu_m"],  # type: ignore[arg-type]
                covariance_map_enu_m2=raw["covariance_map_enu_m2"],  # type: ignore[arg-type]
                source_id=raw["source_id"],  # type: ignore[arg-type]
                camera_calibration_id=raw["camera_calibration_id"],  # type: ignore[arg-type]
                source_verified=raw["source_verified"],  # type: ignore[arg-type]
                timing_verified=raw["timing_verified"],  # type: ignore[arg-type]
                extrinsics=BodyExtrinsics(**dict(extrinsics_raw)),
            )
        elif kind == "velocity":
            observation = VelocityObservation(
                event_id=raw["event_id"],  # type: ignore[arg-type]
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                map_id=raw["map_id"],  # type: ignore[arg-type]
                geometry_id=raw["geometry_id"],  # type: ignore[arg-type]
                clock_id=raw["clock_id"],  # type: ignore[arg-type]
                capture_time=raw["capture_time"],  # type: ignore[arg-type]
                velocity_map_enu_mps=raw["velocity_map_enu_mps"],  # type: ignore[arg-type]
                covariance_m2ps2=raw["covariance_m2ps2"],  # type: ignore[arg-type]
                source_id=raw["source_id"],  # type: ignore[arg-type]
                source_verified=raw["source_verified"],  # type: ignore[arg-type]
                timing_verified=raw["timing_verified"],  # type: ignore[arg-type]
            )
        else:
            observation = HeightObservation(
                event_id=raw["event_id"],  # type: ignore[arg-type]
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                map_id=raw["map_id"],  # type: ignore[arg-type]
                geometry_id=raw["geometry_id"],  # type: ignore[arg-type]
                clock_id=raw["clock_id"],  # type: ignore[arg-type]
                capture_time=raw["capture_time"],  # type: ignore[arg-type]
                height_map_enu_m=raw["height_map_enu_m"],  # type: ignore[arg-type]
                variance_m2=raw["variance_m2"],  # type: ignore[arg-type]
                source_id=raw["source_id"],  # type: ignore[arg-type]
                source_verified=raw["source_verified"],  # type: ignore[arg-type]
                timing_verified=raw["timing_verified"],  # type: ignore[arg-type]
            )
    except (TypeError, ValueError, OverflowError) as error:
        raise PublisherError("sensor record is invalid") from error
    normalized = {"kind": kind, **asdict(observation)}
    return _QueuedRecord(
        kind=kind,  # type: ignore[arg-type]
        event_id=observation.event_id,
        observation=observation,
        digest=hashlib.sha256(_canonical_json(normalized)).hexdigest(),
    )


def _validate_auth_accepted(raw: Mapping[str, object], drone_id: int, session: str) -> None:
    expected = {"v", "t", "type", "event_id", "session", "source", "drone_id", "node"}
    if (
        set(raw) != expected
        or raw["v"] != 1
        or type(raw["v"]) is not int
        or raw["type"] != "auth.accepted"
        or raw["source"] != "localization"
        or raw["drone_id"] != drone_id
        or raw["session"] != session
        or raw["node"] is not None
    ):
        raise PublisherTransportError("relay did not accept the localization identity")
    try:
        nonnegative_int64(raw["t"], "auth timestamp")
        identifier(raw["event_id"], "auth event_id")
    except ValueError as error:
        raise PublisherTransportError("relay authentication response is invalid") from error


def _binding_from_state(raw: Mapping[str, object], drone_id: int, session: str) -> LiveBinding:
    required = {"v", "t", "type", "event_id", "session", "roster_version", "drones"}
    if (
        not required.issubset(raw)
        or raw["v"] != 1
        or type(raw["v"]) is not int
        or raw["type"] != "state"
        or raw["session"] != session
        or not isinstance(raw["drones"], list)
        or len(raw["drones"]) > MAX_DRONES
    ):
        raise PublisherTransportError("relay state handshake is invalid")
    try:
        nonnegative_int64(raw["t"], "state timestamp")
        identifier(raw["event_id"], "state event_id")
        roster_version = nonnegative_int64(raw["roster_version"], "roster_version")
    except ValueError as error:
        raise PublisherTransportError("relay state handshake is invalid") from error
    matches: list[Mapping[str, object]] = []
    seen: set[int] = set()
    for item in raw["drones"]:
        if not isinstance(item, Mapping) or not {
            "drone_id",
            "connection_epoch",
            "membership",
        }.issubset(item):
            raise PublisherTransportError("relay state drone entry is invalid")
        try:
            candidate = positive_int32(item["drone_id"], "state drone_id")
            positive_int32(item["connection_epoch"], "state connection_epoch")
        except ValueError as error:
            raise PublisherTransportError("relay state drone entry is invalid") from error
        membership = item["membership"]
        if type(membership) is not str or membership not in _MEMBERSHIPS or candidate in seen:
            raise PublisherTransportError("relay state drone identities are invalid")
        seen.add(candidate)
        if candidate == drone_id:
            matches.append(item)
    if len(matches) != 1:
        raise PublisherTransportError("authenticated aircraft has no current relay epoch")
    aircraft = matches[0]
    return LiveBinding(
        session=session,
        drone_id=drone_id,
        connection_epoch=aircraft.get("connection_epoch"),  # type: ignore[arg-type]
        roster_version=roster_version,
        membership=aircraft.get("membership"),  # type: ignore[arg-type]
    )


def _decode_message(raw: object) -> Mapping[str, object]:
    if isinstance(raw, bytes):
        if len(raw) > MAX_JSON_BYTES:
            raise PublisherTransportError("relay message is too large")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PublisherTransportError("relay message is not UTF-8") from error
    elif isinstance(raw, str):
        text = raw
    else:
        raise PublisherTransportError("relay message is not text")
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise PublisherTransportError("relay message is too large")
    try:
        parsed = _strict_json_loads(text)
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as error:
        raise PublisherTransportError("relay message is not strict JSON") from error
    if not isinstance(parsed, Mapping):
        raise PublisherTransportError("relay message is not an object")
    return parsed


def _websocket_base_url(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= MAX_URL_CHARS
        or value != value.strip()
        or not value.isprintable()
        or any(character.isspace() for character in value)
    ):
        raise PublisherError("websocket_url must be bounded canonical text")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise PublisherError("websocket_url is invalid") from error
    if (
        parsed.scheme not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port == 0
        or not parsed.path.rstrip("/").endswith("/ws")
    ):
        raise PublisherError("websocket_url must be a credential-free ws or wss base URL")
    return value.rstrip("/")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _strict_json_loads(value: str) -> object:
    return json.loads(
        value,
        object_pairs_hook=_reject_duplicate_keys,
        parse_float=_strict_json_float,
        parse_constant=lambda constant: (_ for _ in ()).throw(
            ValueError(f"non-standard JSON constant {constant}")
        ),
    )


def _strict_json_float(value: str) -> float:
    result = float(value)
    if not isfinite(result):
        raise ValueError("JSON number is outside the finite range")
    return result


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _nonnegative_number(value: object, name: str) -> float:
    if type(value) not in {int, float} or not isfinite(value) or value < 0:
        raise PublisherError(f"{name} must be a non-negative finite number")
    return float(value)


def _close_socket(socket: object) -> None:
    try:
        socket.close()
    except Exception:
        pass


def _enqueue_json_line(publisher: ControlPublisher, line: str, *, replay: bool) -> float | None:
    encoded = line.encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        publisher.refuse_input(encoded, "input_too_large")
        raise PublisherError("sensor input line is too large")
    try:
        raw = _strict_json_loads(line)
    except (ValueError, RecursionError, json.JSONDecodeError):
        publisher.refuse_input(encoded, "invalid_json")
        raise PublisherError("sensor input is not strict JSON") from None
    now_s: float | None = None
    if replay:
        if not isinstance(raw, Mapping) or "now_s" not in raw:
            publisher.refuse_input(encoded, "replay_time_missing")
            raise PublisherError("replay sensor input requires now_s")
        now_s = _nonnegative_number(raw["now_s"], "now_s")
        raw = {key: value for key, value in raw.items() if key != "now_s"}
    try:
        publisher.enqueue(raw)
    except PublisherError:
        raise
    return now_s


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
        except Exception as error:
            incoming.put(error)
        finally:
            incoming.put(end)

    threading.Thread(
        target=read,
        name="control-localization-input",
        daemon=True,
    ).start()
    executor = ThreadPoolExecutor(
        max_workers=len(publisher.config.drones),
        thread_name_prefix="control-localization-send",
    )
    pending: dict[int, Future[dict[str, object]]] = {}
    retry_after = {drone_id: 0.0 for drone_id in publisher.config.drones}

    def collect(drone_id: int, now_s: float) -> None:
        future = pending.get(drone_id)
        if future is None or not future.done():
            return
        del pending[drone_id]
        try:
            future.result()
        except PublisherTransportError:
            retry_after[drone_id] = now_s + LIVE_RECONNECT_BACKOFF_S

    finished = False
    try:
        while not finished:
            cycle_started = _nonnegative_number(clock(), "monotonic_s")
            for _ in range(publisher.config.queue_limit):
                try:
                    item = incoming.get_nowait()
                except queue.Empty:
                    break
                if item is end:
                    finished = True
                    break
                if isinstance(item, Exception):
                    raise item
                if not isinstance(item, str):
                    publisher.refuse_input(repr(item), "input_not_text")
                    continue
                try:
                    _enqueue_json_line(publisher, item, replay=False)
                except PublisherAuditError:
                    raise
                except PublisherError:
                    continue
            now = _nonnegative_number(clock(), "monotonic_s")
            for drone_id in publisher.config.drones:
                collect(drone_id, now)
                if drone_id not in pending and now >= retry_after[drone_id]:
                    pending[drone_id] = executor.submit(
                        publisher.publish_live,
                        drone_id,
                        now,
                    )
            if finished:
                done, _ = wait_for_futures(tuple(pending.values()), timeout=LIVE_SHUTDOWN_TIMEOUT_S)
                for drone_id, future in tuple(pending.items()):
                    if future in done:
                        collect(drone_id, now)
            else:
                elapsed = _nonnegative_number(clock(), "monotonic_s") - cycle_started
                wait(max(0.0, LIVE_PUBLISH_INTERVAL_S - elapsed))
    finally:
        for future in pending.values():
            future.cancel()
        if publisher.transport is not None:
            publisher.transport.close()
        wait_for_futures(tuple(pending.values()), timeout=LIVE_SHUTDOWN_TIMEOUT_S)
        executor.shutdown(wait=False, cancel_futures=True)


def _run_replay(publisher: ControlPublisher, lines: Iterable[str], output: TextIO) -> None:
    for line in lines:
        now_s = _enqueue_json_line(publisher, line, replay=True)
        assert now_s is not None
        parsed = _strict_json_loads(line)
        assert isinstance(parsed, Mapping)
        drone_id = parsed.get("drone_id")
        if type(drone_id) is not int:
            raise PublisherError("replay sensor input drone_id is invalid")
        frame = publisher.publish(drone_id, now_s)
        output.write(_canonical_json(frame).decode("utf-8") + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--replay-output", type=Path)
    args = parser.parse_args()
    config_bytes = args.config.read_bytes()
    if len(config_bytes) > MAX_JSON_BYTES:
        raise SystemExit("publisher config is too large")
    try:
        raw_config = _strict_json_loads(config_bytes.decode("utf-8"))
        config = ControlPublisherConfig.from_mapping(raw_config)
    except (UnicodeDecodeError, ValueError, RecursionError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid publisher config: {error}") from None
    if config.mode == "replay" and args.replay_output is None:
        raise SystemExit("replay mode requires --replay-output")
    if config.mode == "live" and args.replay_output is not None:
        raise SystemExit("live mode cannot write replay output")
    transport = (
        None if config.mode == "replay" else WebSocketPublisherTransport(config.websocket_url)  # type: ignore[arg-type]
    )
    publisher = ControlPublisher(config, transport)
    try:
        publisher.bind_credentials()
        if config.mode == "live":
            _run_live(publisher, os.sys.stdin)
        else:
            assert args.replay_output is not None
            with args.replay_output.open("x", encoding="utf-8") as output:
                _run_replay(publisher, os.sys.stdin, output)
    finally:
        publisher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

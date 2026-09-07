"""Host-qualified world observations for map display and explicit drive-over records.

The shared envelope remains diagnostic. Only a separate, deployment-owned binding
can associate an already-transformed world pose with an approved map. This module
does not transform coordinates, grant motion authority, or consume legacy telemetry.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from uuid import uuid4

from relay.auth import Principal
from spatial.contracts import FrameKind, ObservationError, identifier, integer
from spatial.observations import (
    MAX_OBSERVATION_SOURCES,
    PAYLOAD_MAX_HZ,
    Observation,
    ObservationSource,
    ObservationSubmission,
    PosePayload,
    bounded_json,
    source_registry,
)

POSITION_FRESH_MS = 1_000
DEVICE_FRESH_MS = 5_000
MAX_SESSION_OBSERVATIONS = 100_000
MAX_SESSION_CAPTURES = 20_000
MAX_SESSIONS = 256


class WorldObservationError(ValueError):
    def __init__(self, code: str, detail: str, status_code: int = 409) -> None:
        super().__init__(detail)
        self.code, self.detail, self.status_code = code, detail, status_code


def _exact(value: object, keys: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise WorldObservationError(
            "invalid_request", f"{name} fields do not match the contract", 400
        )
    return value


def _text(value: object, name: str, maximum: int = 256) -> str:
    try:
        return identifier(value, name, maximum)
    except ObservationError as error:
        raise WorldObservationError("invalid_request", error.detail, 400) from error


def _integer(value: object, name: str, minimum: int = 0, maximum: int = 2**53 - 1) -> int:
    try:
        return integer(value, name, minimum, maximum)
    except ObservationError as error:
        raise WorldObservationError("invalid_request", error.detail, 400) from error


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _reference(value: object) -> dict[str, str]:
    value = _exact(value, {"bundleId", "revision", "contentHash"}, "map reference")
    result = {key: _text(value[key], key) for key in value}
    if not re.fullmatch(r"[a-f0-9]{64}", result["contentHash"]):
        raise WorldObservationError("invalid_request", "map reference hash must be SHA-256", 400)
    return result


@dataclass(frozen=True, slots=True)
class WorldRegistration:
    reference: Mapping[str, str]
    map_version: str
    floor_id: str
    source_frame: str
    transform_id: str

    @classmethod
    def parse(cls, value: object) -> WorldRegistration:
        value = _exact(
            value,
            {
                "reference",
                "mapVersion",
                "floorId",
                "sourceFrame",
                "transformId",
                "qualifiedWorldPose",
            },
            "world registration",
        )
        if value["qualifiedWorldPose"] is not True:
            raise WorldObservationError(
                "unqualified_source",
                "a host-qualified, already-transformed world pose is required",
                400,
            )
        return cls(
            MappingProxyType(_reference(value["reference"])),
            _text(value["mapVersion"], "mapVersion"),
            _text(value["floorId"], "floorId"),
            _text(value["sourceFrame"], "sourceFrame"),
            _text(value["transformId"], "transformId"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": dict(self.reference),
            "mapVersion": self.map_version,
            "floorId": self.floor_id,
            "sourceFrame": self.source_frame,
            "transformId": self.transform_id,
            "qualifiedWorldPose": True,
        }


class WorldObservationService:
    def __init__(
        self,
        *,
        sources: Mapping[str, ObservationSource] | None = None,
        registrations: Mapping[str, WorldRegistration | Mapping[str, object]] | None = None,
        approved_bundle: Callable[[str], Mapping[str, object]],
        database: Path,
        clock: Callable[[], int],
    ) -> None:
        self.sources = source_registry({} if sources is None else sources)
        configured = {} if registrations is None else registrations
        if set(configured) != set(self.sources):
            raise WorldObservationError(
                "invalid_configuration", "every world source needs exactly one registration", 400
            )
        self.registrations = MappingProxyType(
            {
                key: WorldRegistration.parse(
                    value.to_dict() if isinstance(value, WorldRegistration) else value
                )
                for key, value in configured.items()
            }
        )
        for source in self.sources.values():
            if (
                source.payload_types != ("pose",)
                or len(source.frames) != 1
                or (
                    source.frames[0].kind is not FrameKind.WORLD
                    or source.frames[0].frame_id != "world"
                )
            ):
                raise WorldObservationError(
                    "unqualified_source",
                    "map consumption requires one world frame and pose payload",
                    400,
                )
        self.approved_bundle, self.database, self.clock = approved_bundle, Path(database), clock
        self._lock = RLock()
        self._latest: dict[tuple[str, str], dict[str, object]] = {}
        self._map_context: dict[str, str | None] = {}
        self._state_context: dict[str, tuple[int, int, int, str]] = {}
        self._contexts: dict[tuple[str, int], tuple[int, str, bool]] = {}
        if self.available:
            self._initialize()

    @property
    def available(self) -> bool:
        return bool(self.sources)

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str],
        *,
        approved_bundle: Callable[[str], Mapping[str, object]],
        database: Path,
        clock: Callable[[], int],
    ) -> WorldObservationService:
        raw = environment.get("SWEEP_WORLD_OBSERVATION_SOURCES", "")
        sources: dict[str, ObservationSource] = {}
        registrations: Mapping[str, object] = {}
        if raw.strip():
            if len(raw.encode("utf-8")) > 128 * 1024:
                raise WorldObservationError(
                    "invalid_configuration", "world source configuration exceeds its bound", 400
                )

            def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate configuration key")
                    result[key] = value
                return result

            try:
                value = _exact(
                    json.loads(raw, object_pairs_hook=unique),
                    {"sources", "registrations"},
                    "world sources",
                )
                if not isinstance(value["sources"], dict) or not isinstance(
                    value["registrations"], dict
                ):
                    raise ValueError("source registries must be objects")
                if len(value["sources"]) > MAX_OBSERVATION_SOURCES:
                    raise ValueError("too many observation sources")
                sources = {
                    key: ObservationSource.parse(key, entry)
                    for key, entry in value["sources"].items()
                }
                registrations = value["registrations"]
            except (ValueError, TypeError) as error:
                raise WorldObservationError(
                    "invalid_configuration", "world source configuration is invalid", 400
                ) from error
        return cls(
            sources=sources,
            registrations=registrations,
            approved_bundle=approved_bundle,
            database=database,
            clock=clock,
        )

    def _initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            if self.database.is_symlink() or not self.database.is_file():
                raise WorldObservationError(
                    "storage_unavailable", "observation database is not a regular file", 503
                ) from None
        else:
            os.close(descriptor)
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS world_observations (
                    seq INTEGER PRIMARY KEY, session TEXT NOT NULL, source TEXT NOT NULL,
                    epoch INTEGER NOT NULL, event_id TEXT NOT NULL, t INTEGER NOT NULL,
                    capture INTEGER NOT NULL, ingest INTEGER NOT NULL, payload TEXT NOT NULL,
                    binding TEXT NOT NULL, UNIQUE(session,event_id)
                );
                CREATE INDEX IF NOT EXISTS world_source_cursor
                    ON world_observations(session,source,epoch,seq);
                CREATE TABLE IF NOT EXISTS world_captures (
                    seq INTEGER PRIMARY KEY, session TEXT NOT NULL, actor TEXT NOT NULL,
                    at_ms INTEGER NOT NULL, receipt TEXT NOT NULL, evidence TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS world_device_contexts (
                    session TEXT NOT NULL, device INTEGER NOT NULL, epoch INTEGER NOT NULL,
                    node_type TEXT NOT NULL, retired INTEGER NOT NULL, PRIMARY KEY(session,device)
                );
            """)
            for row in connection.execute("SELECT * FROM world_device_contexts"):
                self._contexts[(row["session"], row["device"])] = (
                    row["epoch"],
                    row["node_type"],
                    bool(row["retired"]),
                )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(self.database, timeout=5)
        except sqlite3.Error as error:
            raise WorldObservationError(
                "storage_unavailable", "observation audit storage is unavailable", 503
            ) from error
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        except sqlite3.Error as error:
            raise WorldObservationError(
                "storage_unavailable", "observation audit storage is unavailable", 503
            ) from error
        finally:
            connection.close()

    def _require_available(self) -> None:
        if not self.available:
            raise WorldObservationError(
                "observations_unavailable",
                "no qualified world observation sources are configured",
                503,
            )

    def _now(self) -> int:
        return _integer(self.clock(), "relay clock")

    def _forget_device(self, session: str, device: int) -> None:
        for key in tuple(self._latest):
            if key[0] == session and self.sources[key[1]].drone_id == device:
                self._latest.pop(key, None)

    def invalidate(self, session: str) -> None:
        """Retire live map associations immediately after a map mutation."""
        with self._lock:
            self._map_context.pop(session, None)
            self._latest = {key: value for key, value in self._latest.items() if key[0] != session}

    def observe_state(self, session: str, state: object) -> None:
        """Retire disconnected/replaced device origins even without an HTTP request."""
        if not self.available:
            return
        with self._lock:
            try:
                self._state(session, state, self._now())
            except WorldObservationError as error:
                # HTTP reads can advance the authoritative projection before an
                # older queued publication arrives. Retirement is monotonic;
                # ignoring that notification cannot restore a previous origin.
                if error.code not in {"state_changed", "state_stale"}:
                    raise

    def _state(self, session: str, state: object, now: int) -> Mapping[int, Mapping[str, object]]:
        _text(session, "session", 512)
        if (
            not isinstance(state, Mapping)
            or state.get("session") != session
            or state.get("type") != "state"
        ):
            raise WorldObservationError(
                "state_unavailable", "current authoritative session state is required"
            )
        timestamp = _integer(state.get("t"), "state timestamp")
        sequence = _integer(state.get("state_sequence"), "state sequence", 1)
        roster = _integer(state.get("roster_version"), "roster version")
        rows = state.get("drones")
        if (
            not isinstance(rows, list)
            or len(rows) > 64
            or not all(isinstance(row, Mapping) for row in rows)
        ):
            raise WorldObservationError("state_unavailable", "authoritative roster is invalid")
        material = _json({"drones": rows, "selection": state.get("selection")})
        prior = self._state_context.get(session)
        if prior and (
            sequence < prior[0]
            or timestamp < prior[1]
            or roster < prior[2]
            or (sequence == prior[0] and material != prior[3])
        ):
            raise WorldObservationError(
                "state_changed", "authoritative state regressed or changed identity"
            )
        known_sessions = set(self._state_context) | {key[0] for key in self._contexts}
        if session not in known_sessions and len(known_sessions) >= MAX_SESSIONS:
            raise WorldObservationError(
                "capacity", "world observation session capacity is reached", 503
            )
        self._state_context[session] = (sequence, timestamp, roster, material)
        devices: dict[int, Mapping[str, object]] = {}
        configured_ids = {source.drone_id for source in self.sources.values()}
        for row in rows:
            device = _integer(row.get("drone_id"), "device ID", 1, 2**31 - 1)
            if device in devices:
                raise WorldObservationError(
                    "state_unavailable", "authoritative roster has duplicate identities"
                )
            devices[device] = row
        with self._connection() as connection:
            for device in configured_ids:
                key = (session, device)
                previous = self._contexts.get(key)
                row = devices.get(device)
                if row is None:
                    context = None if previous is None else (*previous[:2], True)
                else:
                    epoch = _integer(row.get("connection_epoch"), "connection epoch", 1, 2**31 - 1)
                    node_type = row.get("device_class")
                    if node_type not in {"aircraft", "ground_vehicle"}:
                        node_type = "unknown"
                    retired = row.get("membership") not in {"registered", "ready", "degraded"}
                    if previous and (
                        epoch < previous[0]
                        or (epoch == previous[0] and (previous[2] or node_type != previous[1]))
                    ):
                        context = (*previous[:2], True)
                    else:
                        context = (epoch, node_type, retired)
                if context is not None and context != previous:
                    self._forget_device(session, device)
                    connection.execute(
                        "INSERT OR REPLACE INTO world_device_contexts VALUES(?,?,?,?,?)",
                        (session, device, *context),
                    )
                    self._contexts[key] = context
                if row is not None:
                    seen = row.get("last_seen_at")
                    if type(seen) is not int or not 0 <= now - seen <= DEVICE_FRESH_MS:
                        self._forget_device(session, device)
        if not 0 <= now - timestamp < POSITION_FRESH_MS:
            for device in configured_ids:
                self._forget_device(session, device)
            raise WorldObservationError("state_stale", "authoritative session state is stale")
        return devices

    def _approved(self, session: str) -> Mapping[str, object]:
        try:
            approved = json.loads(_json(self.approved_bundle(session)))
            reference = _reference(approved["reference"])
            manifest = approved["bundle"]["manifest"]
            approval = approved["approval"]
            if not isinstance(manifest, Mapping) or not isinstance(approval, Mapping):
                raise ValueError("missing approved manifest")
            identity = _json({"reference": reference, "approval": approval})
        except (ValueError, TypeError, KeyError) as error:
            identity = None
            if self._map_context.get(session) != identity:
                self._latest = {
                    key: value for key, value in self._latest.items() if key[0] != session
                }
            self._map_context[session] = identity
            raise WorldObservationError(
                "approval_unavailable", "a current approved map bundle is required"
            ) from error
        if self._map_context.get(session) != identity:
            self._latest = {key: value for key, value in self._latest.items() if key[0] != session}
        self._map_context[session] = identity
        return approved

    def _binding(self, source_id: str, approved: Mapping[str, object]) -> dict[str, object]:
        registration = self.registrations[source_id]
        manifest = approved["bundle"]["manifest"]
        measured = manifest.get("registration")
        if not isinstance(measured, Mapping) or (
            dict(registration.reference) != approved["reference"]
            or registration.map_version != manifest.get("mapVersion")
            or registration.floor_id != manifest.get("floorId")
            or manifest.get("frame") != "world"
            or manifest.get("units") != "m"
            or registration.source_frame != measured.get("sourceFrame")
            or registration.transform_id != measured.get("transformId")
        ):
            raise WorldObservationError(
                "registration_changed",
                "host-qualified source registration does not match the approved map",
            )
        return {**registration.to_dict(), "approval": approved["approval"]}

    def ingest(
        self, session: str, raw: object, principal: Principal, state: object
    ) -> dict[str, object]:
        self._require_available()
        try:
            submission = ObservationSubmission.parse(raw)
        except ObservationError as error:
            raise WorldObservationError(error.code, error.detail, 400) from error
        with self._lock:
            now = self._now()
            devices = self._state(session, state, now)
            source = self.sources.get(submission.source_id)
            if (
                source is None
                or not isinstance(principal, Principal)
                or (
                    principal.source != source.principal_source
                    or principal.drone_id != source.drone_id
                    or submission.session != session
                    or submission.drone_id != source.drone_id
                    or submission.node_type != source.node_type
                )
            ):
                raise WorldObservationError(
                    "source_mismatch",
                    "observation source is not bound to this authenticated device",
                    403,
                )
            row = devices.get(submission.drone_id)
            context = self._contexts.get((session, submission.drone_id))
            if row is None or context != (
                submission.connection_epoch,
                source.node_type.value,
                False,
            ):
                raise WorldObservationError(
                    "device_changed", "observation device class or connection epoch is not current"
                )
            seen = row.get("last_seen_at")
            if type(seen) is not int or not 0 <= now - seen <= DEVICE_FRESH_MS:
                raise WorldObservationError("device_stale", "observation device report is stale")
            if submission.frame != "world" or not isinstance(submission.payload, PosePayload):
                raise WorldObservationError(
                    "frame_unqualified", "only an already-transformed world pose can be consumed"
                )
            if not 0 <= now - submission.t < POSITION_FRESH_MS or not (
                submission.t_capture is not None
                and 0 <= now - submission.t_capture < POSITION_FRESH_MS
            ):
                raise WorldObservationError(
                    "observation_stale",
                    "observation capture and transport times must be fresh relay-clock times",
                )
            binding = self._binding(source.source_id, self._approved(session))
            accepted = Observation(submission, now, source.frames[0]).to_dict()
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "SELECT t,capture,ingest FROM world_observations "
                    "WHERE session=? AND source=? AND epoch=? ORDER BY seq DESC LIMIT 1",
                    (session, source.source_id, submission.connection_epoch),
                ).fetchone()
                if cursor and (
                    submission.t <= cursor["t"] or submission.t_capture <= cursor["capture"]
                ):
                    raise WorldObservationError(
                        "observation_reordered", "observation source timestamps did not advance"
                    )
                if cursor and now - cursor["ingest"] < 1000 // PAYLOAD_MAX_HZ["pose"]:
                    raise WorldObservationError(
                        "observation_rate_limited", "pose observations are limited to 20 Hz", 429
                    )
                if (
                    connection.execute(
                        "SELECT COUNT(*) FROM world_observations WHERE session=?", (session,)
                    ).fetchone()[0]
                    >= MAX_SESSION_OBSERVATIONS
                ):
                    raise WorldObservationError(
                        "capacity", "world observation audit capacity is reached", 503
                    )
                try:
                    connection.execute(
                        "INSERT INTO world_observations"
                        "(session,source,epoch,event_id,t,capture,ingest,payload,binding) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            session,
                            source.source_id,
                            submission.connection_epoch,
                            submission.event_id,
                            submission.t,
                            submission.t_capture,
                            now,
                            bounded_json(accepted).decode(),
                            _json(binding),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise WorldObservationError(
                        "observation_replayed", "observation event ID was already accepted"
                    ) from error
            self._latest[(session, source.source_id)] = {
                "observation": json.loads(bounded_json(accepted)),
                "binding": binding,
            }
            return accepted

    def positions(self, session: str, request: object, state: object) -> dict[str, object]:
        self._require_available()
        request = _exact(request, {"mapVersion", "floorId", "reference"}, "position request")
        map_version, floor_id = (_text(request[key], key) for key in ("mapVersion", "floorId"))
        reference = _reference(request["reference"])
        with self._lock:
            now = self._now()
            devices = self._state(session, state, now)
            approved = self._approved(session)
            if reference != approved["reference"]:
                raise WorldObservationError(
                    "reference_changed",
                    "requested map revision is not the active approved revision",
                )
            manifest = approved["bundle"]["manifest"]
            if map_version != manifest.get("mapVersion") or floor_id != manifest.get("floorId"):
                raise WorldObservationError(
                    "reference_changed", "requested map labels do not match the approved revision"
                )
            projected: dict[int, dict[str, object]] = {}
            for (own_session, source_id), latest in tuple(self._latest.items()):
                if own_session != session:
                    continue
                source = self.sources[source_id]
                try:
                    binding = self._binding(source_id, approved)
                except WorldObservationError:
                    self._latest.pop((session, source_id), None)
                    continue
                observation = latest["observation"]
                capture, ingest = observation["t_capture"], observation["t_ingest"]
                row = devices.get(source.drone_id)
                context = self._contexts.get((session, source.drone_id))
                if (
                    latest["binding"] != binding
                    or row is None
                    or context != (observation["connection_epoch"], source.node_type.value, False)
                    or not 0 <= now - capture < POSITION_FRESH_MS
                    or ingest > now
                ):
                    self._latest.pop((session, source_id), None)
                    continue
                if binding["mapVersion"] != map_version or binding["floorId"] != floor_id:
                    continue
                position = observation["payload"]["position"]
                value = {
                    "observationId": observation["event_id"],
                    "sourceId": source_id,
                    "deviceId": source.drone_id,
                    "connectionEpoch": observation["connection_epoch"],
                    "sessionId": session,
                    "reference": dict(reference),
                    "frame": "world",
                    "mapVersion": map_version,
                    "floorId": floor_id,
                    "position": {"x": position["x_m"], "y": position["y_m"]},
                    "tCapture": capture,
                    "tIngest": ingest,
                    "confidence": observation["confidence"],
                    "frameAssociationVerified": True,
                }
                previous = projected.get(source.drone_id)
                if previous is None or (capture, ingest, source_id) > (
                    previous["tCapture"],
                    previous["tIngest"],
                    previous["sourceId"],
                ):
                    projected[source.drone_id] = value
            return {
                "reference": dict(reference),
                "observations": [projected[device] for device in sorted(projected)],
            }

    def record(self, session: str, request: object, state: object, actor: str) -> dict[str, object]:
        self._require_available()
        request = _exact(
            request,
            {"mapVersion", "floorId", "reference", "tagId", "deviceId", "connectionEpoch"},
            "record request",
        )
        actor = _text(actor, "authenticated actor")
        tag = _integer(request["tagId"], "tag ID", 0, 65535)
        device = _integer(request["deviceId"], "device ID", 1, 2**31 - 1)
        epoch = _integer(request["connectionEpoch"], "connection epoch", 1, 2**31 - 1)
        with self._lock:
            values = self.positions(
                session,
                {key: request[key] for key in ("mapVersion", "floorId", "reference")},
                state,
            )["observations"]
            if not isinstance(state, Mapping) or state.get("selection") != [device]:
                raise WorldObservationError(
                    "selection_changed", "recording requires exactly one selected ground robot"
                )
            observation = next(
                (
                    value
                    for value in values
                    if value["deviceId"] == device and value["connectionEpoch"] == epoch
                ),
                None,
            )
            if (
                observation is None
                or self.sources[observation["sourceId"]].node_type.value != "ground_vehicle"
            ):
                raise WorldObservationError(
                    "observation_unavailable",
                    "recording requires a fresh qualified observation "
                    "from the selected ground robot",
                )
            latest = self._latest[(session, observation["sourceId"])]
            receipt = {**observation, "observationId": f"capture-{uuid4().hex}", "tagId": tag}
            evidence = {
                "kind": "drive_over_record",
                "sourceObservation": latest["observation"],
                "registration": latest["binding"],
                "selection": [device],
                "stateEventId": state.get("event_id"),
                "rosterVersion": state.get("roster_version"),
            }
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                recorded_at = self._now()
                if not 0 <= recorded_at - receipt["tCapture"] < POSITION_FRESH_MS:
                    raise WorldObservationError(
                        "observation_stale", "world observation expired before it could be recorded"
                    )
                if (
                    self._binding(observation["sourceId"], self._approved(session))
                    != latest["binding"]
                ):
                    raise WorldObservationError(
                        "registration_changed", "world registration changed before recording"
                    )
                if (
                    connection.execute(
                        "SELECT COUNT(*) FROM world_captures WHERE session=?", (session,)
                    ).fetchone()[0]
                    >= MAX_SESSION_CAPTURES
                ):
                    raise WorldObservationError(
                        "capacity", "map capture audit capacity is reached", 503
                    )
                connection.execute(
                    "INSERT INTO world_captures(session,actor,at_ms,receipt,evidence) "
                    "VALUES(?,?,?,?,?)",
                    (session, actor, recorded_at, _json(receipt), _json(evidence)),
                )
            return receipt

    def audit_records(self, session: str) -> list[dict[str, object]]:
        self._require_available()
        session = _text(session, "session", 512)
        with self._lock, self._connection() as connection:
            return [
                {
                    "actor": row["actor"],
                    "at": row["at_ms"],
                    "receipt": json.loads(row["receipt"]),
                    "evidence": json.loads(row["evidence"]),
                }
                for row in connection.execute(
                    "SELECT * FROM world_captures WHERE session=? ORDER BY seq", (session,)
                )
            ]

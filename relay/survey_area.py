"""Pilot-assisted ground survey lifecycle and immutable scan-candidate storage."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

from relay.capabilities import IntentName
from relay.contracts import LifecycleStatus
from relay.intent_v1 import MAX_INTENT_IDENTIFIER_CHARS, MAX_INTENT_TIMESTAMP, IntentV1
from relay.observations import Observation
from relay.session import IntentSinkResult, RelaySession

MAX_SURVEY_SCANS = 4_096
MAX_SURVEY_CANDIDATE_BYTES = 16 * 1024 * 1024
MAX_SURVEY_DURATION_MS = 30 * 60 * 1_000


class SurveyLifecycleError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SurveyLifecycleRequest:
    v: int
    t: int
    type: str
    event_id: str
    session: str
    operation: str
    intent_id: str
    run_id: str
    connection_epoch: int

    @classmethod
    def parse(cls, raw: object) -> SurveyLifecycleRequest:
        if not isinstance(raw, Mapping) or set(raw) != {
            "v",
            "t",
            "type",
            "event_id",
            "session",
            "operation",
            "intent_id",
            "run_id",
            "connection_epoch",
        }:
            raise SurveyLifecycleError(
                "invalid_survey_lifecycle", "survey lifecycle fields do not match"
            )
        if raw["v"] != 1 or raw["type"] != "survey_lifecycle":
            raise SurveyLifecycleError(
                "invalid_survey_lifecycle", "survey lifecycle version is invalid"
            )
        if (
            not isinstance(raw["t"], int)
            or isinstance(raw["t"], bool)
            or not 0 <= raw["t"] <= MAX_INTENT_TIMESTAMP
        ):
            raise SurveyLifecycleError(
                "invalid_survey_lifecycle", "survey lifecycle timestamp is invalid"
            )
        for name in ("event_id", "session", "intent_id", "run_id"):
            value = raw[name]
            if (
                not isinstance(value, str)
                or not value
                or len(value) > MAX_INTENT_IDENTIFIER_CHARS
                or value != value.strip()
                or not value.isprintable()
            ):
                raise SurveyLifecycleError(
                    "invalid_survey_lifecycle", f"{name} must be bounded canonical text"
                )
        if raw["operation"] not in {"complete", "cancel"}:
            raise SurveyLifecycleError(
                "invalid_survey_lifecycle", "survey operation must be complete or cancel"
            )
        epoch = raw["connection_epoch"]
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise SurveyLifecycleError("invalid_survey_lifecycle", "survey epoch is invalid")
        return cls(**raw)  # type: ignore[arg-type]

    def to_event(self) -> dict[str, object]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "operation": self.operation,
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "connection_epoch": self.connection_epoch,
        }


@dataclass(frozen=True, slots=True)
class SurveyCandidate:
    candidate_id: str
    intent_id: str
    run_id: str
    area_id: str
    device_id: int
    connection_epoch: int
    pose_identity: Mapping[str, object]
    scans: tuple[Mapping[str, object], ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "v": 1,
            "type": "survey_candidate",
            "candidate_id": self.candidate_id,
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "area_id": self.area_id,
            "device_id": self.device_id,
            "connection_epoch": self.connection_epoch,
            "pose_identity": dict(self.pose_identity),
            "scans": [dict(scan) for scan in self.scans],
        }


class SurveyCandidateRegistry:
    """Stores complete candidates with atomic create-only publication."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, candidate: SurveyCandidate) -> Path:
        raw = json.dumps(
            candidate.to_mapping(), allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
        if len(raw) > MAX_SURVEY_CANDIDATE_BYTES:
            raise SurveyLifecycleError(
                "survey_candidate_too_large", "survey candidate exceeds the storage bound"
            )
        final = self.root / f"{candidate.candidate_id}.json"
        if final.exists():
            raise SurveyLifecycleError(
                "survey_candidate_exists", "survey candidate identity already exists"
            )
        descriptor, temporary = tempfile.mkstemp(prefix=".survey-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, final)
            except FileExistsError as error:
                raise SurveyLifecycleError(
                    "survey_candidate_exists", "survey candidate identity already exists"
                ) from error
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return final

    def load(self, candidate_id: str) -> dict[str, object]:
        if not candidate_id or "/" in candidate_id or "\\" in candidate_id:
            raise SurveyLifecycleError("invalid_candidate_id", "candidate identity is invalid")
        path = self.root / f"{candidate_id}.json"
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_SURVEY_CANDIDATE_BYTES + 1)
        except FileNotFoundError as error:
            raise SurveyLifecycleError(
                "survey_candidate_missing", "survey candidate does not exist"
            ) from error
        if len(raw) > MAX_SURVEY_CANDIDATE_BYTES:
            raise SurveyLifecycleError(
                "survey_candidate_too_large", "stored candidate exceeds the storage bound"
            )
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("candidate_id") != candidate_id:
            raise SurveyLifecycleError("invalid_candidate", "stored candidate identity is invalid")
        return value


@dataclass(slots=True)
class _Run:
    intent: IntentV1
    run_id: str
    device_id: int
    connection_epoch: int
    pose_identity: Mapping[str, object]
    started_at: int
    scans: list[Mapping[str, object]] = field(default_factory=list)
    encoded_bytes: int = 0


class SurveyAreaLifecycle:
    """Owns one recording run per session while leaving all movement outside this path."""

    def __init__(self, session: RelaySession, candidates: SurveyCandidateRegistry) -> None:
        self.session = session
        self.candidates = candidates
        self._runs: dict[str, _Run] = {}
        self._lock = RLock()

    def start(self, intent: IntentV1) -> IntentSinkResult:
        if intent.name is not IntentName.SURVEY_AREA:
            raise ValueError("survey lifecycle only accepts survey_area")
        if not intent.confirm or len(intent.selection) != 1:
            return self._refused(
                "survey_selection_invalid", "survey requires one confirmed ground selection"
            )
        device_id = intent.selection[0]
        identity = self.session.registry.ready_ground_identity(device_id, self.session.clock())
        if identity is None:
            return self._refused(
                "survey_ground_not_ready", "selected ground node lacks current ready pose evidence"
            )
        with self._lock:
            if self._runs:
                return self._refused(
                    "survey_already_running", "a survey recording is already active"
                )
            run_id = f"survey-{intent.intent_id}"
            self._runs[intent.intent_id] = _Run(
                intent=intent,
                run_id=run_id,
                device_id=device_id,
                connection_epoch=identity.connection_epoch,
                pose_identity=identity.to_event(),
                started_at=self.session.clock(),
            )
        return IntentSinkResult(
            status=LifecycleStatus.EXECUTING,
            source="survey_area",
            result={"run_id": run_id, "connection_epoch": identity.connection_epoch},
        )

    def accepted_observation(self, observation: Observation) -> list[dict[str, object]]:
        if observation.submission.payload["kind"] != "range_scan":
            return []
        with self._lock:
            run = next(iter(self._runs.values()), None)
            if run is None or (
                observation.submission.device_id,
                observation.submission.connection_epoch,
            ) != (run.device_id, run.connection_epoch):
                return []
            scan = observation.to_mapping()
            encoded = len(
                json.dumps(scan, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
            )
            if (
                len(run.scans) >= MAX_SURVEY_SCANS
                or run.encoded_bytes + encoded > MAX_SURVEY_CANDIDATE_BYTES
            ):
                self._runs.pop(run.intent.intent_id, None)
                return [
                    self.session.record_lifecycle(
                        intent_id=run.intent.intent_id,
                        status=LifecycleStatus.FAILED,
                        source="survey_area",
                        drone_id=run.device_id,
                        connection_epoch=run.connection_epoch,
                        reason="survey_candidate_too_large",
                        detail="survey scan evidence exceeded the bounded candidate capacity",
                    )
                ]
            run.scans.append(scan)
            run.encoded_bytes += encoded
        return []

    def process(self, request: SurveyLifecycleRequest) -> list[dict[str, object]]:
        with self._lock:
            run = self._runs.get(request.intent_id)
            if run is None or run.run_id != request.run_id:
                return [
                    self.session.protocol_refusal(
                        reason="unknown_survey_run", detail="survey run is not active"
                    )
                ]
            if request.connection_epoch != run.connection_epoch:
                return [
                    self.session.protocol_refusal(
                        reason="stale_survey_epoch", detail="survey lifecycle epoch is not current"
                    )
                ]
            identity = self.session.registry.ready_ground_identity(
                run.device_id, self.session.clock()
            )
            if identity is None or identity.connection_epoch != run.connection_epoch:
                self._runs.pop(run.intent.intent_id, None)
                return [
                    self.session.record_lifecycle(
                        intent_id=run.intent.intent_id,
                        status=LifecycleStatus.FAILED,
                        source="survey_area",
                        drone_id=run.device_id,
                        connection_epoch=run.connection_epoch,
                        reason="survey_ground_not_ready",
                        detail="ground readiness was lost before survey completion",
                    )
                ]
            if request.operation == "cancel":
                self._runs.pop(run.intent.intent_id, None)
                return [
                    self.session.record_lifecycle(
                        intent_id=run.intent.intent_id,
                        status=LifecycleStatus.INVALIDATED,
                        source="survey_area",
                        drone_id=run.device_id,
                        connection_epoch=run.connection_epoch,
                        reason="survey_cancelled",
                        detail="operator cancelled the pilot-assisted survey",
                    )
                ]
            if not run.scans:
                return [
                    self.session.protocol_refusal(
                        reason="survey_evidence_missing",
                        detail="survey requires at least one current range scan",
                    )
                ]
            candidate = SurveyCandidate(
                candidate_id=f"candidate-{run.run_id}",
                intent_id=run.intent.intent_id,
                run_id=run.run_id,
                area_id=str(run.intent.args["area_id"]),
                device_id=run.device_id,
                connection_epoch=run.connection_epoch,
                pose_identity=run.pose_identity,
                scans=tuple(run.scans),
            )
            try:
                self.candidates.save(candidate)
            except SurveyLifecycleError as error:
                self._runs.pop(run.intent.intent_id, None)
                return [
                    self.session.record_lifecycle(
                        intent_id=run.intent.intent_id,
                        status=LifecycleStatus.FAILED,
                        source="survey_area",
                        drone_id=run.device_id,
                        connection_epoch=run.connection_epoch,
                        reason=error.code,
                        detail=error.detail,
                    )
                ]
            self._runs.pop(run.intent.intent_id, None)
            return [
                self.session.record_lifecycle(
                    intent_id=run.intent.intent_id,
                    status=LifecycleStatus.COMPLETED,
                    source="survey_area",
                    drone_id=run.device_id,
                    connection_epoch=run.connection_epoch,
                    detail=f"saved immutable candidate {candidate.candidate_id}",
                )
            ]

    def adapter_disconnected(
        self, *, drone_id: int, connection_epoch: int
    ) -> list[dict[str, object]]:
        with self._lock:
            run = next(
                (
                    item
                    for item in self._runs.values()
                    if (item.device_id, item.connection_epoch) == (drone_id, connection_epoch)
                ),
                None,
            )
            if run is None:
                return []
            self._runs.pop(run.intent.intent_id, None)
            return [
                self.session.record_lifecycle(
                    intent_id=run.intent.intent_id,
                    status=LifecycleStatus.FAILED,
                    source="survey_area",
                    drone_id=drone_id,
                    connection_epoch=connection_epoch,
                    reason="survey_adapter_disconnected",
                    detail="ground adapter disconnected during survey",
                )
            ]

    def periodic_events(self) -> list[dict[str, object]]:
        now = self.session.clock()
        with self._lock:
            expired = [
                run for run in self._runs.values() if now - run.started_at > MAX_SURVEY_DURATION_MS
            ]
            for run in expired:
                self._runs.pop(run.intent.intent_id, None)
        return [
            self.session.record_lifecycle(
                intent_id=run.intent.intent_id,
                status=LifecycleStatus.FAILED,
                source="survey_area",
                drone_id=run.device_id,
                connection_epoch=run.connection_epoch,
                reason="survey_timeout",
                detail="survey recording exceeded the configured duration",
            )
            for run in expired
        ]

    @staticmethod
    def _refused(reason: str, detail: str) -> IntentSinkResult:
        return IntentSinkResult(
            status=LifecycleStatus.REFUSED, source="survey_area", reason=reason, detail=detail
        )

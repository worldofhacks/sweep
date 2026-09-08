"""Pilot-assisted ground survey lifecycle with immutable canonical evidence artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import shutil
import stat
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
from tools.ohmni_occupancy_grid import FORMAT as OCCUPANCY_FORMAT
from tools.ohmni_occupancy_grid import GridConfig, build_grid
from tools.ohmni_scan_record import RecordingConfig, record_events

MAX_SURVEY_SCANS = 4_096
MAX_SURVEY_CANDIDATE_BYTES = 16 * 1024 * 1024
MAX_SURVEY_DURATION_MS = 30 * 60 * 1_000
MAX_SURVEY_PREVIEW_METADATA_BYTES = 64 * 1024
MAX_SURVEY_PREVIEW_BYTES = 24 * 1024 * 1024


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
        fields = {
            "v",
            "t",
            "type",
            "event_id",
            "session",
            "operation",
            "intent_id",
            "run_id",
            "connection_epoch",
        }
        if not isinstance(raw, Mapping) or set(raw) != fields:
            raise SurveyLifecycleError(
                "invalid_survey_lifecycle", "survey lifecycle fields do not match"
            )
        if type(raw["v"]) is not int or raw["v"] != 1 or raw["type"] != "survey_lifecycle":
            raise SurveyLifecycleError(
                "invalid_survey_lifecycle", "survey lifecycle version is invalid"
            )
        if type(raw["t"]) is not int or not 0 <= raw["t"] <= MAX_INTENT_TIMESTAMP:
            raise SurveyLifecycleError(
                "invalid_survey_lifecycle", "survey lifecycle timestamp is invalid"
            )
        limits = {
            "event_id": MAX_INTENT_IDENTIFIER_CHARS,
            "session": 512,
            "intent_id": MAX_INTENT_IDENTIFIER_CHARS,
            "run_id": MAX_INTENT_IDENTIFIER_CHARS,
        }
        for name, maximum in limits.items():
            value = raw[name]
            if (
                type(value) is not str
                or not value
                or len(value) > maximum
                or value != value.strip()
                or not value.isprintable()
            ):
                raise SurveyLifecycleError(
                    "invalid_survey_lifecycle", f"{name} must be bounded canonical text"
                )
        if type(raw["operation"]) is not str or raw["operation"] not in {"complete", "cancel"}:
            raise SurveyLifecycleError(
                "invalid_survey_lifecycle", "survey operation must be complete or cancel"
            )
        if (
            type(raw["connection_epoch"]) is not int
            or not 1 <= raw["connection_epoch"] <= 2_147_483_647
        ):
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
class SurveySource:
    source_id: str
    odom_frame: str
    lidar_frame: str
    mount_id: str


@dataclass(frozen=True, slots=True)
class SurveyCandidate:
    candidate_id: str
    session: str
    intent_id: str
    run_id: str
    area_id: str
    device_id: int
    connection_epoch: int
    pose_identity: Mapping[str, object]
    source: SurveySource
    scans: tuple[Observation, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "v": 1,
            "type": "survey_candidate",
            "candidate_id": self.candidate_id,
            "session": self.session,
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "area_id": self.area_id,
            "device_id": self.device_id,
            "connection_epoch": self.connection_epoch,
            "pose_identity": dict(self.pose_identity),
            "source": {
                "source_id": self.source.source_id,
                "odom_frame": self.source.odom_frame,
                "lidar_frame": self.source.lidar_frame,
                "mount_id": self.source.mount_id,
            },
            "scans": [scan.to_mapping() for scan in self.scans],
        }


class SurveyCandidateRegistry:
    """Publishes a completed recording, rendered occupancy grid, and provenance atomically."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, candidate: SurveyCandidate) -> Path:
        raw = _canonical(candidate.to_mapping())
        if len(raw) > MAX_SURVEY_CANDIDATE_BYTES:
            raise SurveyLifecycleError(
                "survey_candidate_too_large", "survey candidate exceeds the storage bound"
            )
        final = self._candidate_path(candidate.candidate_id)
        if final.exists() or final.is_symlink():
            raise SurveyLifecycleError(
                "survey_candidate_exists", "survey candidate identity already exists"
            )
        temporary = Path(tempfile.mkdtemp(prefix=".survey-", dir=self.root))
        published = False
        try:
            recording = record_events(
                (scan.encode() for scan in candidate.scans),
                temporary / "recording",
                RecordingConfig(
                    candidate.session,
                    candidate.device_id,
                    candidate.connection_epoch,
                    candidate.source.source_id,
                    candidate.source.odom_frame,
                    candidate.source.lidar_frame,
                    candidate.source.mount_id,
                    candidate.run_id,
                    max_records=MAX_SURVEY_SCANS,
                    max_bytes=MAX_SURVEY_CANDIDATE_BYTES,
                    duration_s=MAX_SURVEY_DURATION_MS / 1_000,
                ),
            )
            grid = build_grid(temporary / "recording", temporary / "occupancy", GridConfig())
            _write_create_only(
                temporary / "pose_path.json",
                {
                    "v": 1,
                    "type": "survey_pose_path",
                    "initial_pose_identity": dict(candidate.pose_identity),
                    "sensor_poses": [
                        dict(scan.submission.payload["sensor_pose"]) for scan in candidate.scans
                    ],
                },
            )
            _write_create_only(
                temporary / "tag_candidates.json",
                {"v": 1, "type": "survey_tag_candidates", "candidates": []},
            )
            artifact_paths = (
                "recording/recording.json",
                "recording/observations.jsonl",
                "occupancy/manifest.json",
                "occupancy/occupancy.png",
                "pose_path.json",
                "tag_candidates.json",
            )
            files = {}
            for relative in artifact_paths:
                encoded = _read_regular(temporary, relative, MAX_SURVEY_CANDIDATE_BYTES)
                files[relative] = {
                    "bytes": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            manifest = {
                **candidate.to_mapping(),
                "artifacts": {
                    "recording": "recording/recording.json",
                    "occupancy": "occupancy/manifest.json",
                    "pose_path": "pose_path.json",
                    "tag_candidates": "tag_candidates.json",
                },
                "recording": recording,
                "occupancy": grid,
                "files": files,
            }
            _write_create_only(temporary / "candidate.json", manifest)
            os.rename(temporary, final)
            published = True
            self.load(candidate.candidate_id)
        except SurveyLifecycleError:
            shutil.rmtree(temporary, ignore_errors=True)
            if published:
                shutil.rmtree(final, ignore_errors=True)
            raise
        except (OSError, ValueError, TypeError, OverflowError) as error:
            shutil.rmtree(temporary, ignore_errors=True)
            if published:
                shutil.rmtree(final, ignore_errors=True)
            raise SurveyLifecycleError(
                "survey_candidate_write_failed", "could not publish survey evidence"
            ) from error
        return final

    def delete(self, candidate_id: str) -> None:
        path = self._candidate_path(candidate_id)
        shutil.rmtree(path, ignore_errors=True)

    def load(self, candidate_id: str) -> dict[str, object]:
        directory = self._candidate_path(candidate_id)
        if not directory.is_dir() or directory.is_symlink():
            raise SurveyLifecycleError(
                "survey_candidate_missing", "survey candidate does not exist"
            )
        candidate = _read_json_regular(directory / "candidate.json", MAX_SURVEY_CANDIDATE_BYTES)
        if (
            candidate.get("candidate_id") != candidate_id
            or candidate.get("type") != "survey_candidate"
        ):
            raise SurveyLifecycleError("invalid_candidate", "stored candidate identity is invalid")
        artifacts = candidate.get("artifacts")
        if not isinstance(artifacts, dict):
            raise SurveyLifecycleError(
                "invalid_candidate", "stored candidate artifacts are invalid"
            )
        expected = {
            "recording": "recording/recording.json",
            "occupancy": "occupancy/manifest.json",
            "pose_path": "pose_path.json",
            "tag_candidates": "tag_candidates.json",
        }
        if artifacts != expected:
            raise SurveyLifecycleError(
                "invalid_candidate", "stored candidate artifact paths are invalid"
            )
        files = candidate.get("files")
        if not isinstance(files, dict) or set(files) != {
            *expected.values(),
            "recording/observations.jsonl",
            "occupancy/occupancy.png",
        }:
            raise SurveyLifecycleError(
                "invalid_candidate", "stored candidate file inventory is invalid"
            )
        for relative, metadata in files.items():
            encoded = _read_regular(directory, relative, MAX_SURVEY_CANDIDATE_BYTES)
            if metadata != {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}:
                raise SurveyLifecycleError("invalid_candidate", "stored candidate artifact changed")
        return candidate

    def _candidate_path(self, candidate_id: str) -> Path:
        if not _candidate_identifier(candidate_id):
            raise SurveyLifecycleError("invalid_candidate_id", "candidate identity is invalid")
        return self.root / candidate_id

    def preview(self, session: str, candidate_id: str) -> dict[str, object]:
        """Read verified recording evidence without granting world or motion authority."""
        candidate = self.load(candidate_id)
        if candidate.get("session") != session:
            raise SurveyLifecycleError(
                "survey_candidate_missing", "survey candidate does not exist in this session"
            )
        intent_id, run_id = candidate.get("intent_id"), candidate.get("run_id")
        source, pose = candidate.get("source"), candidate.get("pose_identity")
        if (
            type(candidate.get("v")) is not int
            or candidate["v"] != 1
            or not _preview_text(session, 512)
            or not _preview_text(intent_id)
            or not _preview_text(run_id)
            or _candidate_id(session, intent_id, run_id) != candidate_id
            or type(candidate.get("device_id")) is not int
            or not 1 <= candidate["device_id"] <= 2_147_483_647
            or type(candidate.get("connection_epoch")) is not int
            or not 1 <= candidate["connection_epoch"] <= 2_147_483_647
            or not _preview_text(candidate.get("area_id"))
            or not isinstance(source, dict)
            or set(source) != {"source_id", "odom_frame", "lidar_frame", "mount_id"}
            or not all(_preview_text(value) for value in source.values())
            or source["odom_frame"] == source["lidar_frame"]
            or "world" in {source["odom_frame"], source["lidar_frame"]}
            or not isinstance(pose, dict)
            or set(pose) != {"event_id", "source_id", "session", "connection_epoch", "frame"}
            or not _preview_text(pose["event_id"])
            or not _preview_text(pose["source_id"])
            or pose["session"] != session
            or type(pose["connection_epoch"]) is not int
            or pose["connection_epoch"] != candidate["connection_epoch"]
            or pose["frame"] != source["odom_frame"]
        ):
            raise SurveyLifecycleError(
                "invalid_candidate", "stored candidate provenance is invalid"
            )
        directory = self._candidate_path(candidate_id)
        files = candidate["files"]
        # Revalidate the exact bytes returned, including replacement between load
        # and preview. The API never accepts a caller-supplied artifact path.

        def artifact(relative: str, limit: int) -> bytes:
            encoded = _read_regular(directory, relative, limit)
            if files[relative] != {  # type: ignore[index]
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }:
                raise SurveyLifecycleError("invalid_candidate", "stored candidate artifact changed")
            return encoded

        image = artifact("occupancy/occupancy.png", MAX_SURVEY_CANDIDATE_BYTES)
        occupancy = _candidate_json(
            artifact("occupancy/manifest.json", MAX_SURVEY_PREVIEW_METADATA_BYTES)
        )
        recording = _candidate_json(
            artifact("recording/recording.json", MAX_SURVEY_PREVIEW_METADATA_BYTES)
        )
        pose_path = _candidate_json(artifact("pose_path.json", MAX_SURVEY_CANDIDATE_BYTES))
        recorded_source = {
            "transport": "file_jsonl",
            "session": session,
            "device_id": candidate["device_id"],
            "connection_epoch": candidate["connection_epoch"],
            "source_id": source["source_id"],
            "node_type": "ground",
        }
        recorded_frames = {
            "odometry": source["odom_frame"],
            "lidar": source["lidar_frame"],
            "sensor_pose": {
                "parent_frame": source["odom_frame"],
                "child_frame": source["lidar_frame"],
            },
        }
        if (
            recording != candidate.get("recording")
            or _canonical(recording.get("source")) != _canonical(recorded_source)
            or recording.get("frames") != recorded_frames
            or recording.get("run_id") != run_id
            or recording.get("mount_id") != source["mount_id"]
            or occupancy.get("format") != OCCUPANCY_FORMAT
            or occupancy != candidate.get("occupancy")
            or _canonical(occupancy.get("input"))
            != _canonical(
                {
                    "run_id": run_id,
                    "source": recorded_source,
                    "observations_sha256": files["recording/observations.jsonl"]["sha256"],
                }
            )
            or occupancy.get("mount_id") != source["mount_id"]
            or occupancy.get("files") != {"occupancy.png": files["occupancy/occupancy.png"]}
            or not isinstance(occupancy.get("grid"), dict)
            or occupancy["grid"].get("frame") != source.get("odom_frame")
            or occupancy["grid"].get("frame_kind") != "source_scoped_local_odometry"
            or occupancy["grid"].get("registered_to_world") is not False
            or _canonical(pose_path.get("initial_pose_identity")) != _canonical(pose)
            or not image.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            raise SurveyLifecycleError("invalid_candidate", "occupancy evidence is invalid")
        metadata = {
            "v": 1,
            "type": "survey_candidate_preview",
            **{
                key: candidate[key]
                for key in (
                    "candidate_id",
                    "session",
                    "intent_id",
                    "run_id",
                    "device_id",
                    "connection_epoch",
                    "area_id",
                    "source",
                    "pose_identity",
                    "files",
                )
            },
            "occupancy": occupancy,
            "navigation_authority": False,
        }
        if len(_canonical(metadata)) > MAX_SURVEY_PREVIEW_METADATA_BYTES:
            raise SurveyLifecycleError(
                "survey_candidate_too_large", "preview metadata exceeds 64 KiB"
            )
        result = {
            **metadata,
            "image": {
                "mime_type": "image/png",
                "bytes": len(image),
                "sha256": hashlib.sha256(image).hexdigest(),
                "data_base64": base64.b64encode(image).decode("ascii"),
            },
        }
        if len(_canonical(result)) > MAX_SURVEY_PREVIEW_BYTES:
            raise SurveyLifecycleError("survey_candidate_too_large", "preview exceeds 24 MiB")
        return result


def _candidate_identifier(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 42
        and value.startswith("candidate-")
        and all(char in "0123456789abcdef" for char in value[10:])
    )


def _preview_text(value: object, maximum: int = MAX_INTENT_IDENTIFIER_CHARS) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= maximum
        and value == value.strip()
        and value.isprintable()
    )


def _canonical(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _write_create_only(path: Path, value: object) -> None:
    encoded = _canonical(value)
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _read_regular(root: Path, relative: str, limit: int) -> bytes:
    parts = relative.split("/")
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise SurveyLifecycleError("invalid_candidate", "stored candidate path is invalid")
    directory = None
    descriptor = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            next_directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = next_directory
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except OSError as error:
        raise SurveyLifecycleError(
            "invalid_candidate", "stored candidate artifact is missing"
        ) from error
    finally:
        if directory is not None:
            os.close(directory)
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            raise SurveyLifecycleError(
                "invalid_candidate", "stored candidate artifact is not regular"
            )
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(limit + 1)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    if len(raw) > limit:
        raise SurveyLifecycleError(
            "survey_candidate_too_large", "stored candidate exceeds the storage bound"
        )
    return raw


def _read_json_regular(path: Path, limit: int) -> dict[str, object]:
    return _candidate_json(_read_regular(path.parent, path.name, limit))


def _candidate_json(raw: bytes) -> dict[str, object]:

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in pairs:
            if key in output:
                raise SurveyLifecycleError(
                    "invalid_candidate", "stored candidate contains duplicate keys"
                )
            output[key] = value
        return output

    try:
        value = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=_reject_constant,
            parse_float=_finite_json_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        if isinstance(error, SurveyLifecycleError):
            raise
        raise SurveyLifecycleError(
            "invalid_candidate", "stored candidate is not valid JSON"
        ) from error
    if not isinstance(value, dict):
        raise SurveyLifecycleError("invalid_candidate", "stored candidate must be an object")
    return value


def _reject_constant(value: str) -> None:
    raise SurveyLifecycleError("invalid_candidate", f"invalid JSON constant {value}")


def _finite_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise SurveyLifecycleError("invalid_candidate", "stored candidate numbers must be finite")
    return result


@dataclass(slots=True)
class _Run:
    intent: IntentV1
    run_id: str
    device_id: int
    connection_epoch: int
    pose_identity: Mapping[str, object]
    source: SurveySource
    started_at: int
    last_scan_at: int | None = None
    scans: list[Observation] = field(default_factory=list)
    encoded_bytes: int = 0


class SurveyAreaLifecycle:
    """Owns one bounded recording run while leaving drive authority with the operator."""

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
        now = self.session.clock()
        identity = self.session.registry.ready_ground_identity(device_id, now)
        source = self._configured_source(device_id, identity.connection_epoch if identity else None)
        if identity is None:
            return self._refused(
                "survey_ground_not_ready", "selected ground node lacks current ready pose evidence"
            )
        if source is None:
            return self._refused(
                "survey_source_not_configured",
                "survey requires one configured lidar source, frames, and mount",
            )
        run = _Run(
            intent,
            (
                f"survey-{intent.intent_id}"
                if len(intent.intent_id) <= MAX_INTENT_IDENTIFIER_CHARS - 7
                else f"survey-{hashlib.sha256(intent.intent_id.encode()).hexdigest()}"
            ),
            device_id,
            identity.connection_epoch,
            identity.to_event(),
            source,
            now,
        )
        with self._lock:
            if self._runs:
                return self._refused(
                    "survey_already_running", "a survey recording is already active"
                )
            self._runs[intent.intent_id] = run
            self.session.on_audit_rollback(lambda: self._runs.pop(intent.intent_id, None))
        return IntentSinkResult(
            status=LifecycleStatus.EXECUTING,
            source="survey_area",
            result={"run_id": run.run_id, "connection_epoch": run.connection_epoch},
        )

    def accepted_observation(self, observation: Observation) -> list[dict[str, object]]:
        with self._lock:
            run = next(iter(self._runs.values()), None)
            if run is None:
                return []
            now = self.session.clock()
            failure = self._admission_failure(run, now)
            if failure is not None:
                return self._fail(run, *failure)
            if observation.submission.payload["kind"] != "range_scan":
                return []
            if (observation.submission.device_id, observation.submission.connection_epoch) != (
                run.device_id,
                run.connection_epoch,
            ):
                return []
            if not self._matches_source(observation, run.source):
                return self._fail(
                    run,
                    "survey_source_changed",
                    "range source, frames, or mount changed during survey",
                )
            encoded = len(observation.encode())
            if (
                len(run.scans) >= MAX_SURVEY_SCANS
                or run.encoded_bytes + encoded > MAX_SURVEY_CANDIDATE_BYTES
            ):
                return self._fail(
                    run,
                    "survey_candidate_too_large",
                    "survey scan evidence exceeded the bounded candidate capacity",
                )
            run.scans.append(observation)
            run.encoded_bytes += encoded
            run.last_scan_at = now
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
            if request.operation == "cancel":
                return self._invalidate(
                    run, "survey_cancelled", "operator cancelled the pilot-assisted survey"
                )
            now = self.session.clock()
            failure = self._completion_failure(run, now)
            if failure is not None:
                return self._fail(run, *failure)
            candidate = SurveyCandidate(
                candidate_id=_candidate_id(
                    self.session.session_id, run.intent.intent_id, run.run_id
                ),
                session=self.session.session_id,
                intent_id=run.intent.intent_id,
                run_id=run.run_id,
                area_id=str(run.intent.args["area_id"]),
                device_id=run.device_id,
                connection_epoch=run.connection_epoch,
                pose_identity=run.pose_identity,
                source=run.source,
                scans=tuple(run.scans),
            )
            try:
                self.candidates.save(candidate)
            except SurveyLifecycleError as error:
                return self._fail(run, error.code, error.detail)
            self._remove(run)
            self.session.on_audit_rollback(lambda: self.candidates.delete(candidate.candidate_id))
            self.session.on_audit_rollback(
                lambda: self._runs.__setitem__(run.intent.intent_id, run)
            )
            return [
                self.session.record_lifecycle(
                    intent_id=run.intent.intent_id,
                    status=LifecycleStatus.COMPLETED,
                    source="survey_area",
                    drone_id=run.device_id,
                    connection_epoch=run.connection_epoch,
                    detail=f"saved immutable candidate {candidate.candidate_id}",
                    result={
                        "candidate_id": candidate.candidate_id,
                        "run_id": run.run_id,
                        "connection_epoch": run.connection_epoch,
                    },
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
            return (
                []
                if run is None
                else self._fail(
                    run, "survey_adapter_disconnected", "ground adapter disconnected during survey"
                )
            )

    def periodic_events(self) -> list[dict[str, object]]:
        with self._lock:
            now = self.session.clock()
            events: list[dict[str, object]] = []
            for run in tuple(self._runs.values()):
                failure = self._admission_failure(run, now)
                if failure is not None:
                    events.extend(self._fail(run, *failure))
            return events

    def _configured_source(self, device_id: int, epoch: int | None) -> SurveySource | None:
        ingress = self.session.observation_ingress
        if ingress is None or epoch is None:
            return None
        bindings = [
            binding
            for binding in ingress.bindings.values()
            if binding.device_id == device_id
            and binding.connection_epoch == epoch
            and binding.node_type == "ground"
            and "range_scan" in binding.allowed_payload_kinds
            and binding.range_mount_id is not None
        ]
        if len(bindings) != 1:
            return None
        binding = bindings[0]
        declarations = [
            declaration
            for declaration in ingress.frames.declarations
            if (
                declaration.session,
                declaration.device_id,
                declaration.connection_epoch,
                declaration.source_id,
            )
            == (binding.session, binding.device_id, binding.connection_epoch, binding.source_id)
        ]
        lidars = [
            declaration.frame_id
            for declaration in declarations
            if declaration.kind == "lidar" and declaration.frame_id in binding.allowed_frames
        ]
        odoms = [
            declaration.frame_id
            for declaration in declarations
            if declaration.kind == "odom" and declaration.frame_id in binding.allowed_frames
        ]
        if len(lidars) != 1 or len(odoms) != 1:
            return None
        return SurveySource(binding.source_id, odoms[0], lidars[0], binding.range_mount_id)

    def _admission_failure(self, run: _Run, now: int) -> tuple[str, str] | None:
        if now - run.started_at > MAX_SURVEY_DURATION_MS:
            return "survey_timeout", "survey recording exceeded the configured duration"
        identity = self.session.registry.ready_ground_identity(run.device_id, now)
        if identity is None or identity.connection_epoch != run.connection_epoch:
            return (
                "survey_ground_not_ready",
                "ground readiness or pose evidence expired during survey",
            )
        if self._configured_source(run.device_id, run.connection_epoch) != run.source:
            return (
                "survey_source_changed",
                "configured range source, frames, or mount changed during survey",
            )
        return None

    def _completion_failure(self, run: _Run, now: int) -> tuple[str, str] | None:
        failure = self._admission_failure(run, now)
        if failure is not None:
            return failure
        if not run.scans:
            return "survey_evidence_missing", "survey requires at least one current range scan"
        if (
            run.last_scan_at is None
            or now - run.last_scan_at > self.session.limits.telemetry_freshness_ms
        ):
            return "survey_source_stale", "configured range source has no current scan evidence"
        return None

    @staticmethod
    def _matches_source(observation: Observation, source: SurveySource) -> bool:
        submission = observation.submission
        payload = submission.payload
        pose = payload["sensor_pose"]
        return (
            submission.source_id == source.source_id
            and submission.frame == source.lidar_frame
            and payload["mount_id"] == source.mount_id
            and pose["parent_frame"] == source.odom_frame
            and pose["child_frame"] == source.lidar_frame
        )

    def _remove(self, run: _Run) -> None:
        self._runs.pop(run.intent.intent_id, None)

    def _fail(self, run: _Run, reason: str, detail: str) -> list[dict[str, object]]:
        self._remove(run)
        self.session.on_audit_rollback(lambda: self._runs.__setitem__(run.intent.intent_id, run))
        return [
            self.session.record_lifecycle(
                intent_id=run.intent.intent_id,
                status=LifecycleStatus.FAILED,
                source="survey_area",
                drone_id=run.device_id,
                connection_epoch=run.connection_epoch,
                reason=reason,
                detail=detail,
            )
        ]

    def _invalidate(self, run: _Run, reason: str, detail: str) -> list[dict[str, object]]:
        self._remove(run)
        self.session.on_audit_rollback(lambda: self._runs.__setitem__(run.intent.intent_id, run))
        return [
            self.session.record_lifecycle(
                intent_id=run.intent.intent_id,
                status=LifecycleStatus.INVALIDATED,
                source="survey_area",
                drone_id=run.device_id,
                connection_epoch=run.connection_epoch,
                reason=reason,
                detail=detail,
            )
        ]

    @staticmethod
    def _refused(reason: str, detail: str) -> IntentSinkResult:
        return IntentSinkResult(
            status=LifecycleStatus.REFUSED, source="survey_area", reason=reason, detail=detail
        )


def _candidate_id(session: str, intent_id: str, run_id: str) -> str:
    identity = "\x00".join((session, intent_id, run_id)).encode()
    return f"candidate-{hashlib.sha256(identity).hexdigest()[:32]}"

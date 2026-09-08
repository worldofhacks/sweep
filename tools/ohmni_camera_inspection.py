"""Authorize one small camera-inspected forward pulse without weakening lidar admission."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MAX_CHALLENGE_TTL_NS = 30_000_000_000
MAX_APPROVAL_TTL_NS = 1_000_000_000
PULSE_SPEED_M_S = 0.04
PULSE_DURATION_S = 0.5
PULSE_DISTANCE_M = 0.02


class InspectionError(ValueError):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hex(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise InspectionError(f"camera inspection {name} is invalid")
    return value


@dataclass(frozen=True)
class ForwardPulse:
    velocity_m_s: float
    yaw_rate_deg_s: float
    duration_s: float

    def __post_init__(self) -> None:
        if (
            self.velocity_m_s != PULSE_SPEED_M_S
            or self.yaw_rate_deg_s != 0.0
            or not 0 < self.duration_s <= PULSE_DURATION_S
            or self.velocity_m_s * self.duration_s > PULSE_DISTANCE_M
        ):
            raise InspectionError("camera inspection pulse exceeds its fixed forward bound")


@dataclass(frozen=True)
class LiveState:
    device_id: int
    boot_id: str
    positioning_source_sha256: str
    x_m: float
    y_m: float
    yaw_deg: float
    pose_quality: float
    head_position: int
    head_target: int
    head_flags: str

    def __post_init__(self) -> None:
        if type(self.device_id) is not int or self.device_id <= 0 or not self.boot_id:
            raise InspectionError("camera inspection device identity is invalid")
        _hex(self.positioning_source_sha256, "positioning source")
        if not all(
            math.isfinite(value) for value in (self.x_m, self.y_m, self.yaw_deg, self.pose_quality)
        ):
            raise InspectionError("camera inspection live pose is invalid")
        if (
            self.pose_quality <= 0
            or type(self.head_position) is not int
            or type(self.head_target) is not int
        ):
            raise InspectionError("camera inspection live state is unqualified")
        if self.head_flags != "NONE" or abs(self.head_position - self.head_target) > 200:
            raise InspectionError("camera inspection head is unqualified")


@dataclass(frozen=True)
class CaptureChallenge:
    challenge_id: str
    nonce: str
    device_id: int
    source_boot_id: str
    positioning_source_sha256: str
    capture_tool_sha256: str
    issued_at_device_monotonic_ns: int
    expires_at_device_monotonic_ns: int
    issued_state: LiveState

    def to_mapping(self) -> dict[str, object]:
        return {
            "v": 1,
            "challenge_id": self.challenge_id,
            "nonce": self.nonce,
            "device_id": self.device_id,
            "source_boot_id": self.source_boot_id,
            "positioning_source_sha256": self.positioning_source_sha256,
            "capture_tool_sha256": self.capture_tool_sha256,
            "issued_at_device_monotonic_ns": self.issued_at_device_monotonic_ns,
            "expires_at_device_monotonic_ns": self.expires_at_device_monotonic_ns,
            "issued_state": self.issued_state.__dict__.copy(),
        }


def parse_challenge(value: object) -> CaptureChallenge:
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "v",
            "challenge_id",
            "nonce",
            "device_id",
            "source_boot_id",
            "positioning_source_sha256",
            "capture_tool_sha256",
            "issued_at_device_monotonic_ns",
            "expires_at_device_monotonic_ns",
            "issued_state",
        }
        or value["v"] != 1
    ):
        raise InspectionError("camera inspection challenge is invalid")
    if (
        not isinstance(value["challenge_id"], str)
        or not isinstance(value["nonce"], str)
        or len(value["nonce"]) < 32
        or type(value["device_id"]) is not int
        or not isinstance(value["source_boot_id"], str)
        or type(value["issued_at_device_monotonic_ns"]) is not int
        or type(value["expires_at_device_monotonic_ns"]) is not int
        or value["issued_at_device_monotonic_ns"] < 0
        or value["expires_at_device_monotonic_ns"] < value["issued_at_device_monotonic_ns"]
    ):
        raise InspectionError("camera inspection challenge is invalid")
    _hex(value["positioning_source_sha256"], "positioning source")
    _hex(value["capture_tool_sha256"], "capture tool")
    if not isinstance(value["issued_state"], Mapping):
        raise InspectionError("camera inspection issued state is invalid")
    issued_state = LiveState(**dict(value["issued_state"]))
    if (
        value["device_id"] != issued_state.device_id
        or value["source_boot_id"] != issued_state.boot_id
        or value["positioning_source_sha256"] != issued_state.positioning_source_sha256
    ):
        raise InspectionError("camera inspection challenge identity is invalid")
    return CaptureChallenge(
        **{
            **{
                key: value[key]
                for key in CaptureChallenge.__dataclass_fields__
                if key != "issued_state"
            },
            "issued_state": issued_state,
        }
    )


@dataclass(frozen=True)
class FrameEvidence:
    challenge: CaptureChallenge
    frame_record_sha256: str
    image_sha256: str
    source_sha256: str
    capture_pipeline_sha256: str
    raw_capture_collection: str
    camera: str
    frame_index: int

    @classmethod
    def load(cls, frame_path: Path, manifest_path: Path) -> FrameEvidence:
        frame_bytes, manifest_bytes = frame_path.read_bytes(), manifest_path.read_bytes()
        frame, manifest = json.loads(frame_bytes), json.loads(manifest_bytes)
        if not isinstance(frame, Mapping) or not isinstance(manifest, Mapping):
            raise InspectionError("camera inspection evidence is invalid")
        challenge = parse_challenge(frame.get("inspection_challenge"))
        if (
            manifest.get("status") != "complete"
            or manifest.get("inspection_challenge") != challenge.to_mapping()
        ):
            raise InspectionError("camera inspection manifest challenge differs")
        for key in ("image_sha256", "source_sha256", "capture_pipeline_sha256"):
            _hex(frame.get(key), key)
        camera = frame.get("camera")
        collection = frame.get("raw_capture_collection")
        index = frame.get("frame_index")
        if not isinstance(camera, str) or not isinstance(collection, str) or type(index) is not int:
            raise InspectionError("camera inspection frame identity is invalid")
        if (
            frame.get("boot_id") != challenge.source_boot_id
            or manifest.get("boot_id") != challenge.source_boot_id
            or manifest.get("device_id") != challenge.device_id
            or manifest.get("raw_capture_collection") != collection
        ):
            raise InspectionError("camera inspection capture identity differs")
        pipeline = manifest.get("capture_pipeline")
        pipeline_sha = hashlib.sha256(
            json.dumps(pipeline, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            pipeline_sha != manifest.get("capture_pipeline_sha256")
            or pipeline_sha != frame["capture_pipeline_sha256"]
        ):
            raise InspectionError("camera inspection pipeline differs")
        image = frame_path.with_name(str(frame.get("image_file", "")))
        raw = frame_path.with_name(str(frame.get("source_file", "")))
        if (
            not image.is_file()
            or not raw.is_file()
            or _digest(image) != frame["image_sha256"]
            or _digest(raw) != frame["source_sha256"]
            or frame.get("source_device_sha256") != frame["source_sha256"]
            or frame.get("source_device_size_bytes") != raw.stat().st_size
        ):
            raise InspectionError("camera inspection frame bytes differ")
        return cls(
            challenge,
            hashlib.sha256(frame_bytes).hexdigest(),
            frame["image_sha256"],
            frame["source_sha256"],
            frame["capture_pipeline_sha256"],
            collection,
            camera,
            index,
        )


class InspectionAuthority:
    def __init__(self) -> None:
        self._challenges: dict[str, CaptureChallenge] = {}
        self._used_challenges: set[str] = set()
        self._approvals: dict[
            str, tuple[ForwardPulse, LiveState, int, int, bool, dict[str, object]]
        ] = {}

    def issue_challenge(
        self,
        state: LiveState,
        now_ns: int,
        capture_tool_sha256: str,
        *,
        ttl_ns: int = MAX_CHALLENGE_TTL_NS,
    ) -> CaptureChallenge:
        if (
            type(now_ns) is not int
            or now_ns < 0
            or type(ttl_ns) is not int
            or not 0 < ttl_ns <= MAX_CHALLENGE_TTL_NS
        ):
            raise InspectionError("camera inspection challenge deadline is invalid")
        challenge = CaptureChallenge(
            secrets.token_hex(16),
            secrets.token_urlsafe(32),
            state.device_id,
            state.boot_id,
            state.positioning_source_sha256,
            _hex(capture_tool_sha256, "capture tool"),
            now_ns,
            now_ns + ttl_ns,
            state,
        )
        self._challenges[challenge.challenge_id] = challenge
        return challenge

    def approve(
        self,
        frame: FrameEvidence,
        pulse: ForwardPulse,
        state: LiveState,
        now_ns: int,
        *,
        operator_id: str,
        accepted: bool,
        review_notes: str,
    ) -> str:
        challenge = self._challenges.get(frame.challenge.challenge_id)
        if (
            challenge != frame.challenge
            or challenge.challenge_id in self._used_challenges
            or type(now_ns) is not int
            or not challenge.issued_at_device_monotonic_ns
            <= now_ns
            <= challenge.expires_at_device_monotonic_ns
        ):
            raise InspectionError("camera inspection challenge expired or unknown")
        if (
            not operator_id
            or accepted is not True
            or not isinstance(review_notes, str)
            or not review_notes.strip()
            or state != challenge.issued_state
        ):
            raise InspectionError("camera inspection live identity differs")
        approval_id = secrets.token_hex(16)
        expires_at = min(challenge.expires_at_device_monotonic_ns, now_ns + MAX_APPROVAL_TTL_NS)
        record = {
            "approval_id": approval_id,
            "operator_id": operator_id,
            "accepted": True,
            "review_notes": review_notes,
            "frame_record_sha256": frame.frame_record_sha256,
            "image_sha256": frame.image_sha256,
            "source_sha256": frame.source_sha256,
            "capture_pipeline_sha256": frame.capture_pipeline_sha256,
            "pulse": pulse.__dict__,
            "state": state.__dict__.copy(),
            "issued_at_device_monotonic_ns": now_ns,
            "expires_at_device_monotonic_ns": expires_at,
        }
        self._approvals[approval_id] = (pulse, state, now_ns, expires_at, False, record)
        self._used_challenges.add(challenge.challenge_id)
        return approval_id

    def approval_record(self, approval_id: str) -> dict[str, object]:
        record = self._approvals.get(approval_id)
        if record is None:
            raise InspectionError("camera inspection approval is unknown")
        return {**record[5], "consumed": record[4]}

    def consume(
        self, approval_id: str, pulse: ForwardPulse, state: LiveState, now_ns: int
    ) -> str | None:
        record = self._approvals.get(approval_id)
        if record is None:
            return "camera_inspection_approval_unknown"
        expected, approved_state, issued_at, expires_at, spent, decision = record
        if spent:
            return "camera_inspection_approval_spent"
        if type(now_ns) is not int or now_ns < issued_at:
            return "camera_inspection_approval_time_invalid"
        if now_ns > expires_at:
            return "camera_inspection_approval_expired"
        if pulse != expected:
            return "camera_inspection_pulse_changed"
        if state != approved_state:
            return "camera_inspection_live_state_changed"
        self._approvals[approval_id] = (
            expected,
            approved_state,
            issued_at,
            expires_at,
            True,
            decision,
        )
        return None

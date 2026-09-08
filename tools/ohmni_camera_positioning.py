"""Lease-bound Unit 12 camera-position evidence capture."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from adapters.ohmni.botshell import BotShell
from adapters.ohmni.calibration import (
    LEASE_MAX_AGE_S,
    MAX_CAPTURE_DRIFT_DEG,
    MAX_CAPTURE_DRIFT_M,
    MAX_OUTPUT_BYTES,
    REVOLUTIONS_PER_STAGE,
    CalibrationConfig,
    CalibrationError,
    CalibrationRunner,
    HostLease,
    LeaseSocketPump,
    _token,
    _yaw_delta,
    calibration_source_sha256,
)
from adapters.ohmni.device import Config, OhmniDevice
from adapters.ohmni.lidar import Lidar, discover
from adapters.ohmni.odometry import Odometry, Pose
from adapters.ohmni.paired_encoder import PairedEncoderStream, default_socket_path

DEVICE_ID = 12
LIDAR_OFFSET_DEG = 131.269876
LIDAR_ANGLE_SIGN = -1
LIDAR_MOUNT = {"x_m": -0.218548, "y_m": 0.155000, "z_m": 0.5334, "yaw_deg": 0.0}
WHEEL_DIAMETER_MM = 152.4
YAW_TARGET_DEG = 20.0
YAW_RATE_DEG_S = 10.0
FORWARD_TARGET_M = 0.2
FORWARD_SPEED_M_S = 0.04
FORWARD_MAX_PULSES = 28
YAW_MODE_MAX_YAW_DEG = 25.0
YAW_MODE_MAX_WHEEL_TRAVEL_M = 0.15
FORWARD_MAX_YAW_DEG = 5.0
FORWARD_MAX_WHEEL_TRAVEL_M = 0.26
PAIR_SAMPLES_PER_STAGE = 16
BASELINE_CAPTURE_TIMEOUT_S = 5.0
RESUME_POSE_TOLERANCE_M = 0.02
RESUME_YAW_TOLERANCE_DEG = 1.0
RESUME_NECK_TOLERANCE = 200
RESUME_REQUEST_MAX_BYTES = 4096
FORWARD_TERMINAL_TOLERANCE_M = FORWARD_SPEED_M_S * 0.1


class PositioningProfile:
    def __init__(
        self,
        device_id: int,
        lidar_offset_deg: float,
        lidar_angle_sign: int,
        lidar_mount: dict[str, float],
        wheel_diameter_mm: float,
        footprint_radius_m: float,
        stopping_distance_m: float,
        clearance_margin_m: float,
    ) -> None:
        self.device_id = device_id
        self.lidar_offset_deg = lidar_offset_deg
        self.lidar_angle_sign = lidar_angle_sign
        self.lidar_mount = lidar_mount
        self.wheel_diameter_mm = wheel_diameter_mm
        self.footprint_radius_m = footprint_radius_m
        self.stopping_distance_m = stopping_distance_m
        self.clearance_margin_m = clearance_margin_m


_POSITIONING_PROFILES = {
    DEVICE_ID: PositioningProfile(
        DEVICE_ID,
        LIDAR_OFFSET_DEG,
        LIDAR_ANGLE_SIGN,
        LIDAR_MOUNT,
        WHEEL_DIAMETER_MM,
        0.3,
        0.1,
        0.1,
    )
}
_PAUSE_REASONS = frozenset(
    {
        "obstacle_within_clearance",
        "lidar_scan_missing",
        "lidar_scan_coverage_missing",
        "lidar_scan_coverage_sparse",
        "lidar_scan_stale",
        "lidar_scan_invalid",
        "lidar_scan_geometry_invalid",
        "lidar_read_error",
        # Kept while installed nodes report the previous guard vocabulary.
        "lidar_stale",
        "lidar_full_circle_coverage_missing",
    }
)


class CameraPosePaused(CalibrationError):
    pass


class _LiveResumeGate:
    def __init__(
        self,
        *,
        boot_id: str,
        device_id: int,
        source_sha256: str,
        deadline: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.boot_id, self.device_id = boot_id, device_id
        self.source_sha256, self.deadline = source_sha256, deadline
        self.monotonic = monotonic
        self.nonce = secrets.token_hex(32)
        self.path = Path(tempfile.gettempdir()) / f"sweep-positioning-{secrets.token_hex(8)}.sock"
        self._accepted = threading.Event()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._artifact_sha256: str | None = None
        self._used = False
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def publish(self, artifact: bytes) -> None:
        self._artifact_sha256 = hashlib.sha256(artifact).hexdigest()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.settimeout(0.1)
        listener.bind(str(self.path))
        os.chmod(self.path, 0o600)
        listener.listen(4)
        self._listener = listener
        self._thread = threading.Thread(
            target=self._serve, name="camera-position-resume", daemon=True
        )
        self._thread.start()

    def wait(
        self,
        require_lease: Callable[[], None],
        observe_pause: Callable[[], None],
        sleep: Callable[[float], None],
    ) -> None:
        while not self._accepted.is_set():
            require_lease()
            observe_pause()
            if self.monotonic() >= self.deadline:
                raise CalibrationError("camera_pose_resume_request_expired")
            sleep(0.01)

    def close(self) -> None:
        self._closed.set()
        if self._listener is not None:
            self._listener.close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.2)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._closed.is_set():
            try:
                connection, _ = self._listener.accept()
            except (OSError, TimeoutError):
                continue
            with connection:
                try:
                    connection.settimeout(0.35)
                    payload = connection.recv(RESUME_REQUEST_MAX_BYTES + 1)
                    response = self._accept(payload)
                    connection.sendall((response + "\n").encode())
                except OSError:
                    continue

    def _accept(self, payload: bytes) -> str:
        if self.monotonic() >= self.deadline:
            return "camera_pose_resume_request_expired"
        if len(payload) > RESUME_REQUEST_MAX_BYTES:
            return "camera_pose_resume_request_invalid"
        try:
            request = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return "camera_pose_resume_request_invalid"
        expected = {
            "schema_version",
            "kind",
            "pause_evidence_sha256",
            "pause_nonce",
            "boot_id",
            "device_id",
            "tool_bundle_sha256",
        }
        if (
            not isinstance(request, dict)
            or set(request) != expected
            or request.get("schema_version") != 1
            or request.get("kind") != "camera_pose_live_resume_request"
            or request.get("pause_evidence_sha256") != self._artifact_sha256
            or request.get("pause_nonce") != self.nonce
            or request.get("boot_id") != self.boot_id
            or request.get("device_id") != self.device_id
            or request.get("tool_bundle_sha256") != self.source_sha256
        ):
            return "camera_pose_resume_request_invalid"
        with self._lock:
            if self._used:
                return "camera_pose_resume_request_used"
            self._used = True
            self._accepted.set()
        return "camera_pose_resume_request_accepted"


def _profile(device_id: int) -> PositioningProfile:
    try:
        return _POSITIONING_PROFILES[device_id]
    except KeyError as error:
        raise CalibrationError("camera_pose_device_profile_unavailable") from error


class PositioningConfig:
    pulse_duration_s = 0.5
    max_runtime_s = 60.0
    forward_speed_m_s = FORWARD_SPEED_M_S
    yaw_rate_deg_s = YAW_RATE_DEG_S
    max_wheel_travel_m = 0.6
    max_yaw_degrees = 40.0


def _neck_status() -> dict[str, int | str]:
    shell = BotShell()
    try:
        reply = shell.command(
            "neck_status",
            expected=r"neck_status = pos -?\d+ targ -?\d+ flags (?:NONE|OVERLOAD)",
            timeout=0.5,
        )
    finally:
        shell.close()
    match = re.search(r"neck_status = pos (-?\d+) targ (-?\d+) flags (NONE|OVERLOAD)", reply)
    if match is None:
        raise CalibrationError("camera_pose_neck_status_unavailable")
    return {"position": int(match[1]), "target": int(match[2]), "flags": match[3]}


def _raw_revolution(revolution: object) -> dict[str, object]:
    points = revolution.points
    return {
        "monotonic_s": revolution.monotonic_s,
        "points": [
            {
                "angle_deg": point.angle_deg,
                "distance_mm": point.distance_mm,
                "quality": point.quality,
            }
            for point in points
        ],
    }


def _pose(pose: Pose) -> dict[str, float]:
    return {
        "x_m": pose.x,
        "y_m": pose.y,
        "yaw_deg": pose.yaw_deg,
        "quality": pose.quality,
    }


class ReadOnlyBaselineCapture:
    def __init__(
        self,
        lease: HostLease,
        output: Path,
        *,
        profile: PositioningProfile,
        boot_id: str,
        source_sha256: str,
    ) -> None:
        self.lease, self.output = lease, output
        self.profile = profile
        self.boot_id, self.source_sha256 = boot_id, source_sha256
        self.stream = PairedEncoderStream(default_socket_path())
        self.odometry = Odometry(
            self.stream, (0.0, 0.0, 0.0), wheel_diameter_mm=profile.wheel_diameter_mm
        )
        port = discover()
        if port is None:
            raise CalibrationError("camera_pose_lidar_missing")
        self.lidar = Lidar(
            BotShell(),
            port,
            self.odometry.snapshot,
            offset_deg=profile.lidar_offset_deg,
            angle_sign=profile.lidar_angle_sign,
        )
        self.started = 0.0

    def run(self) -> Path:
        self.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.started = time.monotonic()
        stages: dict[str, object] = {}
        neck_before: dict[str, int | str] | None = None
        neck_after: dict[str, int | str] | None = None
        try:
            self._require_lease()
            neck_before = _neck_status()
            self.odometry.start()
            self.lidar.start()
            stages["baseline"] = self._capture_stage()
            neck_after = _neck_status()
            self._write(descriptor, stages, neck_before, neck_after)
        except BaseException as error:
            self._remove_owned_output(descriptor)
            self._write_failure(stages, neck_before, neck_after, error)
            raise
        finally:
            os.close(descriptor)
            self.close()
        return self.output

    def close(self) -> None:
        self.odometry.close()
        self.lidar.close()

    def _require_lease(self) -> None:
        if time.monotonic() - self.started >= CalibrationConfig.longer().max_runtime_s:
            raise CalibrationError("camera_pose_runtime_expired")
        reason = self.lease.reason(time.monotonic())
        if reason is not None:
            raise CalibrationError(reason)

    def _capture_stage(self) -> dict[str, object]:
        stage_started = time.monotonic()
        deadline = stage_started + BASELINE_CAPTURE_TIMEOUT_S
        origin: Pose | None = None
        pairs: list[dict[str, object]] = []
        revolutions: list[dict[str, object]] = []
        pair_receipts: set[int] = set()
        revolution_timestamps: set[float] = set()
        max_translation_drift_m = 0.0
        max_yaw_drift_deg = 0.0
        max_encoder_revolution_delta_s = 0.0
        while len(pairs) < PAIR_SAMPLES_PER_STAGE or len(revolutions) < REVOLUTIONS_PER_STAGE:
            self._require_lease()
            now = time.monotonic()
            pose, pair = self.odometry.snapshot_with_sample(now)
            if not pose.quality or pair is None:
                if now >= deadline:
                    raise CalibrationError("wheel_odometry_unavailable")
                time.sleep(0.01)
                continue
            if origin is None:
                origin = pose
            translation_drift_m = math.hypot(pose.x - origin.x, pose.y - origin.y)
            yaw_drift_deg = abs(_yaw_delta(pose.yaw_deg, origin.yaw_deg))
            max_translation_drift_m = max(max_translation_drift_m, translation_drift_m)
            max_yaw_drift_deg = max(max_yaw_drift_deg, yaw_drift_deg)
            if translation_drift_m > MAX_CAPTURE_DRIFT_M or yaw_drift_deg > MAX_CAPTURE_DRIFT_DEG:
                raise CalibrationError("camera_pose_read_only_capture_drift")
            if pair.right_receipt_ns not in pair_receipts:
                pair_receipts.add(pair.right_receipt_ns)
                pairs.append({"pose": _pose(pose), "encoder": asdict(pair)})
            revolution = self.lidar.raw_revolution(now)
            if (
                revolution is not None
                and revolution.monotonic_s >= stage_started
                and revolution.monotonic_s not in revolution_timestamps
            ):
                if now - revolution.monotonic_s > LEASE_MAX_AGE_S:
                    raise CalibrationError("raw_lidar_revolution_stale")
                revolution_timestamps.add(revolution.monotonic_s)
                revolutions.append(_raw_revolution(revolution))
                max_encoder_revolution_delta_s = max(
                    max_encoder_revolution_delta_s,
                    abs(revolution.monotonic_s - pair.right_receipt_ns / 1_000_000_000),
                )
            if len(pairs) >= PAIR_SAMPLES_PER_STAGE and len(revolutions) >= REVOLUTIONS_PER_STAGE:
                break
            if now >= deadline:
                raise CalibrationError("camera_pose_read_only_capture_timeout")
            time.sleep(0.01)
        assert origin is not None
        return {
            "pose": _pose(origin),
            "paired_pose_samples": pairs,
            "revolutions": revolutions,
            "stage_started_monotonic_s": stage_started,
            "stage_completed_monotonic_s": time.monotonic(),
            "max_translation_drift_m": max_translation_drift_m,
            "max_yaw_drift_deg": max_yaw_drift_deg,
            "max_encoder_revolution_delta_s": max_encoder_revolution_delta_s,
            "monotonic_clock": "linux_monotonic",
        }

    def _body(
        self, stages: dict[str, object], neck_before: object, neck_after: object
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "camera_calibration_poses",
            "device_id": self.profile.device_id,
            "boot_id": self.boot_id,
            "executed_bundle_source_sha256": self.source_sha256,
            "mode": "baseline",
            "purpose": "camera_pose_correspondence_capture",
            "motion": None,
            "neck_before": neck_before,
            "neck_after": neck_after,
            "lidar_settings": _lidar_settings(self.profile),
            "limits": _limits(),
            "lease_diagnostics": {"current_reason": self.lease.reason(time.monotonic())},
            "stages": stages,
        }

    def _write(
        self,
        descriptor: int,
        stages: dict[str, object],
        neck_before: object,
        neck_after: object,
    ) -> None:
        body = self._body(stages, neck_before, neck_after)
        _write_owned(self.output, descriptor, body)

    def _write_failure(
        self,
        stages: dict[str, object],
        neck_before: object,
        neck_after: object,
        error: BaseException,
    ) -> None:
        body = self._body(stages, neck_before, neck_after)
        body.update(
            {
                "kind": "camera_calibration_poses_failed_attempt",
                "failure": str(error),
                "completed_stages": stages,
            }
        )
        _write_new(self.output.with_name(self.output.name + ".failed.json"), body)

    def _remove_owned_output(self, descriptor: int) -> None:
        try:
            current = self.output.lstat()
        except FileNotFoundError:
            return
        owned = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
            self.output.unlink()


class CameraPoseCaptureRunner(CalibrationRunner):
    def __init__(
        self,
        *args: object,
        mode: str,
        device_id: int = DEVICE_ID,
        resume: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        if mode not in {"yaw", "forward"}:
            raise ValueError("camera pose mode must be yaw or forward")
        _profile(device_id)
        if resume is not None:
            raise ValueError("resume requests are handled by the live camera capture owner")
        super().__init__(*args, **kwargs)
        self.config = PositioningConfig()
        self.mode = mode
        self.device_id = device_id
        self.neck_before: dict[str, int | str] | None = None
        self.neck_after_motion: dict[str, int | str] | None = None
        self.neck_after_cleanup: dict[str, int | str] | None = None
        self.forward_pulses_completed = 0
        self.forward_distance_m = 0.0
        self._forward_origin: Pose | None = None
        self._resume_decision: dict[str, object] | None = None
        self._pause_guard_evidence: dict[str, object] | None = None
        self._live_pause: dict[str, object] | None = None
        self._resume_gate: _LiveResumeGate | None = None
        self._deadline = 0.0
        self.pre_pulse_guard = None

    def _check_private_limits(self, *, reserve_duration_s: float = 0.0) -> None:
        wheel_travel_m, yaw_degrees = self._progress.values()
        if reserve_duration_s:
            if self.mode == "forward":
                wheel_travel_m += FORWARD_SPEED_M_S * reserve_duration_s
            else:
                yaw_degrees += YAW_RATE_DEG_S * reserve_duration_s
        max_wheel_travel_m = (
            FORWARD_MAX_WHEEL_TRAVEL_M if self.mode == "forward" else YAW_MODE_MAX_WHEEL_TRAVEL_M
        )
        max_yaw_degrees = FORWARD_MAX_YAW_DEG if self.mode == "forward" else YAW_MODE_MAX_YAW_DEG
        if wheel_travel_m > max_wheel_travel_m:
            self.device.stop()
            raise CalibrationError("camera_pose_wheel_travel_limit")
        if yaw_degrees > max_yaw_degrees:
            self.device.stop()
            raise CalibrationError("camera_pose_yaw_limit")

    def _snapshot(self, now: float | None = None):  # type: ignore[no-untyped-def]
        pose, pair = super()._snapshot(now)
        self._check_private_limits()
        return pose, pair

    def _device_guard(self, now: float) -> str | None:
        reason = super()._device_guard(now)
        if reason is not None:
            return reason
        try:
            reserve_duration_s = (
                self.config.pulse_duration_s
                if getattr(self.device, "motion", None) is None
                else 0.1
            )
            self._check_private_limits(reserve_duration_s=reserve_duration_s)
        except CalibrationError as error:
            return str(error)
        return None

    def _forward_device_guard(self, now: float) -> str | None:
        reason = self._device_guard(now)
        if reason is None:
            reason = self.device.guard_reason(forward=True, now=now)
        if self._pause_reason(reason):
            self._pause_guard_evidence = self._lidar_guard_evidence(reason, now)
        return reason

    def _lidar_guard_evidence(self, reason: str, now: float) -> dict[str, object]:
        probe = getattr(self.device, "lidar_guard_evidence", None)
        evidence = probe(now=now) if callable(probe) else None
        if evidence is None:
            evidence = getattr(self.device, "last_lidar_guard_fault", None)
        report = getattr(evidence, "operator_report", None)
        return {
            "reason": reason,
            "checked_at_monotonic_s": now,
            "lidar": report() if callable(report) else None,
        }

    @staticmethod
    def _pause_reason(reason: str | None) -> bool:
        return reason in _PAUSE_REASONS

    def _forward_displacement(self, pose: Pose) -> float:
        if getattr(self, "_forward_origin", None) is None:
            raise CalibrationError("camera_pose_forward_origin_unavailable")
        heading_rad = math.radians(self._forward_origin.yaw_deg)
        return (pose.x - self._forward_origin.x) * math.cos(heading_rad) + (
            pose.y - self._forward_origin.y
        ) * math.sin(heading_rad)

    def _refresh_forward_progress(self) -> tuple[Pose, object]:
        pose, pair = super()._snapshot()
        self.forward_distance_m = self._forward_displacement(pose)
        return pose, pair

    @staticmethod
    def _neck_is_qualified(neck: object) -> bool:
        return (
            isinstance(neck, dict)
            and set(neck) == {"position", "target", "flags"}
            and type(neck["position"]) is int
            and type(neck["target"]) is int
            and neck["flags"] == "NONE"
            and abs(neck["position"] - neck["target"]) <= RESUME_NECK_TOLERANCE
        )

    def _admit_live_resume(self) -> None:
        state = self._live_pause
        gate = self._resume_gate
        if state is None or gate is None:
            raise CalibrationError("camera_pose_resume_owner_unavailable")
        self._require_lease()
        current_neck = _neck_status()
        if not self._neck_is_qualified(current_neck):
            raise CalibrationError("camera_pose_resume_head_unqualified")
        paused_neck = state["neck"]
        assert isinstance(paused_neck, dict)
        if abs(current_neck["position"] - paused_neck["position"]) > RESUME_NECK_TOLERANCE:
            raise CalibrationError("camera_pose_resume_head_changed")
        current, _ = self._refresh_forward_progress()
        paused = state["pose"]
        assert isinstance(paused, Pose)
        if (
            math.hypot(current.x - paused.x, current.y - paused.y) > RESUME_POSE_TOLERANCE_M
            or abs(_yaw_delta(current.yaw_deg, paused.yaw_deg)) > RESUME_YAW_TOLERANCE_DEG
        ):
            raise CalibrationError("camera_pose_resume_pose_changed")
        measured = float(state["distance_m"])
        if not math.isclose(self.forward_distance_m, measured, abs_tol=RESUME_POSE_TOLERANCE_M):
            raise CalibrationError("camera_pose_resume_progress_changed")
        clearance_reason = self._forward_device_guard(self.monotonic())
        if clearance_reason is not None:
            raise CalibrationError(f"camera_pose_resume_{clearance_reason}")
        issued_at = self.monotonic()
        self._resume_decision = {
            "pause_evidence_sha256": state["artifact_sha256"],
            "pause_reason": state["reason"],
            "issued_at_monotonic_s": issued_at,
            "expires_at_monotonic_s": self._deadline,
            "remaining_distance_m": FORWARD_TARGET_M - self.forward_distance_m,
            "pose": _pose(current),
            "neck": current_neck,
            "clearance": "forward_guard_passed",
            "clearance_evidence": self._lidar_guard_evidence(
                "forward_guard_passed", self.monotonic()
            ),
        }

    def _observe_live_pause(self) -> None:
        state = self._live_pause
        if state is None:
            raise CalibrationError("camera_pose_resume_owner_unavailable")
        current_neck = _neck_status()
        if not self._neck_is_qualified(current_neck):
            raise CalibrationError("camera_pose_resume_head_unqualified")
        paused_neck = state["neck"]
        assert isinstance(paused_neck, dict)
        if abs(current_neck["position"] - paused_neck["position"]) > RESUME_NECK_TOLERANCE:
            raise CalibrationError("camera_pose_resume_head_changed")
        current, _ = self._refresh_forward_progress()
        paused = state["pose"]
        assert isinstance(paused, Pose)
        if (
            math.hypot(current.x - paused.x, current.y - paused.y) > RESUME_POSE_TOLERANCE_M
            or abs(_yaw_delta(current.yaw_deg, paused.yaw_deg)) > RESUME_YAW_TOLERANCE_DEG
        ):
            raise CalibrationError("camera_pose_resume_pose_changed")

    def _pulse_until(self, velocity_m_s: float, yaw_rate_deg_s: float, target: float) -> None:
        try:
            super()._pulse_until(velocity_m_s, yaw_rate_deg_s, target)
        except CalibrationError as error:
            if str(error) == "calibration_pulse_failed" and self.device.last_refusal:
                raise CalibrationError(self.device.last_refusal) from error
            raise

    def _forward_until_target(self) -> None:
        if getattr(self, "_forward_origin", None) is None:
            self._forward_origin, _ = self._snapshot()

        while True:
            self._require_lease()
            pose_before, _ = self._snapshot()
            self.forward_distance_m = self._forward_displacement(pose_before)
            if self.forward_distance_m >= FORWARD_TARGET_M:
                return
            if self.forward_pulses_completed >= FORWARD_MAX_PULSES:
                self.device.stop()
                raise CalibrationError("camera_pose_forward_target_unreached")
            remaining_distance_m = FORWARD_TARGET_M - self.forward_distance_m
            if remaining_distance_m <= FORWARD_TERMINAL_TOLERANCE_M:
                return
            pulse_duration_s = min(
                self.config.pulse_duration_s, remaining_distance_m / FORWARD_SPEED_M_S
            )
            neck_before = getattr(self, "neck_before", None)
            neck = _neck_status() if neck_before is not None else None
            if neck is not None and not self._neck_is_qualified(neck):
                raise CalibrationError("camera_pose_head_unqualified")
            if getattr(self, "_live_pause", None) is not None:
                paused_neck = self._live_pause["neck"]
                assert isinstance(paused_neck, dict)
                assert neck is not None
                if abs(neck["position"] - paused_neck["position"]) > RESUME_NECK_TOLERANCE:
                    raise CalibrationError("camera_pose_resume_head_changed")
            guard = self._forward_device_guard(self.monotonic())
            if guard is not None:
                if self._pause_reason(guard):
                    raise CameraPosePaused(guard)
                raise CalibrationError(guard)
            if callable(getattr(self, "pre_pulse_guard", None)):
                assert neck is not None
                inspection_reason = self.pre_pulse_guard(
                    pose_before, neck, self.monotonic(), pulse_duration_s
                )
                if inspection_reason is not None:
                    raise CalibrationError(inspection_reason)
            try:
                motion_id = self.device.calibration_drive_velocity(
                    FORWARD_SPEED_M_S,
                    0.0,
                    pulse_duration_s,
                    host_lease=self._forward_device_guard,
                )
            except RuntimeError as error:
                self._refresh_forward_progress()
                reason = self.device.last_refusal or str(error)
                if self._pause_reason(reason):
                    raise CameraPosePaused(reason) from error
                raise
            deadline = self.monotonic() + pulse_duration_s + LEASE_MAX_AGE_S
            while True:
                completed = self.device.motion_done(motion_id)
                if completed is not False:
                    break
                self._require_lease()
                self._snapshot()
                if self.monotonic() >= deadline:
                    self.device.stop()
                    raise CalibrationError("camera_pose_pulse_timeout")
                self.sleep(0.01)
            if completed is not True:
                self.device.stop()
                self._refresh_forward_progress()
                reason = self.device.last_refusal or "camera_pose_pulse_failed"
                if self._pause_reason(reason):
                    raise CameraPosePaused(reason)
                raise CalibrationError(reason)
            pose_after, _ = self._snapshot()
            pulse_displacement_m = self._forward_displacement(
                pose_after
            ) - self._forward_displacement(pose_before)
            if pulse_displacement_m < -0.001:
                self.device.stop()
                raise CalibrationError("camera_pose_reverse_motion")
            if pulse_displacement_m < 0.001:
                self.device.stop()
                raise CalibrationError("camera_pose_no_motion")
            self.forward_pulses_completed += 1
            self.forward_distance_m = self._forward_displacement(pose_after)

    def run(self) -> Path:
        self.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._started = self.monotonic()
        self._deadline = self._started + self.config.max_runtime_s
        stages: dict[str, object] = {}
        try:
            self._require_lease()
            self.neck_before = _neck_status()
            if not self._neck_is_qualified(self.neck_before):
                raise CalibrationError("camera_pose_head_unqualified")
            if not self.device.enable():
                raise CalibrationError(
                    self.device.last_refusal or "camera_pose_device_enable_refused"
                )
            self._initialize()
            if self.mode == "forward":
                stages["before_motion"] = self._capture_stage()
                while True:
                    try:
                        self._forward_until_target()
                        break
                    except CameraPosePaused as error:
                        self._remove_owned_output(descriptor)
                        os.close(descriptor)
                        descriptor = -1
                        self.device.disable()
                        self.neck_after_cleanup = _neck_status()
                        self._pause_for_live_resume(stages, str(error))
                        assert self._resume_gate is not None
                        self._resume_gate.wait(
                            self._require_lease, self._observe_live_pause, self.sleep
                        )
                        self._admit_live_resume()
                        if not self.device.enable():
                            raise CalibrationError(
                                self.device.last_refusal or "camera_pose_device_enable_refused"
                            ) from error
                        descriptor = os.open(
                            self.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                        )
                self._settle()
                stages["after_forward"] = self._capture_stage()
            else:
                self._pulse_until(0.0, YAW_RATE_DEG_S, YAW_TARGET_DEG)
                self._settle()
                stages["after_yaw"] = self._capture_stage()
            self.neck_after_motion = _neck_status()
            self.device.stop()
            self.device.disable()
            self.neck_after_cleanup = _neck_status()
            self._write(stages, descriptor)
        except BaseException as error:
            if descriptor >= 0:
                self._remove_owned_output(descriptor)
            try:
                self.device.disable()
            finally:
                try:
                    self.neck_after_cleanup = _neck_status()
                finally:
                    self._write_failure(stages, error)
            raise
        finally:
            try:
                self.device.stop()
                self.device.disable()
            except BaseException:
                if descriptor >= 0:
                    self._remove_owned_output(descriptor)
                raise
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if getattr(self, "_resume_gate", None) is not None:
                    self._resume_gate.close()
        return self.output

    def _write_failure(self, stages: dict[str, object], error: BaseException) -> None:
        wheel_travel_m, yaw_travel_deg = self._progress.values()
        body = self._body(stages)
        body.update(
            {
                "kind": "camera_calibration_poses_failed_attempt",
                "failure": str(error),
                "device_refusal": self.device.last_refusal,
                "elapsed_s": self.monotonic() - self._started,
                "wheel_travel_m": wheel_travel_m,
                "yaw_travel_deg": yaw_travel_deg,
                "forward_pulses_completed": self.forward_pulses_completed,
                "completed_stages": stages,
            }
        )
        _write_new(self.output.with_name(self.output.name + ".failed.json"), body)

    def _pause_for_live_resume(self, stages: dict[str, object], reason: str) -> None:
        if self._resume_gate is not None:
            self._resume_gate.close()
        if self._forward_origin is None:
            raise CalibrationError("camera_pose_pause_progress_unavailable")
        try:
            paused_pose, paused_pair = self._refresh_forward_progress()
            neck_at_pause = _neck_status()
        except (CalibrationError, OSError) as error:
            raise CalibrationError("camera_pose_pause_evidence_unavailable") from error
        if not self._neck_is_qualified(neck_at_pause):
            raise CalibrationError("camera_pose_pause_head_unqualified")
        if paused_pair is None:
            raise CalibrationError("camera_pose_pause_encoder_unavailable")
        if not 0 <= self.forward_distance_m < FORWARD_TARGET_M:
            raise CalibrationError("camera_pose_pause_progress_invalid")
        gate = _LiveResumeGate(
            boot_id=self.boot_id,
            device_id=self.device_id,
            source_sha256=self.executed_bundle_source_sha256,
            deadline=self._deadline,
            monotonic=self.monotonic,
        )
        body = self._body(stages)
        body.update(
            {
                "kind": "camera_calibration_poses_paused",
                "pause_reason": reason,
                "pause_guard_evidence": self._pause_guard_evidence,
                "completed_stages": stages,
                "forward_origin": _pose(self._forward_origin),
                "paused_pose": _pose(paused_pose),
                "neck_at_pause": neck_at_pause,
                "paused_encoder": asdict(paused_pair),
                "live_resume": {
                    "socket": str(gate.path),
                    "pause_nonce": gate.nonce,
                    "expires_at_monotonic_s": self._deadline,
                    "boot_id": self.boot_id,
                    "device_id": self.device_id,
                    "tool_bundle_sha256": self.executed_bundle_source_sha256,
                },
                "motion": {
                    "shape": "forward_only",
                    "target_distance_m": FORWARD_TARGET_M,
                    "terminal_tolerance_m": FORWARD_TERMINAL_TOLERANCE_M,
                    "measured_distance_m": self.forward_distance_m,
                    "remaining_distance_m": FORWARD_TARGET_M - self.forward_distance_m,
                    "pulse_duration_s": self.config.pulse_duration_s,
                    "maximum_pulses": FORWARD_MAX_PULSES,
                    "pulses_completed": self.forward_pulses_completed,
                },
            }
        )
        artifact = _encode(body)
        gate.publish(artifact)
        try:
            written = _write_new(self.output.with_name(self.output.name + ".paused.json"), body)
        except BaseException:
            gate.close()
            raise
        if written != artifact:
            gate.close()
            raise CalibrationError("camera_pose_pause_artifact_changed")
        self._resume_gate = gate
        self._live_pause = {
            "reason": reason,
            "pose": paused_pose,
            "neck": neck_at_pause,
            "distance_m": self.forward_distance_m,
            "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
            "stages": stages,
        }

    def _write(self, stages: dict[str, object], descriptor: int) -> None:
        _write_owned(self.output, descriptor, self._body(stages))

    def _body(self, stages: dict[str, object]) -> dict[str, object]:
        motion = (
            {
                "shape": "forward_only",
                "velocity_m_s": FORWARD_SPEED_M_S,
                "target_distance_m": FORWARD_TARGET_M,
                "terminal_tolerance_m": FORWARD_TERMINAL_TOLERANCE_M,
                "measured_distance_m": self.forward_distance_m,
                "pulse_duration_s": self.config.pulse_duration_s,
                "maximum_pulses": FORWARD_MAX_PULSES,
                "pulses_completed": self.forward_pulses_completed,
            }
            if self.mode == "forward"
            else {
                "shape": "yaw_only",
                "direction": "left",
                "velocity_m_s": 0.0,
                "yaw_rate_deg_s": YAW_RATE_DEG_S,
                "target_yaw_deg": YAW_TARGET_DEG,
                "pulse_duration_s": self.config.pulse_duration_s,
            }
        )
        return {
            "schema_version": 1,
            "kind": "camera_calibration_poses",
            "device_id": getattr(self, "device_id", DEVICE_ID),
            "boot_id": self.boot_id,
            "executed_bundle_source_sha256": self.executed_bundle_source_sha256,
            "mode": self.mode,
            "purpose": "camera_position_correspondence_capture",
            "neck_before": self.neck_before,
            "neck_after_motion": self.neck_after_motion,
            "neck_after_cleanup": self.neck_after_cleanup,
            "lidar_settings": _lidar_settings(_profile(getattr(self, "device_id", DEVICE_ID))),
            "motion": motion,
            "limits": _limits(),
            "lease_diagnostics": {"current_reason": self.lease.reason(self.monotonic())},
            "resume_decision": getattr(self, "_resume_decision", None),
            "stages": stages,
        }


def _lidar_settings(profile: PositioningProfile) -> dict[str, object]:
    return {
        "offset_deg": profile.lidar_offset_deg,
        "angle_sign": profile.lidar_angle_sign,
        "wheel_diameter_mm": profile.wheel_diameter_mm,
        "runtime_mount": profile.lidar_mount,
        "runtime_mount_used_by_direct_capture": False,
    }


def _limits() -> dict[str, object]:
    return {
        "inherited_profile": PositioningConfig().__dict__,
        "predeclared_modes": {
            "baseline": {"motion": None},
            "yaw": {
                "target_yaw_deg": YAW_TARGET_DEG,
                "yaw_rate_deg_s": YAW_RATE_DEG_S,
                "pulse_duration_s": 0.5,
                "max_wheel_travel_m": YAW_MODE_MAX_WHEEL_TRAVEL_M,
                "max_yaw_degrees": YAW_MODE_MAX_YAW_DEG,
            },
            "forward": {
                "target_distance_m": FORWARD_TARGET_M,
                "velocity_m_s": FORWARD_SPEED_M_S,
                "pulse_duration_s": 0.5,
                "maximum_pulses": FORWARD_MAX_PULSES,
                "max_wheel_travel_m": FORWARD_MAX_WHEEL_TRAVEL_M,
                "max_yaw_degrees": FORWARD_MAX_YAW_DEG,
            },
        },
    }


def _encode(body: dict[str, object]) -> bytes:
    encoded = (json.dumps(body, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise CalibrationError("camera_pose_capture_exceeds_byte_limit")
    return encoded


def _write_owned(path: Path, descriptor: int, body: dict[str, object]) -> None:
    with os.fdopen(os.dup(descriptor), "wb") as stream:
        stream.write(_encode(body))
        stream.flush()
        os.fsync(stream.fileno())
    current, owned = path.lstat(), os.fstat(descriptor)
    if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
        raise CalibrationError("camera_pose_output_replaced")


def _write_new(path: Path, body: dict[str, object]) -> bytes:
    encoded = _encode(body)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return encoded


def _read_pause(path: Path) -> dict[str, object]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        encoded = os.read(descriptor, MAX_OUTPUT_BYTES + 1)
    finally:
        os.close(descriptor)
    if not encoded or len(encoded) > MAX_OUTPUT_BYTES:
        raise CalibrationError("camera_pose_resume_record_invalid")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("duplicate pause field")
            result[name] = value
        return result

    try:
        value = json.loads(encoded, object_pairs_hook=unique, parse_constant=lambda _: None)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise CalibrationError("camera_pose_resume_record_invalid") from error
    if not isinstance(value, dict):
        raise CalibrationError("camera_pose_resume_record_invalid")
    value["pause_evidence_sha256"] = hashlib.sha256(encoded).hexdigest()
    return value


def camera_positioning_source_sha256() -> str:
    tool = Path(__file__).resolve()
    digest = hashlib.sha256()
    digest.update(b"adapters\0")
    digest.update(calibration_source_sha256().encode())
    digest.update(b"tools/ohmni_camera_positioning.py\0")
    digest.update(hashlib.sha256(tool.read_bytes()).digest())
    return digest.hexdigest()


def _submit_live_resume(
    record: dict[str, object], *, boot_id: str, device_id: int, source_sha256: str
) -> None:
    live = record.get("live_resume")
    expected = {
        "socket",
        "pause_nonce",
        "expires_at_monotonic_s",
        "boot_id",
        "device_id",
        "tool_bundle_sha256",
    }
    if (
        not isinstance(live, dict)
        or set(live) != expected
        or not isinstance(record.get("pause_evidence_sha256"), str)
        or not isinstance(live["socket"], str)
        or not isinstance(live["pause_nonce"], str)
        or type(live["expires_at_monotonic_s"]) not in (int, float)
        or live["boot_id"] != boot_id
        or live["device_id"] != device_id
        or live["tool_bundle_sha256"] != source_sha256
    ):
        raise CalibrationError("camera_pose_resume_record_invalid")
    request = {
        "schema_version": 1,
        "kind": "camera_pose_live_resume_request",
        "pause_evidence_sha256": record["pause_evidence_sha256"],
        "pause_nonce": live["pause_nonce"],
        "boot_id": boot_id,
        "device_id": device_id,
        "tool_bundle_sha256": source_sha256,
    }
    encoded = json.dumps(request, separators=(",", ":"), allow_nan=False).encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(1.0)
            connection.connect(live["socket"])
            connection.sendall(encoded)
            response = connection.recv(128).decode().strip()
    except OSError as error:
        raise CalibrationError("camera_pose_resume_owner_unavailable") from error
    if response != "camera_pose_resume_request_accepted":
        raise CalibrationError(response or "camera_pose_resume_request_invalid")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lease-port", required=True, type=int)
    parser.add_argument("--lease-token-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("baseline", "yaw", "forward"))
    parser.add_argument("--device-id", required=True, type=int)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--expected-boot-id", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    args = parser.parse_args(argv)
    profile = _profile(args.device_id)
    if args.resume_from is not None and args.mode != "forward":
        parser.error("--resume-from requires --mode forward")
    resume = _read_pause(args.resume_from) if args.resume_from is not None else None
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    source_sha256 = camera_positioning_source_sha256()
    if boot_id != args.expected_boot_id or source_sha256 != args.expected_source_sha256:
        raise CalibrationError("camera_pose_provenance_mismatch")
    if resume is not None:
        _submit_live_resume(
            resume,
            boot_id=boot_id,
            device_id=profile.device_id,
            source_sha256=source_sha256,
        )
        return 0
    token = _token(args.lease_token_file)
    resource: OhmniDevice | ReadOnlyBaselineCapture | None = None
    lease = HostLease(token)
    pump: LeaseSocketPump | None = None
    prior_signals = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}

    def stop(_signal: int, _frame: object) -> None:
        if pump is not None:
            pump.close()
        if resource is not None:
            if isinstance(resource, OhmniDevice):
                try:
                    resource.stop()
                finally:
                    resource.disable()
            else:
                resource.close()
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        if args.mode == "baseline":
            resource = ReadOnlyBaselineCapture(
                lease,
                args.output,
                profile=profile,
                boot_id=boot_id,
                source_sha256=source_sha256,
            )
            pump = LeaseSocketPump("127.0.0.1", args.lease_port, token, lease, lambda: None)
        else:
            resource = OhmniDevice(
                Config(
                    spotter_present=True,
                    lidar_offset_deg=profile.lidar_offset_deg,
                    lidar_angle_sign=profile.lidar_angle_sign,
                    wheel_diameter_mm=profile.wheel_diameter_mm,
                    footprint_radius_m=profile.footprint_radius_m,
                    stopping_distance_m=profile.stopping_distance_m,
                    clearance_margin_m=profile.clearance_margin_m,
                    lidar_mount_x_m=profile.lidar_mount["x_m"],
                    lidar_mount_y_m=profile.lidar_mount["y_m"],
                    lidar_mount_z_m=profile.lidar_mount["z_m"],
                )
            )
            pump = LeaseSocketPump("127.0.0.1", args.lease_port, token, lease, resource.disable)
        pump.start()
        deadline = time.monotonic() + 2.0
        while not lease.ready() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not lease.ready():
            raise CalibrationError("camera_pose_host_lease_unavailable")
        if isinstance(resource, ReadOnlyBaselineCapture):
            resource.run()
        else:
            CameraPoseCaptureRunner(
                resource,
                lease,
                args.output,
                mode=args.mode,
                device_id=profile.device_id,
                boot_id=boot_id,
                executed_bundle_source_sha256=source_sha256,
            ).run()
    finally:
        try:
            if pump is not None:
                pump.close()
        finally:
            try:
                if resource is not None:
                    resource.close()
            finally:
                for signum, previous in prior_signals.items():
                    signal.signal(signum, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

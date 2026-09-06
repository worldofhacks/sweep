"""Host-pinned projection from signed fuser evidence to diagnostic control poses."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import ceil
from types import MappingProxyType

from relay.control_localization_contracts import (
    MAX_CLOCK_ERROR_MS,
    MAX_POSITION_UNCERTAINTY_MM,
    ClockMapping,
    ControlLocalizationPins,
    ControlLocalizationWire,
    ControlPose,
    bounded_nonnegative,
    finite,
    identifier,
    nonnegative_int64,
    position_uncertainty_p95_m,
    positive_int32,
    session_identifier,
)


class LocalizationProjectionError(ValueError):
    """A bounded, public reason for refusing a localization projection."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True, init=False)
class ControlLocalizationProjector:
    """Pure validator configured with host-owned identities and age limits.

    This class retains no pose and imports no planner code. RelaySession owns the
    sole retained diagnostic state so its mutation participates in audit rollback.
    """

    pins: Mapping[int, ControlLocalizationPins]
    relay_clock_id: str
    max_clock_error_ms: int
    max_fix_age_ms: int
    max_velocity_age_ms: int
    max_height_age_ms: int
    max_position_uncertainty_p95_m: float

    def __init__(
        self,
        pins: Mapping[int, ControlLocalizationPins],
        *,
        relay_clock_id: str,
        max_clock_error_ms: int,
        max_fix_age_ms: int,
        max_velocity_age_ms: int,
        max_height_age_ms: int,
        max_position_uncertainty_p95_m: float,
    ) -> None:
        clock_id = identifier(relay_clock_id, "relay_clock_id")
        copied = dict(pins)
        if not copied or len(copied) > 64:
            raise ValueError("control localization requires 1 through 64 pinned aircraft")
        if any(
            type(key) is not int
            or not isinstance(pin, ControlLocalizationPins)
            or key != pin.drone_id
            for key, pin in copied.items()
        ):
            raise ValueError("control localization pin keys must match bounded drone IDs")
        if any(pin.clock_mapping.relay_clock_id != clock_id for pin in copied.values()):
            raise ValueError("all clock mappings must target the relay runtime clock")
        object.__setattr__(self, "pins", MappingProxyType(copied))
        object.__setattr__(self, "relay_clock_id", clock_id)
        object.__setattr__(
            self,
            "max_clock_error_ms",
            bounded_nonnegative(
                max_clock_error_ms,
                "max_clock_error_ms",
                MAX_CLOCK_ERROR_MS,
            ),
        )
        object.__setattr__(
            self,
            "max_fix_age_ms",
            _freshness_bound(max_fix_age_ms, "max_fix_age_ms"),
        )
        object.__setattr__(
            self,
            "max_velocity_age_ms",
            _freshness_bound(max_velocity_age_ms, "max_velocity_age_ms"),
        )
        object.__setattr__(
            self,
            "max_height_age_ms",
            _freshness_bound(max_height_age_ms, "max_height_age_ms"),
        )
        uncertainty = finite(
            max_position_uncertainty_p95_m,
            "max_position_uncertainty_p95_m",
        )
        if not 0 < uncertainty <= MAX_POSITION_UNCERTAINTY_MM / 1_000:
            raise ValueError("max_position_uncertainty_p95_m exceeds the control-pose envelope")
        object.__setattr__(self, "max_position_uncertainty_p95_m", uncertainty)

    def project(
        self,
        wire: ControlLocalizationWire,
        *,
        authenticated_drone_id: int,
        authenticated_connection_epoch: int,
        now_ms: int,
        event_id: str,
        session: str,
        previous: ControlPose | None,
    ) -> ControlPose:
        """Validate one signed body and return a bounded, non-approved pose."""
        now = nonnegative_int64(now_ms, "now_ms")
        drone_id = positive_int32(authenticated_drone_id, "authenticated_drone_id")
        epoch = positive_int32(
            authenticated_connection_epoch,
            "authenticated_connection_epoch",
        )
        pin = self.pins.get(drone_id)
        if pin is None or wire.drone_id != drone_id or wire.connection_epoch != epoch:
            raise LocalizationProjectionError(
                "localization_identity_mismatch",
                "localization evidence does not match its authenticated aircraft epoch",
            )
        if (
            wire.map_id != pin.map_id
            or wire.geometry_id != pin.geometry_id
            or wire.camera_calibration_id != pin.camera_calibration_id
            or wire.body_extrinsics_id != pin.body_extrinsics_id
            or wire.source_ids != pin.source_ids
            or wire.clock_mapping != pin.clock_mapping
            or wire.capture_clock_id != pin.clock_mapping.capture_clock_id
        ):
            raise LocalizationProjectionError(
                "localization_provenance_mismatch",
                "localization evidence does not match the host-owned deployment pins",
            )
        if wire.clock_mapping.max_error_ms > self.max_clock_error_ms:
            raise LocalizationProjectionError(
                "localization_clock_uncertain",
                "clock conversion uncertainty exceeds the configured bound",
            )
        if (
            wire.position_map_enu_m is None
            or wire.covariance_map_enu_m2 is None
            or wire.fix_capture_time_s is None
        ):
            raise LocalizationProjectionError(
                "localization_pose_unavailable",
                "localization evidence does not contain a projectable pose",
            )

        uncertainty_m = position_uncertainty_p95_m(wire.covariance_map_enu_m2)
        if uncertainty_m * 1_000 > MAX_POSITION_UNCERTAINTY_MM:
            raise LocalizationProjectionError(
                "localization_position_uncertain",
                "localization uncertainty exceeds the control-pose envelope",
            )
        if wire.status == "ready":
            assert wire.fix_age_s is not None
            assert wire.velocity_age_s is not None
            assert wire.height_age_s is not None
            _require_fresh_age(
                wire.clock_mapping,
                wire.fix_age_s,
                self.max_fix_age_ms,
                "localization_fix_stale",
            )
            _require_fresh_age(
                wire.clock_mapping,
                wire.velocity_age_s,
                self.max_velocity_age_ms,
                "localization_velocity_stale",
            )
            _require_fresh_age(
                wire.clock_mapping,
                wire.height_age_s,
                self.max_height_age_ms,
                "localization_height_stale",
            )
            if uncertainty_m > self.max_position_uncertainty_p95_m:
                raise LocalizationProjectionError(
                    "localization_position_uncertain",
                    "ready localization uncertainty exceeds the configured bound",
                )
        try:
            nominal_pose_ms = wire.clock_mapping.to_relay_ms(wire.evaluated_at_s)
            nominal_fix_ms = wire.clock_mapping.to_relay_ms(wire.fix_capture_time_s)
        except ValueError as error:
            raise LocalizationProjectionError(
                "localization_clock_invalid",
                "localization timestamps cannot be represented on the relay clock",
            ) from error
        error_ms = wire.clock_mapping.max_error_ms
        if nominal_pose_ms > now + error_ms or nominal_fix_ms > now + error_ms:
            raise LocalizationProjectionError(
                "localization_time_in_future",
                "localization evidence maps beyond the relay clock uncertainty",
            )
        pose_time_ms = max(0, nominal_pose_ms - error_ms)
        fix_time_ms = max(0, nominal_fix_ms - error_ms)
        if wire.status == "ready" and now - fix_time_ms >= self.max_fix_age_ms:
            raise LocalizationProjectionError(
                "localization_fix_stale",
                "ready localization fix exceeds the configured freshness bound",
            )
        if previous is not None and previous.connection_epoch == epoch:
            if pose_time_ms < previous.pose_time_ms or fix_time_ms < previous.fix_time_ms:
                raise LocalizationProjectionError(
                    "localization_time_regressed",
                    "localization evidence regresses within the current aircraft epoch",
                )
            same_evidence_time = (
                pose_time_ms == previous.pose_time_ms and fix_time_ms == previous.fix_time_ms
            )
            status_rank = {"ready": 0, "hold": 1, "land": 2}
            if same_evidence_time:
                if status_rank[wire.status] < status_rank[previous.status]:
                    raise LocalizationProjectionError(
                        "localization_status_regressed",
                        "equal-time evidence cannot replace a conservative status",
                    )
                if status_rank[wire.status] == status_rank[previous.status]:
                    raise LocalizationProjectionError(
                        "duplicate_localization_state",
                        "equal-time evidence must become strictly more conservative",
                    )

        x_mm, y_mm, z_mm = (round(coordinate * 1_000) for coordinate in wire.position_map_enu_m)
        uncertainty_mm = ceil(uncertainty_m * 1_000)
        return ControlPose(
            t=now,
            event_id=identifier(event_id, "event_id"),
            session=session_identifier(session),
            drone_id=drone_id,
            connection_epoch=epoch,
            map_id=wire.map_id,
            geometry_id=wire.geometry_id,
            camera_calibration_id=wire.camera_calibration_id,
            body_extrinsics_id=wire.body_extrinsics_id,
            pose_time_ms=pose_time_ms,
            fix_time_ms=fix_time_ms,
            x_mm=x_mm,
            y_mm=y_mm,
            z_mm=z_mm,
            position_frame="map_enu",
            position_uncertainty_mm=uncertainty_mm,
            status=wire.status,
            flight_approved=False,
        )


def _freshness_bound(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= 500:
        raise ValueError(f"{name} must be from 1 through the phone's 500 ms bound")
    return value


def _require_fresh_age(
    mapping: ClockMapping,
    age_s: float,
    maximum_ms: int,
    code: str,
) -> None:
    try:
        age_ms = mapping.age_to_relay_ms(age_s)
    except ValueError as error:
        raise LocalizationProjectionError(
            "localization_clock_invalid",
            "localization age cannot be represented on the relay clock",
        ) from error
    if age_ms >= maximum_ms:
        raise LocalizationProjectionError(code, "ready localization source is stale")

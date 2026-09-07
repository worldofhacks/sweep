"""Own the host localization reader without taking ownership of flight or video."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Protocol


class LocalizationReader(Protocol):
    def start(self) -> LocalizationReader: ...

    def read(self, timeout: float = 0.0) -> tuple[object, float] | None: ...

    def close(self) -> None: ...


class LocalizationLoop(Protocol):
    def update(self, image: object, decode_time: float, now: float) -> Mapping[str, object]: ...

    def at(self, now: float) -> Mapping[str, object]: ...


class LocalizationLeaseState(StrEnum):
    PAUSED = "paused"
    REVALIDATING = "revalidating"
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LocalizationLeaseStatus:
    state: LocalizationLeaseState
    current_fix: Mapping[str, object] | None
    failure_reason: str | None

    @property
    def localization_usable(self) -> bool:
        return self.state is LocalizationLeaseState.ACTIVE

    def payload(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "localization_usable": self.localization_usable,
            "failure_reason": self.failure_reason,
            "current_fix": None if self.current_fix is None else dict(self.current_fix),
        }


class ManagedWebcamLocalizer:
    """Pause and resume a localization-only RTSP reader with fresh-fix revalidation."""

    def __init__(
        self,
        reader_factory: Callable[[], LocalizationReader],
        localizer: LocalizationLoop,
    ) -> None:
        if not callable(reader_factory):
            raise ValueError("reader_factory must be callable")
        if not callable(getattr(localizer, "update", None)) or not callable(
            getattr(localizer, "at", None)
        ):
            raise ValueError("localizer must implement update and at")
        self._reader_factory = reader_factory
        self._localizer = localizer
        self._reader: LocalizationReader | None = None
        self._state = LocalizationLeaseState.PAUSED
        self._resumed_at: float | None = None
        self._current_fix: Mapping[str, object] | None = None
        self._failure_reason: str | None = None

    @property
    def status(self) -> LocalizationLeaseStatus:
        return LocalizationLeaseStatus(
            state=self._state,
            current_fix=self._current_fix,
            failure_reason=self._failure_reason,
        )

    @property
    def reader_status(self) -> str:
        reader = self._reader
        status = getattr(reader, "status", None)
        return status if isinstance(status, str) else self._state.value

    def pause(self) -> LocalizationLeaseStatus:
        reader = self._reader
        self._resumed_at = None
        self._current_fix = None
        self._failure_reason = None
        self._state = LocalizationLeaseState.PAUSED
        if reader is not None:
            try:
                reader.close()
            except Exception:
                self._state = LocalizationLeaseState.UNAVAILABLE
                self._failure_reason = "reader_close_failed"
                return self.status
        self._reader = None
        return self.status

    def resume(self, now: float) -> LocalizationLeaseStatus:
        _monotonic(now, "now")
        if self.pause().state is not LocalizationLeaseState.PAUSED:
            return self.status
        try:
            reader = self._reader_factory().start()
        except Exception:
            self._state = LocalizationLeaseState.UNAVAILABLE
            self._failure_reason = "reader_start_failed"
            return self.status
        self._reader = reader
        self._resumed_at = now
        self._state = LocalizationLeaseState.REVALIDATING
        return self.status

    def poll(self, now: float) -> LocalizationLeaseStatus:
        _monotonic(now, "now")
        if self._state not in {LocalizationLeaseState.REVALIDATING, LocalizationLeaseState.ACTIVE}:
            return self.status
        reader = self._reader
        if reader is None or self._resumed_at is None:
            self._state = LocalizationLeaseState.UNAVAILABLE
            self._current_fix = None
            self._failure_reason = "reader_missing"
            return self.status
        try:
            frame = reader.read(0)
        except Exception:
            self._state = LocalizationLeaseState.UNAVAILABLE
            self._current_fix = None
            self._failure_reason = "reader_failed"
            return self.status
        if frame is None:
            if self._state is LocalizationLeaseState.ACTIVE:
                try:
                    state = self._localizer.at(now)
                except Exception:
                    self._state = LocalizationLeaseState.UNAVAILABLE
                    self._current_fix = None
                    self._failure_reason = "localization_state_failed"
                    return self.status
                if not isinstance(state, Mapping) or not _current_accepted_pose(state):
                    self._state = LocalizationLeaseState.REVALIDATING
                    self._current_fix = None
                    self._failure_reason = "fresh_fix_required"
                    return self.status
                self._current_fix = MappingProxyType(dict(state))
            return self.status
        image, decoded_at = frame
        if not _finite(decoded_at) or decoded_at < self._resumed_at or decoded_at > now:
            self._state = LocalizationLeaseState.REVALIDATING
            self._current_fix = None
            self._failure_reason = "frame_time_invalid"
            return self.status
        try:
            state = self._localizer.update(image, decoded_at, now)
        except Exception:
            self._state = LocalizationLeaseState.REVALIDATING
            self._current_fix = None
            self._failure_reason = "localization_update_failed"
            return self.status
        if not isinstance(state, Mapping):
            self._state = LocalizationLeaseState.REVALIDATING
            self._current_fix = None
            self._failure_reason = "localization_state_invalid"
            return self.status
        if _current_accepted_pose(state):
            self._state = LocalizationLeaseState.ACTIVE
            self._current_fix = MappingProxyType(dict(state))
            self._failure_reason = None
        else:
            self._state = LocalizationLeaseState.REVALIDATING
            self._current_fix = None
            self._failure_reason = "fresh_fix_required"
        return self.status

    def close(self) -> None:
        self.pause()


def _current_accepted_pose(state: Mapping[str, object]) -> bool:
    if state.get("accepted") is not True:
        return False
    pose = state.get("pose_observation")
    return isinstance(pose, Mapping) and pose.get("accepted") is True


def _finite(value: object) -> bool:
    return type(value) in (int, float) and isfinite(value)


def _monotonic(value: object, name: str) -> None:
    if not _finite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative monotonic timestamp")

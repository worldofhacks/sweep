"""Bound a robot monotonic clock against host monotonic probe intervals."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_UPTIME = re.compile(r"[0-9]+(?:\.[0-9]{1,9})?\Z")
_NANOSECONDS = 1_000_000_000


class ClockProbeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ClockProbe:
    boot_id: str
    robot_monotonic_ns: int
    host_before_ns: int
    host_after_ns: int
    robot_resolution_ns: int

    def __post_init__(self) -> None:
        if not _BOOT_ID.fullmatch(self.boot_id):
            raise ClockProbeError("robot boot ID is invalid")
        for name in (
            "robot_monotonic_ns",
            "host_before_ns",
            "host_after_ns",
            "robot_resolution_ns",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ClockProbeError("clock probe fields must be nonnegative integers")
        if self.host_after_ns < self.host_before_ns or self.robot_resolution_ns == 0:
            raise ClockProbeError("clock probe interval is invalid")

    @property
    def offset_interval_ns(self) -> tuple[int, int]:
        robot_first = self.robot_monotonic_ns
        robot_last = robot_first + self.robot_resolution_ns
        return (robot_first - self.host_after_ns, robot_last - self.host_before_ns)


@dataclass(frozen=True, slots=True)
class MonotonicClockMapping:
    boot_id: str
    offset_lower_ns: int
    offset_upper_ns: int

    def __post_init__(self) -> None:
        if not _BOOT_ID.fullmatch(self.boot_id) or self.offset_upper_ns < self.offset_lower_ns:
            raise ClockProbeError("clock mapping is invalid")

    @property
    def maximum_error_ns(self) -> int:
        return (self.offset_upper_ns - self.offset_lower_ns + 1) // 2

    def robot_time_ns(self, host_monotonic_ns: int) -> int:
        if type(host_monotonic_ns) is not int or host_monotonic_ns < 0:
            raise ClockProbeError("host monotonic timestamp is invalid")
        return host_monotonic_ns + (self.offset_lower_ns + self.offset_upper_ns) // 2


def parse_probe_reply(value: str) -> tuple[str, int, int]:
    lines = value.strip().splitlines()
    if len(lines) != 2 or not _BOOT_ID.fullmatch(lines[0]) or not _UPTIME.fullmatch(lines[1]):
        raise ClockProbeError("robot clock probe reply is invalid")
    whole, _, decimal = lines[1].partition(".")
    precision = len(decimal)
    robot_ns = int(whole) * _NANOSECONDS + int((decimal + "0" * 9)[:9])
    resolution = 10 ** (9 - precision) if precision else _NANOSECONDS
    return lines[0], robot_ns, resolution


def probe_clock(query: Callable[[], str], *, monotonic_ns: Callable[[], int]) -> ClockProbe:
    before = monotonic_ns()
    reply = query()
    after = monotonic_ns()
    boot_id, robot_ns, resolution = parse_probe_reply(reply)
    return ClockProbe(boot_id, robot_ns, before, after, resolution)


def clock_mapping(probes: Iterable[ClockProbe], *, maximum_error_ns: int) -> MonotonicClockMapping:
    if type(maximum_error_ns) is not int or maximum_error_ns < 0:
        raise ClockProbeError("maximum clock error is invalid")
    samples = tuple(probes)
    if not samples:
        raise ClockProbeError("at least one clock probe is required")
    boot_ids = {sample.boot_id for sample in samples}
    if len(boot_ids) != 1:
        raise ClockProbeError("robot rebooted during clock qualification")
    lower = max(sample.offset_interval_ns[0] for sample in samples)
    upper = min(sample.offset_interval_ns[1] for sample in samples)
    mapping = MonotonicClockMapping(samples[0].boot_id, lower, upper)
    if mapping.maximum_error_ns > maximum_error_ns:
        raise ClockProbeError("clock probe uncertainty exceeds the configured bound")
    return mapping

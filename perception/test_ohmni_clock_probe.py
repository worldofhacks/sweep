from __future__ import annotations

import pytest

from perception.ohmni_clock_probe import (
    ClockProbe,
    ClockProbeError,
    clock_mapping,
    parse_probe_reply,
    probe_clock,
)

BOOT = "e6c4ece6-3f03-4471-bfb8-5d9ff9865243"


def test_clock_probe_keeps_the_actual_host_rtt_interval_and_robot_resolution() -> None:
    ticks = iter((1_000_000_000, 1_012_000_000))
    sample = probe_clock(lambda: f"{BOOT}\n39.82\n", monotonic_ns=lambda: next(ticks))

    assert sample.offset_interval_ns == (38_808_000_000, 38_830_000_000)


def test_clock_mapping_intersects_actual_probe_intervals() -> None:
    first = ClockProbe(BOOT, 40_000_000_000, 1_000_000_000, 1_004_000_000, 10_000_000)
    second = ClockProbe(BOOT, 41_000_000_000, 2_001_000_000, 2_005_000_000, 10_000_000)

    mapping = clock_mapping((first, second), maximum_error_ns=10_000_000)

    assert mapping.offset_lower_ns == 38_996_000_000
    assert mapping.offset_upper_ns == 39_009_000_000
    assert mapping.maximum_error_ns == 6_500_000
    assert mapping.robot_time_ns(3_000_000_000) == 42_002_500_000


def test_clock_mapping_refuses_reboot_in_probe_set_or_excessive_uncertainty() -> None:
    sample = ClockProbe(BOOT, 40_000_000_000, 1_000_000_000, 1_100_000_000, 10_000_000)
    rebooted = ClockProbe(
        "128b44e8-50aa-43c5-8f06-c36eecdcdddf",
        40_000_000_000,
        1_000_000_000,
        1_100_000_000,
        10_000_000,
    )

    with pytest.raises(ClockProbeError, match="uncertainty"):
        clock_mapping((sample,), maximum_error_ns=10_000_000)
    with pytest.raises(ClockProbeError, match="rebooted"):
        clock_mapping((sample, rebooted), maximum_error_ns=1_000_000_000)


@pytest.mark.parametrize("reply", ["bad\n39.82\n", f"{BOOT}\n-1\n", f"{BOOT}\n39.8x\n"])
def test_probe_reply_is_strict(reply: str) -> None:
    with pytest.raises(ClockProbeError):
        parse_probe_reply(reply)

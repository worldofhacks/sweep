"""Shared bounds for a signed, node-timed body-forward velocity pulse."""

from collections.abc import Mapping

BODY_PULSE_CAPABILITY = "body_pulse_v1"
MAX_BODY_PULSE_SPEED_MM_S = 250
MIN_BODY_PULSE_DURATION_MS = 100
MAX_BODY_PULSE_DURATION_MS = 500
# Android supports a minimum 5 Hz controller tick; neutralization runs on a tick.
MAX_BODY_PULSE_TICK_OVERRUN_MS = 200


def valid_body_pulse_args(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"forward_mm_s", "duration_ms"}:
        return False
    forward, duration = value["forward_mm_s"], value["duration_ms"]
    return (
        type(forward) is int
        and 0 < abs(forward) <= MAX_BODY_PULSE_SPEED_MM_S
        and type(duration) is int
        and MIN_BODY_PULSE_DURATION_MS <= duration <= MAX_BODY_PULSE_DURATION_MS
    )


def body_pulse_displacement_bound_m(value: Mapping[str, object]) -> float:
    """Nominal velocity travel plus one slowest-supported Android tick.

    This is a software command envelope. It does not claim a hard realtime or
    physical braking bound; measured stopping/positioning margins remain required.
    """
    if not valid_body_pulse_args(value):
        raise ValueError("body_pulse requires bounded integer forward_mm_s and duration_ms")
    return (
        abs(value["forward_mm_s"])
        * (value["duration_ms"] + MAX_BODY_PULSE_TICK_OVERRUN_MS)
        / 1_000_000
    )

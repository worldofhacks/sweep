"""Bounded ground language facts and exact pulse phrases; no frame conversion."""

import re
from collections.abc import Mapping

_PULSE = re.compile(
    r"(?:ground |robot )?(?:pulse (forward|left|right)|"
    r"(forward|left|right) pulse|turn (left|right) pulse)[.!]?",
    re.I,
)
_HOME = re.compile(r"(?:return|come|go) home[.!]?", re.I)


def pulse_arguments(transcript: str) -> dict[str, int] | None:
    match = _PULSE.fullmatch(" ".join(transcript.strip().split()))
    if match is None:
        return None
    direction = next(value.lower() for value in match.groups() if value is not None)
    return {
        "linear_mm_s": 80 if direction == "forward" else 0,
        "angular_mrad_s": {"forward": 0, "left": 350, "right": -350}[direction],
        "duration_ms": 250,
    }


def is_return_phrase(transcript: str) -> bool:
    return _HOME.fullmatch(" ".join(transcript.strip().split())) is not None


def ground_facts(raw: Mapping[str, object]) -> dict[str, object] | None:
    if raw.get("node_type") != "ground":
        return None
    epoch = raw.get("connection_epoch")
    authority = raw.get("control_authority")
    readiness = raw.get("ground_readiness")
    source = readiness.get("source_id") if isinstance(readiness, Mapping) else None
    unit = raw.get("unit")
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 0
        or not isinstance(authority, bool)
        or (
            source is not None
            and (
                not isinstance(source, str)
                or not source
                or len(source) > 256
                or source.strip() != source
                or not source.isprintable()
            )
        )
        or (
            unit is not None
            and (isinstance(unit, bool) or not isinstance(unit, int) or not 1 <= unit <= 64)
        )
    ):
        raise ValueError("ground identity and authority fields are invalid")
    return {
        "connection_epoch": epoch,
        "source_id": source,
        "control_authority": authority,
        "drive_available": "ground_drive" in raw.get("adapter_capabilities", ()),
        "unit": unit,
    }


def ground_ready(drone: Mapping[str, object]) -> bool:
    ground = drone.get("ground")
    return (
        isinstance(ground, Mapping)
        and drone.get("membership") == "ready"
        and drone.get("selectable") is True
        and ground.get("control_authority") is True
        and ground.get("drive_available") is True
        and isinstance(ground.get("source_id"), str)
        and bool(ground["source_id"])
    )

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class GroundStatus:
    x: float
    y: float
    yaw_deg: float
    vx: float
    vy: float
    battery: float
    link: float
    pos_quality: float
    state: str
    drive_authority: bool
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RangeScan:
    t_ms: int
    pose: tuple[float, float, float]
    angle_min_deg: float
    angle_increment_deg: float
    range_min_m: float
    range_max_m: float
    ranges_cm: list[int]

    def __post_init__(self) -> None:
        expected = int(round(360 / self.angle_increment_deg))
        if self.angle_increment_deg not in (0.5, 1.0, 2.0) or len(self.ranges_cm) != expected:
            raise ValueError("range scan bins do not match its angular increment")

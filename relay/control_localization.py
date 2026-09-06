"""Public localization transport surface.

Contracts and projection live in cohesive modules; this re-export keeps the new
PR's import path stable without retaining a planner-coupled store.
"""

from relay.control_localization_contracts import (
    MAX_COORDINATE_MM,
    MAX_POSITION_UNCERTAINTY_MM,
    ClockMapping,
    ControlLocalizationPins,
    ControlLocalizationWire,
    ControlPose,
    to_wire_payload,
)
from relay.control_localization_projection import (
    ControlLocalizationProjector,
    LocalizationProjectionError,
)

__all__ = [
    "MAX_COORDINATE_MM",
    "MAX_POSITION_UNCERTAINTY_MM",
    "ClockMapping",
    "ControlLocalizationPins",
    "ControlLocalizationProjector",
    "ControlLocalizationWire",
    "ControlPose",
    "LocalizationProjectionError",
    "to_wire_payload",
]

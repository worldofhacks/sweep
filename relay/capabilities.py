"""Bounded intent capability profiles shared by relay validation and planning."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

_SAFE_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class IntentName(StrEnum):
    ARM = "arm"
    DISARM = "disarm"
    ESTOP = "estop"
    SELECT = "select"
    TAKEOFF = "takeoff"
    LAND = "land"
    LAND_ALL = "land_all"
    HOLD = "hold"
    TRANSLATE = "translate"
    ALTITUDE = "altitude"
    FORMATION_NEXT = "formation_next"
    FORMATION_SET = "formation_set"
    SPACING = "spacing"
    COME_HOME = "come_home"
    SWEEP = "sweep"
    CAPTURE_ROOM = "capture_room"
    SURVEY_AREA = "survey_area"
    MAP_AREA = "map_area"


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    name: str
    enabled_intent_names: frozenset[IntentName]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SAFE_PROFILE_NAME.fullmatch(self.name) is None:
            raise ValueError("capability profile name must be a non-empty safe identifier")
        raw_names = self.enabled_intent_names
        if isinstance(raw_names, (str, bytes)) or not isinstance(raw_names, Iterable):
            raise ValueError("enabled intent names must be an iterable of intent names")
        try:
            names = frozenset(IntentName(name) for name in raw_names)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "enabled intent names must contain only registered intent names"
            ) from error
        if not names:
            raise ValueError("enabled intent names must not be empty")
        object.__setattr__(self, "enabled_intent_names", names)
        unsupported = names - C1_IMPLEMENTED_INTENT_NAMES
        if unsupported:
            names = ", ".join(sorted(name.value for name in unsupported))
            raise ValueError(f"capability profile enables unimplemented intents: {names}")

    def supports(self, intent_name: IntentName) -> bool:
        return intent_name in self.enabled_intent_names

    def state_value(self) -> dict[str, object]:
        return {
            "capability_profile": self.name,
            "enabled_intent_names": sorted(name.value for name in self.enabled_intent_names),
        }


C1_IMPLEMENTED_INTENT_NAMES = frozenset(
    {
        IntentName.ARM,
        IntentName.SELECT,
        IntentName.TAKEOFF,
        IntentName.TRANSLATE,
        IntentName.HOLD,
        IntentName.COME_HOME,
        IntentName.LAND,
        IntentName.LAND_ALL,
        IntentName.ESTOP,
        IntentName.CAPTURE_ROOM,
        IntentName.ALTITUDE,
        IntentName.FORMATION_NEXT,
        IntentName.FORMATION_SET,
        IntentName.SPACING,
        IntentName.SWEEP,
    }
)

C1_CAPABILITY_PROFILE = CapabilityProfile(
    name="c1_basic_control",
    enabled_intent_names=C1_IMPLEMENTED_INTENT_NAMES,
)

"""Bounded intent capability profiles shared by relay validation and planning."""

from dataclasses import dataclass
from enum import StrEnum


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
        if not self.name:
            raise ValueError("capability profile name must not be empty")
        unsupported = self.enabled_intent_names - C1_IMPLEMENTED_INTENT_NAMES
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
    }
)

C1_CAPABILITY_PROFILE = CapabilityProfile(
    name="c1_basic_control",
    enabled_intent_names=C1_IMPLEMENTED_INTENT_NAMES,
)

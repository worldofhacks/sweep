from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal


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


class Mode(StrEnum):
    INDOOR = "indoor"
    OUTDOOR_C = "outdoorC"
    OUTDOOR_F = "outdoorF"


class RejectionReason(StrEnum):
    INVALID_PAYLOAD = "invalid_payload"
    UNKNOWN_SOURCE = "unknown_source"
    UNKNOWN_INTENT = "unknown_intent"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class IntentV1:
    v: Literal[1]
    t: int
    type: Literal["intent"]
    intent_id: str
    retry_of: str | None
    source: str
    session: str
    name: IntentName
    args: Mapping[str, object]
    selection: tuple[int, ...]
    mode: Mode
    confirm: bool


@dataclass(frozen=True, slots=True)
class AcceptedIntent:
    intent: IntentV1


@dataclass(frozen=True, slots=True)
class RejectedIntent:
    reason: RejectionReason
    detail: str


type ValidationResult = AcceptedIntent | RejectedIntent

# Operator sources that may authenticate without an aircraft binding and emit
# Intent v1. Console buttons, the keyboard network stop, and the webcam gesture
# producer are each bound to their own connection; an intent never moves
# between them. Adding a source changes this constant and its conformance tests.
REGISTERED_SOURCES = frozenset({"console", "keyboard", "webcam"})
M20_SUPPORTED_NAMES = frozenset(
    {
        IntentName.ARM,
        IntentName.SELECT,
        IntentName.TAKEOFF,
        IntentName.TRANSLATE,
        IntentName.HOLD,
        IntentName.COME_HOME,
        IntentName.LAND_ALL,
        IntentName.ESTOP,
        IntentName.CAPTURE_ROOM,
    }
)

_REQUIRED_FIELDS = frozenset(
    {
        "v",
        "t",
        "type",
        "intent_id",
        "source",
        "session",
        "name",
        "args",
        "selection",
        "mode",
        "confirm",
    }
)
_FIELDS = _REQUIRED_FIELDS | {"retry_of"}


def validate_intent(raw: object) -> ValidationResult:
    """Validate untrusted input without raising; failures are returned as typed rejections."""
    if not isinstance(raw, Mapping) or not _REQUIRED_FIELDS <= set(raw) or not set(raw) <= _FIELDS:
        return RejectedIntent(
            RejectionReason.INVALID_PAYLOAD, "payload fields do not match Intent v1"
        )

    if not _has_valid_envelope(raw):
        return RejectedIntent(RejectionReason.INVALID_PAYLOAD, "payload values are invalid")

    source = raw["source"]
    if source not in REGISTERED_SOURCES:
        return RejectedIntent(RejectionReason.UNKNOWN_SOURCE, f"unregistered source: {source}")

    try:
        name = IntentName(raw["name"])
    except ValueError:
        return RejectedIntent(RejectionReason.UNKNOWN_INTENT, f"unknown intent: {raw['name']}")

    try:
        args = _parse_args(name, raw["args"])
    except (KeyError, RecursionError, TypeError, ValueError):
        return RejectedIntent(RejectionReason.INVALID_PAYLOAD, f"invalid args for {name}")

    if not _has_valid_scope(name, raw):
        return RejectedIntent(
            RejectionReason.INVALID_PAYLOAD, f"invalid selection or confirmation for {name}"
        )

    mode = Mode(raw["mode"])
    if mode is not Mode.INDOOR:
        return RejectedIntent(
            RejectionReason.UNSUPPORTED, f"{mode} is outside the M2.0 capability set"
        )

    if name not in M20_SUPPORTED_NAMES:
        return RejectedIntent(
            RejectionReason.UNSUPPORTED, f"{name} is outside the M2.0 capability set"
        )

    return AcceptedIntent(
        IntentV1(
            v=1,
            t=raw["t"],
            type="intent",
            intent_id=raw["intent_id"],
            retry_of=raw.get("retry_of"),
            source=source,
            session=raw["session"],
            name=name,
            args=args,
            selection=tuple(raw["selection"]),
            mode=mode,
            confirm=raw["confirm"],
        )
    )


def _has_valid_envelope(raw: Mapping[object, object]) -> bool:
    try:
        Mode(raw["mode"])
    except (KeyError, TypeError, ValueError):
        return False

    return (
        isinstance(raw["v"], int)
        and raw["v"] == 1
        and not isinstance(raw["v"], bool)
        and isinstance(raw["t"], int)
        and not isinstance(raw["t"], bool)
        and raw["t"] >= 0
        and raw["type"] == "intent"
        and isinstance(raw["intent_id"], str)
        and bool(raw["intent_id"])
        and _is_valid_retry_of(raw.get("retry_of"), raw["intent_id"])
        and isinstance(raw["source"], str)
        and bool(raw["source"])
        and isinstance(raw["session"], str)
        and bool(raw["session"])
        and isinstance(raw["name"], str)
        and bool(raw["name"])
        and isinstance(raw["confirm"], bool)
        and _is_drone_ids(raw["selection"], allow_empty=True)
    )


def _is_drone_ids(value: object, *, allow_empty: bool) -> bool:
    if not isinstance(value, list) or (not value and not allow_empty):
        return False
    return all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in value
    ) and (len(set(value)) == len(value))


def _is_valid_retry_of(value: object, intent_id: object) -> bool:
    return value is None or (isinstance(value, str) and bool(value) and value != intent_id)


def _has_valid_scope(name: IntentName, raw: Mapping[object, object]) -> bool:
    if name is IntentName.CAPTURE_ROOM:
        return raw["confirm"] is True and len(raw["selection"]) == 1
    if name is IntentName.SURVEY_AREA:
        return raw["confirm"] is True
    if name is IntentName.MAP_AREA:
        return raw["confirm"] is True and bool(raw["selection"])
    return True


def _parse_args(name: IntentName, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError

    if name is IntentName.SELECT:
        if set(value) != {"ids"} or not _is_drone_ids(value["ids"], allow_empty=False):
            raise ValueError
        return MappingProxyType({"ids": tuple(value["ids"])})

    if name is IntentName.TRANSLATE:
        if set(value) != {"dx", "dy"}:
            raise ValueError
        if not _is_finite_number(value["dx"]) or not _is_finite_number(value["dy"]):
            raise ValueError
        return MappingProxyType({"dx": value["dx"], "dy": value["dy"]})

    if name in {
        IntentName.ARM,
        IntentName.DISARM,
        IntentName.ESTOP,
        IntentName.TAKEOFF,
        IntentName.LAND,
        IntentName.LAND_ALL,
        IntentName.HOLD,
        IntentName.FORMATION_NEXT,
        IntentName.COME_HOME,
    }:
        if value:
            raise ValueError
        return MappingProxyType({})

    if name in {IntentName.ALTITUDE, IntentName.SPACING}:
        if set(value) != {"delta"} or not _is_finite_number(value["delta"]):
            raise ValueError
        return MappingProxyType({"delta": value["delta"]})

    if name is IntentName.FORMATION_SET:
        if set(value) != {"name"} or not isinstance(value["name"], str) or not value["name"]:
            raise ValueError
        return MappingProxyType({"name": value["name"]})

    if name is IntentName.SWEEP:
        if not set(value) <= {"box"}:
            raise ValueError
        if "box" in value and not isinstance(value["box"], Mapping):
            raise ValueError
        return MappingProxyType({"box": _freeze_json(value["box"])} if "box" in value else {})

    if name in {IntentName.SURVEY_AREA, IntentName.MAP_AREA}:
        if (
            set(value) != {"area_id"}
            or not isinstance(value["area_id"], str)
            or not value["area_id"]
        ):
            raise ValueError
        return MappingProxyType({"area_id": value["area_id"]})

    if name is IntentName.CAPTURE_ROOM:
        if set(value) != {"room_id", "capture_id", "pattern"}:
            raise ValueError
        if not isinstance(value["room_id"], str) or not value["room_id"]:
            raise ValueError
        if not isinstance(value["capture_id"], str) or not value["capture_id"]:
            raise ValueError
        if value["pattern"] not in ("pano_360", "reconstruct_8"):
            raise ValueError
        return MappingProxyType(
            {
                "room_id": value["room_id"],
                "capture_id": value["capture_id"],
                "pattern": value["pattern"],
            }
        )

    raise ValueError


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    return value == value and abs(value) != float("inf")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, str | bool):
        return value
    if _is_finite_number(value):
        return value
    raise ValueError

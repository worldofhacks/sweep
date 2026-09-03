from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from relay.intent_v1 import AcceptedIntent, IntentName, Mode, validate_intent

MAX_PLAN_STEPS = 12
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MEMBERSHIPS = frozenset({"registered", "ready", "leaving", "disconnected", "degraded"})
_FLIGHT_STATES = frozenset(
    {"disarmed", "landed", "armed", "taking_off", "airborne", "hovering", "landing", "emergency"}
)


class OutcomeKind(StrEnum):
    PLAN = "plan"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"
    REFUSE = "refuse"


class CompilerReason(StrEnum):
    AMBIGUOUS_SELECTION = "ambiguous_selection"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    ESTOP_ACTIVE = "estop_active"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    MODEL_UNAVAILABLE = "model_unavailable"
    NO_SELECTION = "no_selection"
    STALE_STATE = "stale_state"
    UNKNOWN_REFERENCE = "unknown_reference"


@dataclass(frozen=True, slots=True)
class ProposedIntent:
    name: IntentName
    args: Mapping[str, object]
    selection: tuple[int, ...]
    mode: Mode

    def semantic_dict(self) -> dict[str, object]:
        return {
            "name": self.name.value,
            "args": _thaw(self.args),
            "selection": list(self.selection),
            "mode": self.mode.value,
        }


@dataclass(frozen=True, slots=True)
class CompilerOutcome:
    kind: OutcomeKind
    intents: tuple[ProposedIntent, ...] = ()
    reason: CompilerReason | None = None
    detail: str | None = None
    source: Literal["claude", "template"] = "claude"


@dataclass(frozen=True, slots=True)
class GroundingFacts:
    state_time_ms: int
    state_version: int
    capability_version: str
    state_digest: str
    armed: bool
    estop: bool
    selection: tuple[int, ...]
    drones: tuple[Mapping[str, object], ...]
    rooms: tuple[str, ...]

    def model_dict(self) -> dict[str, object]:
        return {
            "state_time_ms": self.state_time_ms,
            "state_version": self.state_version,
            "capability_version": self.capability_version,
            "armed": self.armed,
            "estop": self.estop,
            "selection": list(self.selection),
            "drones": [_thaw(drone) for drone in self.drones],
            "rooms": list(self.rooms),
        }


def build_grounding_facts(
    relay_state: object,
    *,
    capability_version: str,
    rooms: tuple[str, ...] = (),
) -> GroundingFacts:
    if not isinstance(relay_state, Mapping):
        raise ValueError("relay state must be an object")
    if relay_state.get("type") != "state" or relay_state.get("mode") != "indoor":
        raise ValueError("relay state must be an indoor state event")
    state_version = relay_state.get("roster_version")
    if not isinstance(state_version, int) or isinstance(state_version, bool) or state_version < 0:
        raise ValueError("relay state requires a non-negative roster version")
    state_time_ms = relay_state.get("t")
    if not isinstance(state_time_ms, int) or isinstance(state_time_ms, bool) or state_time_ms < 0:
        raise ValueError("relay state requires a non-negative timestamp")
    armed = relay_state.get("armed")
    estop = relay_state.get("estop")
    if not isinstance(armed, bool) or not isinstance(estop, bool):
        raise ValueError("relay state requires armed and estop flags")
    selection = _positive_ids(relay_state.get("selection"), "selection")
    if not isinstance(capability_version, str) or not _SAFE_IDENTIFIER.fullmatch(
        capability_version
    ):
        raise ValueError("capability version must be a safe bounded identifier")
    if len(set(rooms)) != len(rooms) or any(
        not isinstance(room, str) or not _SAFE_IDENTIFIER.fullmatch(room) for room in rooms
    ):
        raise ValueError("rooms must be unique safe identifiers")

    raw_drones = relay_state.get("drones")
    if not isinstance(raw_drones, list) or len(raw_drones) > 32:
        raise ValueError("relay state requires a bounded drone list")
    drones: list[Mapping[str, object]] = []
    ids: set[int] = set()
    for raw in raw_drones:
        if not isinstance(raw, Mapping):
            raise ValueError("each drone must be an object")
        drone_id = raw.get("drone_id")
        if (
            not isinstance(drone_id, int)
            or isinstance(drone_id, bool)
            or drone_id <= 0
            or drone_id in ids
        ):
            raise ValueError("drone IDs must be unique positive integers")
        ids.add(drone_id)
        membership = raw.get("membership")
        selectable = raw.get("selectable")
        flight_state = raw.get("flight_state")
        patterns = raw.get("camera_patterns")
        if membership not in _MEMBERSHIPS or not isinstance(selectable, bool):
            raise ValueError("drone membership and selectable fields are required")
        if flight_state is not None and flight_state not in _FLIGHT_STATES:
            raise ValueError("flight state must use the supported vocabulary")
        if not _camera_pattern_list(patterns):
            raise ValueError("camera patterns must use the supported pattern vocabulary")
        capabilities = raw.get("adapter_capabilities")
        if not _string_list(capabilities):
            raise ValueError("drone capabilities must be a string list")
        drones.append(
            MappingProxyType(
                {
                    "drone_id": drone_id,
                    "membership": membership,
                    "selectable": selectable,
                    "flight_state": flight_state,
                    "camera_patterns": tuple(sorted(patterns)),
                    "flight_available": "flight" in capabilities,
                }
            )
        )
    if any(drone_id not in ids for drone_id in selection):
        raise ValueError("selection references an unknown drone")

    model_facts = {
        "state_time_ms": state_time_ms,
        "state_version": state_version,
        "capability_version": capability_version,
        "armed": armed,
        "estop": estop,
        "selection": list(selection),
        "drones": [_thaw(drone) for drone in drones],
        "rooms": list(rooms),
    }
    stable_facts = dict(model_facts)
    stable_facts.pop("state_time_ms")
    digest = hashlib.sha256(
        json.dumps(stable_facts, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return GroundingFacts(
        state_time_ms=state_time_ms,
        state_version=state_version,
        capability_version=capability_version,
        state_digest=digest,
        armed=armed,
        estop=estop,
        selection=selection,
        drones=tuple(drones),
        rooms=rooms,
    )


def validate_model_outcome(raw: object, facts: GroundingFacts) -> CompilerOutcome:
    if not isinstance(raw, Mapping) or not set(raw) <= {"kind", "intents", "reason", "detail"}:
        return _invalid()
    try:
        kind = OutcomeKind(raw.get("kind"))
    except (TypeError, ValueError):
        return _invalid()
    detail = raw.get("detail")
    if detail is not None and (not isinstance(detail, str) or len(detail) > 500):
        return _invalid()

    if kind is not OutcomeKind.PLAN:
        if raw.get("intents") not in (None, []):
            return _invalid()
        try:
            reason = CompilerReason(raw.get("reason"))
        except (TypeError, ValueError):
            return _invalid()
        return CompilerOutcome(kind=kind, reason=reason, detail=detail)

    if raw.get("reason") is not None:
        return _invalid()
    items = raw.get("intents")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_PLAN_STEPS:
        return _invalid()
    intents: list[ProposedIntent] = []
    for index, item in enumerate(items):
        intent = _validate_proposed_intent(item, facts, index)
        if intent is None:
            return _invalid()
        intents.append(intent)
    return CompilerOutcome(kind=kind, intents=tuple(intents), detail=detail)


def intent_payload(
    proposal: ProposedIntent,
    *,
    session: str,
    intent_id: str,
    timestamp_ms: int,
    source: str = "console",
) -> dict[str, object]:
    return {
        "v": 1,
        "t": timestamp_ms,
        "type": "intent",
        "intent_id": intent_id,
        "retry_of": None,
        "source": source,
        "session": session,
        "name": proposal.name.value,
        "args": _thaw(proposal.args),
        "selection": list(proposal.selection),
        "mode": proposal.mode.value,
        "confirm": True,
    }


def _validate_proposed_intent(
    raw: object, facts: GroundingFacts, index: int
) -> ProposedIntent | None:
    if not isinstance(raw, Mapping) or set(raw) != {"name", "args", "selection", "mode"}:
        return None
    selection = raw.get("selection")
    if not isinstance(selection, list):
        return None
    candidate = {
        "v": 1,
        "t": 0,
        "type": "intent",
        "intent_id": f"compiler-validation-{index}",
        "retry_of": None,
        "source": "console",
        "session": "compiler-validation",
        "name": raw.get("name"),
        "args": raw.get("args"),
        "selection": selection,
        "mode": raw.get("mode"),
        "confirm": True,
    }
    result = validate_intent(candidate)
    if not isinstance(result, AcceptedIntent):
        return None
    known = {drone["drone_id"]: drone for drone in facts.drones}
    fleet_wide = result.intent.name in {IntentName.ESTOP, IntentName.LAND_ALL}
    if fleet_wide and selection:
        return None
    if any(drone_id not in known or not known[drone_id]["selectable"] for drone_id in selection):
        return None
    if result.intent.name is IntentName.SELECT:
        ids = tuple(result.intent.args["ids"])
        if any(drone_id not in known or not known[drone_id]["selectable"] for drone_id in ids):
            return None
    if result.intent.name is IntentName.CAPTURE_ROOM:
        if result.intent.args["room_id"] not in facts.rooms:
            return None
        drone = known[result.intent.selection[0]]
        if result.intent.args["pattern"] not in drone["camera_patterns"]:
            return None
    return ProposedIntent(
        name=result.intent.name,
        args=result.intent.args,
        selection=result.intent.selection,
        mode=result.intent.mode,
    )


def _invalid() -> CompilerOutcome:
    return CompilerOutcome(
        kind=OutcomeKind.REFUSE,
        reason=CompilerReason.INVALID_MODEL_OUTPUT,
        detail="The proposed plan did not pass deterministic validation.",
        source="template",
    )


def _positive_ids(value: object, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value
    ):
        raise ValueError(f"{field} must be a list of positive integer IDs")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must not contain duplicate IDs")
    return tuple(value)


def _string_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 64
        and all(isinstance(item, str) and 0 < len(item) <= 128 for item in value)
    )


def _camera_pattern_list(value: object) -> bool:
    return _string_list(value) and set(value) <= {"pano_360", "reconstruct_8"}


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value

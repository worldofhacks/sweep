from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from planner.models import TranslationGrounding, TranslationPolicy
from relay.intent_v1 import AcceptedIntent, IntentName, Mode, validate_intent

MAX_PLAN_STEPS = 12
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MEMBERSHIPS = frozenset({"registered", "ready", "leaving", "disconnected", "degraded"})
_FLIGHT_STATES = frozenset(
    {"disarmed", "landed", "armed", "taking_off", "airborne", "hovering", "landing", "emergency"}
)
_SELECTION_TARGETED = frozenset(
    {
        IntentName.TAKEOFF,
        IntentName.LAND,
        IntentName.TRANSLATE,
        IntentName.HOLD,
        IntentName.COME_HOME,
        IntentName.CAPTURE_ROOM,
    }
)
_AIRBORNE_STATES = frozenset({"taking_off", "airborne", "hovering", "landing"})
_STABLE_MOTION_STATES = frozenset({"airborne", "hovering"})
type OutcomeSource = Literal["anthropic", "replay", "synthetic", "template"]


class OutcomeKind(StrEnum):
    PLAN = "plan"
    CANCEL_PENDING = "cancel_pending"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"
    REFUSE = "refuse"


class CompilerReason(StrEnum):
    AMBIGUOUS_ACTION = "ambiguous_action"
    AMBIGUOUS_LOCATION = "ambiguous_location"
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
    source: OutcomeSource
    intents: tuple[ProposedIntent, ...] = ()
    reason: CompilerReason | None = None
    detail: str | None = None
    pending_intent_id: str | None = None


@dataclass(frozen=True, slots=True)
class GroundingFacts:
    session: str
    state_event_id: str
    state_time_ms: int
    state_version: int
    capability_version: str
    state_digest: str
    armed: bool
    estop: bool
    selection: tuple[int, ...]
    drones: tuple[Mapping[str, object], ...]
    rooms: tuple[str, ...]
    translation_frame: str | None
    translation_step_m: float | None
    pending: Mapping[str, str] | None
    qualified_voice_intents: tuple[str, ...]

    def model_dict(self) -> dict[str, object]:
        return {
            "session": self.session,
            "state_event_id": self.state_event_id,
            "state_time_ms": self.state_time_ms,
            "state_version": self.state_version,
            "capability_version": self.capability_version,
            "armed": self.armed,
            "estop": self.estop,
            "selection": list(self.selection),
            "drones": [_model_drone(drone) for drone in self.drones],
            "rooms": list(self.rooms),
            "translation": (
                None
                if self.translation_frame is None
                else {"frame": self.translation_frame, "step_m": self.translation_step_m}
            ),
            "pending": None if self.pending is None else dict(self.pending),
            "qualified_voice_intents": list(self.qualified_voice_intents),
        }

    def record_dict(self) -> dict[str, object]:
        return {
            **self.model_dict(),
            "drones": [_thaw(drone) for drone in self.drones],
            "state_digest": self.state_digest,
        }

    @classmethod
    def from_record(cls, raw: object) -> GroundingFacts:
        if not isinstance(raw, Mapping) or set(raw) != {
            "session",
            "state_event_id",
            "state_time_ms",
            "state_version",
            "capability_version",
            "state_digest",
            "armed",
            "estop",
            "selection",
            "drones",
            "rooms",
            "translation",
            "pending",
            "qualified_voice_intents",
        }:
            raise ValueError("persisted grounding facts are invalid")
        drones = raw["drones"]
        if not isinstance(drones, list):
            raise ValueError("persisted grounding drones are invalid")
        relay_drones = []
        for drone in drones:
            if not isinstance(drone, Mapping) or set(drone) != {
                "drone_id",
                "membership",
                "selectable",
                "flight_state",
                "camera_patterns",
                "flight_available",
                "heading_deg",
                "position",
                "position_time_ms",
                "home_position",
            }:
                raise ValueError("persisted grounding drone is invalid")
            if not isinstance(drone["flight_available"], bool):
                raise ValueError("persisted flight capability is invalid")
            relay_drones.append(
                {
                    "drone_id": drone["drone_id"],
                    "membership": drone["membership"],
                    "selectable": drone["selectable"],
                    "flight_state": drone["flight_state"],
                    "camera_patterns": drone["camera_patterns"],
                    "adapter_capabilities": ["flight"] if drone["flight_available"] else [],
                    "heading_deg": drone["heading_deg"],
                    "telemetry": (
                        None
                        if drone["position"] is None
                        else {
                            **dict(zip(("x", "y", "z"), drone["position"], strict=True)),
                            "t": drone["position_time_ms"],
                        }
                    ),
                    "home_pose": (
                        None
                        if drone["home_position"] is None
                        else dict(zip(("x", "y", "z"), drone["home_position"], strict=True))
                    ),
                }
            )
        facts = build_grounding_facts(
            {
                "v": 1,
                "t": raw["state_time_ms"],
                "type": "state",
                "event_id": raw["state_event_id"],
                "session": raw["session"],
                "mode": "indoor",
                "roster_version": raw["state_version"],
                "armed": raw["armed"],
                "estop": raw["estop"],
                "selection": raw["selection"],
                "drones": relay_drones,
            },
            capability_version=raw["capability_version"],
            rooms=tuple(raw["rooms"]) if isinstance(raw["rooms"], list) else (),
            translation=(
                None
                if raw["translation"] is None
                else _translation_from_record(raw["translation"], drones)
            ),
            qualified_voice_intents=(
                tuple(raw["qualified_voice_intents"])
                if isinstance(raw["qualified_voice_intents"], list)
                else ()
            ),
            pending=raw["pending"],
        )
        if facts.state_digest != raw["state_digest"]:
            raise ValueError("persisted grounding digest does not match its facts")
        return facts


def build_grounding_facts(
    relay_state: object,
    *,
    capability_version: str,
    rooms: tuple[str, ...] = (),
    translation: object = None,
    qualified_voice_intents: tuple[str, ...] = (),
    pending: object = None,
) -> GroundingFacts:
    if not isinstance(relay_state, Mapping):
        raise ValueError("relay state must be an object")
    if (
        relay_state.get("v") != 1
        or relay_state.get("type") != "state"
        or relay_state.get("mode") != "indoor"
        or not isinstance(relay_state.get("event_id"), str)
        or not relay_state["event_id"]
        or not isinstance(relay_state.get("session"), str)
        or not relay_state["session"]
    ):
        raise ValueError("relay state must be an indoor state event")
    session = relay_state["session"]
    state_event_id = relay_state["event_id"]
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
    translation_frame: str | None = None
    translation_step_m: float | None = None
    translation_headings: Mapping[int, float] = {}
    if translation is not None:
        if not isinstance(translation, TranslationGrounding):
            raise ValueError("translation must come from the trusted planning policy")
        translation_frame = translation.policy.frame
        translation_step_m = translation.policy.step_m
        translation_headings = translation.headings
    if any(not isinstance(value, str) for value in qualified_voice_intents):
        raise ValueError("qualified voice intents must be unique Intent v1 names")
    normalized_qualified = tuple(qualified_voice_intents)
    if len(set(normalized_qualified)) != len(normalized_qualified) or any(
        value not in {name.value for name in IntentName} for value in normalized_qualified
    ):
        raise ValueError("qualified voice intents must be unique Intent v1 names")
    pending_value = relay_state.get("pending") if pending is None else pending
    normalized_pending: Mapping[str, str] | None = None
    if pending_value is not None:
        if (
            isinstance(pending_value, Mapping)
            and set(pending_value) == {"intent_id", "name"}
            and isinstance(pending_value["intent_id"], str)
            and _SAFE_IDENTIFIER.fullmatch(pending_value["intent_id"])
            and isinstance(pending_value["name"], str)
            and pending_value["name"] in {name.value for name in IntentName}
        ):
            normalized_pending = MappingProxyType(dict(pending_value))

    raw_drones = relay_state.get("drones")
    if (
        not isinstance(raw_drones, Sequence)
        or isinstance(raw_drones, str | bytes)
        or len(raw_drones) > 32
    ):
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
        heading = translation_headings.get(drone_id)
        if membership not in _MEMBERSHIPS or not isinstance(selectable, bool):
            raise ValueError("drone membership and selectable fields are required")
        if flight_state is not None and flight_state not in _FLIGHT_STATES:
            raise ValueError("flight state must use the supported vocabulary")
        if not _camera_pattern_list(patterns):
            raise ValueError("camera patterns must use the supported pattern vocabulary")
        if heading is not None and (
            not _is_finite_number(heading) or not 0 <= float(heading) < 360
        ):
            raise ValueError("heading must be in degrees from zero up to 360")
        capabilities = raw.get("adapter_capabilities")
        if not _string_list(capabilities):
            raise ValueError("drone capabilities must be a string list")
        position = _coordinates(raw.get("telemetry"), "telemetry")
        position_time_ms = _telemetry_time(raw.get("telemetry"))
        home_position = _coordinates(raw.get("home_pose"), "home pose")
        drones.append(
            MappingProxyType(
                {
                    "drone_id": drone_id,
                    "membership": membership,
                    "selectable": selectable,
                    "flight_state": flight_state,
                    "camera_patterns": tuple(sorted(patterns)),
                    "flight_available": "flight" in capabilities,
                    "heading_deg": heading,
                    "position": position,
                    "position_time_ms": position_time_ms,
                    "home_position": home_position,
                }
            )
        )
    if any(drone_id not in ids for drone_id in selection):
        raise ValueError("selection references an unknown drone")

    model_facts = {
        "session": session,
        "state_event_id": state_event_id,
        "state_time_ms": state_time_ms,
        "state_version": state_version,
        "capability_version": capability_version,
        "armed": armed,
        "estop": estop,
        "selection": list(selection),
        "drones": [_thaw(drone) for drone in drones],
        "rooms": list(rooms),
        "translation": (
            None
            if translation_frame is None
            else {"frame": translation_frame, "step_m": translation_step_m}
        ),
        "pending": None if normalized_pending is None else dict(normalized_pending),
        "qualified_voice_intents": list(normalized_qualified),
    }
    stable_facts = dict(model_facts)
    stable_facts["drones"] = [_thaw(drone) for drone in drones]
    stable_facts.pop("state_time_ms")
    digest = hashlib.sha256(
        json.dumps(stable_facts, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return GroundingFacts(
        session=session,
        state_event_id=state_event_id,
        state_time_ms=state_time_ms,
        state_version=state_version,
        capability_version=capability_version,
        state_digest=digest,
        armed=armed,
        estop=estop,
        selection=selection,
        drones=tuple(drones),
        rooms=rooms,
        translation_frame=translation_frame,
        translation_step_m=translation_step_m,
        pending=normalized_pending,
        qualified_voice_intents=normalized_qualified,
    )


def validate_model_outcome(
    raw: object,
    facts: GroundingFacts,
    *,
    capture_id: Callable[[int], str],
    source: OutcomeSource,
    transcript: str,
) -> CompilerOutcome:
    if not isinstance(raw, Mapping) or not set(raw) <= {
        "kind",
        "intents",
        "reason",
        "detail",
        "pending_intent_id",
    }:
        return _invalid(source)
    try:
        kind = OutcomeKind(raw.get("kind"))
    except (TypeError, ValueError):
        return _invalid(source)
    detail = raw.get("detail")
    if detail is not None and (not isinstance(detail, str) or len(detail) > 500):
        return _invalid(source)

    if kind is not OutcomeKind.PLAN:
        if kind is OutcomeKind.CANCEL_PENDING:
            if set(raw) != {"kind", "pending_intent_id"} or facts.pending is None:
                return _invalid(source)
            pending_intent_id = raw.get("pending_intent_id")
            if pending_intent_id != facts.pending["intent_id"]:
                return _invalid(source)
            return CompilerOutcome(kind=kind, source=source, pending_intent_id=pending_intent_id)
        allowed_fields = {"kind", "reason"} | ({"detail"} if "detail" in raw else set())
        if set(raw) != allowed_fields:
            return _invalid(source)
        try:
            reason = CompilerReason(raw.get("reason"))
        except (TypeError, ValueError):
            return _invalid(source)
        if not facts.selection and reason is CompilerReason.AMBIGUOUS_LOCATION:
            return CompilerOutcome(
                kind=OutcomeKind.REFUSE, reason=CompilerReason.NO_SELECTION, source=source
            )
        return CompilerOutcome(kind=kind, reason=reason, detail=detail, source=source)

    if set(raw) != {"kind", "intents"}:
        return _invalid(source)
    items = raw.get("intents")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_PLAN_STEPS:
        return _invalid(source)
    intents: list[ProposedIntent] = []
    expected_selection = facts.selection
    expected_estop = facts.estop
    expected_armed = facts.armed
    expected_flight_states = {
        int(drone["drone_id"]): drone["flight_state"] for drone in facts.drones
    }
    for index, item in enumerate(items):
        intent = _validate_proposed_intent(
            item,
            facts,
            index,
            expected_selection=expected_selection,
            expected_estop=expected_estop,
            capture_id=capture_id,
            allow_persisted_capture_id=False,
        )
        if intent is None:
            return _invalid(source)
        transition = _fold_semantic_state(
            intent,
            facts,
            armed=expected_armed,
            flight_states=expected_flight_states,
        )
        if transition is None:
            return _invalid(source)
        intents.append(intent)
        expected_armed, expected_flight_states = transition
        if intent.name is IntentName.SELECT:
            expected_selection = tuple(intent.args["ids"])
        if intent.name is IntentName.ESTOP:
            expected_estop = True
    if any(intent.name is IntentName.ESTOP for intent in intents) and (
        transcript != "Emergency stop."
        or IntentName.ESTOP.value not in facts.qualified_voice_intents
        or len(intents) != 1
    ):
        return CompilerOutcome(
            kind=OutcomeKind.UNSUPPORTED,
            reason=CompilerReason.CAPABILITY_UNAVAILABLE,
            source=source,
        )
    return CompilerOutcome(kind=kind, intents=tuple(intents), detail=detail, source=source)


def rehydrate_plan_intents(raw: object, facts: GroundingFacts) -> tuple[ProposedIntent, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_PLAN_STEPS:
        raise ValueError("persisted plan intents are invalid")
    intents: list[ProposedIntent] = []
    expected_selection = facts.selection
    expected_estop = facts.estop
    expected_armed = facts.armed
    expected_flight_states = {
        int(drone["drone_id"]): drone["flight_state"] for drone in facts.drones
    }
    for index, item in enumerate(raw):
        intent = _validate_proposed_intent(
            item,
            facts,
            index,
            expected_selection=expected_selection,
            expected_estop=expected_estop,
            capture_id=lambda _index: "",
            allow_persisted_capture_id=True,
        )
        if intent is None:
            raise ValueError("persisted plan intent is invalid")
        transition = _fold_semantic_state(
            intent,
            facts,
            armed=expected_armed,
            flight_states=expected_flight_states,
        )
        if transition is None:
            raise ValueError("persisted plan flight sequence is invalid")
        intents.append(intent)
        expected_armed, expected_flight_states = transition
        if intent.name is IntentName.SELECT:
            expected_selection = tuple(intent.args["ids"])
        if intent.name is IntentName.ESTOP:
            expected_estop = True
    return tuple(intents)


def intent_payload(
    proposal: ProposedIntent,
    *,
    session: str,
    intent_id: str,
    timestamp_ms: int,
    source: str = "console",
) -> dict[str, object]:
    args = _thaw(proposal.args)
    return {
        "v": 1,
        "t": timestamp_ms,
        "type": "intent",
        "intent_id": intent_id,
        "retry_of": None,
        "source": source,
        "session": session,
        "name": proposal.name.value,
        "args": args,
        "selection": list(proposal.selection),
        "mode": proposal.mode.value,
        "confirm": True,
    }


def plan_step_matches_facts(intent: ProposedIntent, facts: GroundingFacts) -> bool:
    try:
        restored = rehydrate_plan_intents([intent.semantic_dict()], facts)
    except ValueError:
        return False
    return restored == (intent,)


def _validate_proposed_intent(
    raw: object,
    facts: GroundingFacts,
    index: int,
    *,
    expected_selection: tuple[int, ...],
    expected_estop: bool,
    capture_id: Callable[[int], str],
    allow_persisted_capture_id: bool,
) -> ProposedIntent | None:
    if not isinstance(raw, Mapping) or set(raw) != {"name", "args", "selection", "mode"}:
        return None
    selection = raw.get("selection")
    if not isinstance(selection, list):
        return None
    raw_name = raw.get("name")
    raw_args = raw.get("args")
    if raw_name == IntentName.CAPTURE_ROOM.value:
        expected_args = (
            {"room_id", "capture_id", "pattern"}
            if allow_persisted_capture_id
            else {"room_id", "pattern"}
        )
        if not isinstance(raw_args, Mapping) or set(raw_args) != expected_args:
            return None
        if not allow_persisted_capture_id:
            minted_capture_id = capture_id(index)
            if not isinstance(minted_capture_id, str) or not _SAFE_IDENTIFIER.fullmatch(
                minted_capture_id
            ):
                return None
            raw_args = {**raw_args, "capture_id": minted_capture_id}
    candidate = {
        "v": 1,
        "t": 0,
        "type": "intent",
        "intent_id": f"compiler-validation-{index}",
        "retry_of": None,
        "source": "console",
        "session": "compiler-validation",
        "name": raw_name,
        "args": raw_args,
        "selection": selection,
        "mode": raw.get("mode"),
        "confirm": True,
    }
    result = validate_intent(candidate)
    if not isinstance(result, AcceptedIntent):
        return None
    known = {drone["drone_id"]: drone for drone in facts.drones}
    fleet_wide = result.intent.name in {IntentName.ARM, IntentName.ESTOP, IntentName.LAND_ALL}
    if fleet_wide and selection:
        return None
    if any(drone_id not in known or not known[drone_id]["selectable"] for drone_id in selection):
        return None
    if result.intent.name is IntentName.SELECT:
        ids = tuple(result.intent.args["ids"])
        if result.intent.selection != ids:
            return None
        if any(drone_id not in known or not known[drone_id]["selectable"] for drone_id in ids):
            return None
    elif (
        result.intent.name in _SELECTION_TARGETED and result.intent.selection != expected_selection
    ):
        return None
    if result.intent.name is IntentName.TRANSLATE and (
        facts.translation_frame not in {"world", "aircraft_relative"}
        or facts.translation_step_m is None
        or (
            facts.translation_frame == "aircraft_relative"
            and any(known[drone_id]["heading_deg"] is None for drone_id in result.intent.selection)
        )
    ):
        return None
    if result.intent.name in {
        IntentName.ARM,
        IntentName.DISARM,
        IntentName.TAKEOFF,
        IntentName.LAND,
        IntentName.LAND_ALL,
        IntentName.HOLD,
        IntentName.TRANSLATE,
        IntentName.COME_HOME,
    } and any(not known[drone_id]["flight_available"] for drone_id in result.intent.selection):
        return None
    if expected_estop and result.intent.name not in {
        IntentName.ESTOP,
        IntentName.HOLD,
        IntentName.LAND_ALL,
    }:
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


def _invalid(source: OutcomeSource = "template") -> CompilerOutcome:
    return CompilerOutcome(
        kind=OutcomeKind.REFUSE,
        reason=CompilerReason.INVALID_MODEL_OUTPUT,
        detail="The proposed plan did not pass deterministic validation.",
        source=source,
    )


def _fold_semantic_state(
    intent: ProposedIntent,
    facts: GroundingFacts,
    *,
    armed: bool,
    flight_states: Mapping[int, object],
) -> tuple[bool, dict[int, object]] | None:
    states = dict(flight_states)
    selected = intent.selection
    name = intent.name

    if name in _SELECTION_TARGETED and not selected:
        return None
    if name is IntentName.ARM:
        armed = True
    elif name is IntentName.TAKEOFF:
        if not armed or any(
            states[drone_id] not in {"armed", "disarmed", "landed"} for drone_id in selected
        ):
            return None
        for drone_id in selected:
            states[drone_id] = "hovering"
    elif name in {IntentName.TRANSLATE, IntentName.COME_HOME}:
        if not armed or any(states[drone_id] not in _STABLE_MOTION_STATES for drone_id in selected):
            return None
        for drone_id in selected:
            states[drone_id] = "hovering"
    elif name is IntentName.HOLD:
        if any(states[drone_id] not in _AIRBORNE_STATES for drone_id in selected):
            return None
        for drone_id in selected:
            states[drone_id] = "hovering"
    elif name is IntentName.LAND:
        if any(states[drone_id] not in _AIRBORNE_STATES for drone_id in selected):
            return None
        for drone_id in selected:
            states[drone_id] = "landed"
    elif name is IntentName.CAPTURE_ROOM:
        if not armed or any(states[drone_id] != "hovering" for drone_id in selected):
            return None
    elif name is IntentName.LAND_ALL:
        targets = [
            int(drone["drone_id"])
            for drone in facts.drones
            if drone["membership"] in {"ready", "degraded"}
            and states[int(drone["drone_id"])] in _AIRBORNE_STATES
        ]
        if not targets:
            return None
        for drone_id in targets:
            states[drone_id] = "landed"
    elif name is IntentName.ESTOP:
        for drone in facts.drones:
            drone_id = int(drone["drone_id"])
            if (
                drone["membership"] in {"ready", "degraded"}
                and states[drone_id] in _AIRBORNE_STATES
            ):
                states[drone_id] = "hovering"
    return armed, states


def _positive_ids(value: object, field: str) -> tuple[int, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str | bytes)
        or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value)
    ):
        raise ValueError(f"{field} must be a list of positive integer IDs")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must not contain duplicate IDs")
    return tuple(value)


def _translation_from_record(
    raw: object, drones: Sequence[Mapping[str, object]]
) -> TranslationGrounding:
    if not isinstance(raw, Mapping) or set(raw) != {"frame", "step_m"}:
        raise ValueError("persisted translation policy is invalid")
    return TranslationGrounding(
        policy=TranslationPolicy(frame=raw["frame"], step_m=raw["step_m"]),
        headings={
            drone["drone_id"]: drone["heading_deg"]
            for drone in drones
            if drone["heading_deg"] is not None
        },
    )


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and value == value
        and abs(value) != float("inf")
    )


def _coordinates(value: object, field: str) -> tuple[float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"drone {field} must be an object or null")
    coordinates = tuple(value.get(axis) for axis in ("x", "y", "z"))
    if not all(_is_finite_number(coordinate) for coordinate in coordinates):
        raise ValueError(f"drone {field} coordinates must be finite numbers")
    return tuple(float(coordinate) for coordinate in coordinates)


def _telemetry_time(value: object) -> int | None:
    if value is None:
        return None
    assert isinstance(value, Mapping)
    timestamp = value.get("t")
    if timestamp is None:
        return None
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
        raise ValueError("drone telemetry requires a non-negative timestamp")
    return timestamp


def _model_drone(drone: Mapping[str, object]) -> dict[str, object]:
    return {
        key: _thaw(drone[key])
        for key in (
            "drone_id",
            "membership",
            "selectable",
            "flight_state",
            "camera_patterns",
            "flight_available",
            "heading_deg",
        )
    }


def _string_list(value: object) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, str | bytes)
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

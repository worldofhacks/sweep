from __future__ import annotations

from evals.language_corpus import StaticResponseTransport
from language.compiler import CompiledPlan, InMemoryAuditSink, TranscriptCompiler
from language.contracts import CompilerReason, OutcomeKind
from language.navigation import NavigationGrounding, Zone, navigation_from_metadata
from relay.capabilities import (
    C1_CAPABILITY_PROFILE,
    C1_IMPLEMENTED_INTENT_NAMES,
    CapabilityProfile,
)
from relay.intent_v1 import IntentName

PROFILE = CapabilityProfile(
    "mapped_navigation", C1_IMPLEMENTED_INTENT_NAMES | {IntentName.NAVIGATE}
)
NAVIGATION = NavigationGrounding(
    PROFILE,
    ("map", "v1"),
    ("geometry", "v1"),
    "navigation-v1",
    "level_1",
    "catalog-v1",
    (
        Zone("atrium", "level_1", True, ("atrium-1",), ("the atrium", "atrium")),
        Zone("lobby", "level_1", True, ("lobby-1",), ("the lobby", "lobby")),
        Zone("upstairs", "level_2", True, ("upstairs-1",), ("upstairs",)),
    ),
)


def state(
    *, selection: list[int] | None = None, flight_state: str = "hovering"
) -> dict[str, object]:
    return {
        "v": 1,
        "t": 100,
        "type": "state",
        "event_id": "navigation-state",
        "session": "navigation",
        "mode": "indoor",
        "roster_version": 1,
        "armed": True,
        "estop": False,
        "selection": [1] if selection is None else selection,
        "drones": [
            {
                "drone_id": 1,
                "membership": "ready",
                "selectable": True,
                "flight_state": flight_state,
                "camera_patterns": ["pano_360"],
                "adapter_capabilities": ["flight"],
                "telemetry": {"x": 0, "y": 0, "z": 1, "t": 100},
                "home_pose": {"x": 0, "y": 0, "z": 0},
            }
        ],
    }


def compile(transcript: str, payload: object, **kwargs: object):
    return TranscriptCompiler(StaticResponseTransport(payload), audit=InMemoryAuditSink()).compile(
        transcript,
        state(**kwargs),
        capability_version="mapped_navigation",
        navigation=NAVIGATION,
        now_ms=100,
    )


def test_navigation_resolves_catalog_destination_and_rejects_model_substitution() -> None:
    payload = {
        "kind": "plan",
        "intents": [
            {"name": "navigate", "args": {"zone_id": "atrium"}, "selection": [1], "mode": "indoor"}
        ],
    }
    outcome, plan = compile("fly to the atrium", payload)
    assert outcome.kind is OutcomeKind.PLAN
    assert plan is not None and plan.intents[0].args == {"zone_id": "atrium"}
    outcome, _ = compile(
        "fly to the atrium",
        {**payload, "intents": [{**payload["intents"][0], "args": {"zone_id": "lobby"}}]},
    )
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT


def test_navigation_requires_known_airborne_selected_destination() -> None:
    payload = {
        "kind": "plan",
        "intents": [
            {
                "name": "navigate",
                "args": {"zone_id": "upstairs"},
                "selection": [1],
                "mode": "indoor",
            }
        ],
    }
    outcome, _ = compile("fly to upstairs", payload)
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT
    outcome, _ = compile(
        "fly to the atrium",
        {
            "kind": "plan",
            "intents": [
                {"name": "takeoff", "args": {}, "selection": [1], "mode": "indoor"},
                {
                    "name": "navigate",
                    "args": {"zone_id": "atrium"},
                    "selection": [1],
                    "mode": "indoor",
                },
            ],
        },
        flight_state="landed",
    )
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT


def test_navigation_select_then_go_keeps_the_spoken_aircraft() -> None:
    payload = {
        "kind": "plan",
        "intents": [
            {"name": "select", "args": {"ids": [1]}, "selection": [1], "mode": "indoor"},
            {"name": "navigate", "args": {"zone_id": "lobby"}, "selection": [1], "mode": "indoor"},
        ],
    }
    outcome, plan = compile("drone one go to the lobby", payload)
    assert outcome.kind is OutcomeKind.PLAN
    assert plan is not None and [item.name.value for item in plan.intents] == ["select", "navigate"]


def test_navigation_metadata_and_audit_preserve_canonical_destination_resolution() -> None:
    grounding = navigation_from_metadata(NAVIGATION.model_dict(), PROFILE)
    assert grounding.resolve("THE ATRIUM") == (grounding.zones[0],)

    outcome, plan = compile(
        "fly to the atrium",
        {
            "kind": "plan",
            "intents": [
                {
                    "name": "navigate",
                    "args": {"zone_id": "the atrium"},
                    "selection": [1],
                    "mode": "indoor",
                }
            ],
        },
    )
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT
    assert plan is None

    outcome, plan = compile(
        "fly to the atrium",
        {
            "kind": "plan",
            "intents": [
                {
                    "name": "navigate",
                    "args": {"zone_id": "atrium"},
                    "selection": [1],
                    "mode": "indoor",
                }
            ],
        },
    )
    assert outcome.kind is OutcomeKind.PLAN and plan is not None
    assert CompiledPlan.from_audit_event(plan.audit_record()) == plan


def test_navigation_grounding_rejects_a_conflicting_advertised_profile() -> None:
    relay_state = state()
    relay_state.update(C1_CAPABILITY_PROFILE.state_value())
    outcome, plan = TranscriptCompiler(
        StaticResponseTransport({"kind": "clarify", "reason": "ambiguous_location"}),
        audit=InMemoryAuditSink(),
    ).compile(
        "fly to the atrium",
        relay_state,
        capability_version="mapped_navigation",
        navigation=NAVIGATION,
        now_ms=100,
    )
    assert outcome.reason is CompilerReason.STALE_STATE
    assert plan is None

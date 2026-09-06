from __future__ import annotations

from evals.language_corpus import StaticResponseTransport
from language.compiler import InMemoryAuditSink, TranscriptCompiler
from language.contracts import CompilerReason, OutcomeKind
from language.navigation import NavigationGrounding, Zone, navigation_from_record
from relay.capabilities import C1_IMPLEMENTED_INTENT_NAMES, CapabilityProfile
from relay.intent_v1 import IntentName

PROFILE = CapabilityProfile(
    "mapped_navigation", C1_IMPLEMENTED_INTENT_NAMES | {IntentName.NAVIGATE, IntentName.SEARCH}
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
    (("atrium-line", "atrium"),),
    ("atrium",),
    ("backpack", "bottle", "suitcase"),
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


def test_search_and_mapped_formation_are_grounded_to_configured_missions() -> None:
    outcome, plan = compile(
        "search the atrium for backpack",
        {
            "kind": "plan",
            "intents": [
                {
                    "name": "search",
                    "args": {"zone_id": "atrium", "target_class": "backpack"},
                    "selection": [1],
                    "mode": "indoor",
                }
            ],
        },
    )
    assert outcome.kind is OutcomeKind.PLAN and plan is not None
    outcome, _ = compile(
        "search the atrium for diamond",
        {
            "kind": "plan",
            "intents": [
                {
                    "name": "search",
                    "args": {"zone_id": "atrium", "target_class": "backpack"},
                    "selection": [1],
                    "mode": "indoor",
                }
            ],
        },
    )
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT
    outcome, plan = compile(
        "set formation atrium-line",
        {
            "kind": "plan",
            "intents": [
                {
                    "name": "formation_set",
                    "args": {"name": "atrium-line"},
                    "selection": [1],
                    "mode": "indoor",
                }
            ],
        },
    )
    assert outcome.kind is OutcomeKind.PLAN and plan is not None


def test_persisted_navigation_keeps_configured_mission_metadata() -> None:
    restored = navigation_from_record(NAVIGATION.record_dict(), PROFILE)
    assert restored.formations == (("atrium-line", "atrium"),)
    assert restored.search_zones == ("atrium",)
    assert restored.target_classes == ("backpack", "bottle", "suitcase")


def test_search_and_formation_metadata_can_be_enabled_independently() -> None:
    search_only = NAVIGATION.record_dict()
    search_only.pop("formations")
    restored = navigation_from_record(search_only, PROFILE)
    assert restored.search_zones == ("atrium",)
    assert restored.formations == ()
    formation_only = NAVIGATION.record_dict()
    formation_only.pop("search")
    restored = navigation_from_record(formation_only, PROFILE)
    assert restored.formations == (("atrium-line", "atrium"),)
    assert restored.search_zones == ()

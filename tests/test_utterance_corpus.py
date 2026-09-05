from __future__ import annotations

import json
from pathlib import Path

CORPUS_PATH = Path("datasets/utterances/transcript_plan_cases.jsonl")
RESPONSES_PATH = Path("datasets/utterances/transcript_plan_responses.synthetic.json")
ORDERED_INTENTS = {
    "capture_room",
    "arm",
    "select",
    "takeoff",
    "translate",
    "hold",
    "come_home",
    "land",
    "land_all",
    "estop",
}
REFUSAL_CATEGORIES = {
    "unknown_id",
    "current_selection",
    "ambiguity",
    "stale_state",
    "unavailable_capability",
    "unresolved_location",
}


def load_cases() -> list[dict[str, object]]:
    lines = CORPUS_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


def test_utterance_corpus_has_complete_compatible_cases() -> None:
    cases = load_cases()

    assert len(cases) == 50
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["category"] for case in cases} >= REFUSAL_CATEGORIES
    assert sum(case["live_demo"] is True for case in cases) == 20

    planned = {
        intent["name"]
        for case in cases
        if case["expected"]["kind"] == "plan"
        for intent in case["expected"]["intents"]
    }
    assert ORDERED_INTENTS <= planned
    ordered = {
        case["id"]: [intent["name"] for intent in case["expected"].get("intents", [])]
        for case in cases
    }
    assert ordered["select-one-then-takeoff"] == ["select", "takeoff"]
    assert ordered["select-all-then-translate"] == ["select", "translate"]
    explicit_multi_id = next(case for case in cases if case["id"] == "select-drones-one-and-two")
    assert explicit_multi_id["expected"]["intents"][0]["args"] == {"ids": [1, 2]}

    for case in cases:
        assert set(case) == {
            "id",
            "transcript",
            "relay_state",
            "context",
            "expected",
            "category",
            "live_demo",
        }
        assert isinstance(case["id"], str) and case["id"]
        assert isinstance(case["transcript"], str) and case["transcript"]
        assert isinstance(case["relay_state"], dict)
        assert {"capability_version", "rooms", "now_ms"} <= set(case["context"])
        assert isinstance(case["live_demo"], bool)


def test_owner_decisions_are_encoded_in_corpus() -> None:
    cases = {case["id"]: case for case in load_cases()}

    for case_id in ("capture-this-room", "capture-the-room", "take-a-capture-here"):
        assert cases[case_id]["expected"] == {
            "kind": "clarify",
            "reason": "ambiguous_location",
        }
    assert cases["no-selection-capture"]["expected"] == {
        "kind": "refuse",
        "reason": "no_selection",
    }

    movement = {
        "move-right-half-meter": {"dx": 0.0, "dy": -1.0},
        "move-left-half-meter": {"dx": 0.0, "dy": 0.6096},
        "move-forward-half-meter": {"dx": 1.0, "dy": 0.0},
        "select-all-then-translate": {"dx": 0.0, "dy": -1.0},
        "move-right-one-meter": {"dx": 0.0, "dy": -2.0},
    }
    for case_id, expected_args in movement.items():
        case = cases[case_id]
        assert case["context"]["translation"] == {
            "frame": "aircraft_relative",
            "step_m": 0.5,
        }
        selected = case["expected"]["intents"][-1]["selection"]
        headings = {
            drone["drone_id"]: drone["heading_deg"] for drone in case["relay_state"]["drones"]
        }
        assert all(drone_id in headings for drone_id in selected)
        assert case["expected"]["intents"][-1]["args"] == expected_args

    assert cases["prepare-the-aircraft"]["expected"]["intents"][0]["name"] == "arm"
    assert cases["launch"]["expected"]["intents"][0]["name"] == "takeoff"
    assert cases["land-now"]["expected"]["intents"][0] == {
        "name": "land",
        "args": {},
        "selection": [1, 2],
        "mode": "indoor",
    }

    assert cases["voice-stop-pending"]["expected"]["intents"][0]["name"] == "hold"
    assert cases["voice-abort-pending"]["expected"] == {
        "kind": "clarify",
        "reason": "ambiguous_action",
    }
    assert cases["voice-emergency-stop-qualified"]["context"]["qualified_voice_intents"] == [
        "estop"
    ]
    assert cases["voice-emergency-stop-qualified"]["expected"]["intents"][0]["name"] == "estop"
    assert cases["abort-pending-takeoff"]["expected"] == {
        "kind": "cancel_pending",
        "pending_intent_id": "pending-takeoff-1",
    }
    assert cases["stop-pending-takeoff"]["expected"] == {
        "kind": "cancel_pending",
        "pending_intent_id": "pending-takeoff-2",
    }

    assert cases["unresolved-three-doors-down"]["expected"] == {
        "kind": "unsupported",
        "reason": "capability_unavailable",
    }
    for case_id in ("unresolved-kitchen", "ambiguous-that-room"):
        assert cases[case_id]["expected"] == {
            "kind": "clarify",
            "reason": "ambiguous_location",
        }
    assert cases["unavailable-panorama"]["expected"] == {
        "kind": "clarify",
        "reason": "ambiguous_location",
    }
    unavailable_room = cases["unavailable-room-capture"]["expected"]
    assert unavailable_room["kind"] == "clarify"
    assert unavailable_room["reason"] == "capability_unavailable"
    assert "reconstruct_8" in unavailable_room["detail"]


def test_cached_responses_cover_every_corpus_case() -> None:
    cases = load_cases()
    responses = json.loads(RESPONSES_PATH.read_text(encoding="utf-8"))

    assert responses["version"] == 1
    assert set(responses) == {"version", "responses"}
    assert set(responses["responses"]) == {case["id"] for case in cases}
    for case in cases:
        expected = case["expected"]
        if expected["kind"] == "plan":
            expected = {
                **expected,
                "intents": [
                    {
                        **intent,
                        "args": {
                            key: value
                            for key, value in intent["args"].items()
                            if intent["name"] != "capture_room" or key != "capture_id"
                        },
                    }
                    for intent in expected["intents"]
                ],
            }
        assert responses["responses"][case["id"]] == expected


def test_voice_estop_requires_qualified_exact_phrase() -> None:
    cases = load_cases()
    pending_cases = [case for case in cases if case["category"] == "estop_pending"]
    planned_estop_cases = [
        case
        for case in cases
        if any(intent["name"] == "estop" for intent in case["expected"].get("intents", []))
    ]

    assert [case["id"] for case in planned_estop_cases] == ["voice-emergency-stop-qualified"]
    assert planned_estop_cases[0]["transcript"] == "Emergency stop."
    assert len(pending_cases) == 1
    assert all(case["live_demo"] is False for case in pending_cases)
    expected = {"kind": "unsupported", "reason": "capability_unavailable"}
    assert all(case["expected"] == expected for case in pending_cases)

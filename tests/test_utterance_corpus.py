from __future__ import annotations

import json
from collections import Counter
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

    assert len(cases) == 44
    assert len({case["id"] for case in cases}) == len(cases)
    assert Counter(case["category"] for case in cases)["core"] == 24
    assert {case["category"] for case in cases} >= REFUSAL_CATEGORIES
    assert sum(case["live_demo"] is True for case in cases) == 20

    planned = {
        intent["name"]
        for case in cases
        if case["expected"]["kind"] == "plan"
        for intent in case["expected"]["intents"]
    }
    assert ORDERED_INTENTS - {"estop"} <= planned
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
        assert set(case["context"]) == {"capability_version", "rooms", "now_ms"}
        assert isinstance(case["live_demo"], bool)


def test_cached_responses_cover_every_corpus_case() -> None:
    cases = load_cases()
    responses = json.loads(RESPONSES_PATH.read_text(encoding="utf-8"))

    assert responses["version"] == 1
    assert set(responses) == {"version", "responses"}
    assert set(responses["responses"]) == {case["id"] for case in cases}
    assert all(responses["responses"][case["id"]] == case["expected"] for case in cases)


def test_voice_estop_cases_remain_non_emitting_pending_owner_signoff() -> None:
    estop_cases = [case for case in load_cases() if case["category"] == "estop_pending"]

    assert len(estop_cases) == 3
    assert all(case["live_demo"] is False for case in estop_cases)
    expected = {"kind": "unsupported", "reason": "capability_unavailable"}
    assert all(case["expected"] == expected for case in estop_cases)

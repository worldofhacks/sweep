import json
from pathlib import Path

import pytest

from language.search_queries import SearchQueryFacts, resolve_search_query
from language.telemetry import NoOpTraceSink

CASES = json.loads((Path(__file__).parent / "fixtures/search_queries.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_search_query_replay_resolves_only_configured_targets_and_rooms(case) -> None:
    facts = SearchQueryFacts(tuple(case["zones"]), ("person", "backpack", "suitcase", "bottle"))
    result = resolve_search_query(
        case["query"],
        facts,
        session_id="query-replay",
        correlation_id=case["id"],
        tracer=NoOpTraceSink(),
    )
    for field, expected in case["expected"].items():
        assert getattr(result, field) == expected
    assert result.source == "template"
    if result.status == "resolved":
        assert result.zone_id in facts.zones
        if result.mode == "survey":
            assert result.target_class is None
        else:
            assert result.target_class in facts.target_classes


def test_query_trace_records_grounding_outcome_without_user_text() -> None:
    class Recorder:
        def __init__(self):
            self.events = []

        def record(self, event):
            self.events.append(event)

    recorder = Recorder()
    result = resolve_search_query(
        "find backpack in lobby",
        SearchQueryFacts(("lobby",), ("backpack",)),
        session_id="session-opaque",
        correlation_id="query-opaque",
        tracer=recorder,
    )
    assert result.status == "resolved"
    assert [event["event"] for event in recorder.events] == [
        "compiler_started",
        "compiler_completed",
    ]
    assert recorder.events[-1]["grounded"] == 1
    assert recorder.events[-1]["source"] == "template"
    assert all(event["correlation_id"] == "query-opaque" for event in recorder.events)
    assert "find backpack in lobby" not in json.dumps(recorder.events)


def test_search_query_without_telemetry_credentials_uses_the_no_op_sink(monkeypatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    result = resolve_search_query(
        "find backpack in lobby",
        SearchQueryFacts(("lobby",), ("backpack",)),
        session_id="session-opaque",
        correlation_id="query-opaque",
    )
    assert result.status == "resolved"


def test_survey_query_resolves_a_configured_room_without_a_target_class() -> None:
    result = resolve_search_query(
        "survey the lobby grid",
        SearchQueryFacts(("lobby",), ("backpack",)),
        session_id="survey-session",
        correlation_id="survey-query",
        tracer=NoOpTraceSink(),
    )

    assert result.status == "resolved"
    assert result.mode == "survey"
    assert result.zone_id == "lobby"
    assert result.target_class is None

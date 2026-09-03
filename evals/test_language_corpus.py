from __future__ import annotations

import json

import pytest

from evals.language_corpus import (
    StaticResponseTransport,
    append_jsonl_run,
    evaluate_case,
    load_corpus,
    load_synthetic_responses,
    write_dashboard,
)
from language.contracts import build_grounding_facts
from language.transport import ModelRequest, RecordingTransport, ReplayTransport


def test_synthetic_corpus_runs_through_recorded_production_requests(tmp_path) -> None:
    cases = load_corpus()
    responses = load_synthetic_responses()
    assert len(cases) >= 10
    cassette = tmp_path / "language-replay.json"
    for case in cases:
        facts = build_grounding_facts(
            case.relay_state,
            capability_version=case.capability_version,
            rooms=case.rooms,
        )
        request = ModelRequest(transcript=case.transcript, facts=facts.model_dict())
        RecordingTransport(StaticResponseTransport(responses[case.case_id]), cassette).complete(
            request
        )
    replay = ReplayTransport(cassette)
    results = [evaluate_case(case, replay) for case in cases]

    assert all(result.passed for result in results), results

    results_path = tmp_path / "results.jsonl"
    dashboard_path = tmp_path / "dashboard.html"
    append_jsonl_run(results, results_path, run_id="synthetic-v1")
    write_dashboard(results, dashboard_path, run_id="synthetic-v1")
    rows = [json.loads(line) for line in results_path.read_text().splitlines()]
    assert rows[0] == {
        "type": "manifest",
        "run_id": "synthetic-v1",
        "cases": len(cases),
        "passed": len(cases),
    }
    assert len(rows) == len(cases) + 1
    assert "10/10 cases passed" in dashboard_path.read_text()


def test_loader_rejects_duplicate_case_ids(tmp_path) -> None:
    case = {
        "id": "duplicate",
        "transcript": "hold",
        "relay_state": {},
        "context": {"capability_version": "v1", "rooms": [], "now_ms": 0},
        "expected": {"kind": "refuse", "reason": "stale_state"},
    }
    path = tmp_path / "corpus.jsonl"
    path.write_text("\n".join(json.dumps(case) for _ in range(2)), encoding="utf-8")

    with pytest.raises(ValueError, match="unique"):
        load_corpus(path)


@pytest.mark.parametrize(
    "expected",
    [
        {"kind": "plan", "intents": []},
        {"kind": "refuse", "reason": "stale_state", "intents": [{}]},
        {"kind": "unknown", "reason": "stale_state"},
    ],
)
def test_loader_rejects_malformed_expectations(tmp_path, expected) -> None:
    path = tmp_path / "corpus.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "case-1",
                "transcript": "hold",
                "relay_state": {},
                "context": {
                    "capability_version": "v1",
                    "rooms": [],
                    "now_ms": 0,
                },
                "expected": expected,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_corpus(path)

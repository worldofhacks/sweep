from __future__ import annotations

import gc
import hashlib
import json
import weakref
from dataclasses import replace

import pytest

from evals.language_corpus import (
    DEFAULT_CORPUS_PATH,
    LEGACY_CORPUS_PATH,
    LoadedCorpus,
    StaticResponseTransport,
    append_jsonl_run,
    evaluate_case,
    load_corpus,
    load_synthetic_responses,
    write_dashboard,
)
from language.contracts import build_grounding_facts
from language.transport import (
    PROMPT_SCHEMA_VERSION,
    AnthropicTransport,
    ModelRequest,
    ModelResponse,
    RecordingTransport,
    ReplayTransport,
)
from planner.models import TranslationGrounding, TranslationPolicy


def test_synthetic_corpus_runs_through_recorded_production_requests(tmp_path) -> None:
    cases = load_corpus()
    responses = load_synthetic_responses(corpus=cases)
    assert len(cases) == 50
    cassette = tmp_path / "language-replay.json"
    for case in cases:
        facts = build_grounding_facts(
            case.relay_state,
            capability_version=case.capability_version,
            rooms=case.rooms,
            translation=(
                None
                if case.translation_frame is None
                else TranslationGrounding(
                    policy=TranslationPolicy(
                        frame=case.translation_frame,
                        step_m=case.translation_step_m,
                    ),
                    headings={
                        drone["drone_id"]: drone["heading_deg"]
                        for drone in case.relay_state["drones"]
                        if "heading_deg" in drone
                    },
                )
            ),
            qualified_voice_intents=case.qualified_voice_intents,
        )
        request = ModelRequest(transcript=case.transcript, facts=facts.model_dict())
        RecordingTransport(StaticResponseTransport(responses[case.case_id]), cassette).complete(
            request
        )
    replay = ReplayTransport(cassette)
    results = [evaluate_case(case, replay) for case in cases]

    failures = [result for result in results if not result.passed]
    assert failures == []

    results_path = tmp_path / "results.jsonl"
    dashboard_path = tmp_path / "dashboard.html"
    append_jsonl_run(results, results_path, run_id="synthetic-v1", corpus=cases)
    write_dashboard(results, dashboard_path, run_id="synthetic-v1", corpus=cases)
    rows = [json.loads(line) for line in results_path.read_text().splitlines()]
    assert rows[0]["type"] == "manifest"
    assert rows[0]["run_id"] == "synthetic-v1"
    assert rows[0]["cases"] == len(cases)
    assert rows[0]["passed"] == len(cases)
    assert rows[0]["case_ids"] == [case.case_id for case in cases]
    template_results = [result for result in results if result.source == "template"]
    assert all(result.actual_reason == "stale_state" for result in template_results)
    assert rows[0]["response_sources"] == sorted({result.source for result in results})
    assert rows[0]["response_origins"] == sorted({result.origin for result in results})
    assert rows[0]["models"]
    assert rows[0]["prompt_schema_versions"]
    assert len(rows[0]["corpus_digest"]) == 64
    selected_corpus = DEFAULT_CORPUS_PATH if DEFAULT_CORPUS_PATH.exists() else LEGACY_CORPUS_PATH
    assert rows[0]["corpus_digest"] == hashlib.sha256(selected_corpus.read_bytes()).hexdigest()
    assert len(rows[0]["cassette_digests"]) == 1
    assert {row["origin"] for row in rows[1:]} <= {"unverified_replay", "template"}
    assert {row["source"] for row in rows[1:]} <= {"replay", "template"}
    assert len(rows) == len(cases) + 1
    assert f"{len(cases)}/{len(cases)} cases passed" in dashboard_path.read_text()


def test_loader_and_eval_support_reviewed_grounding_contract(tmp_path) -> None:
    case = {
        "id": "qualified-estop",
        "transcript": "Emergency stop.",
        "relay_state": {
            "type": "state",
            "t": 100,
            "mode": "indoor",
            "roster_version": 1,
            "armed": True,
            "estop": False,
            "selection": [1],
            "pending": {"intent_id": "pending-1", "name": "takeoff"},
            "drones": [
                {
                    "drone_id": 1,
                    "membership": "ready",
                    "selectable": True,
                    "flight_state": "hovering",
                    "heading_deg": 90.0,
                    "camera_patterns": ["pano_360"],
                    "adapter_capabilities": ["flight", "pano_360"],
                }
            ],
        },
        "context": {
            "capability_version": "sim-v1",
            "rooms": ["living-room"],
            "now_ms": 100,
            "translation": {"frame": "aircraft_relative", "step_m": 0.5},
            "qualified_voice_intents": ["estop"],
        },
        "expected": {
            "kind": "plan",
            "intents": [{"name": "estop", "args": {}, "selection": [], "mode": "indoor"}],
        },
        "category": "qualified_estop",
        "live_demo": False,
    }
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(case) + "\n", encoding="utf-8")

    loaded = load_corpus(path)
    result = evaluate_case(loaded[0], StaticResponseTransport(case["expected"]))

    assert loaded[0].relay_state["session"] == "language-eval"
    assert loaded[0].translation_frame == "aircraft_relative"
    assert loaded[0].translation_step_m == 0.5
    assert loaded[0].qualified_voice_intents == ("estop",)
    assert result.passed


def test_loaded_corpus_is_deeply_immutable() -> None:
    case = load_corpus()[0]

    with pytest.raises(TypeError):
        case.relay_state["armed"] = False
    with pytest.raises(TypeError):
        case.relay_state["drones"][0]["selectable"] = False
    with pytest.raises(TypeError):
        case.expected["kind"] = "refuse"
    with pytest.raises(TypeError):
        dict.__setitem__(case.expected, "kind", "refuse")
    with pytest.raises(AttributeError):
        case.expected._values = {"kind": "refuse"}
    assert case.expected["kind"] != "refuse"


def test_manifest_rejects_replaced_semantic_result(tmp_path) -> None:
    cases = load_corpus()
    responses = load_synthetic_responses(corpus=cases)
    results = [
        evaluate_case(case, StaticResponseTransport(responses[case.case_id])) for case in cases
    ]
    first = results[0]
    tampered = replace(
        first,
        passed=True,
        actual_intents=({"name": "estop", "args": {}, "selection": [], "mode": "indoor"},),
    )
    results[0] = tampered
    output = tmp_path / "results.jsonl"

    with pytest.raises(ValueError, match="evaluation"):
        append_jsonl_run(results, output, run_id="tampered", corpus=cases)

    assert not output.exists()


def test_default_corpus_is_pinned_to_the_reviewed_50_case_release(tmp_path) -> None:
    selected = DEFAULT_CORPUS_PATH
    truncated = tmp_path / selected.name
    truncated.write_text("\n".join(selected.read_text().splitlines()[:-1]) + "\n")

    loaded = load_corpus(truncated)
    assert not loaded.reviewed
    responses = load_synthetic_responses(corpus=load_corpus())
    result = evaluate_case(
        load_corpus()[0], StaticResponseTransport(next(iter(responses.values())))
    )
    with pytest.raises(ValueError, match="reviewed loaded corpus"):
        append_jsonl_run([result], tmp_path / "result.jsonl", run_id="truncated", corpus=loaded)


def test_eval_compares_clarification_detail(tmp_path) -> None:
    case = {
        "id": "clarify-alternative",
        "transcript": "Take a panorama.",
        "relay_state": {
            "type": "state",
            "t": 100,
            "mode": "indoor",
            "roster_version": 1,
            "armed": True,
            "estop": False,
            "selection": [1],
            "drones": [
                {
                    "drone_id": 1,
                    "membership": "ready",
                    "selectable": True,
                    "flight_state": "hovering",
                    "camera_patterns": ["reconstruct_8"],
                    "adapter_capabilities": ["flight", "reconstruct_8"],
                }
            ],
        },
        "context": {"capability_version": "sim-v1", "rooms": ["living-room"], "now_ms": 100},
        "expected": {
            "kind": "clarify",
            "reason": "capability_unavailable",
            "detail": "Use reconstruct_8?",
        },
    }
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(case) + "\n", encoding="utf-8")
    loaded = load_corpus(path)

    wrong = evaluate_case(
        loaded[0],
        StaticResponseTransport(
            {"kind": "clarify", "reason": "capability_unavailable", "detail": "Different detail"}
        ),
    )

    assert not wrong.passed


def test_manifest_rejects_results_from_different_corpus_bytes(tmp_path) -> None:
    selected = DEFAULT_CORPUS_PATH if DEFAULT_CORPUS_PATH.exists() else LEGACY_CORPUS_PATH
    alternate = tmp_path / selected.name
    alternate.write_bytes(selected.read_bytes() + b" \n")
    original_case = load_corpus(selected)[0]
    alternate_case = load_corpus(alternate)[0]
    cases = load_corpus(selected)
    alternate_cases = load_corpus(alternate)
    responses = load_synthetic_responses(corpus=cases)
    results = [
        evaluate_case(case, StaticResponseTransport(responses[case.case_id])) for case in cases
    ]

    assert original_case.case_id == alternate_case.case_id
    with pytest.raises(ValueError, match="loaded corpus"):
        append_jsonl_run(
            results,
            tmp_path / "results.jsonl",
            run_id="mixed",
            corpus=alternate_cases,
        )


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra", "reordered"])
def test_manifest_requires_exact_ordered_corpus_coverage(tmp_path, mutation) -> None:
    cases = load_corpus()
    responses = load_synthetic_responses(corpus=cases)
    results = [
        evaluate_case(case, StaticResponseTransport(responses[case.case_id])) for case in cases
    ]
    if mutation == "missing":
        results = results[:-1]
    elif mutation == "duplicate":
        results = [results[0], *results]
    elif mutation == "extra":
        results = [*results, replace(results[0], case_id="extra")]
    else:
        results = list(reversed(results))

    with pytest.raises(ValueError, match="exactly once in corpus order"):
        append_jsonl_run(
            results,
            tmp_path / "results.jsonl",
            run_id=mutation,
            corpus=cases,
        )


def test_manifest_rejects_a_sliced_corpus_with_the_full_digest(tmp_path) -> None:
    cases = load_corpus()
    responses = load_synthetic_responses(corpus=cases)
    result = evaluate_case(cases[0], StaticResponseTransport(responses[cases[0].case_id]))

    with pytest.raises(ValueError, match="requires a loaded corpus"):
        append_jsonl_run(
            [result],
            tmp_path / "results.jsonl",
            run_id="subset",
            corpus=cases.cases[:1],
        )


@pytest.mark.parametrize("mutation", ["missing", "extra", "reordered"])
def test_synthetic_response_ids_must_match_corpus_order(tmp_path, mutation) -> None:
    cases = load_corpus()
    responses = load_synthetic_responses(corpus=cases)
    items = list(responses.items())
    if mutation == "missing":
        items = items[:-1]
    elif mutation == "extra":
        items.append(("extra", {"kind": "refuse", "reason": "unknown_reference"}))
    else:
        items.reverse()
    path = tmp_path / "responses.json"
    path.write_text(json.dumps({"version": 1, "responses": dict(items)}), encoding="utf-8")

    with pytest.raises(ValueError, match="exactly match corpus order"):
        load_synthetic_responses(path, corpus=cases)


def test_synthetic_provider_response_does_not_repair_model_supplied_capture_id(tmp_path) -> None:
    cases = load_corpus()
    source = load_synthetic_responses(corpus=cases)
    capture = next(
        case
        for case in cases
        if any(intent["name"] == "capture_room" for intent in case.expected.get("intents", []))
    )
    raw = {"version": 1, "responses": dict(source)}
    payload = json.loads(json.dumps(raw["responses"][capture.case_id]))
    payload["intents"][0]["args"]["capture_id"] = "model-owned-id"
    raw["responses"][capture.case_id] = payload
    path = tmp_path / "responses.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    responses = load_synthetic_responses(path, corpus=cases)
    result = evaluate_case(capture, StaticResponseTransport(responses[capture.case_id]))

    assert not result.passed
    assert result.actual_reason == "invalid_model_output"


def test_generated_capture_id_matches_exact_deterministic_host_value() -> None:
    cases = load_corpus()
    responses = load_synthetic_responses(corpus=cases)
    capture = next(
        case
        for case in cases
        if any(intent["name"] == "capture_room" for intent in case.expected.get("intents", []))
    )
    result = evaluate_case(
        capture,
        StaticResponseTransport(responses[capture.case_id]),
    )

    assert result.passed


def test_generated_capture_id_requires_exact_host_uuid(monkeypatch) -> None:
    cases = load_corpus()
    responses = load_synthetic_responses(corpus=cases)
    capture = next(
        case
        for case in cases
        if any(intent["name"] == "capture_room" for intent in case.expected.get("intents", []))
    )
    monkeypatch.setattr(
        "language.compiler._capture_id", lambda _correlation_id, _index: "capture-" + "z" * 32
    )

    result = evaluate_case(
        capture,
        StaticResponseTransport(responses[capture.case_id]),
    )

    assert not result.passed


def test_arbitrary_capture_gold_is_not_treated_as_host_minted() -> None:
    cases = load_corpus()
    responses = load_synthetic_responses(corpus=cases)
    capture = next(case for case in cases if case.case_id == "capture-explicit-living-room")
    altered = replace(
        capture,
        expected={
            "kind": "plan",
            "intents": [
                {
                    "name": "capture_room",
                    "args": {
                        "room_id": "living-room",
                        "capture_id": "capture-wrong-but-prefix",
                        "pattern": "pano_360",
                    },
                    "selection": [1],
                    "mode": "indoor",
                }
            ],
        },
    )

    result = evaluate_case(altered, StaticResponseTransport(responses[capture.case_id]))

    assert not result.passed


def test_manifest_rejects_relabelled_result_provenance(tmp_path) -> None:
    cases = load_corpus()
    responses = load_synthetic_responses(corpus=cases)
    results = [
        evaluate_case(case, StaticResponseTransport(responses[case.case_id])) for case in cases
    ]
    results[0] = replace(results[0], source="anthropic", origin="anthropic")

    with pytest.raises(ValueError, match="evaluation"):
        append_jsonl_run(results, tmp_path / "results.jsonl", run_id="relabelled", corpus=cases)

    assert not (tmp_path / "results.jsonl").exists()


def test_dashboard_rejects_replaced_results(tmp_path) -> None:
    cases = load_corpus()
    responses = load_synthetic_responses(corpus=cases)
    results = [
        evaluate_case(case, StaticResponseTransport(responses[case.case_id])) for case in cases
    ]
    results[0] = replace(results[0], passed=True, actual_kind="refuse")
    output = tmp_path / "dashboard.html"

    with pytest.raises(ValueError, match="evaluation"):
        write_dashboard(results, output, run_id="tampered", corpus=cases)

    assert not output.exists()


def test_manifest_rejects_forged_reviewed_corpus(tmp_path) -> None:
    corpus = load_corpus()
    responses = load_synthetic_responses(corpus=corpus)
    forged_case = replace(corpus[0], transcript="different reviewed input")
    forged = LoadedCorpus(
        cases=(forged_case, *corpus.cases[1:]),
        digest=corpus.digest,
        reviewed=True,
    )
    results = [
        evaluate_case(case, StaticResponseTransport(responses[case.case_id])) for case in forged
    ]

    with pytest.raises(ValueError, match="reviewed loaded corpus"):
        append_jsonl_run(results, tmp_path / "forged.jsonl", run_id="forged", corpus=forged)


def test_eval_rejects_transport_that_self_attests_anthropic_provenance() -> None:
    corpus = load_corpus()
    responses = load_synthetic_responses(corpus=corpus)

    class SpoofTransport:
        def complete(self, _request):
            return ModelResponse(
                payload=responses[corpus[0].case_id],
                source="anthropic",
                origin="anthropic",
                model="claude-sonnet-5",
                prompt_schema_version=PROMPT_SCHEMA_VERSION,
            )

    result = evaluate_case(corpus[0], SpoofTransport())

    assert (result.source, result.origin) == ("template", "template")


@pytest.mark.parametrize("record", [False, True])
def test_eval_does_not_trust_rebound_anthropic_transport(monkeypatch, tmp_path, record) -> None:
    corpus = load_corpus()
    responses = load_synthetic_responses(corpus=corpus)
    transport = AnthropicTransport(api_key="unused")
    monkeypatch.setattr(
        transport,
        "complete",
        lambda _request: ModelResponse(
            payload=responses[corpus[0].case_id],
            source="anthropic",
            origin="anthropic",
            model="claude-sonnet-5",
            prompt_schema_version=PROMPT_SCHEMA_VERSION,
        ),
    )

    if record:
        transport = RecordingTransport(transport, tmp_path / "forged.json")
    result = evaluate_case(corpus[0], transport)

    assert (result.source, result.origin) == ("template", "template")


def test_reviewed_corpus_registry_does_not_retain_discarded_loads() -> None:
    corpus = load_corpus()
    reference = weakref.ref(corpus)

    del corpus
    gc.collect()

    assert reference() is None


@pytest.mark.parametrize("writer", ["jsonl", "dashboard"])
def test_eval_writers_materialize_stateful_results_once(tmp_path, writer) -> None:
    corpus = load_corpus()
    responses = load_synthetic_responses(corpus=corpus)
    results = tuple(
        evaluate_case(case, StaticResponseTransport(responses[case.case_id])) for case in corpus
    )

    class StatefulResults:
        def __init__(self):
            self.iterations = 0

        def __len__(self):
            return len(results)

        def __getitem__(self, index):
            if isinstance(index, slice):
                return results[index]
            return results[index]

        def __iter__(self):
            self.iterations += 1
            return iter(results if self.iterations == 1 else tuple(reversed(results)))

    stateful = StatefulResults()
    output = tmp_path / ("results.jsonl" if writer == "jsonl" else "dashboard.html")
    if writer == "jsonl":
        append_jsonl_run(stateful, output, run_id="one-pass", corpus=corpus)
    else:
        write_dashboard(stateful, output, run_id="one-pass", corpus=corpus)

    assert stateful.iterations == 1
    assert output.exists()


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


def test_live_recording_grades_exact_responses_and_preserves_artifact_digests(
    tmp_path, monkeypatch
) -> None:
    corpus = load_corpus()
    responses = load_synthetic_responses(corpus=corpus)
    requested = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "model": "claude-sonnet-5",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "submit_compiler_outcome",
                        "input": self.payload,
                    }
                ],
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }

    def post(_url, **kwargs):
        body = json.loads(kwargs["json"]["messages"][0]["content"])
        requested.append(body)
        return Response(responses[case.case_id])

    monkeypatch.setattr("language.transport.httpx.post", post)
    cassette = tmp_path / "live.json"
    transport = RecordingTransport(AnthropicTransport(api_key="test-key"), cassette)
    results = []
    for case in corpus:
        results.append(evaluate_case(case, transport))
    artifact = tmp_path / "live-results.jsonl"
    append_jsonl_run(results, artifact, run_id="live", corpus=corpus)
    write_dashboard(results, tmp_path / "live.html", run_id="live", corpus=corpus)
    rows = [json.loads(line) for line in artifact.read_text().splitlines()]
    provider_results = [result for result in results if result.source == "anthropic"]
    assert provider_results
    assert len(requested) == len(provider_results)
    assert rows[0]["passed"] == len(corpus)
    assert all(result.origin == "anthropic" for result in provider_results)
    assert rows[0]["cassette_digests"] == sorted(
        {result.cassette_digest for result in provider_results}
    )
    for result, request in zip(provider_results, requested, strict=True):
        snapshot = tmp_path / "live.json.snapshots" / f"{result.cassette_digest}.json"
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == result.cassette_digest
        replayed = ReplayTransport(snapshot).complete(
            ModelRequest(
                transcript=request["operator_transcript"],
                facts=request["authoritative_facts"],
            )
        )
        assert replayed.payload == responses[result.case_id]
        assert result.input_units == 7
        assert result.output_units == 3
        assert replayed.origin == "unverified_replay"

from __future__ import annotations

import pytest

from language.compiler import InMemoryAuditSink, TranscriptCompiler
from language.contracts import (
    CompilerReason,
    OutcomeKind,
    ReviewCatalog,
    ReviewDestination,
    ReviewKind,
    validate_model_outcome,
)
from language.relay_compiler import voice_plan_from_outcome
from language.test_provider_contract import _facts
from language.transport import (
    PINNED_COMPILER_MODEL,
    PROMPT_SCHEMA_VERSION,
    ModelRequest,
    ModelResponse,
    _anthropic_body,
)
from relay.voice import parse_voice_plan


class CapturingTransport:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            payload=self.payload,
            source="synthetic",
            origin="synthetic",
            model=PINNED_COMPILER_MODEL,
            prompt_schema_version=PROMPT_SCHEMA_VERSION,
        )


def _state() -> dict[str, object]:
    return {
        "v": 1,
        "type": "state",
        "mode": "indoor",
        "session": "provider-contract",
        "event_id": "provider-state",
        "t": 1_000,
        "roster_version": 1,
        "armed": True,
        "estop": False,
        "selection": [1, 2],
        "drones": [
            {
                "drone_id": drone_id,
                "membership": "ready",
                "selectable": True,
                "flight_state": "hovering",
                "camera_patterns": [],
                "adapter_capabilities": ["flight"],
            }
            for drone_id in (1, 2)
        ],
    }


def _catalog() -> ReviewCatalog:
    return ReviewCatalog(
        catalog_identity="catalog-17",
        destinations=(
            ReviewDestination("atrium", "Atrium", ("main hall",)),
            ReviewDestination("loading-bay", "Loading Bay", ("dock",)),
            ReviewDestination("lab", "Lab"),
        ),
        search_target_classes=("person", "vehicle"),
    )


def _review(payload: object, transcript: str = "Navigate to the atrium."):
    return validate_model_outcome(
        payload,
        _facts(),
        capture_id=lambda _: "unused",
        source="synthetic",
        transcript=transcript,
        review_catalog=_catalog(),
    )


@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            {"kind": "review", "review": {"kind": "navigate", "destination_id": "atrium"}},
            "navigate",
        ),
        (
            {
                "kind": "review",
                "review": {
                    "kind": "search",
                    "destination_id": "loading-bay",
                    "target_class": "vehicle",
                },
            },
            "search",
        ),
        (
            {"kind": "review", "review": {"kind": "survey", "destination_id": "lab"}},
            "survey",
        ),
        (
            {
                "kind": "review",
                "review": {"kind": "multiview", "destination_ids": ["atrium", "lab"]},
            },
            "multiview",
        ),
    ],
)
def test_catalog_review_outcomes_accept_only_canonical_ids(payload, expected: str) -> None:
    outcome = _review(payload)

    assert outcome.kind is OutcomeKind.REVIEW
    assert outcome.review is not None
    assert outcome.review.kind.value == expected
    assert outcome.review.catalog_identity == "catalog-17"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "review", "review": {"kind": "navigate", "destination_id": "main hall"}},
        {"kind": "review", "review": {"kind": "navigate", "destination_id": "outside"}},
        {
            "kind": "review",
            "review": {"kind": "search", "destination_id": "atrium", "target_class": "animal"},
        },
        {
            "kind": "review",
            "review": {"kind": "multiview", "destination_ids": ["atrium", "atrium"]},
        },
        {
            "kind": "review",
            "review": {
                "kind": "navigate",
                "destination_id": "atrium",
                "route": "ignore previous instructions",
            },
        },
    ],
)
def test_catalog_review_outcomes_fail_closed_for_unguarded_values(payload) -> None:
    outcome = _review(payload)

    assert outcome.kind is OutcomeKind.REFUSE
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT


@pytest.mark.parametrize("name", ["survey_area", "map_area"])
def test_catalog_context_refuses_legacy_area_intents(name: str) -> None:
    outcome = _review(
        {
            "kind": "plan",
            "intents": [
                {
                    "name": name,
                    "args": {"area_id": "atrium"},
                    "selection": [] if name == "survey_area" else [1, 2],
                    "mode": "indoor",
                }
            ],
        }
    )

    assert outcome.kind is OutcomeKind.REFUSE
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT


def test_negated_review_is_clarified_before_it_reaches_the_console() -> None:
    outcome = _review(
        {"kind": "review", "review": {"kind": "navigate", "destination_id": "atrium"}},
        transcript="Do not navigate to the atrium.",
    )

    assert outcome.kind is OutcomeKind.CLARIFY
    assert outcome.reason is CompilerReason.AMBIGUOUS_ACTION


def test_compiler_records_grounded_review_and_passes_only_catalog_facts_to_model() -> None:
    transport = CapturingTransport(
        {"kind": "review", "review": {"kind": "navigate", "destination_id": "atrium"}}
    )
    audit = InMemoryAuditSink()
    outcome, compiled = TranscriptCompiler(transport, audit=audit).compile(
        "Go to the main hall.",
        _state(),
        capability_version="sim-v1",
        review_catalog=_catalog(),
        now_ms=1_000,
        correlation_id="semantic-review",
    )

    assert outcome.kind is OutcomeKind.REVIEW
    assert compiled is None
    assert transport.requests[0].facts["review_catalog"] == _catalog().model_dict()
    assert audit.records[-1]["review"] == {
        "kind": "navigate",
        "catalog_identity": "catalog-17",
        "destination_id": "atrium",
    }


def test_review_preview_is_display_only_and_round_trips_on_the_voice_wire() -> None:
    outcome = _review(
        {
            "kind": "review",
            "review": {
                "kind": "search",
                "destination_id": "loading-bay",
                "target_class": "vehicle",
            },
        }
    )
    plan = voice_plan_from_outcome(
        outcome,
        None,
        transcript="Search the dock for a vehicle.",
        relay_state={"event_id": "event-4", "roster_version": 2},
        rooms=(),
        review_catalog=_catalog(),
        now_ms=1_000,
        correlation_id="semantic-review",
        session_id="provider-contract",
    )

    assert plan.kind == "review"
    assert plan.steps == ()
    assert plan.plan_digest is None
    assert parse_voice_plan(plan.to_dict()) == plan


def test_provider_schema_separates_review_from_intent_v1_commands() -> None:
    body = _anthropic_body(ModelRequest(transcript="Go to the atrium", facts={}))
    schema = body["tools"][0]["input_schema"]["properties"]

    assert "review" in schema["kind"]["enum"]
    assert set(schema["review"]["properties"]["kind"]["enum"]) == {
        item.value for item in ReviewKind
    }
    assert "navigate" not in schema["intents"]["items"]["properties"]["name"]["enum"]


def test_review_catalog_rejects_unsafe_identity_and_duplicate_destination_ids() -> None:
    with pytest.raises(ValueError, match="safe identifier"):
        ReviewCatalog("catalog with spaces", (ReviewDestination("atrium", "Atrium"),))
    with pytest.raises(ValueError, match="destinations"):
        ReviewCatalog(
            "catalog-17",
            (ReviewDestination("atrium", "Atrium"), ReviewDestination("atrium", "Second Atrium")),
        )

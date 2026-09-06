from __future__ import annotations

import pytest

from language.contracts import (
    CompilerReason,
    OutcomeKind,
    build_grounding_facts,
    validate_model_outcome,
)
from language.transport import ModelRequest, _anthropic_body


def _facts():
    return build_grounding_facts(
        {
            "v": 1,
            "type": "state",
            "mode": "indoor",
            "session": "provider-contract",
            "event_id": "provider-state",
            "t": 1000,
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
        },
        capability_version="sim-v1",
    )


def test_recorded_provider_hold_with_detail_passes_runtime_validation() -> None:
    payload = {
        "detail": (
            "Operator said 'Stop.' interpreted as hold command for "
            'currently selected drones (1,2)."'
        ),
        "intents": [{"args": {}, "mode": "indoor", "name": "hold", "selection": [1, 2]}],
        "kind": "plan",
    }
    outcome = validate_model_outcome(
        payload, _facts(), capture_id=lambda _: "unused", source="synthetic", transcript="Stop."
    )
    assert outcome.kind is OutcomeKind.PLAN
    assert outcome.detail == payload["detail"]
    assert outcome.intents[0].name.value == "hold"
    assert outcome.intents[0].selection == (1, 2)


@pytest.mark.parametrize("detail", [500 * "x", ""])
def test_plan_detail_accepts_only_bounded_text(detail: str) -> None:
    outcome = validate_model_outcome(
        {
            "kind": "plan",
            "detail": detail,
            "intents": [{"name": "land", "args": {}, "selection": [1, 2], "mode": "indoor"}],
        },
        _facts(),
        capture_id=lambda _: "unused",
        source="synthetic",
        transcript="Land.",
    )
    assert outcome.kind is OutcomeKind.PLAN


@pytest.mark.parametrize("detail", [501 * "x", 3, {}])
def test_invalid_plan_detail_remains_fail_closed(detail: object) -> None:
    outcome = validate_model_outcome(
        {
            "kind": "plan",
            "detail": detail,
            "intents": [{"name": "hold", "args": {}, "selection": [1, 2], "mode": "indoor"}],
        },
        _facts(),
        capture_id=lambda _: "unused",
        source="synthetic",
        transcript="Stop.",
    )
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT


def test_provider_reason_enum_matches_runtime_contract_and_rejects_recorded_free_text() -> None:
    body = _anthropic_body(ModelRequest(transcript="Select by the door", facts={}))
    reasons = body["tools"][0]["input_schema"]["properties"]["reason"]["enum"]
    assert reasons == [reason.value for reason in CompilerReason]
    for reason in reasons:
        outcome = validate_model_outcome(
            {"kind": "clarify", "reason": reason},
            _facts(),
            capture_id=lambda _: "unused",
            source="synthetic",
            transcript="Select by the door",
        )
        assert outcome.reason.value == reason
    outcome = validate_model_outcome(
        {
            "kind": "clarify",
            "reason": (
                "Cannot determine which drone is 'by the door'; "
                "no positional data links drones to that location."
            ),
        },
        _facts(),
        capture_id=lambda _: "unused",
        source="synthetic",
        transcript="Select by the door",
    )
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT


def test_provider_instruction_supplies_selection_and_motion_contracts() -> None:
    system = _anthropic_body(ModelRequest(transcript="Hold", facts={}))["system"]
    for contract in (
        "selection must equal args.ids",
        "selection: []",
        "first emit select",
        "LAND means land the selected aircraft",
        "divide by step_m",
        "Do not rotate it yourself",
        "takeoff, land, hold, translate, altitude, come_home",
        "altitude and spacing={delta}",
        "hover at a height",
        "Plain hover means hold",
        "capture_room={room_id,pattern}",
        "explanations in detail, never in reason",
    ):
        assert contract in system


@pytest.mark.parametrize(
    "intent",
    [
        {"name": "select", "args": {}, "selection": [1], "mode": "indoor"},
        {"name": "land_all", "args": {}, "selection": [1, 2], "mode": "indoor"},
        {"name": "land", "args": {}, "selection": [1], "mode": "indoor"},
    ],
)
def test_plan_detail_does_not_allow_ungrounded_provider_intents(intent) -> None:
    outcome = validate_model_outcome(
        {"kind": "plan", "detail": "Operator requested this action.", "intents": [intent]},
        _facts(),
        capture_id=lambda _: "unused",
        source="synthetic",
        transcript="Land.",
    )
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT

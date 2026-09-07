"""The shared schema is registered without expanding executable capability profiles."""

import pytest

from language.contracts import (
    CompilerReason,
    OutcomeKind,
    build_grounding_facts,
    validate_model_outcome,
)
from language.transport import ModelRequest, _anthropic_body
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.capabilities import C1_CAPABILITY_PROFILE, C2_CAPABILITY_PROFILE, IntentName
from relay.intent_v1 import RejectedIntent, RejectionReason, validate_intent
from relay.session import CapabilityBoundIntentSink, RelayLimits, RelaySession


def payload(**changes):
    return {
        "v": 1,
        "type": "intent",
        "t": 1000,
        "intent_id": "navigation-request",
        "session": "navigation-test",
        "source": "console",
        "name": "navigate",
        "args": {"zone_id": "room-a"},
        "selection": [1],
        "mode": "indoor",
        "confirm": True,
        **changes,
    }


@pytest.mark.parametrize("profile", [C1_CAPABILITY_PROFILE, C2_CAPABILITY_PROFILE])
@pytest.mark.parametrize("source", ["console", "language", "webcam", "keyboard"])
def test_navigation_schema_is_known_but_never_widens_a_motion_profile(profile, source):
    result = validate_intent(payload(source=source), capability_profile=profile)
    assert IntentName.NAVIGATE.value == "navigate"
    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.UNSUPPORTED
    assert "navigate" not in profile.state_value()["enabled_intent_names"]


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"zone_id": ""},
        {"zone_id": "Room A"},
        {"zone_id": "room-a", "x": 1},
        {"x": 1, "y": 2},
        {"zone_id": "room-a", "route": []},
        {"zone_id": True},
        {"zone_id": "x" * 129},
        {"room_id": "room-a"},
    ],
)
def test_shared_navigation_arguments_accept_only_a_canonical_identity(args):
    result = validate_intent(payload(args=args))
    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


@pytest.mark.parametrize("changes", [{"confirm": False}, {"selection": []}])
def test_navigation_schema_requires_explicit_confirmation_and_selection(changes):
    result = validate_intent(payload(**changes))
    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


@pytest.mark.parametrize("selection", [[1], [11], [1, 11]])
@pytest.mark.parametrize("profile", [C1_CAPABILITY_PROFILE, C2_CAPABILITY_PROFILE])
def test_production_router_cannot_bypass_review_into_any_class_or_capture(
    tmp_path, selection, profile
):
    delivered = []
    session = RelaySession(
        session_id="navigation-test",
        audit_log=SessionAuditLog(tmp_path, "navigation-test"),
        limits=RelayLimits(5000, 5000, 1000, 1000),
        clock=lambda: 1000,
        capability_profile=profile,
        intent_sink=CapabilityBoundIntentSink(
            lambda intent, state: delivered.append(intent), profile
        ),
    )
    events = session.process_frame(
        payload(selection=selection), Principal("console", None, b"x" * 32)
    )
    assert events[0]["status"] == "refused"
    assert events[0]["reason"] == "unsupported"
    assert delivered == []
    state = session.current_state()
    assert state["pending"] is None
    assert state["accepted_plan"] is None


@pytest.mark.parametrize("selection", [[1], [11], [1, 11]])
def test_generic_language_plan_cannot_substitute_for_frozen_navigation_review(selection):
    facts = build_grounding_facts(
        {
            "v": 1,
            "type": "state",
            "mode": "indoor",
            "session": "navigation-test",
            "event_id": "state-1",
            "t": 1000,
            "roster_version": 1,
            "armed": True,
            "estop": False,
            "selection": selection,
            "drones": [
                {
                    "drone_id": device_id,
                    "membership": "ready",
                    "selectable": True,
                    "flight_state": "hovering",
                    "camera_patterns": [],
                    "adapter_capabilities": ["flight"],
                }
                for device_id in selection
            ],
        },
        capability_version="test-capability",
    )
    captures = []
    outcome = validate_model_outcome(
        {
            "kind": "plan",
            "intents": [
                {
                    "name": "navigate",
                    "args": {"zone_id": "room-a"},
                    "selection": selection,
                    "mode": "indoor",
                },
                {
                    "name": "capture_room",
                    "args": {"room_id": "room-a", "pattern": "pano_360"},
                    "selection": selection,
                    "mode": "indoor",
                },
            ],
        },
        facts,
        capture_id=lambda index: captures.append(index),
        source="synthetic",
        transcript="Room A",
    )
    assert outcome.kind is OutcomeKind.UNSUPPORTED
    assert outcome.reason is CompilerReason.CAPABILITY_UNAVAILABLE
    assert outcome.intents == ()
    assert captures == []


def test_provider_schema_exposes_the_exact_shared_identity_without_execution_permission():
    body = _anthropic_body(ModelRequest(transcript="Room A", facts={}))
    intent_schema = body["tools"][0]["input_schema"]["properties"]["intents"]["items"]
    assert "navigate" in intent_schema["properties"]["name"]["enum"]
    zone_schema = intent_schema["properties"]["args"]["properties"]["zone_id"]
    assert zone_schema["type"] == "string"
    assert "minLength: 1" in zone_schema["description"]
    assert "maxLength: 128" in zone_schema["description"]
    assert "never implies takeoff, capture, survey" in body["system"]

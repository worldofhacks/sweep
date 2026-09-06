from __future__ import annotations

import pytest

from evals.language_corpus import StaticResponseTransport
from language.contracts import CompilerReason, OutcomeKind
from language.transport import ModelResponse, TransportError
from relay.capabilities import C1_CAPABILITY_PROFILE
from relay.language_runtime import LanguageRuntime
from tests.autonomy_fixtures import make_snapshot, planning_config


class RecordingTransport(StaticResponseTransport):
    def __init__(self, payload: object) -> None:
        super().__init__(payload)
        self.requests: list[object] = []

    def complete(self, request: object) -> ModelResponse:
        self.requests.append(request)
        return super().complete(request)


class FailingTransport:
    def complete(self, _request: object) -> ModelResponse:
        raise TransportError("offline")


def _grounding(snapshot):
    config = planning_config()
    return config, config.translation_grounding(snapshot)


def test_runtime_invokes_grounded_compiler_and_returns_stageable_intents():
    snapshot = make_snapshot(count=1, selection=(1,))
    config, translation = _grounding(snapshot)
    transport = RecordingTransport(
        {
            "kind": "plan",
            "intents": [{"name": "hold", "args": {}, "selection": [1], "mode": "indoor"}],
        }
    )

    outcome = LanguageRuntime(transport).compile(
        "hold the selected aircraft",
        snapshot,
        None,
        (),
        session_id="language-session",
        state_event_id="state-event-1",
        capability_profile=C1_CAPABILITY_PROFILE,
        translation=translation,
        altitude_grounding=config.altitude_grounding(),
        correlation_id="language-request-1",
    )

    assert outcome.kind is OutcomeKind.PLAN
    assert len(transport.requests) == 1
    assert transport.requests[0].facts["session"] == "language-session"
    assert transport.requests[0].facts["selection"] == [1]
    assert transport.requests[0].facts["translation"] == {"frame": "world", "step_m": 0.5}
    assert outcome.intents[0]["name"] == "hold"
    assert outcome.intents[0]["source"] == "console"
    assert outcome.to_dict()["state_digest"] is not None


def test_runtime_refuses_when_the_model_is_unavailable():
    snapshot = make_snapshot(count=1, selection=(1,))
    config, translation = _grounding(snapshot)

    outcome = LanguageRuntime(FailingTransport()).compile(
        "hold",
        snapshot,
        None,
        (),
        session_id="language-session",
        state_event_id="state-event-1",
        capability_profile=C1_CAPABILITY_PROFILE,
        translation=translation,
        altitude_grounding=config.altitude_grounding(),
    )

    assert outcome.kind is OutcomeKind.REFUSE
    assert outcome.reason is CompilerReason.MODEL_UNAVAILABLE


def test_runtime_rejects_navigation_grounding_for_another_profile():
    snapshot = make_snapshot(count=1, selection=(1,))
    config, translation = _grounding(snapshot)
    from language.navigation import NavigationGrounding, Zone
    from relay.capabilities import CapabilityProfile
    from relay.intent_v1 import IntentName

    other = CapabilityProfile("other", {IntentName.HOLD})
    navigation = NavigationGrounding(
        other,
        ("map", "v1"),
        ("geometry", "v1"),
        "navigation-v1",
        "level-1",
        "catalog-v1",
        (Zone("lobby", "level-1", True, ("lobby-1",)),),
    )

    with pytest.raises(ValueError, match="capability profile"):
        LanguageRuntime(StaticResponseTransport({"kind": "refuse"})).compile(
            "hold",
            snapshot,
            navigation,
            (),
            session_id="language-session",
            state_event_id="state-event-1",
            capability_profile=C1_CAPABILITY_PROFILE,
            translation=translation,
            altitude_grounding=config.altitude_grounding(),
        )


def test_runtime_refuses_invalid_model_output_without_staging_an_intent():
    snapshot = make_snapshot(count=1, selection=(1,))
    config, translation = _grounding(snapshot)

    outcome = LanguageRuntime(StaticResponseTransport({"kind": "plan", "intents": []})).compile(
        "hold",
        snapshot,
        None,
        (),
        session_id="language-session",
        state_event_id="state-event-1",
        capability_profile=C1_CAPABILITY_PROFILE,
        translation=translation,
        altitude_grounding=config.altitude_grounding(),
    )

    assert outcome.kind is OutcomeKind.REFUSE
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT

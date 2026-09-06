from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evals.language_corpus import StaticResponseTransport
from language.transport import ModelRequest, ModelResponse
from relay.auth import Principal
from relay.autonomy import AutonomyComposition, AutonomyConfig, create_autonomy_app
from relay.language_runtime import LanguageRuntime
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    membership_payload,
    telemetry_payload,
)
from relay.voice import AudioUpload
from tests.autonomy_fixtures import camera_config, planning_config, safety_config


class RecordingTransport(StaticResponseTransport):
    def __init__(self) -> None:
        super().__init__(
            {
                "kind": "plan",
                "intents": [{"name": "hold", "args": {}, "selection": [1], "mode": "indoor"}],
            }
        )
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return super().complete(request)


@dataclass
class FixedTranscriptionTransport:
    transcript: str = "hold the selected aircraft"

    def transcribe(self, _upload: AudioUpload) -> str:
        return self.transcript


@dataclass
class CompiledRelay:
    client: TestClient
    transport: RecordingTransport
    composition: AutonomyComposition


@pytest.fixture
def compiled_relay(tmp_path: Path, clock: MutableClock, event_ids: EventIds) -> CompiledRelay:
    transport = RecordingTransport()
    app, composition = create_autonomy_app(
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={1: ADAPTER_KEY},
            log_dir=tmp_path,
            adapter_backend=AdapterBackend.SIM,
        ),
        AutonomyConfig(
            planning=planning_config(),
            safety=safety_config(),
            sim_camera=camera_config(),
            language=LanguageRuntime(transport),
        ),
        clock=clock,
        event_ids=event_ids,
    )
    try:
        with TestClient(app) as client:
            runtime = app.state.relay_runtime
            runtime.authoritative_rooms_factory = lambda _session: ("room-101",)
            session = runtime.session(SESSION)
            adapter = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
            session.process_membership(
                membership_payload(action="join", event_id="join-1"), adapter
            )
            session.process_telemetry(telemetry_payload(event_id="telemetry-1"), adapter)
            session.process_membership(
                membership_payload(action="readiness", event_id="ready-1"), adapter
            )
            session.update_control_projection(selection=(1,))
            yield CompiledRelay(client, transport, composition)
    finally:
        composition.close()


def _authorization() -> dict[str, str]:
    return {"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}


def test_compile_api_runs_the_composed_language_runtime_with_current_grounding(
    compiled_relay: CompiledRelay,
) -> None:
    response = compiled_relay.client.post(
        f"/api/sessions/{SESSION}/compile",
        headers=_authorization(),
        json={"text": "hold the selected aircraft", "correlation_id": "language-http-1"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "language_compilation"
    assert payload["session"] == SESSION
    assert payload["correlation_id"] == "language-http-1"
    assert payload["compilation"]["kind"] == "plan"
    assert payload["compilation"]["intents"][0]["name"] == "hold"
    assert payload["compilation"]["intents"][0]["selection"] == [1]

    facts = compiled_relay.transport.requests[0].facts
    assert facts["session"] == SESSION
    assert facts["selection"] == [1]
    assert facts["rooms"] == ["room-101"]
    assert facts["capability_profile"] == "c1_basic_control"
    assert facts["enabled_intent_names"] == sorted(
        name.value for name in compiled_relay.composition.capability_profile.enabled_intent_names
    )


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token"}])
def test_compile_api_requires_the_console_bearer_token(
    headers: dict[str, str], tmp_path: Path
) -> None:
    transport = RecordingTransport()
    app, composition = create_autonomy_app(
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={1: ADAPTER_KEY},
            log_dir=tmp_path,
            adapter_backend=AdapterBackend.SIM,
        ),
        AutonomyConfig(
            planning=planning_config(),
            safety=safety_config(),
            sim_camera=camera_config(),
            language=LanguageRuntime(transport),
        ),
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/sessions/{SESSION}/compile",
                headers=headers,
                json={"text": "hold", "correlation_id": "language-auth-1"},
            )
    finally:
        composition.close()

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}
    assert transport.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {"text": "", "correlation_id": "language-invalid-1"},
        {"text": "hold", "correlation_id": ""},
        {"text": "hold", "correlation_id": "language-invalid-2", "extra": True},
    ],
)
def test_compile_api_rejects_malformed_bounded_requests(
    compiled_relay: CompiledRelay, body: dict[str, object]
) -> None:
    response = compiled_relay.client.post(
        f"/api/sessions/{SESSION}/compile", headers=_authorization(), json=body
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid text request"}
    assert compiled_relay.transport.requests == []


def test_compile_api_rejects_a_request_over_its_body_limit(compiled_relay: CompiledRelay) -> None:
    response = compiled_relay.client.post(
        f"/api/sessions/{SESSION}/compile",
        headers={**_authorization(), "Content-Type": "application/json"},
        content=b"x" * 16_385,
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "text request is too large"}
    assert compiled_relay.transport.requests == []


def test_transcript_api_preserves_the_composed_language_compilation(
    compiled_relay: CompiledRelay,
) -> None:
    service = compiled_relay.client.app.state.transcript_service
    service._transcription = FixedTranscriptionTransport()
    service._duration_probe = lambda _upload: 1_000

    response = compiled_relay.client.post(
        f"/api/sessions/{SESSION}/transcripts",
        headers={
            **_authorization(),
            "Content-Type": "audio/webm",
            "X-Sweep-Correlation-Id": "voice-language-1",
        },
        content=b"audio",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "transcribed"
    assert payload["transcript"] == "hold the selected aircraft"
    assert payload["compilation"]["kind"] == "plan"
    assert payload["compilation"]["intents"][0]["name"] == "hold"
    assert compiled_relay.transport.requests[0].facts["selection"] == [1]

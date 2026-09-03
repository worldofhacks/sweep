from __future__ import annotations

import json

import pytest

from language.transport import (
    PINNED_COMPILER_MODEL,
    PROMPT_SCHEMA_VERSION,
    AnthropicTransport,
    ModelRequest,
    ModelResponse,
    RecordingTransport,
    ReplayTransport,
    TransportError,
    request_key,
)
from relay.intent_v1 import IntentName


class StaticTransport:
    def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            payload={"kind": "refuse", "reason": "unknown_reference"},
            source="synthetic",
            origin="synthetic",
            model=PINNED_COMPILER_MODEL,
            prompt_schema_version=PROMPT_SCHEMA_VERSION,
            input_units=10,
            output_units=4,
            latency_ms=12,
        )


def test_replay_transport_returns_exact_recorded_response(tmp_path) -> None:
    request = ModelRequest(transcript="hold", facts={"selection": [1]})
    key = request_key(request)
    cassette = tmp_path / "cassette.json"
    cassette.write_text(
        json.dumps(
            {
                "version": 2,
                "model": PINNED_COMPILER_MODEL,
                "prompt_schema_version": PROMPT_SCHEMA_VERSION,
                "origin": "anthropic",
                "entries": {
                    key: {
                        "payload": {"kind": "refuse", "reason": "unknown_reference"},
                        "input_units": 10,
                        "output_units": 4,
                        "latency_ms": 12,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    response = ReplayTransport(cassette).complete(request)

    assert response.payload == {"kind": "refuse", "reason": "unknown_reference"}
    assert response.input_units == 10
    assert response.source == "replay"
    assert response.origin == "anthropic"
    assert response.cassette_digest is not None


def test_replay_transport_fails_closed_on_miss(tmp_path) -> None:
    cassette = tmp_path / "cassette.json"
    cassette.write_text(
        json.dumps(
            {
                "version": 2,
                "model": PINNED_COMPILER_MODEL,
                "prompt_schema_version": PROMPT_SCHEMA_VERSION,
                "origin": "synthetic",
                "entries": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TransportError, match="replay miss"):
        ReplayTransport(cassette).complete(ModelRequest(transcript="hold", facts={}))


def test_recording_transport_round_trips_through_replay(tmp_path) -> None:
    request = ModelRequest(transcript="hold", facts={"selection": [1]})
    cassette = tmp_path / "cassette.json"
    recorded = RecordingTransport(StaticTransport(), cassette).complete(request)
    replayed = ReplayTransport(cassette).complete(request)
    assert replayed.payload == recorded.payload
    assert replayed.origin == recorded.origin == "synthetic"
    assert replayed.source == "replay"
    assert recorded.source == "synthetic"
    assert replayed.model == recorded.model == PINNED_COMPILER_MODEL
    assert replayed.cassette_digest == recorded.cassette_digest


def test_anthropic_transport_without_key_makes_no_request(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def fail(*args, **kwargs):
        raise AssertionError("network should not be called")

    monkeypatch.setattr("language.transport.httpx.post", fail)
    with pytest.raises(TransportError, match="not configured"):
        AnthropicTransport().complete(ModelRequest(transcript="hold", facts={}))


def test_compiler_uses_active_pinned_model_and_strict_tool_schema(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "submit_compiler_outcome",
                        "input": {"kind": "refuse", "reason": "unknown_reference"},
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    def post(_url: str, **kwargs: object) -> Response:
        captured.update(kwargs["json"])  # type: ignore[arg-type]
        return Response()

    monkeypatch.setattr("language.transport.httpx.post", post)
    AnthropicTransport(api_key="test-key").complete(
        ModelRequest(transcript="hold", facts={"session": "test-session"})
    )

    assert captured["model"] == "claude-sonnet-5"
    assert "temperature" not in captured
    assert "top_p" not in captured
    assert "top_k" not in captured
    assert "thinking" not in captured
    tools = captured["tools"]
    assert isinstance(tools, list)
    assert tools[0]["strict"] is True
    intent_schema = tools[0]["input_schema"]["properties"]["intents"]["items"]
    assert intent_schema["properties"]["name"]["enum"] == [name.value for name in IntentName]
    assert "land" in intent_schema["properties"]["name"]["enum"]
    assert intent_schema["properties"]["args"]["additionalProperties"] is False
    assert {
        "ids",
        "dx",
        "dy",
        "delta",
        "name",
        "box",
        "room_id",
        "area_id",
        "pattern",
    } <= intent_schema["properties"]["args"]["properties"].keys()
    assert "capture_id" not in intent_schema["properties"]["args"]["properties"]
    outcome_schema = tools[0]["input_schema"]["properties"]
    assert "cancel_pending" in outcome_schema["kind"]["enum"]
    assert "pending_intent_id" in outcome_schema

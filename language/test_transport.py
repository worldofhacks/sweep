from __future__ import annotations

import json

import pytest

from language.transport import (
    AnthropicTransport,
    ModelRequest,
    ModelResponse,
    RecordingTransport,
    ReplayTransport,
    TransportError,
    request_key,
)


class StaticTransport:
    def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            payload={"kind": "refuse", "reason": "unknown_reference"},
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
                "version": 1,
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


def test_replay_transport_fails_closed_on_miss(tmp_path) -> None:
    cassette = tmp_path / "cassette.json"
    cassette.write_text('{"version": 1, "entries": {}}', encoding="utf-8")

    with pytest.raises(TransportError, match="replay miss"):
        ReplayTransport(cassette).complete(ModelRequest(transcript="hold", facts={}))


def test_recording_transport_round_trips_through_replay(tmp_path) -> None:
    request = ModelRequest(transcript="hold", facts={"selection": [1]})
    cassette = tmp_path / "cassette.json"
    recorded = RecordingTransport(StaticTransport(), cassette).complete(request)
    replayed = ReplayTransport(cassette).complete(request)
    assert replayed == recorded


def test_anthropic_transport_without_key_makes_no_request(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def fail(*args, **kwargs):
        raise AssertionError("network should not be called")

    monkeypatch.setattr("language.transport.httpx.post", fail)
    with pytest.raises(TransportError, match="not configured"):
        AnthropicTransport().complete(ModelRequest(transcript="hold", facts={}))

from __future__ import annotations

import hashlib
import json
import multiprocessing
import time
from pathlib import Path

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


def _record_in_process(cassette: str, transcript: str, start: object) -> None:
    start.wait()
    RecordingTransport(StaticTransport(), Path(cassette)).complete(
        ModelRequest(transcript=transcript, facts={"selection": [1]})
    )


def test_replay_transport_returns_exact_recorded_response(tmp_path) -> None:
    request = ModelRequest(transcript="hold", facts={"selection": [1]})
    key = request_key(request)
    cassette = tmp_path / "cassette.json"
    cassette.write_text(
        json.dumps(
            {
                "version": 3,
                "model": PINNED_COMPILER_MODEL,
                "prompt_schema_version": PROMPT_SCHEMA_VERSION,
                "recorded_origin": "anthropic",
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
    assert response.origin == "unverified_replay"
    assert response.cassette_digest is not None


def test_replay_transport_fails_closed_on_miss(tmp_path) -> None:
    cassette = tmp_path / "cassette.json"
    cassette.write_text(
        json.dumps(
            {
                "version": 3,
                "model": PINNED_COMPILER_MODEL,
                "prompt_schema_version": PROMPT_SCHEMA_VERSION,
                "recorded_origin": "synthetic",
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
    assert recorded.origin == "synthetic"
    assert replayed.origin == "unverified_replay"
    assert replayed.source == "replay"
    assert recorded.source == "synthetic"
    assert replayed.model == recorded.model == PINNED_COMPILER_MODEL
    assert replayed.cassette_digest == recorded.cassette_digest


def test_recording_transport_serializes_writers_across_processes(tmp_path, monkeypatch) -> None:
    cassette = tmp_path / "cassette.json"
    original_load = RecordingTransport._load

    def slow_load(self):
        value = original_load(self)
        time.sleep(0.05)
        return value

    monkeypatch.setattr(RecordingTransport, "_load", slow_load)
    context = multiprocessing.get_context("fork")
    start = context.Event()
    workers = [
        context.Process(target=_record_in_process, args=(str(cassette), transcript, start))
        for transcript in ("hold", "land")
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(timeout=5)
        assert worker.exitcode == 0

    body = json.loads(cassette.read_text())
    assert set(body["entries"]) == {
        request_key(ModelRequest(transcript="hold", facts={"selection": [1]})),
        request_key(ModelRequest(transcript="land", facts={"selection": [1]})),
    }


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
                "model": PINNED_COMPILER_MODEL,
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
    assert (
        "An omitted movement distance means one foot, exactly 0.3048 meters" in captured["system"]
    )
    assert "ordinary stop or hover means hold" in captured["system"]
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

    def schema_keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | set().union(*(schema_keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(schema_keys(item) for item in value))
        return set()

    assert not schema_keys(tools[0]["input_schema"]) & {
        "minimum",
        "minLength",
        "maxLength",
        "maxItems",
        "uniqueItems",
    }


@pytest.mark.parametrize(
    "body",
    [
        {
            "model": "claude-opus-5",
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "submit_compiler_outcome",
                    "input": {"kind": "refuse", "reason": "unknown_reference"},
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        {
            "model": PINNED_COMPILER_MODEL,
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "submit_compiler_outcome",
                    "input": {"kind": "refuse", "reason": "unknown_reference"},
                }
            ],
            "usage": [],
        },
    ],
)
def test_anthropic_transport_rejects_invalid_response_envelope(monkeypatch, body) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return body

    monkeypatch.setattr("language.transport.httpx.post", lambda *_args, **_kwargs: Response())

    with pytest.raises(TransportError):
        AnthropicTransport(api_key="test-key").complete(
            ModelRequest(transcript="hold", facts={"session": "test-session"})
        )


def test_replay_binds_payload_and_digest_to_one_immutable_read(tmp_path, monkeypatch) -> None:
    request = ModelRequest(transcript="hold", facts={"selection": [1]})
    cassette = tmp_path / "cassette.json"
    RecordingTransport(StaticTransport(), cassette).complete(request)
    original = cassette.read_bytes()
    replacement = original.replace(b"unknown_reference", b"stale_state")
    reads = iter((original, replacement))

    monkeypatch.setattr(cassette.__class__, "read_bytes", lambda _path: next(reads))
    replay = ReplayTransport(cassette)
    response = replay.complete(request)
    assert response.payload == {"kind": "refuse", "reason": "unknown_reference"}
    assert response.cassette_digest == hashlib.sha256(original).hexdigest()

    mutable = response.payload
    assert isinstance(mutable, dict)
    mutable["reason"] = "stale_state"
    assert replay.complete(request).payload == {
        "kind": "refuse",
        "reason": "unknown_reference",
    }

from __future__ import annotations

import logging

import language.telemetry as telemetry
from language.telemetry import LangfuseTraceSink, NoOpTraceSink, get_default_trace_sink


class FakeObservation:
    def __init__(self) -> None:
        self.scores: list[dict[str, object]] = []
        self.updates: list[dict[str, object]] = []
        self.ended = False

    def score_trace(self, **fields: object) -> None:
        self.scores.append(fields)

    def update(self, **fields: object) -> None:
        self.updates.append(fields)

    def end(self) -> None:
        self.ended = True


class FakeLangfuse:
    def __init__(self) -> None:
        self.observation = FakeObservation()
        self.observation_calls: list[dict[str, object]] = []
        self.flushes = 0

    def create_trace_id(self, *, seed: str) -> str:
        return f"trace-{seed}"

    def start_observation(self, **fields: object) -> FakeObservation:
        self.observation_calls.append(fields)
        return self.observation

    def flush(self) -> None:
        self.flushes += 1


def test_default_tracer_is_inert_without_credentials(monkeypatch, caplog) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    caplog.set_level(logging.WARNING)

    tracer = get_default_trace_sink()
    tracer.record({"event": "compiler_started", "correlation_id": "trace-1"})

    assert isinstance(tracer, NoOpTraceSink)
    assert caplog.records == []


def test_credentials_select_installed_langfuse_client(monkeypatch) -> None:
    client = FakeLangfuse()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    monkeypatch.setattr(telemetry, "_langfuse_client", client)

    tracer = get_default_trace_sink()

    assert isinstance(tracer, LangfuseTraceSink)


def test_langfuse_tracer_records_one_generation_and_score_without_text() -> None:
    client = FakeLangfuse()
    tracer = LangfuseTraceSink(client)
    tracer.record(
        {
            "event": "compiler_started",
            "correlation_id": "trace-1",
            "session_id": "session-1",
            "model": "model-1",
            "state_digest": "state-1",
        }
    )
    tracer.record(
        {
            "event": "compiler_completed",
            "correlation_id": "trace-1",
            "model": "model-1",
            "state_digest": "state-1",
            "outcome": "plan",
            "source": "claude",
            "origin": "anthropic",
            "prompt_schema_version": "schema-1",
            "cassette_digest": "a" * 64,
            "grounded": 1,
            "input_units": 20,
            "output_units": 8,
            "provider_latency_ms": 12,
            "elapsed_ms": 13,
            "reason": None,
        }
    )

    assert client.observation_calls[0]["trace_context"] == {"trace_id": "trace-trace-1"}
    assert client.observation.updates[0]["usage_details"] == {"input": 20, "output": 8}
    assert client.observation.updates[0]["metadata"] == {
        "provider_latency_ms": 12,
        "elapsed_ms": 13,
        "origin": "anthropic",
        "prompt_schema_version": "schema-1",
        "cassette_digest": "a" * 64,
    }
    assert client.observation.scores[0] == {"name": "grounded", "value": 1, "comment": None}
    assert client.observation.ended is True
    assert client.flushes == 1
    assert "transcript" not in repr(client.observation_calls + client.observation.updates)

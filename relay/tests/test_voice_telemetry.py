from __future__ import annotations

from relay.voice_telemetry import (
    LangfuseVoiceTraceSink,
    NoOpVoiceTraceSink,
    get_default_voice_trace_sink,
)


def test_keyless_voice_telemetry_is_inert_without_constructing_a_client() -> None:
    called = False

    def factory(*args: object) -> tuple[object, object | None]:
        nonlocal called
        called = True
        raise AssertionError("keyless telemetry must not construct a client")

    sink = get_default_voice_trace_sink({}, client_factory=factory)
    sink.record({"event": "voice_started", "correlation_id": "voice-1"})

    assert isinstance(sink, NoOpVoiceTraceSink)
    assert called is False


class FakeObservation:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self.ended = False

    def update(self, **kwargs: object) -> None:
        self.updates.append(kwargs)

    def end(self) -> None:
        self.ended = True


class FakeLangfuse:
    def __init__(self) -> None:
        self.observation = FakeObservation()
        self.flushed = False

    def create_trace_id(self, *, seed: str) -> str:
        return f"trace-{seed}"

    def start_observation(self, **kwargs: object) -> FakeObservation:
        return self.observation

    def flush(self) -> None:
        self.flushed = True


def test_completion_update_carries_measured_cost_to_langfuse() -> None:
    client = FakeLangfuse()
    sink = LangfuseVoiceTraceSink(client)
    sink.record(
        {
            "event": "voice_started",
            "correlation_id": "voice-1",
            "provider_cost_usd": 0.0015,
            "combined_cost_usd": 0.0015,
        }
    )
    sink.record(
        {
            "event": "voice_completed",
            "correlation_id": "voice-1",
            "status": "transcribed",
            "source": "whisper",
            "reason": None,
            "provider_cost_usd": 0.0015,
            "combined_cost_usd": 0.0015,
        }
    )

    assert client.observation.updates == [
        {
            "output": {"status": "transcribed", "source": "whisper"},
            "metadata": {
                "reason": None,
                "provider_cost_usd": 0.0015,
                "combined_cost_usd": 0.0015,
            },
        }
    ]
    assert client.observation.ended is True
    assert client.flushed is True

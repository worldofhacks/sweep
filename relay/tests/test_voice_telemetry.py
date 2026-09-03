from __future__ import annotations

from relay.voice_telemetry import NoOpVoiceTraceSink, get_default_voice_trace_sink


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

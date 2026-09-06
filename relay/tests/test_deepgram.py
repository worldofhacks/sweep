from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from relay import voice
from relay.settings import RelaySettings, SettingsError
from relay.tests.conftest import CONSOLE_KEY
from relay.voice import (
    AudioUpload,
    DeepgramTransport,
    OpenAIWhisperTransport,
    RecordingTranscriptionTransport,
    ReplayTranscriptionTransport,
    TranscriptionError,
    configured_transcription,
)

UPLOAD = AudioUpload("audio/webm", b"audio")


def response(body, status=200):
    return httpx.Response(
        status, json=body, request=httpx.Request("POST", "https://api.deepgram.com/v1/listen")
    )


def test_deepgram_sends_audio_bytes_and_command_keyterms(monkeypatch) -> None:
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return response({"results": {"channels": [{"alternatives": [{"transcript": "Hold."}]}]}})

    monkeypatch.setattr(voice.httpx, "post", post)
    assert DeepgramTransport(api_key="private-key").transcribe(UPLOAD) == "Hold."
    assert len(calls) == 1
    url, request = calls[0]
    assert url == "https://api.deepgram.com/v1/listen"
    assert request["content"] == UPLOAD.body
    assert request["headers"] == {
        "Authorization": "Token private-key",
        "Content-Type": "audio/webm",
    }
    assert dict(request["params"])["model"] == "nova-3"
    assert dict(request["params"])["language"] == "en"
    assert dict(request["params"])["smart_format"] == "true"
    assert {value for key, value in request["params"] if key == "keyterm"} == set(
        voice.COMMAND_KEYTERMS
    )


@pytest.mark.parametrize("status", [400, 401, 403, 408, 429, 500, 503])
def test_deepgram_http_failure_is_typed_and_makes_one_request(monkeypatch, status) -> None:
    calls = []

    def post(*args, **kwargs):
        calls.append(True)
        return response({"error": "provider failure"}, status)

    monkeypatch.setattr(voice.httpx, "post", post)
    with pytest.raises(TranscriptionError, match="provider request failed"):
        DeepgramTransport(api_key="private-key").transcribe(UPLOAD)
    assert len(calls) == 1


@pytest.mark.parametrize("error", [httpx.ReadTimeout("timeout"), httpx.ConnectError("offline")])
def test_deepgram_transport_failure_is_typed(monkeypatch, error) -> None:
    def post(*args, **kwargs):
        raise error

    monkeypatch.setattr(voice.httpx, "post", post)
    with pytest.raises(TranscriptionError, match="provider request failed"):
        DeepgramTransport(api_key="private-key").transcribe(UPLOAD)


@pytest.mark.parametrize("body", [None, {}, [], {"results": None}, {"results": {"channels": []}}])
def test_deepgram_malformed_response_is_typed(monkeypatch, body) -> None:
    monkeypatch.setattr(voice.httpx, "post", lambda *a, **kw: response(body))
    with pytest.raises(TranscriptionError, match="response is malformed"):
        DeepgramTransport(api_key="private-key").transcribe(UPLOAD)


@pytest.mark.parametrize(
    "text",
    [None, "", "   ", 42, "x" * 4001],
    ids=["null", "empty", "blank", "number", "oversized"],
)
def test_deepgram_rejects_invalid_transcript(monkeypatch, text) -> None:
    monkeypatch.setattr(
        voice.httpx,
        "post",
        lambda *a, **kw: response(
            {"results": {"channels": [{"alternatives": [{"transcript": text}]}]}}
        ),
    )
    with pytest.raises(ValueError):
        DeepgramTransport(api_key="private-key").transcribe(UPLOAD)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({}, "whisper"),
        ({"DEEPGRAM_API_KEY": "key"}, "deepgram"),
        ({"DEEPGRAM_API_KEY": "key", "SWEEP_TRANSCRIPTION_PROVIDER": "whisper"}, "whisper"),
        ({"SWEEP_TRANSCRIPTION_PROVIDER": "deepgram"}, "deepgram"),
    ],
)
def test_provider_selection_is_shared_by_settings_and_transport(values, expected) -> None:
    settings = RelaySettings.from_env({"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(), **values})
    transport = configured_transcription(values)
    assert settings.transcription_provider == expected
    assert transport.provider == expected


def test_unknown_provider_refuses_configuration() -> None:
    with pytest.raises(SettingsError, match="SWEEP_TRANSCRIPTION_PROVIDER"):
        configured_transcription({"SWEEP_TRANSCRIPTION_PROVIDER": "typo"})


@pytest.mark.parametrize("provider", ["whisper", "deepgram"])
def test_explicit_keyless_environment_does_not_borrow_process_secret(monkeypatch, provider) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "ambient-secret")
    transport = configured_transcription({"SWEEP_TRANSCRIPTION_PROVIDER": provider})
    with pytest.raises(TranscriptionError, match="not configured"):
        transport.transcribe(UPLOAD)


def test_provider_recordings_have_distinct_replay_keys(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        voice.httpx,
        "post",
        lambda *a, **kw: response(
            {
                "text": "Whisper hold",
                "results": {"channels": [{"alternatives": [{"transcript": "Deepgram hold"}]}]},
            }
        ),
    )
    cassette = tmp_path / "providers.json"
    for transport, expected in [
        (OpenAIWhisperTransport(api_key="key"), "Whisper hold"),
        (DeepgramTransport(api_key="key"), "Deepgram hold"),
    ]:
        recording = RecordingTranscriptionTransport(transport, cassette)
        assert recording.transcribe(UPLOAD) == expected
        replay = ReplayTranscriptionTransport(cassette, provider=transport.provider)
        assert replay.transcribe(UPLOAD) == expected
    assert voice.transcription_request_key(UPLOAD) != voice.transcription_request_key(
        UPLOAD, model=voice.DEEPGRAM_MODEL
    )


@pytest.mark.parametrize(
    ("body", "status", "reason"),
    [
        ({}, 403, "transcription_unavailable"),
        ({}, 429, "transcription_unavailable"),
        ({}, 200, "transcription_unavailable"),
        (
            {"results": {"channels": [{"alternatives": [{"transcript": ""}]}]}},
            200,
            "invalid_transcript",
        ),
    ],
)
def test_deepgram_failures_keep_existing_zero_emission_console_outcomes(
    monkeypatch, body, status, reason
) -> None:
    from relay.tests.test_voice import SpyCompiler, fixed_audio_duration, valid_relay_state
    from relay.voice import TranscriptService

    monkeypatch.setattr(voice.httpx, "post", lambda *a, **kw: response(body, status))
    compiler = SpyCompiler()
    service = TranscriptService(
        transcription=DeepgramTransport(api_key="key"),
        compiler=compiler,
        duration_probe=fixed_audio_duration,
    )
    outcome = service.process(
        session_id="session-1",
        correlation_id="failure",
        content_type="audio/webm",
        body=b"audio",
        relay_state=valid_relay_state(),
        now_ms=1_756_700_000_001,
    )
    assert outcome.reason == reason
    assert outcome.source == "template"
    assert outcome.emissions == ()
    assert compiler.calls == []


def test_deepgram_success_labels_source_and_telemetry_model(monkeypatch) -> None:
    from relay.tests.test_voice import SpyCompiler, fixed_audio_duration, valid_relay_state
    from relay.voice import TranscriptService

    monkeypatch.setattr(
        voice.httpx,
        "post",
        lambda *a, **kw: response(
            {"results": {"channels": [{"alternatives": [{"transcript": "Hold"}]}]}}
        ),
    )

    class Trace:
        def __init__(self):
            self.events = []

        def record(self, event):
            self.events.append(event)

    trace = Trace()
    service = TranscriptService(
        transcription=DeepgramTransport(api_key="key"),
        compiler=SpyCompiler(),
        duration_probe=fixed_audio_duration,
        tracer=trace,
    )
    outcome = service.process(
        session_id="session-1",
        correlation_id="success",
        content_type="audio/webm",
        body=b"audio",
        relay_state=valid_relay_state(),
        now_ms=1_756_700_000_001,
    )
    assert outcome.source == "deepgram"
    assert outcome.status == "transcribed"
    assert outcome.emissions == ()
    assert trace.events[0]["model"] == "nova-3"
    assert trace.events[0]["provider_cost_usd"] is None


@pytest.mark.parametrize("provider", ["deepgram", "whisper"])
def test_normal_relay_startup_posts_to_selected_provider(monkeypatch, tmp_path: Path, provider):
    from fastapi.testclient import TestClient

    from relay.app import create_app
    from relay.tests.conftest import SESSION
    from relay.tests.test_voice import opus_webm

    monkeypatch.setenv("DEEPGRAM_API_KEY", "deepgram-private")
    monkeypatch.setenv("OPENAI_API_KEY", "whisper-private")
    urls = []

    def post(url, **kwargs):
        urls.append(url)
        return response(
            {"text": "Hold", "results": {"channels": [{"alternatives": [{"transcript": "Hold"}]}]}}
        )

    monkeypatch.setattr(voice.httpx, "post", post)
    app = create_app(
        RelaySettings(
            relay_token=CONSOLE_KEY,
            log_dir=tmp_path,
            transcription_provider=provider,
        )
    )
    with TestClient(app) as client:
        app.state.relay_runtime.session(SESSION)
        result = client.post(
            f"/api/sessions/{SESSION}/transcripts",
            headers={
                "Authorization": f"Bearer {CONSOLE_KEY.decode()}",
                "Content-Type": "audio/webm",
                "X-Sweep-Correlation-Id": "startup-provider",
            },
            content=opus_webm(1),
        )
    assert len(urls) == 1
    assert ("deepgram.com" in urls[0]) == (provider == "deepgram")
    assert result.json()["transcript"] == "Hold"
    assert result.json()["reason"] == "compiler_unavailable"
    assert result.json()["emissions"] == []
    assert "private" not in result.text


def test_changing_deepgram_keyterms_invalidates_replay_key(monkeypatch):
    before = voice.transcription_request_key(UPLOAD, model=voice.DEEPGRAM_MODEL)
    monkeypatch.setattr(voice, "COMMAND_KEYTERMS", (*voice.COMMAND_KEYTERMS, "north hall"))
    assert voice.transcription_request_key(UPLOAD, model=voice.DEEPGRAM_MODEL) != before

from __future__ import annotations

import io
import wave
from pathlib import Path

import httpx
import pytest

from adapters.sim.demo import DemoConfig
from adapters.sim.language_demo import SyntheticPhraseTransport, language_demo
from language.telemetry import NoOpTraceSink
from relay.voice_telemetry import NoOpVoiceTraceSink


def _audio() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * 4_000)
    return buffer.getvalue()


def test_synthetic_audio_runs_the_grounded_compiler_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_default_tracing():
        raise AssertionError("synthetic input must not initialize tracing from host credentials")

    monkeypatch.setattr("relay.voice.get_default_voice_trace_sink", no_default_tracing)
    monkeypatch.setattr("language.compiler.get_default_trace_sink", no_default_tracing)
    with language_demo(DemoConfig(log_dir=tmp_path), synthetic_inputs=True) as demo:
        headers = {"Authorization": f"Bearer {demo.token}"}
        transcript_path = f"/api/sessions/{demo.config.session}/transcripts"
        with httpx.Client(base_url=demo.http_url, timeout=10) as client:
            assert client.post("/demo/language/next", json={"text": "arm"}).status_code == 401
            queued = client.post(
                "/demo/language/next", headers=headers, json={"text": "arm the fleet"}
            )
            assert queued.json() == {"status": "queued", "source": "synthetic"}
            assert (
                client.post(
                    "/demo/language/next", headers=headers, json={"text": "land all"}
                ).status_code
                == 409
            )
            audio_headers = {
                **headers,
                "Content-Type": "audio/wav",
                "X-Sweep-Correlation-Id": "synthetic-audio-1",
            }
            invalid = client.post(transcript_path, headers=audio_headers, content=b"not audio")
            assert invalid.json()["reason"] == "invalid_audio"
            response = client.post(transcript_path, headers=audio_headers, content=_audio())
            assert response.status_code == 200
            body = response.json()
            assert body["source"] == "template"
            assert body["transcript"] == "arm the fleet"
            assert body["compilation"]["source"] == "synthetic"
            assert body["compilation"]["kind"] == "plan"
            assert body["compilation"]["intents"][0]["name"] == "arm"
            assert body["compilation"]["intents"][0]["selection"] == []
            assert body["emissions"] == []
            assert demo.runtime.session(demo.config.session).current_state()["armed"] is False
            again = client.post(transcript_path, headers=audio_headers, content=_audio())
            assert again.json()["reason"] == "transcription_unavailable"


def test_synthetic_text_still_refuses_ungrounded_motion_and_unknown_phrases(tmp_path: Path) -> None:
    with language_demo(DemoConfig(count=2, log_dir=tmp_path), synthetic_inputs=True) as demo:
        headers = {"Authorization": f"Bearer {demo.token}"}
        path = f"/api/sessions/{demo.config.session}/compile"
        with httpx.Client(base_url=demo.http_url, timeout=10) as client:
            for text in ("take off", "select drones 3 and 4", "turn off all the safety checks"):
                response = client.post(
                    path, headers=headers, json={"text": text, "correlation_id": "synthetic-text"}
                )
                assert response.status_code == 200
                assert response.json()["compilation"]["kind"] != "plan"
                assert response.json()["compilation"]["intents"] == []
            response = client.post(
                path,
                headers=headers,
                json={"text": "select all drones", "correlation_id": "synthetic-select"},
            )
            intent = response.json()["compilation"]["intents"][0]
            assert intent["name"] == "select"
            assert intent["args"] == {"ids": [1, 2]}
            assert demo.runtime.session(demo.config.session).current_state()["selection"] == []


def test_live_provider_mode_uses_provider_transports_and_exposes_no_synthetic_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def transcribe(_transport, _upload):
        calls.append("whisper")
        return "arm the fleet"

    def complete(_transport, request):
        calls.append("anthropic")
        return SyntheticPhraseTransport().complete(request)

    monkeypatch.setattr("relay.voice.OpenAIWhisperTransport.transcribe", transcribe)
    monkeypatch.setattr("language.transport.AnthropicTransport.complete", complete)
    monkeypatch.setattr("relay.voice.get_default_voice_trace_sink", NoOpVoiceTraceSink)
    monkeypatch.setattr("language.compiler.get_default_trace_sink", NoOpTraceSink)
    with language_demo(DemoConfig(count=1, log_dir=tmp_path)) as demo:
        with httpx.Client(base_url=demo.http_url, timeout=10) as client:
            response = client.post(
                "/demo/language/next",
                headers={"Authorization": f"Bearer {demo.token}"},
                json={"text": "arm"},
            )
            assert response.status_code == 404
            response = client.post(
                f"/api/sessions/{demo.config.session}/transcripts",
                headers={
                    "Authorization": f"Bearer {demo.token}",
                    "Content-Type": "audio/wav",
                    "X-Sweep-Correlation-Id": "provider-wiring",
                },
                content=_audio(),
            )
            assert response.json()["compilation"]["kind"] == "plan"
            assert calls == ["whisper", "anthropic"]

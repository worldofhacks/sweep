from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from relay.app import RelayRuntime, create_app
from relay.settings import RelaySettings, SettingsError
from relay.tests.conftest import CONSOLE_KEY, SESSION
from relay.tests.test_voice import FixedTranscriptionTransport, SpyCompiler
from relay.voice import TranscriptService


def test_partial_upload_stall_expires_before_decode_provider_or_compiler(tmp_path: Path) -> None:
    settings = RelaySettings(
        relay_token=CONSOLE_KEY, log_dir=tmp_path, transcript_upload_timeout_ms=20
    )
    app = create_app(settings)
    runtime = RelayRuntime(settings)
    runtime.session(SESSION)
    app.state.relay_runtime = runtime
    provider = FixedTranscriptionTransport()
    compiler = SpyCompiler()
    decoded = []

    def probe(upload):
        decoded.append(upload)
        return 1000

    app.state.transcript_service = TranscriptService(
        transcription=provider, compiler=compiler, duration_probe=probe
    )

    async def run():
        cancelled = asyncio.Event()

        async def stalled_body():
            yield b"partial audio"
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await asyncio.wait_for(
                client.post(
                    f"/api/sessions/{SESSION}/transcripts",
                    headers={
                        "Authorization": f"Bearer {CONSOLE_KEY.decode()}",
                        "Content-Type": "audio/webm",
                        "X-Sweep-Correlation-Id": "stalled-upload",
                    },
                    content=stalled_body(),
                ),
                timeout=1,
            )
        assert cancelled.is_set()
        return response

    response = asyncio.run(run())
    assert response.status_code == 408
    assert response.json()["reason"] == "upload_timeout"
    assert response.json()["emissions"] == []
    assert response.json()["correlation_id"] == "stalled-upload"
    assert decoded == []
    assert provider.calls == 0
    assert compiler.calls == []


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_upload_deadline_requires_positive_integer(value) -> None:
    with pytest.raises(SettingsError, match="SWEEP_TRANSCRIPT_UPLOAD_TIMEOUT_MS"):
        RelaySettings(relay_token=CONSOLE_KEY, transcript_upload_timeout_ms=value)


def test_upload_deadline_uses_environment_setting() -> None:
    settings = RelaySettings.from_env(
        {"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(), "SWEEP_TRANSCRIPT_UPLOAD_TIMEOUT_MS": "1200"}
    )
    assert settings.transcript_upload_timeout_ms == 1200

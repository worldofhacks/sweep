from __future__ import annotations

import asyncio
import io
import tracemalloc
import wave
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import av
import httpx
import pytest

from relay import voice
from relay.app import RelayRuntime, _bounded_request_body, create_app
from relay.settings import RelaySettings
from relay.tests.conftest import CONSOLE_KEY, SESSION
from relay.voice import (
    AudioUpload,
    CompilerUnavailable,
    OpenAIWhisperTransport,
    RecordingTranscriptionTransport,
    ReplayTranscriptionTransport,
    TranscriptionError,
    TranscriptService,
)


@dataclass
class FixedTranscriptionTransport:
    transcript: str = "hold the selected aircraft"
    calls: int = 0

    def transcribe(self, upload: AudioUpload) -> str:
        self.calls += 1
        assert upload.content_type == "audio/webm"
        return self.transcript


class SpyCompiler:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def compile(
        self,
        transcript: str,
        relay_state: object,
        *,
        capability_version: str,
        rooms: tuple[str, ...] = (),
        now_ms: int,
        correlation_id: str | None = None,
        session_id: str | None = None,
    ) -> object:
        self.calls.append(
            (transcript, relay_state, capability_version, rooms, now_ms, correlation_id, session_id)
        )
        return (object(), None)


def fixed_audio_duration(_upload: AudioUpload) -> int:
    return 1_000


def fifteen_second_audio_duration(_upload: AudioUpload) -> int:
    return 15_000


def opus_webm(seconds: int) -> bytes:
    output = io.BytesIO()
    with av.open(output, mode="w", format="webm") as container:
        stream = container.add_stream("libopus", rate=48_000)
        stream.layout = "mono"
        pts = 0
        samples_remaining = seconds * 48_000
        while samples_remaining:
            samples = min(960, samples_remaining)
            frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
            frame.sample_rate = 48_000
            frame.pts = pts
            frame.time_base = Fraction(1, 48_000)
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in stream.encode(frame):
                container.mux(packet)
            pts += samples
            samples_remaining -= samples
        for packet in stream.encode(None):
            container.mux(packet)
    return output.getvalue()


def opus_webm_with_video() -> bytes:
    output = io.BytesIO()
    with av.open(output, mode="w", format="webm") as container:
        video = container.add_stream("libvpx", rate=1)
        video.width = 16
        video.height = 16
        video.pix_fmt = "yuv420p"
        audio = container.add_stream("libopus", rate=48_000)
        audio.layout = "mono"

        video_frame = av.VideoFrame(16, 16, "yuv420p")
        video_frame.pts = 0
        video_frame.time_base = Fraction(1, 1)
        for packet in video.encode(video_frame):
            container.mux(packet)

        audio_frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        audio_frame.sample_rate = 48_000
        audio_frame.pts = 0
        audio_frame.time_base = Fraction(1, 48_000)
        for plane in audio_frame.planes:
            plane.update(bytes(plane.buffer_size))
        for packet in audio.encode(audio_frame):
            container.mux(packet)
        for packet in video.encode(None):
            container.mux(packet)
        for packet in audio.encode(None):
            container.mux(packet)
    return output.getvalue()


def test_transcript_service_hands_only_valid_audio_to_the_compiler() -> None:
    transport = FixedTranscriptionTransport()
    compiler = SpyCompiler()
    rooms = ("room-101", "room-102")
    service = TranscriptService(
        transcription=transport, compiler=compiler, duration_probe=fixed_audio_duration
    )
    relay_state = {
        "v": 1,
        "t": 1_756_700_000_000,
        "type": "state",
        "event_id": "state-event-1",
        "session": SESSION,
        "roster_version": 1,
        "armed": False,
        "estop": False,
        "selection": [1],
        "mode": "indoor",
        "drones": [
            {
                "drone_id": 1,
                "membership": "ready",
                "selectable": True,
                "flight_state": "hovering",
                "camera_patterns": ["pano_360"],
                "adapter_capabilities": ["flight", "raw-device-label"],
            }
        ],
    }

    outcome = service.process(
        session_id=SESSION,
        correlation_id="voice-request-1",
        content_type="audio/webm;codecs=opus",
        body=b"webm-bytes",
        relay_state=relay_state,
        rooms=rooms,
        now_ms=1_756_700_000_001,
    )

    assert outcome.status == "transcribed"
    assert outcome.source == "whisper"
    assert outcome.emissions == ()
    assert transport.calls == 1
    (
        transcript,
        received_state,
        capability_version,
        rooms,
        now_ms,
        correlation_id,
        session_id,
    ) = compiler.calls[0]
    assert transcript == "hold the selected aircraft"
    assert received_state == {
        "v": 1,
        "t": 1_756_700_000_000,
        "type": "state",
        "event_id": "state-event-1",
        "session": SESSION,
        "roster_version": 1,
        "armed": False,
        "estop": False,
        "selection": [1],
        "mode": "indoor",
        "drones": [
            {
                "drone_id": 1,
                "membership": "ready",
                "selectable": True,
                "flight_state": "hovering",
                "camera_patterns": ["pano_360"],
                "adapter_capabilities": ["flight"],
            }
        ],
    }
    assert capability_version.startswith("relay-capabilities-")
    assert rooms == ("room-101", "room-102")
    assert now_ms == 1_756_700_000_001
    assert correlation_id == "voice-request-1"
    assert session_id == SESSION

    changed_state = {
        **relay_state,
        "drones": [{**relay_state["drones"][0], "camera_patterns": ["reconstruct_8"]}],
    }
    second_outcome = service.process(
        session_id=SESSION,
        correlation_id="voice-request-2",
        content_type="audio/webm",
        body=b"webm-bytes",
        relay_state=changed_state,
        rooms=rooms,
        now_ms=1_756_700_000_002,
    )

    assert second_outcome.status == "transcribed"
    assert compiler.calls[1][2] != capability_version


def test_transcript_endpoint_passes_only_session_authoritative_rooms_to_compiler(
    tmp_path: Path,
) -> None:
    settings = RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path)
    compiler = SpyCompiler()
    app = create_app(settings)
    runtime = RelayRuntime(
        settings,
        authoritative_rooms_factory=lambda session: (
            ("room-from-authoritative-catalog",) if session.session_id == SESSION else ()
        ),
    )
    app.state.relay_runtime = runtime
    app.state.transcript_service = TranscriptService(
        transcription=FixedTranscriptionTransport(),
        compiler=compiler,
        duration_probe=fixed_audio_duration,
    )
    runtime.session(SESSION)

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                f"/api/sessions/{SESSION}/transcripts",
                headers={
                    "Authorization": f"Bearer {CONSOLE_KEY.decode()}",
                    "Content-Type": "audio/webm",
                    "X-Sweep-Correlation-Id": "voice-authoritative-rooms",
                },
                content=b"audio",
            )

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["reason"] is None
    assert compiler.calls[0][3] == ("room-from-authoritative-catalog",)


def test_voice_trace_records_duration_and_provider_plus_combined_cost() -> None:
    events: list[dict[str, object]] = []

    class Trace:
        def record(self, event: object) -> None:
            assert isinstance(event, dict)
            events.append(event)

    outcome = TranscriptService(
        transcription=FixedTranscriptionTransport(),
        compiler=SpyCompiler(),
        tracer=Trace(),
        duration_probe=fifteen_second_audio_duration,
    ).process(
        session_id=SESSION,
        correlation_id="voice-cost-1",
        content_type="audio/webm",
        body=b"audio",
        relay_state=valid_relay_state(),
        now_ms=1_756_700_000_001,
    )

    assert outcome.status == "transcribed"
    assert events[0] == {
        "event": "voice_started",
        "correlation_id": "voice-cost-1",
        "session_id": SESSION,
        "model": "whisper-1",
        "content_type": "audio/webm",
        "bytes": 5,
        "audio_duration_ms": 15_000,
        "provider_cost_usd": 0.0015,
        "combined_cost_usd": 0.0015,
    }
    assert events[1] == {
        "event": "voice_completed",
        "correlation_id": "voice-cost-1",
        "session_id": SESSION,
        "status": "transcribed",
        "source": "whisper",
        "reason": None,
        "provider_cost_usd": 0.0015,
        "combined_cost_usd": 0.0015,
    }


@pytest.mark.parametrize(
    ("content_type", "body", "reason"),
    [
        ("text/plain", b"audio", "unsupported_content_type"),
        ("audio/webm", b"", "empty_upload"),
        ("audio/webm", b"x" * (8 * 1024 * 1024 + 1), "upload_too_large"),
    ],
)
def test_invalid_uploads_are_typed_and_never_call_transcription_or_compiler(
    content_type: str, body: bytes, reason: str
) -> None:
    transport = FixedTranscriptionTransport()
    compiler = SpyCompiler()
    outcome = TranscriptService(
        transcription=transport, compiler=compiler, duration_probe=fixed_audio_duration
    ).process(
        session_id=SESSION,
        correlation_id="voice-request-invalid",
        content_type=content_type,
        body=body,
        relay_state={},
        now_ms=1_756_700_000_001,
    )

    assert outcome.status == "refused"
    assert outcome.source == "template"
    assert outcome.reason == reason
    assert outcome.emissions == ()
    assert transport.calls == 0
    assert compiler.calls == []


def test_transcription_and_compiler_failures_are_typed_no_emission_outcomes() -> None:
    class FailingTransport:
        def transcribe(self, upload: AudioUpload) -> str:
            raise TranscriptionError("provider unavailable")

    class FailingCompiler:
        def compile(self, *args: object) -> object:
            raise CompilerUnavailable()

    relay_state = valid_relay_state()
    transcription_failure = TranscriptService(
        transcription=FailingTransport(),
        compiler=SpyCompiler(),
        duration_probe=fixed_audio_duration,
    ).process(
        session_id=SESSION,
        correlation_id="voice-request-provider-failure",
        content_type="audio/webm",
        body=b"audio",
        relay_state=relay_state,
        now_ms=1_756_700_000_001,
    )
    compiler_failure = TranscriptService(
        transcription=FixedTranscriptionTransport(),
        compiler=FailingCompiler(),
        duration_probe=fixed_audio_duration,
    ).process(
        session_id=SESSION,
        correlation_id="voice-request-compiler-failure",
        content_type="audio/webm",
        body=b"audio",
        relay_state=relay_state,
        now_ms=1_756_700_000_001,
    )

    assert (transcription_failure.reason, transcription_failure.emissions) == (
        "transcription_unavailable",
        (),
    )
    assert (compiler_failure.reason, compiler_failure.emissions) == ("compiler_unavailable", ())


def test_transcript_endpoint_is_authenticated_session_bound_and_key_safe(tmp_path: Path) -> None:
    settings = RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path)
    transport = FixedTranscriptionTransport()
    compiler = SpyCompiler()
    app = create_app(
        settings,
        transcript_service_factory=lambda _runtime: TranscriptService(
            transcription=transport, compiler=compiler, duration_probe=fixed_audio_duration
        ),
    )
    headers = {
        "Authorization": f"Bearer {CONSOLE_KEY.decode()}",
        "Content-Type": "audio/webm;codecs=opus",
        "X-Sweep-Correlation-Id": "voice-http-1",
    }

    runtime = RelayRuntime(settings)
    app.state.relay_runtime = runtime
    app.state.transcript_service = TranscriptService(
        transcription=transport, compiler=compiler, duration_probe=fixed_audio_duration
    )

    async def requests() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            unavailable = await client.post(
                f"/api/sessions/{SESSION}/transcripts", headers=headers, content=b"audio"
            )
            runtime.session(SESSION)
            accepted = await client.post(
                f"/api/sessions/{SESSION}/transcripts", headers=headers, content=b"audio"
            )
            return unavailable, accepted

    unavailable, accepted = asyncio.run(requests())

    assert unavailable.status_code == 409
    assert unavailable.json()["reason"] == "session_unavailable"
    assert accepted.status_code == 200
    assert accepted.json() == {
        "v": 1,
        "type": "voice_outcome",
        "session": SESSION,
        "correlation_id": "voice-http-1",
        "status": "transcribed",
        "source": "whisper",
        "reason": None,
        "transcript": "hold the selected aircraft",
        "compilation": None,
        "emissions": [],
    }
    assert CONSOLE_KEY.decode() not in accepted.text


def test_transcript_endpoint_rejects_oversized_content_length_before_reading_or_transcribing(
    tmp_path: Path,
) -> None:
    transport = FixedTranscriptionTransport()
    app = create_app(
        RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path),
        transcript_service_factory=lambda _runtime: TranscriptService(
            transcription=transport, compiler=SpyCompiler(), duration_probe=fixed_audio_duration
        ),
    )
    headers = {
        "Authorization": f"Bearer {CONSOLE_KEY.decode()}",
        "Content-Type": "audio/webm",
        "Content-Length": str(8 * 1024 * 1024 + 1),
        "X-Sweep-Correlation-Id": "voice-too-large",
    }

    runtime = RelayRuntime(RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path))
    runtime.session(SESSION)
    app.state.relay_runtime = runtime

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                f"/api/sessions/{SESSION}/transcripts", headers=headers, content=b""
            )

    response = asyncio.run(request())

    assert response.status_code == 413
    assert response.json()["reason"] == "upload_too_large"
    assert response.json()["emissions"] == []
    assert transport.calls == 0


def test_transcript_service_rejects_decoded_audio_over_thirty_seconds_before_provider_io() -> None:
    transport = FixedTranscriptionTransport()
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(8_000)
        wav.writeframes(b"\x80" * (31 * 8_000))
    outcome = TranscriptService(transcription=transport, compiler=SpyCompiler()).process(
        session_id=SESSION,
        correlation_id="voice-too-long",
        content_type="audio/wav",
        body=audio.getvalue(),
        relay_state=valid_relay_state(),
        now_ms=1_756_700_000_001,
    )

    assert outcome.reason == "audio_too_long"
    assert outcome.emissions == ()
    assert transport.calls == 0


def test_transcript_endpoint_decodes_audio_when_video_is_the_first_stream(tmp_path: Path) -> None:
    transport = FixedTranscriptionTransport()
    settings = RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path)
    app = create_app(settings)
    runtime = RelayRuntime(settings)
    runtime.session(SESSION)
    app.state.relay_runtime = runtime
    app.state.transcript_service = TranscriptService(
        transcription=transport, compiler=SpyCompiler()
    )
    upload = opus_webm_with_video()

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                f"/api/sessions/{SESSION}/transcripts",
                headers={
                    "Authorization": f"Bearer {CONSOLE_KEY.decode()}",
                    "Content-Type": "audio/webm",
                    "X-Sweep-Correlation-Id": "voice-mixed-streams",
                },
                content=upload,
            )

    with av.open(io.BytesIO(upload)) as container:
        assert [(stream.type, stream.index) for stream in container.streams] == [
            ("video", 0),
            ("audio", 1),
        ]
    response = asyncio.run(request())

    assert response.status_code == 200
    assert response.json()["reason"] is None
    assert transport.calls == 1


@pytest.mark.parametrize(
    "sample_rates,pts_values",
    [
        ([0], [0]),
        ([48_000], [-1]),
        ([48_000, 48_000], [0, 0]),
    ],
)
def test_audio_duration_probe_rejects_missing_rate_and_nonmonotonic_timestamps(
    monkeypatch: pytest.MonkeyPatch,
    sample_rates: list[int],
    pts_values: list[int],
) -> None:
    frames = []
    for sample_rate, pts in zip(sample_rates, pts_values, strict=True):
        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.sample_rate = sample_rate
        frame.pts = pts
        frame.time_base = Fraction(1, 48_000)
        frames.append(frame)

    class Container:
        streams = [SimpleNamespace(type="audio", index=0)]

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def decode(self, *, audio: int):
            assert audio == 0
            yield from frames

    monkeypatch.setattr(voice.av, "open", lambda _source: Container())

    with pytest.raises(ValueError):
        voice.probe_audio_duration_ms(AudioUpload("audio/webm", b"audio"))


def test_transcript_endpoint_rejects_declared_audio_over_thirty_seconds(tmp_path: Path) -> None:
    app = create_app(RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path))
    runtime = RelayRuntime(RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path))
    runtime.session(SESSION)
    app.state.relay_runtime = runtime

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                f"/api/sessions/{SESSION}/transcripts",
                headers={
                    "Authorization": f"Bearer {CONSOLE_KEY.decode()}",
                    "Content-Type": "audio/webm",
                    "X-Sweep-Audio-Duration-Ms": "60000",
                    "X-Sweep-Correlation-Id": "voice-declared-too-long",
                },
                content=b"audio",
            )

    response = asyncio.run(request())

    assert response.status_code == 413
    assert response.json()["reason"] == "audio_too_long"


@pytest.mark.parametrize("declared_duration", [None, "0"])
def test_transcript_endpoint_rejects_concatenated_audio_with_reset_timestamps_before_provider_io(
    tmp_path: Path, declared_duration: str | None
) -> None:
    transport = FixedTranscriptionTransport()
    settings = RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path)
    app = create_app(settings)
    runtime = RelayRuntime(settings)
    runtime.session(SESSION)
    app.state.relay_runtime = runtime
    app.state.transcript_service = TranscriptService(
        transcription=transport, compiler=SpyCompiler()
    )
    headers = {
        "Authorization": f"Bearer {CONSOLE_KEY.decode()}",
        "Content-Type": "audio/webm",
        "X-Sweep-Correlation-Id": "voice-reset-timestamps",
    }
    if declared_duration is not None:
        headers["X-Sweep-Audio-Duration-Ms"] = declared_duration
    audio = opus_webm(16) + opus_webm(15)

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                f"/api/sessions/{SESSION}/transcripts", headers=headers, content=audio
            )

    response = asyncio.run(request())

    assert response.status_code == 200
    assert response.json()["reason"] == "invalid_audio"
    assert response.json()["emissions"] == []
    assert transport.calls == 0


def test_transcript_endpoint_accepts_browser_limit_with_opus_padding(tmp_path: Path) -> None:
    transport = FixedTranscriptionTransport()
    settings = RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path)
    app = create_app(settings)
    runtime = RelayRuntime(settings)
    runtime.session(SESSION)
    app.state.relay_runtime = runtime
    app.state.transcript_service = TranscriptService(
        transcription=transport, compiler=SpyCompiler()
    )
    browser_limit_ms = voice.MAX_AUDIO_DURATION_MS - 1_000
    audio = opus_webm(browser_limit_ms // 1_000)

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                f"/api/sessions/{SESSION}/transcripts",
                headers={
                    "Authorization": f"Bearer {CONSOLE_KEY.decode()}",
                    "Content-Type": "audio/webm",
                    "X-Sweep-Audio-Duration-Ms": str(browser_limit_ms),
                    "X-Sweep-Correlation-Id": "voice-browser-limit",
                },
                content=audio,
            )

    response = asyncio.run(request())

    assert response.status_code == 200
    assert response.json()["reason"] is None
    assert transport.calls == 1


def test_transcript_endpoint_requires_authentication(tmp_path: Path) -> None:
    app = create_app(RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path))
    app.state.relay_runtime = RelayRuntime(RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path))

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                f"/api/sessions/{SESSION}/transcripts",
                headers={"Content-Type": "audio/webm"},
                content=b"audio",
            )

    response = asyncio.run(request())

    assert response.status_code == 401


def test_transcript_endpoint_allows_configured_browser_preflight(tmp_path: Path) -> None:
    app = create_app(RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path))

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.options(
                f"/api/sessions/{SESSION}/transcripts",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": (
                        "authorization,content-type,x-sweep-correlation-id,"
                        "x-sweep-audio-duration-ms"
                    ),
                },
            )

    response = asyncio.run(request())

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_streaming_upload_accepts_fragmented_body_below_the_byte_limit() -> None:
    class ChunkedRequest:
        async def stream(self):
            for _ in range(1_024):
                yield b"x" * 1_024

    body = asyncio.run(_bounded_request_body(ChunkedRequest()))  # type: ignore[arg-type]

    assert len(body) == 1_024 * 1_024


def test_streaming_upload_rejects_one_oversized_chunk_before_copying_it() -> None:
    oversized_chunk = b"x" * (voice.MAX_AUDIO_BYTES + 1)

    class ChunkedRequest:
        async def stream(self):
            yield oversized_chunk

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        with pytest.raises(ValueError, match="upload_too_large"):
            asyncio.run(_bounded_request_body(ChunkedRequest()))  # type: ignore[arg-type]
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 1_000_000


def test_recording_and_replay_transport_share_a_content_addressed_cassette(tmp_path: Path) -> None:
    cassette = tmp_path / "voice.json"
    recorder = RecordingTranscriptionTransport(FixedTranscriptionTransport("land all"), cassette)
    upload = AudioUpload(content_type="audio/webm", body=b"voice-fixture")

    assert recorder.transcribe(upload) == "land all"
    assert ReplayTranscriptionTransport(cassette).transcribe(upload) == "land all"
    with pytest.raises(TranscriptionError, match="replay miss"):
        ReplayTranscriptionTransport(cassette).transcribe(
            AudioUpload(content_type="audio/webm", body=b"different-fixture")
        )


def test_whisper_transport_posts_the_documented_model_without_exposing_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"text": "hold"}

    def post(url: str, **kwargs: object) -> Response:
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(voice.httpx, "post", post)
    transcript = OpenAIWhisperTransport(api_key="server-only-key").transcribe(
        AudioUpload(content_type="audio/webm", body=b"audio")
    )

    assert transcript == "hold"
    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["data"] == {"model": "whisper-1", "response_format": "json"}
    assert captured["files"] == {"file": ("speech.webm", b"audio", "audio/webm")}
    visible_request = {key: value for key, value in captured.items() if key != "headers"}
    assert "server-only-key" not in repr(visible_request)


def test_whisper_transport_retries_transient_provider_failure_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"text": "hold"}

    def post(*args: object, **kwargs: object) -> Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("network unavailable")
        return Response()

    monkeypatch.setattr(voice.httpx, "post", post)

    assert (
        OpenAIWhisperTransport(api_key="server-only-key").transcribe(
            AudioUpload(content_type="audio/webm", body=b"audio")
        )
        == "hold"
    )
    assert calls == 2


def test_whisper_transport_stops_after_its_bounded_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def post(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("network unavailable")

    monkeypatch.setattr(voice.httpx, "post", post)

    with pytest.raises(TranscriptionError, match="provider request failed"):
        OpenAIWhisperTransport(api_key="server-only-key").transcribe(
            AudioUpload(content_type="audio/webm", body=b"audio")
        )
    assert calls == 2


@pytest.mark.parametrize("status_code", [400, 401, 413])
def test_whisper_transport_does_not_retry_permanent_provider_statuses(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    calls = 0

    def post(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            request=httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions"),
        )

    monkeypatch.setattr(voice.httpx, "post", post)

    with pytest.raises(TranscriptionError, match="provider request failed"):
        OpenAIWhisperTransport(api_key="server-only-key").transcribe(
            AudioUpload(content_type="audio/webm", body=b"audio")
        )
    assert calls == 1


@pytest.mark.parametrize("status_code", [408, 409, 429, 500])
def test_whisper_transport_retries_transient_provider_statuses_once(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    calls = 0

    def post(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
        if calls == 1:
            return httpx.Response(status_code, request=request)
        return httpx.Response(200, json={"text": "hold"}, request=request)

    monkeypatch.setattr(voice.httpx, "post", post)

    assert (
        OpenAIWhisperTransport(api_key="server-only-key").transcribe(
            AudioUpload(content_type="audio/webm", body=b"audio")
        )
        == "hold"
    )
    assert calls == 2


def test_transcript_service_wraps_provider_decoding_errors_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def post(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.DecodingError(
            "malformed compressed response",
            request=httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions"),
        )

    monkeypatch.setattr(voice.httpx, "post", post)
    outcome = TranscriptService(
        transcription=OpenAIWhisperTransport(api_key="server-only-key"),
        compiler=SpyCompiler(),
        duration_probe=fixed_audio_duration,
    ).process(
        session_id=SESSION,
        correlation_id="voice-provider-decoding-error",
        content_type="audio/webm",
        body=b"audio",
        relay_state=valid_relay_state(),
        now_ms=1_756_700_000_001,
    )

    assert outcome.status == "refused"
    assert outcome.reason == "transcription_unavailable"
    assert outcome.emissions == ()
    assert calls == 1


def test_keyless_whisper_transport_never_starts_an_http_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    called = False

    def post(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("missing credentials must stop before HTTP")

    monkeypatch.setattr(voice.httpx, "post", post)

    with pytest.raises(TranscriptionError, match="OPENAI_API_KEY"):
        OpenAIWhisperTransport().transcribe(AudioUpload(content_type="audio/webm", body=b"audio"))

    assert called is False


def test_invalid_relay_state_never_reaches_transcription_or_the_compiler() -> None:
    transport = FixedTranscriptionTransport()
    compiler = SpyCompiler()

    outcome = TranscriptService(
        transcription=transport, compiler=compiler, duration_probe=fixed_audio_duration
    ).process(
        session_id=SESSION,
        correlation_id="voice-request-invalid-state",
        content_type="audio/webm",
        body=b"audio",
        relay_state={"type": "state", "mode": "indoor"},
        now_ms=1_756_700_000_001,
    )

    assert (outcome.reason, outcome.emissions) == ("invalid_relay_state", ())
    assert transport.calls == 0
    assert compiler.calls == []


def test_invalid_room_labels_never_reach_transcription_or_the_compiler() -> None:
    transport = FixedTranscriptionTransport()
    compiler = SpyCompiler()

    outcome = TranscriptService(
        transcription=transport, compiler=compiler, duration_probe=fixed_audio_duration
    ).process(
        session_id=SESSION,
        correlation_id="voice-request-invalid-rooms",
        content_type="audio/webm",
        body=b"audio",
        relay_state=valid_relay_state(),
        rooms=("room name from untrusted text",),
        now_ms=1_756_700_000_001,
    )

    assert (outcome.reason, outcome.emissions) == ("invalid_relay_state", ())
    assert transport.calls == 0
    assert compiler.calls == []


def test_unhashable_room_values_return_a_typed_refusal_before_provider_io() -> None:
    transport = FixedTranscriptionTransport()
    compiler = SpyCompiler()

    outcome = TranscriptService(
        transcription=transport, compiler=compiler, duration_probe=fixed_audio_duration
    ).process(
        session_id=SESSION,
        correlation_id="voice-request-unhashable-room",
        content_type="audio/webm",
        body=b"audio",
        relay_state=valid_relay_state(),
        rooms=(["nested-room"],),  # type: ignore[arg-type]
        now_ms=1_756_700_000_001,
    )

    assert (outcome.reason, outcome.emissions) == ("invalid_relay_state", ())
    assert transport.calls == 0
    assert compiler.calls == []


def valid_relay_state() -> dict[str, object]:
    return {
        "v": 1,
        "t": 1_756_700_000_000,
        "type": "state",
        "event_id": "state-event-1",
        "session": SESSION,
        "roster_version": 1,
        "armed": False,
        "estop": False,
        "selection": [],
        "mode": "indoor",
        "drones": [],
    }


def test_transcript_service_preserves_injected_language_compilation_outcome() -> None:
    from language.contracts import OutcomeKind
    from relay.language_runtime import LanguageCompilationOutcome

    compilation = LanguageCompilationOutcome(
        kind=OutcomeKind.CLARIFY,
        source="anthropic",
        reason=None,
        detail="which drone?",
        pending_intent_id=None,
        intents=(),
        plan_digest=None,
        expires_at_ms=None,
        state_digest="facts-sha",
    )

    class Compiler:
        def compile(self, *_args: object, **_kwargs: object) -> tuple[object, None]:
            return compilation, None

    outcome = TranscriptService(
        transcription=FixedTranscriptionTransport(),
        compiler=Compiler(),
        duration_probe=fixed_audio_duration,
    ).process(
        session_id=SESSION,
        correlation_id="voice-compilation-1",
        content_type="audio/webm",
        body=b"audio",
        relay_state=valid_relay_state(),
        now_ms=1_756_700_000_001,
    )

    assert outcome.compilation is compilation
    assert (
        outcome.to_dict(session_id=SESSION, correlation_id="voice-compilation-1")["compilation"]
        == compilation.to_dict()
    )

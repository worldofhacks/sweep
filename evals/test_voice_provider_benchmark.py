from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.voice_provider_benchmark import run, word_errors
from relay import voice
from relay.tests.test_voice import opus_webm


@pytest.mark.parametrize(
    ("expected", "actual", "errors", "words"),
    [
        ("Hold now.", "hold now", 0, 2),
        ("hold now", "land", 2, 2),
        ("hold", "hold now", 1, 1),
        ("hold now", "hold", 1, 2),
    ],
)
def test_word_accuracy_counts_substitution_insertion_and_deletion(expected, actual, errors, words):
    assert word_errors(expected, actual) == (errors, words)


def test_benchmark_calls_both_providers_and_replay_never_reports_network_latency(
    monkeypatch, tmp_path: Path
):
    audio = tmp_path / "hold.webm"
    audio.write_bytes(opus_webm(1))
    manifest = tmp_path / "cases.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "id": f"case-{i}",
                    "audio": audio.name,
                    "content_type": "audio/webm",
                    "transcript": "hold",
                }
                for i in range(20)
            ]
        )
    )
    calls = []

    class Provider:
        def __init__(self, provider):
            self.provider = provider
            self.model = "nova-3" if provider == "deepgram" else "whisper-1"

        def transcribe(self, upload):
            calls.append((self.provider, upload.body))
            return "hold"

    monkeypatch.setattr(
        "evals.voice_provider_benchmark.configured_transcription",
        lambda *, provider: Provider(provider),
    )
    output = tmp_path / "results"
    live = run(manifest, output)
    assert len(calls) == 40
    assert all(row["latency_ms"] >= 0 for row in live["rows"])
    assert live["summary"]["deepgram"]["word_accuracy"] == 1
    assert live["summary"]["whisper"]["failures"] == 0
    monkeypatch.setattr(voice.httpx, "post", lambda *a, **kw: pytest.fail("replay network call"))
    replay = run(manifest, output, replay=True)
    assert len(calls) == 40
    assert all(row["latency_ms"] is None for row in replay["rows"])
    assert replay["summary"]["deepgram"]["failures"] == 0
    assert replay["summary"]["whisper"]["word_accuracy"] == 1
    assert (output / "live-results.json").exists()



def test_public_stop_preflight_replays_provider_cassettes_without_http(monkeypatch) -> None:
    fixture_root = Path(__file__).parent / "fixtures" / "voice_provider_preflight"
    source = json.loads((fixture_root / "source.json").read_text())
    audio = (fixture_root / source["audio"]).read_bytes()
    assert hashlib.sha256(audio).hexdigest() == source["sha256"]
    upload = voice.AudioUpload(source["content_type"], audio)

    monkeypatch.setattr(voice.httpx, "post", lambda *args, **kwargs: pytest.fail("replay HTTP"))
    for provider in ("deepgram", "whisper"):
        cassette = Path(__file__).parent / "cassettes" / f"voice-{provider}-wikimedia-stop-v1.json"
        replay = voice.ReplayTranscriptionTransport(cassette, provider=provider)
        assert replay.transcribe(upload) == "Stop."


def test_benchmark_rejects_transcript_only_fixture_before_provider_io(tmp_path: Path) -> None:
    manifest = tmp_path / "cases.json"
    manifest.write_text(json.dumps([{"id": f"case-{i}", "transcript": "hold"} for i in range(20)]))
    with pytest.raises(ValueError, match="needs id, audio"):
        run(manifest, tmp_path / "results")

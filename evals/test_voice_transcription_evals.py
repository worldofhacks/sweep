from __future__ import annotations

import json
from pathlib import Path

import pytest

from relay.voice import AudioUpload, ReplayTranscriptionTransport

_ROOT = Path(__file__).parent
_CASES = json.loads((_ROOT / "fixtures" / "voice_transcription_cases.json").read_text())
_REPLAY = ReplayTranscriptionTransport(_ROOT / "cassettes" / "voice-whisper-v1.json")


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
def test_voice_transcription_fixture_replays_without_network(case: dict[str, str]) -> None:
    upload = AudioUpload(content_type=case["content_type"], body=case["audio"].encode())

    assert _REPLAY.transcribe(upload) == case["transcript"]

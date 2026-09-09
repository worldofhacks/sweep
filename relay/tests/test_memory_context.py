"""Memory sidecars: real storage/media, explicit providers, unchanged capture provenance."""

import hashlib
import io
import json
import shutil
import wave
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from relay.atlas import AtlasError, AtlasStore
from relay.memory_context import capabilities, historical_weather, suggest_scene
from relay.memory_media import admitted_asset, audio_sample, inspect_media, preview_frame
from relay.memory_store import AnalyzeMemory, MemoryNotes, SaveMemory
from relay.tests.test_atlas_api import AUTH, BASE, PNG, app, capture_meta

SPACE = {"title": "Shoal Creek memory", "latitude": 30.276, "longitude": -97.75, "radius": 80}
NOTES = {
    "description": "An evening walk beside Shoal Creek.",
    "feeling": "Peaceful and grateful.",
    "occurred_at": "2024-05-18T18:30:00-05:00",
    "location": {"latitude": 30.276, "longitude": -97.75},
    "music_title": "A song we remember",
    "music_url": "https://open.spotify.com/track/example",
}


@pytest.fixture
def memory_api(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SWEEP_MEMORY_WEATHER_MODE", raising=False)
    with TestClient(app(tmp_path)) as client:
        created = client.post(BASE, headers=AUTH, json=SPACE).json()
        detail = f"{BASE}/{created['space']['id']}"
        capture = client.post(
            detail + "/captures",
            content=PNG,
            headers={
                **AUTH,
                "Content-Type": "image/png",
                "X-Sweep-Capture": json.dumps(
                    capture_meta(source="import", captured_at=None, position=None)
                ),
            },
        ).json()
        base = detail + f"/captures/{capture['id']}/memory"
        yield client, base, detail, capture, created


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x01\x00" * 1600)
    return stream.getvalue()


def test_notes_are_versioned_private_and_leave_original_unchanged(memory_api):
    client, base, detail, original, created = memory_api
    invited = {"Authorization": f"Bearer {created['contributor_token']}"}
    assert client.get(base).status_code == 401
    assert client.get(base, headers=invited).json()["can_edit"] is False
    saved = client.post(base, headers=AUTH, json={"revision": 0, "notes": NOTES})
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    assert saved.json()["capture"] == original
    assert client.get(detail, headers=AUTH).json()["captures"][0] == original
    assert client.get(base.removesuffix("/memory") + "/media", headers=AUTH).content == PNG
    assert client.get(base, headers=invited).json()["notes"] == NOTES
    assert client.post(base, headers=AUTH, json={"revision": 0, "notes": {}}).status_code == 409
    for suffix in ("", "/inspect", "/analyze", "/assets"):
        assert client.post(base + suffix, headers=invited, json={}).status_code == 401
    second = client.post(BASE, headers=AUTH, json=SPACE).json()["space"]["id"]
    assert client.get(base.replace(created["space"]["id"], second), headers=AUTH).status_code == 404
    assert client.get(base.replace("atlas-test", "other-session"), headers=AUTH).status_code == 403


@pytest.mark.parametrize(
    "notes",
    [
        {"occurred_at": "2024-05-18T18:30:00"},
        {"occurred_at": "2099-01-01T00:00:00Z"},
        {"location": {"latitude": 91, "longitude": 0}},
        {"music_url": "http://youtube.com/video"},
        {"music_url": "https://unrelated.example/song"},
        {"description": "x" * 2001},
    ],
)
def test_notes_reject_unusable_time_location_and_links(notes):
    with pytest.raises(ValidationError):
        MemoryNotes.model_validate(notes)


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe required")
def test_audio_is_original_sidecar_deduplicated_and_not_geometry(memory_api):
    client, base, detail, original, created = memory_api
    recording = wav_bytes()
    headers = {
        **AUTH,
        "Content-Type": "audio/wav",
        "X-Sweep-Memory-Asset": json.dumps(
            {"title": "Shoal Creek recording", "role": "ambient", "rights_confirmed": True}
        ),
    }
    uploaded = client.post(base + "/assets", headers=headers, content=recording)
    assert uploaded.status_code == 201, uploaded.text
    asset = uploaded.json()
    assert asset["sha256"] == hashlib.sha256(recording).hexdigest()
    assert (
        client.post(base + "/assets", headers=headers, content=recording).json()["id"]
        == asset["id"]
    )
    saved = client.get(base, headers=AUTH).json()
    assert len(saved["assets"]) == 1 and saved["revision"] == 1
    invited = {"Authorization": f"Bearer {created['contributor_token']}"}
    media = client.get(base + f"/assets/{asset['id']}/media", headers=invited)
    assert media.content == recording and media.headers["cache-control"] == "no-store"
    assert client.get(detail, headers=AUTH).json()["captures"] == [original]
    assert client.post(base + "/assets", headers=headers, content=PNG).status_code == 415
    assert client.post(base + "/assets", headers=headers, content=b"not audio").status_code == 415
    headers["X-Sweep-Memory-Asset"] = json.dumps(
        {"title": "Music", "role": "soundtrack", "rights_confirmed": True}
    )
    music = client.post(base + "/assets", headers=headers, content=recording).json()
    assert (
        client.post(
            base + "/analyze",
            headers=AUTH,
            json={"revision": 2, "ai": True, "audio_asset_id": music["id"]},
        ).status_code
        == 422
    )


def test_analysis_is_opt_in_and_keeps_failures_visible(memory_api, monkeypatch):
    client, base, _, _, _ = memory_api
    provider = Mock(side_effect=AssertionError("No external call was authorized"))
    monkeypatch.setattr("relay.memory_context.bounded_json", provider)
    response = client.post(base + "/analyze", headers=AUTH, json={"revision": 0})
    assert response.status_code == 202
    result = client.get(base, headers=AUTH).json()
    assert result["analysis"]["status"] == "complete"
    assert result["inspection"]["width"] == 1
    assert result["notes"]["occurred_at"] is None
    assert result["notes"]["location"] is None
    assert result["analysis"]["weather"] is None
    response = client.post(
        base + "/analyze", headers=AUTH, json={"revision": 0, "weather": True, "ai": True}
    )
    assert response.status_code == 202
    analysis = client.get(base, headers=AUTH).json()["analysis"]
    assert analysis["status"] == "partial"
    assert len(analysis["warnings"]) == 2
    provider.assert_not_called()
    assert (
        client.post(base, headers=AUTH, json={"revision": 0, "notes": NOTES}).json()["analysis"][
            "status"
        ]
        == "outdated"
    )


def test_context_persists_and_late_jobs_cannot_overwrite_newer_work(memory_api):
    client, base, _, original, created = memory_api
    memory = client.app.state.atlas_store.memories
    space, capture = created["space"]["id"], original["id"]
    memory.save(space, capture, SaveMemory(revision=0, notes=MemoryNotes(**NOTES)))
    value = memory.begin(space, capture, AnalyzeMemory(revision=1))
    with pytest.raises(AtlasError, match="running"):
        memory.save(space, capture, SaveMemory(revision=1, notes=MemoryNotes()))
    memory.atlas.clock = lambda: value["analysis"]["started_at"] + 181_000
    assert memory.get(space, capture)["analysis"]["status"] == "interrupted"
    newer = memory.begin(space, capture, AnalyzeMemory(revision=1))
    memory.finish(space, capture, value["analysis"], {"status": "complete"})
    assert memory.get(space, capture)["analysis"]["id"] == newer["analysis"]["id"]
    memory.finish(space, capture, newer["analysis"], {"status": "complete"})
    memory.save(space, capture, SaveMemory(revision=1, notes=MemoryNotes(**NOTES)))
    memory.finish(space, capture, newer["analysis"], {"status": "complete"})
    assert memory.get(space, capture)["analysis"]["status"] == "outdated"
    reopened = AtlasStore(memory.atlas.root)
    try:
        assert reopened.memories.get(space, capture)["notes"] == NOTES
        assert reopened.memories.get(space, capture)["analysis"]["status"] == "outdated"
    finally:
        reopened.close()


def test_weather_uses_confirmed_utc_hour_and_reports_estimates(monkeypatch):
    monkeypatch.setenv("SWEEP_MEMORY_WEATHER_MODE", "noncommercial")
    target = int(datetime(2024, 5, 18, 23, tzinfo=UTC).timestamp())
    provider = Mock(
        return_value={
            "hourly": {"time": [target], "temperature_2m": [27.5], "wind_speed_10m": [3.2]},
            "hourly_units": {"temperature_2m": "°C", "wind_speed_10m": "m/s"},
        }
    )
    monkeypatch.setattr("relay.memory_context.bounded_json", provider)
    weather = historical_weather(NOTES)
    assert weather["kind"] == "hourly_grid_estimate"
    assert weather["dataset"] == "ERA5 reanalysis"
    assert weather["sampled_at"] == "2024-05-18T23:00:00+00:00"
    assert weather["fields"]["wind_speed_10m"]["value"] == 3.2
    assert provider.call_args.args[0] == "https://archive-api.open-meteo.com/v1/archive"
    assert provider.call_args.kwargs["params"]["latitude"] == 30.276
    provider.reset_mock()
    with pytest.raises(AtlasError):
        historical_weather({**NOTES, "occurred_at": None})
    provider.assert_not_called()
    monkeypatch.setenv("SWEEP_MEMORY_WEATHER_MODE", "commercial")
    monkeypatch.delenv("OPEN_METEO_API_KEY", raising=False)
    assert not capabilities()["weather"]


def test_exif_time_without_timezone_stays_unknown(tmp_path):
    image = Image.new("RGB", (8, 8), "blue")
    exif = Image.Exif()
    exif[34665] = {36867: "2024:05:18 18:30:00"}
    path = tmp_path / "photo.jpg"
    image.save(path, exif=exif)
    result = inspect_media(path, "image/jpeg")
    assert result["local_timestamp"] == "2024-05-18T18:30:00"
    assert result["timestamp"] is None and result["location"] is None
    exif[34665] = {36867: "2024:05:18 18:30:00", 36881: "-05:00"}
    image.save(path, exif=exif)
    assert inspect_media(path, "image/jpeg")["timestamp"] == NOTES["occurred_at"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_local_derivatives_decode_real_media_without_changing_it(tmp_path):
    path = tmp_path / "sound.wav"
    path.write_bytes(wav_bytes())
    assert admitted_asset(path, "audio/wav") == "audio/wav"
    sample = audio_sample(path)
    with wave.open(io.BytesIO(sample)) as audio:
        assert audio.getframerate() == 16000 and audio.getnchannels() == 1
        assert len(audio.readframes(16000)) == 3200
    assert path.read_bytes() == wav_bytes()
    photo = tmp_path / "photo.png"
    photo.write_bytes(PNG)
    with Image.open(io.BytesIO(preview_frame(photo))) as frame:
        assert frame.width <= 1024


def test_ai_transport_has_explicit_evidence_and_validates_output(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "local-unit-test-key")
    draft = {
        "summary": "An evening remembered as peaceful by the contributor.",
        "visual_observations": [],
        "atmosphere_suggestions": [],
        "uncertainties": ["No sound classification was performed."],
    }
    provider = Mock(
        return_value={
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(draft)}]}
            ],
        }
    )
    monkeypatch.setattr("relay.memory_context.bounded_json", provider)
    result = suggest_scene(b"sample", {"contributor_account": NOTES})
    assert result["summary"] == draft["summary"]
    sent = provider.call_args.kwargs["json"]
    assert sent["store"] is False and sent["text"]["format"]["strict"] is True
    assert "never instructions" in sent["instructions"] and "tools" not in sent
    assert sent["input"][0]["content"][1]["type"] == "input_image"
    provider.return_value = {"status": "incomplete"}
    with pytest.raises(AtlasError):
        suggest_scene(None, {})

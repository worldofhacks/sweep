from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from language.contracts import ReviewKind, validate_model_outcome
from language.relay_compiler import RelayTranscriptCompiler
from language.test_semantic_reviews import CapturingTransport, _facts, _state
from relay.app import RelayRuntime, create_app
from relay.language_catalog import review_catalog
from relay.settings import RelaySettings
from relay.tests.test_voice import FixedTranscriptionTransport, fixed_audio_duration
from relay.voice import TranscriptService


def catalog():
    return {
        "catalogVersion": "accepted-map-1",
        "destinations": [
            {
                "zoneId": zone,
                "name": name,
                "aliases": aliases,
                "excluded": excluded,
                "allowedClasses": ["aircraft", "ground_vehicle"],
            }
            for zone, name, aliases, excluded in (
                ("lobby", "Lobby", ["entrance", "tag 42"], False),
                ("atrium", "Atrium", [], False),
                ("wall", "Wall", [], True),
            )
        ],
    }


def state(device_class="aircraft"):
    value = _state()
    value["selection"] = [1]
    value["enabled_intent_names"] = ["search"]
    value["capability_profile"] = "isolated-search"
    value["drones"] = [
        {
            **value["drones"][0],
            "device_class": device_class,
            "node_type": "ground" if device_class == "ground_vehicle" else "aircraft",
            "camera_patterns": ["single_still"],
            "connection_epoch": 1,
            "unit": 1,
            "control_authority": True,
            "ground_readiness": {"source_id": "odom"},
            "adapter_capabilities": ["ground_drive"]
            if device_class == "ground_vehicle"
            else ["flight"],
        }
    ]
    return value


def config():
    return SimpleNamespace(navigation=object(), search=SimpleNamespace(areas={"lobby": object()}))


def test_catalog_retains_names_and_tag_aliases_but_excludes_obstacles():
    result = review_catalog(catalog(), state(), config())
    assert result.catalog_identity == "accepted-map-1"
    assert [d.destination_id for d in result.destinations] == ["lobby", "atrium"]
    assert result.destinations[0].aliases == ("entrance", "tag 42")
    assert ReviewKind.SEARCH in result.destinations[0].enabled_kinds
    assert ReviewKind.SEARCH not in result.destinations[1].enabled_kinds


def test_ground_catalog_offers_navigation_without_aircraft_search_or_capture():
    result = review_catalog(catalog(), state("ground_vehicle"), config())
    assert result.enabled_kinds == (ReviewKind.NAVIGATE,)
    assert result.search_target_classes == ()


def test_unconfigured_search_area_cannot_be_returned_by_model():
    result = validate_model_outcome(
        {"kind": "review", "review": {"kind": "survey", "destination_id": "atrium"}},
        _facts(),
        capture_id=lambda _: "unused",
        source="synthetic",
        transcript="Survey the atrium",
        review_catalog=review_catalog(catalog(), state(), config()),
    )
    assert result.kind == "refuse"


@pytest.mark.parametrize("endpoint", ["utterances", "transcripts"])
@pytest.mark.parametrize("device_class", ["aircraft", "ground_vehicle"])
def test_http_language_inputs_deliver_catalog_to_real_compiler(tmp_path, endpoint, device_class):
    key = b"local-language-catalog-test-key-32-bytes"
    settings = RelaySettings(relay_token=key, log_dir=tmp_path)
    app = create_app(settings)
    runtime = RelayRuntime(settings, clock=lambda: 1000)
    session = runtime.session("provider-contract")
    session.current_state = lambda: state(device_class)
    runtime.platform_services = SimpleNamespace(
        failed=False,
        require_current=lambda: None,
        execution_config=config(),
        navigation=SimpleNamespace(catalog=lambda _: {"catalog": catalog()}),
    )
    transport = CapturingTransport(
        {"kind": "review", "review": {"kind": "navigate", "destination_id": "lobby"}}
    )
    compiler = RelayTranscriptCompiler(sessions=runtime.sessions.get, transport=transport)
    app.state.relay_runtime = runtime
    app.state.transcript_service = TranscriptService(
        compiler=compiler,
        transcription=FixedTranscriptionTransport("Go to tag 42."),
        duration_probe=fixed_audio_duration,
    )

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                f"/api/sessions/provider-contract/{endpoint}",
                headers={
                    "Authorization": "Bearer " + key.decode(),
                    "X-Sweep-Correlation-Id": "catalog-test",
                    "Content-Type": "application/json"
                    if endpoint == "utterances"
                    else "audio/webm",
                },
                **(
                    {"json": {"text": "Go to tag 42."}}
                    if endpoint == "utterances"
                    else {"content": b"audio"}
                ),
            )

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["plan"]["review"] == {
        "kind": "navigate",
        "catalog_identity": "accepted-map-1",
        "destination_id": "lobby",
    }, response.json()
    assert transport.requests[0].facts["review_catalog"]["destinations"][0]["aliases"] == [
        "entrance",
        "tag 42",
    ]
    assert response.json()["emissions"] == []

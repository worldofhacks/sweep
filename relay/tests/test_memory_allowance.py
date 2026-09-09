"""Real SQLite request ceilings; all provider calls use disposable test doubles."""

import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import httpx
import pytest

from relay.atlas import AtlasError, AtlasStore
from relay.memory_allowance import DAY_MS, SETTINGS
from relay.memory_store import AnalyzeMemory, CancelMemory
from relay.tests.test_atlas_api import AUTH, BASE, PNG, capture_meta
from relay.tests.test_memory_context import SPACE, wav_bytes
from relay.tests.test_memory_context import memory_api as memory_api


@pytest.fixture
def allowance(memory_api, monkeypatch):
    client, _, _, capture, created = memory_api
    memory = client.app.state.atlas_store.memories
    for setting in SETTINGS.values():
        monkeypatch.setenv(setting, "10")
    monkeypatch.setattr(memory.atlas, "clock", lambda: 20_000 * DAY_MS)
    return memory, created["space"]["id"], capture["id"]


def begin(memory, space, capture, **changes):
    request = AnalyzeMemory(revision=0, ai=True, weather=True, **changes)
    return memory.begin(space, capture, request)["analysis"]


def extra_space(client):
    space = client.post(BASE, headers=AUTH, json=SPACE).json()["space"]["id"]
    response = client.post(
        f"{BASE}/{space}/captures",
        content=PNG,
        headers={
            **AUTH,
            "Content-Type": "image/png",
            "X-Sweep-Capture": json.dumps(
                capture_meta(source="import", captured_at=None, position=None)
            ),
        },
    )
    assert response.status_code == 201, response.text
    return space, response.json()["id"]


@pytest.mark.parametrize("raw", ["", "-1", "1.0", "false", " 3", "３", "1000001", "00000000"])
def test_invalid_configuration_fails_closed_without_reservation(allowance, monkeypatch, raw):
    memory, sid, cid = allowance
    job = begin(memory, sid, cid)
    monkeypatch.setenv(SETTINGS["space"], raw)
    with pytest.raises(AtlasError, match="not validly configured"):
        memory.allowance.claim(cid, job["id"], "suggestion")
    assert memory.atlas.db.execute("SELECT count(*) FROM memory_provider_counts").fetchone()[0] == 0
    assert memory.allowance.status(sid)["error"]


@pytest.mark.parametrize("scope", ["space", "relay"])
def test_each_missing_allowance_disables_external_calls(allowance, monkeypatch, scope):
    memory, sid, cid = allowance
    job = begin(memory, sid, cid)
    monkeypatch.delenv(SETTINGS[scope])
    with pytest.raises(AtlasError, match="needs explicit"):
        memory.allowance.claim(cid, job["id"], "weather")
    assert memory.allowance.status(sid)["remaining"] == 0


def test_stage_claims_once_and_daily_limits_survive_reopen(allowance, monkeypatch):
    memory, sid, cid = allowance
    monkeypatch.setenv(SETTINGS["space"], "2")
    job = begin(memory, sid, cid)
    memory.allowance.claim(cid, job["id"], "weather")
    memory.allowance.claim(cid, job["id"], "transcript")
    with pytest.raises(AtlasError, match="already reserved"):
        memory.allowance.claim(cid, job["id"], "weather")
    reopened = AtlasStore(memory.atlas.root, clock=memory.atlas.clock)
    try:
        with pytest.raises(AtlasError, match="space daily"):
            reopened.memories.allowance.claim(cid, job["id"], "suggestion")
        assert reopened.memories.allowance.status(sid)["reserved"] == {"space": 2, "relay": 2}
        with pytest.raises(AtlasError, match="already reserved"):
            reopened.memories.allowance.claim(cid, job["id"], "transcript")
    finally:
        reopened.close()


def test_sqlite_serializes_competing_connections_at_relay_limit(allowance, memory_api, monkeypatch):
    memory, sid, cid = allowance
    sid2, cid2 = extra_space(memory_api[0])
    first, second = begin(memory, sid, cid), begin(memory, sid2, cid2)
    monkeypatch.setenv(SETTINGS["relay"], "1")
    reopened = AtlasStore(memory.atlas.root, clock=memory.atlas.clock)

    def claim(store, capture, job):
        try:
            store.allowance.claim(capture, job["id"], "suggestion")
            return "admitted"
        except AtlasError as error:
            return error.status

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [
                pool.submit(claim, memory, cid, first),
                pool.submit(claim, reopened.memories, cid2, second),
            ]
            assert sorted(str(t.result()) for t in tasks) == ["429", "admitted"]
        counts = memory.atlas.db.execute(
            "SELECT scope,reserved FROM memory_provider_counts"
        ).fetchall()
        assert len(counts) == 2 and all(n == 1 for _, n in counts)
    finally:
        reopened.close()


def test_space_ceiling_does_not_use_another_spaces_allowance(allowance, memory_api, monkeypatch):
    memory, sid, cid = allowance
    sid2, cid2 = extra_space(memory_api[0])
    monkeypatch.setenv(SETTINGS["space"], "1")
    first = begin(memory, sid, cid)
    memory.allowance.claim(cid, first["id"], "weather")
    with pytest.raises(AtlasError, match="space daily"):
        memory.allowance.claim(cid, first["id"], "suggestion")
    second = begin(memory, sid2, cid2)
    memory.allowance.claim(cid2, second["id"], "suggestion")
    assert memory.allowance.status(sid2)["reserved"] == {"space": 1, "relay": 2}


def test_midnight_charges_dispatch_day_but_never_repeats_old_stage(allowance, monkeypatch):
    memory, sid, cid = allowance
    job = begin(memory, sid, cid)
    memory.allowance.claim(cid, job["id"], "weather")
    monkeypatch.setattr(memory.atlas, "clock", lambda: 20_001 * DAY_MS)
    memory.allowance.claim(cid, job["id"], "suggestion")
    assert memory.allowance.status(sid)["reserved"] == {"space": 1, "relay": 1}
    assert memory.allowance.status(sid)["resets_at"] == 20_002 * DAY_MS
    with pytest.raises(AtlasError, match="already reserved"):
        memory.allowance.claim(cid, job["id"], "weather")
    monkeypatch.setattr(memory.atlas, "clock", lambda: 20_000 * DAY_MS)
    with pytest.raises(AtlasError, match="clock moved backwards"):
        memory.allowance.claim(cid, job["id"], "transcript")
    assert memory.allowance.status(sid)["remaining"] == 0
    assert "clock moved backwards" in memory.allowance.status(sid)["error"]


def test_old_aggregates_prune_but_crashed_claims_do_not(allowance, monkeypatch):
    memory, sid, cid = allowance
    job = begin(memory, sid, cid)
    memory.allowance.claim(cid, job["id"], "weather")
    monkeypatch.setattr(memory.atlas, "clock", lambda: 20_031 * DAY_MS)
    memory.allowance.claim(cid, job["id"], "suggestion")
    assert memory.atlas.db.execute(
        "SELECT DISTINCT day FROM memory_provider_counts"
    ).fetchall() == [(20_031,)]
    assert memory.atlas.db.execute("SELECT count(*) FROM memory_provider_claims").fetchone()[0] == 2
    with pytest.raises(AtlasError, match="already reserved"):
        memory.allowance.claim(cid, job["id"], "weather")


def test_failed_transaction_does_not_partially_consume_allowance(allowance):
    memory, sid, cid = allowance
    job = begin(memory, sid, cid)
    memory.atlas.db.execute(
        "CREATE TEMP TRIGGER test_no_claim BEFORE INSERT ON memory_provider_claims "
        "BEGIN SELECT RAISE(ABORT, 'disposable storage fault'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="disposable storage fault"):
        memory.allowance.claim(cid, job["id"], "weather")
    assert memory.atlas.db.execute("SELECT count(*) FROM memory_provider_counts").fetchone()[0] == 0
    memory.atlas.db.execute("DROP TRIGGER test_no_claim")
    memory.allowance.claim(cid, job["id"], "weather")
    assert memory.allowance.status(sid)["reserved"]["relay"] == 1


def test_finish_cancellation_removal_never_refund_possible_provider_usage(allowance):
    memory, sid, cid = allowance
    job = begin(memory, sid, cid)
    memory.allowance.claim(cid, job["id"], "weather")
    memory.cancel(sid, cid, CancelMemory(analysis_id=job["id"]))
    with pytest.raises(AtlasError, match="no longer available"):
        memory.allowance.claim(cid, job["id"], "suggestion")
    plan = memory.atlas.removal.preview(sid, cid, operator=True)
    memory.atlas.removal.remove(sid, cid, plan["confirmation"], operator=True)
    with pytest.raises(AtlasError, match="removed"):
        memory.allowance.claim(cid, job["id"], "suggestion")
    assert memory.atlas.db.execute("SELECT count(*) FROM memory_provider_claims").fetchone()[0] == 1
    memory.finish(sid, cid, job, {"status": "failed"})
    assert memory.atlas.db.execute("SELECT count(*) FROM memory_provider_claims").fetchone()[0] == 0
    assert memory.allowance.status(sid)["reserved"] == {"space": 1, "relay": 1}
    rows = repr(memory.atlas.db.execute("SELECT * FROM memory_provider_counts").fetchall())
    assert sid not in rows and cid not in rows and job["id"] not in rows


def test_late_or_wrong_scope_completion_cannot_release_active_claim(allowance, memory_api):
    memory, sid, cid = allowance
    sid2, cid2 = extra_space(memory_api[0])
    job = begin(memory, sid, cid)
    memory.allowance.claim(cid, job["id"], "weather")
    memory.finish(sid2, cid2, job, {"status": "failed"})
    with pytest.raises(AtlasError, match="already reserved"):
        memory.allowance.claim(cid, job["id"], "weather")
    memory.finish(sid, cid, job, {"status": "complete"})
    newer = begin(memory, sid, cid)
    memory.allowance.claim(cid, newer["id"], "weather")
    memory.finish(sid, cid, job, {"status": "failed"})
    with pytest.raises(AtlasError, match="already reserved"):
        memory.allowance.claim(cid, newer["id"], "weather")


def test_unrequested_unknown_and_wrong_job_stages_are_not_admitted(allowance):
    memory, sid, cid = allowance
    job = memory.begin(sid, cid, AnalyzeMemory(revision=0))["analysis"]
    for stage in ("weather", "transcript", "suggestion"):
        with pytest.raises(AtlasError, match="not requested"):
            memory.allowance.claim(cid, job["id"], stage)
    with pytest.raises(AtlasError, match="Unknown"):
        memory.allowance.claim(cid, job["id"], "other")
    with pytest.raises(AtlasError, match="no longer available"):
        memory.allowance.claim(cid, str(uuid.uuid4()), "weather")
    assert memory.allowance.status(sid)["reserved"]["relay"] == 0


def test_http_retry_legacy_and_failed_provider_respect_same_allowance(
    allowance, memory_api, monkeypatch
):
    memory, sid, _ = allowance
    client, base, _, _, created = memory_api
    monkeypatch.setenv(SETTINGS["space"], "1")
    monkeypatch.setenv("OPENAI_API_KEY", "disposable-key-never-sent")
    monkeypatch.setattr("relay.memory_context.preview_frame", lambda *a: None)
    provider = Mock(side_effect=TimeoutError("synthetic lost provider response"))
    monkeypatch.setattr("relay.memory_context.suggest_scene", provider)
    body = {"revision": 0, "ai": True, "request_id": str(uuid.uuid4())}
    assert client.post(base + "/analyze", headers=AUTH, json=body).status_code == 202
    assert client.post(base + "/analyze", headers=AUTH, json=body).status_code == 200
    legacy = client.post(base + "/analyze", headers=AUTH, json={"revision": 0, "ai": True})
    assert legacy.status_code == 202
    result = client.get(base, headers=AUTH).json()
    assert result["analysis"]["status"] == "partial"
    assert any(
        "daily memory provider request allowance" in w for w in result["analysis"]["warnings"]
    )
    assert provider.call_count == 1
    assert result["analysis_allowance"]["remaining"] == 0
    invited = {"Authorization": "Bearer " + created["contributor_token"]}
    assert "analysis_allowance" not in client.get(base, headers=invited).json()
    assert client.post(base + "/analyze", headers=invited, json=body).status_code == 401
    assert memory.allowance.status(sid)["reserved"] == {"space": 1, "relay": 1}


def test_every_external_stage_is_bounded_and_local_media_remains_available(
    allowance, memory_api, monkeypatch
):
    memory, sid, _ = allowance
    client, base, detail, capture, _ = memory_api
    monkeypatch.setenv(SETTINGS["space"], "2")
    monkeypatch.setenv("OPENAI_API_KEY", "disposable-key-never-sent")
    monkeypatch.setenv("SWEEP_MEMORY_WEATHER_MODE", "noncommercial")
    monkeypatch.setattr("relay.memory_context.inspect_media", lambda *a: {"has_audio": True})
    monkeypatch.setattr("relay.memory_context.audio_sample", lambda *a: wav_bytes())
    monkeypatch.setattr("relay.memory_context.preview_frame", lambda *a: None)
    weather, transcript, suggestion = (
        Mock(return_value={}),
        Mock(return_value="test speech"),
        Mock(),
    )
    monkeypatch.setattr("relay.memory_context.historical_weather", weather)
    monkeypatch.setattr("relay.voice.OpenAIWhisperTransport.transcribe", transcript)
    monkeypatch.setattr("relay.memory_context.suggest_scene", suggestion)
    response = client.post(
        base + "/analyze", headers=AUTH, json={"revision": 0, "ai": True, "weather": True}
    )
    assert response.status_code == 202
    assert weather.call_count == transcript.call_count == 1
    suggestion.assert_not_called()
    assert memory.allowance.status(sid)["reserved"] == {"space": 2, "relay": 2}
    assert client.get(detail + f"/captures/{capture['id']}/media", headers=AUTH).content == PNG
    assert (
        client.post(
            base, headers=AUTH, json={"revision": 0, "notes": {"description": "Still editable"}}
        ).status_code
        == 200
    )


def test_provider_storage_fault_prevents_dispatch(allowance, memory_api, monkeypatch):
    memory, sid, _ = allowance
    client, base, _, _, _ = memory_api
    monkeypatch.setenv("OPENAI_API_KEY", "disposable-key-never-sent")
    monkeypatch.setattr("relay.memory_context.preview_frame", lambda *a: None)
    provider = Mock()
    monkeypatch.setattr("relay.memory_context.suggest_scene", provider)
    monkeypatch.setattr(
        memory.allowance,
        "claim",
        Mock(side_effect=sqlite3.OperationalError("synthetic storage fault")),
    )
    assert (
        client.post(base + "/analyze", headers=AUTH, json={"revision": 0, "ai": True}).status_code
        == 202
    )
    provider.assert_not_called()
    assert memory.allowance.status(sid)["reserved"]["relay"] == 0


@pytest.mark.parametrize("failure", ["timeout", "connection", 429, 503])
def test_memory_transcription_makes_one_http_attempt_even_on_retryable_failure(
    allowance, memory_api, monkeypatch, failure
):
    memory, sid, _ = allowance
    client, base, _, _, _ = memory_api
    monkeypatch.setenv("OPENAI_API_KEY", "disposable-key-never-sent")
    monkeypatch.setattr("relay.memory_context.inspect_media", lambda *a: {"has_audio": True})
    monkeypatch.setattr("relay.memory_context.audio_sample", lambda *a: wav_bytes())
    monkeypatch.setattr("relay.memory_context.preview_frame", lambda *a: None)
    monkeypatch.setattr(
        "relay.memory_context.suggest_scene", Mock(return_value={"summary": "test"})
    )

    def failed(*args, **kwargs):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic lost reply")
        if failure == "connection":
            raise httpx.ConnectError("synthetic failed connection")
        return httpx.Response(failure, request=httpx.Request("POST", "https://example.test"))

    transport = Mock(side_effect=failed)
    monkeypatch.setattr("relay.voice.httpx.post", transport)
    body = {"revision": 0, "ai": True, "request_id": str(uuid.uuid4())}
    assert client.post(base + "/analyze", headers=AUTH, json=body).status_code == 202
    assert client.post(base + "/analyze", headers=AUTH, json=body).status_code == 200
    assert transport.call_count == 1
    assert memory.allowance.status(sid)["reserved"] == {"space": 2, "relay": 2}


@pytest.mark.parametrize("attempts", [0, -1, 3, True, 1.5, "1"])
def test_transcription_cannot_expand_existing_retry_ceiling(attempts):
    from relay.voice import OpenAIWhisperTransport

    with pytest.raises(ValueError, match="supported transcription attempt"):
        OpenAIWhisperTransport(max_attempts=attempts)

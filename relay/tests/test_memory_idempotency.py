"""Analysis intent deduplication uses real SQLite/HTTP and disposable mocked workers."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest

from relay.atlas import AtlasError, AtlasStore
from relay.memory_store import AnalyzeMemory, CancelMemory, MemoryNotes, SaveMemory
from relay.tests.test_atlas_api import AUTH
from relay.tests.test_memory_context import memory_api as memory_api


def intent(**changes):
    return {"revision": 0, "ai": True, "request_id": str(uuid.uuid4()), **changes}


def test_lost_reply_retry_and_old_intent_never_start_duplicate_work(memory_api, monkeypatch):
    client, base, _, original, created = memory_api
    worker = Mock(return_value={"status": "complete", "suggestion": {"summary": "Draft"}})
    monkeypatch.setattr("relay.memory_routes.analyze", worker)
    body = intent()
    started = client.post(base + "/analyze", headers=AUTH, json=body)
    assert started.status_code == 202
    first = started.json()["analysis_request"]
    assert first == {
        "id": body["request_id"],
        "analysis_id": started.json()["analysis"]["id"],
        "replayed": False,
        "current": True,
        "rejection": None,
    }
    replay = client.post(base + "/analyze", headers=AUTH, json=body)
    assert replay.status_code == 200
    assert replay.headers["cache-control"] == "no-store"
    assert replay.json()["analysis"]["status"] == "complete"
    assert replay.json()["analysis_request"] == {**first, "replayed": True}
    assert worker.call_count == 1
    changed = client.post(base + "/analyze", headers=AUTH, json={**body, "weather": True})
    assert changed.status_code == 409
    assert worker.call_count == 1
    newer = client.post(base + "/analyze", headers=AUTH, json=intent()).json()
    assert worker.call_count == 2
    old = client.post(base + "/analyze", headers=AUTH, json=body).json()
    assert old["analysis"]["id"] == newer["analysis"]["id"]
    assert old["analysis_request"] == {**first, "replayed": True, "current": False}
    assert worker.call_count == 2
    assert old["capture"] == original
    invited = {"Authorization": "Bearer " + created["contributor_token"]}
    assert client.post(base + "/analyze", headers=invited, json=body).status_code == 401


def test_duplicate_recovers_while_all_worker_slots_are_busy(memory_api, monkeypatch):
    client, base, _, _, _ = memory_api
    analysis_entered, inspect_entered, release = Event(), Event(), Event()
    calls = []

    def work(*args):
        calls.append(True)
        analysis_entered.set()
        assert release.wait(10)
        return {"status": "complete"}

    def inspect(*args):
        inspect_entered.set()
        assert release.wait(10)
        return {"has_audio": False}

    monkeypatch.setattr("relay.memory_routes.analyze", work)
    monkeypatch.setattr("relay.memory_routes.inspect_media", inspect)
    body = intent()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, base + "/analyze", headers=AUTH, json=body)
        local = None
        try:
            assert analysis_entered.wait(5)
            local = pool.submit(client.post, base + "/inspect", headers=AUTH)
            assert inspect_entered.wait(5)
            replay = client.post(base + "/analyze", headers=AUTH, json=body)
            assert replay.status_code == 200
            assert replay.json()["analysis"]["status"] == "running"
            assert replay.json()["analysis_request"]["replayed"]
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 202
        assert local.result(timeout=5).status_code == 200


@pytest.mark.parametrize("status", ["complete", "partial", "failed", "cancelled", "interrupted"])
def test_reopen_keeps_request_identity_in_every_outcome(memory_api, status):
    client, _, _, capture, created = memory_api
    store = client.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]
    request = AnalyzeMemory(**intent())
    value = store.memories.begin(sid, cid, request)
    job = value["analysis"]
    if status == "cancelled":
        store.memories.cancel(sid, cid, CancelMemory(analysis_id=job["id"]))
    if status != "interrupted":
        store.memories.finish(sid, cid, job, {"status": status})
    reopened = AtlasStore(store.root, clock=lambda: job["started_at"] + 181_000)
    try:
        reserve = Mock(return_value=True)
        admission = reopened.memories.admit(sid, cid, request, reserve=reserve)
        assert (
            admission.analysis_id == job["id"]
            and not admission.created
            and admission.rejection is None
        )
        assert admission.replayed
        assert admission.context["analysis"]["status"] == status
        reserve.assert_not_called()
        assert (
            reopened.db.execute("SELECT count(*) FROM memory_analysis_requests").fetchone()[0] == 1
        )
    finally:
        reopened.close()


def test_refused_intent_is_fenced_against_a_late_copy(memory_api, monkeypatch):
    client, base, _, capture, created = memory_api
    store = client.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]
    active = store.memories.begin(sid, cid, AnalyzeMemory(revision=0))["analysis"]
    worker = Mock(return_value={"status": "complete"})
    monkeypatch.setattr("relay.memory_routes.analyze", worker)
    body = intent()
    refused = client.post(base + "/analyze", headers=AUTH, json=body)
    assert refused.status_code == 200  # A definitive receipt, not an ambiguous transport failure.
    receipt = refused.json()["analysis_request"]
    assert receipt["analysis_id"] is None and receipt["rejection"] and not receipt["current"]
    store.memories.finish(sid, cid, active, {"status": "complete"})
    replay = client.post(base + "/analyze", headers=AUTH, json=body).json()
    assert replay["analysis_request"] == {**receipt, "replayed": True}
    assert replay["analysis"]["id"] == active["id"]
    worker.assert_not_called()
    assert client.post(base + "/analyze", headers=AUTH, json=intent()).status_code == 202
    assert worker.call_count == 1


def test_stale_notes_recovery_returns_current_story_without_analyzing_it(memory_api, monkeypatch):
    client, base, _, capture, created = memory_api
    store = client.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]
    store.memories.save(
        sid, cid, SaveMemory(revision=0, notes=MemoryNotes(description="A friend's newer story"))
    )
    worker = Mock()
    monkeypatch.setattr("relay.memory_routes.analyze", worker)
    body = intent()
    result = client.post(base + "/analyze", headers=AUTH, json=body).json()
    assert result["analysis_request"]["analysis_id"] is None
    assert result["notes"]["description"] == "A friend's newer story"
    assert result["revision"] == 1
    worker.assert_not_called()
    # The ledger contains a settings digest, never a second copy of the story.
    rows = store.db.execute("SELECT * FROM memory_analysis_requests").fetchall()
    assert "A friend's newer story" not in json.dumps(rows)


def test_request_limit_keeps_existing_replays_and_removal_erases_ledger(memory_api):
    client, _, _, capture, created = memory_api
    store = client.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]
    request = AnalyzeMemory(**intent())
    job = store.memories.begin(sid, cid, request)["analysis"]
    store.memories.finish(sid, cid, job, {"status": "complete"})
    with store.db:
        store.db.executemany(
            "INSERT INTO memory_analysis_requests VALUES (?,?,?,?,?,?)",
            [(sid, cid, str(uuid.uuid4()), "fixture", str(uuid.uuid4()), None) for _ in range(199)],
        )
    assert not store.memories.admit(sid, cid, request)[2]
    with pytest.raises(AtlasError, match="200 analysis-request"):
        store.memories.begin(sid, cid, AnalyzeMemory(**intent()))
    plan = store.removal.preview(sid, cid, operator=True)
    store.removal.remove(sid, cid, plan["confirmation"], operator=True)
    assert store.db.execute("SELECT count(*) FROM memory_analysis_requests").fetchone()[0] == 0
    with pytest.raises(AtlasError, match="Capture not found"):
        store.memories.admit(sid, cid, request)


def test_failed_commit_releases_slot_and_does_not_leave_a_deduplication_record(
    memory_api, monkeypatch
):
    client, base, _, _, _ = memory_api
    memory = client.app.state.atlas_store.memories
    write = memory._write
    monkeypatch.setattr(
        memory, "_write", Mock(side_effect=RuntimeError("disposable write failure"))
    )
    body = intent()
    for _ in range(3):
        with pytest.raises(RuntimeError, match="disposable write failure"):
            client.post(base + "/analyze", headers=AUTH, json=body)
    assert (
        memory.atlas.db.execute("SELECT count(*) FROM memory_analysis_requests").fetchone()[0] == 0
    )
    assert (
        memory.atlas.db.execute("SELECT count(*) FROM atlas_source_operations").fetchone()[0] == 0
    )
    monkeypatch.setattr(memory, "_write", write)
    worker = Mock(return_value={"status": "complete"})
    monkeypatch.setattr("relay.memory_routes.analyze", worker)
    assert client.post(base + "/analyze", headers=AUTH, json=body).status_code == 202
    assert worker.call_count == 1


@pytest.mark.parametrize(
    "key, status",
    [("", 422), ("../job", 422), ("z" * 36, 422), ("0" * 36, 422), ("a" * 10000, 413)],
    ids=["empty", "path", "nonhex", "shape", "oversize"],
)
def test_request_id_is_bounded_and_strict(memory_api, key, status):
    client, base, _, _, _ = memory_api
    assert (
        client.post(base + "/analyze", headers=AUTH, json=intent(request_id=key)).status_code
        == status
    )

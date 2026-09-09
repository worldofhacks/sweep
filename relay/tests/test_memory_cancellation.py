"""Cooperative memory-analysis cancellation; fixtures never call external providers."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from relay.atlas import AtlasError, AtlasStore
from relay.memory_context import analyze
from relay.memory_store import AnalyzeMemory, CancelMemory, MemoryNotes, SaveMemory
from relay.tests.test_atlas_accounts import OWNER, join, signed
from relay.tests.test_atlas_accounts import api as api
from relay.tests.test_atlas_accounts import keys as keys
from relay.tests.test_memory_collaboration import setup_memory


def test_contributor_can_stop_own_analysis_without_gaining_paid_authority(api, keys):
    url, created, auth, capture, base = setup_memory(api, keys)
    store = api.app.state.atlas_store
    space, cid = created["space"]["id"], capture["id"]
    job = store.memories.begin(space, cid, AnalyzeMemory(revision=0, ai=True))["analysis"]
    body = {"analysis_id": job["id"]}
    viewer, other = signed(keys, "viewer"), signed(keys, "other")
    join(api, url, viewer, "viewer")
    join(api, url, other)
    for refused in (
        viewer,
        other,
        signed(keys, "outsider"),
    ):
        assert api.post(base + "/cancel", headers=refused, json=body).status_code == 403
    assert (
        api.post(
            base + "/cancel",
            headers={"Authorization": "Bearer " + created["contributor_token"]},
            json=body,
        ).status_code
        == 401
    )
    assert api.get(base, headers=auth).json()["can_cancel"]
    assert not api.get(base, headers=auth).json()["can_analyze"]
    assert api.post(base + "/analyze", headers=auth, json={"revision": 0}).status_code == 401
    stopped = api.post(base + "/cancel", headers=auth, json=body)
    assert stopped.status_code == 200
    assert stopped.headers["cache-control"] == "no-store"
    assert stopped.json()["analysis"]["status"] == "cancelling"
    assert stopped.json()["analysis"]["cancel_requested_by"] == capture["account_id"]
    assert api.post(base + "/cancel", headers=auth, json=body).json() == stopped.json()
    assert (
        store.db.execute(
            "SELECT count(*) FROM atlas_source_operations WHERE id=?", (job["id"],)
        ).fetchone()[0]
        == 1
    )
    assert api.post(base, headers=auth, json={"revision": 0, "notes": {}}).status_code == 409
    assert api.post(base + "/cancel", headers=auth, json={**body, "force": True}).status_code == 422
    assert (
        api.post(
            base + "/cancel", headers=auth, json={"analysis_id": str(uuid.uuid4())}
        ).status_code
        == 409
    )
    store.memories.finish(
        space,
        cid,
        job,
        {
            "status": "complete",
            "transcript": {"text": "private late transcript"},
            "suggestion": {"summary": "late output"},
        },
    )
    final = api.get(base, headers=auth).json()
    assert final["analysis"]["status"] == "cancelled"
    assert not final["can_cancel"]
    assert (
        "private late transcript" not in json.dumps(final) and "suggestion" not in final["analysis"]
    )
    assert final["notes"]["description"] == "" and final["capture"] == capture
    assert (
        store.db.execute(
            "SELECT count(*) FROM atlas_source_operations WHERE id=?", (job["id"],)
        ).fetchone()[0]
        == 0
    )
    assert api.post(base + "/cancel", headers=auth, json=body).json() == final
    assert (
        api.post(
            base, headers=auth, json={"revision": 0, "notes": {"description": "My story"}}
        ).status_code
        == 200
    )


def test_cancelled_analysis_checks_before_next_stage_and_keeps_cleanup_pending(
    api, keys, monkeypatch
):
    _, created, auth, capture, _ = setup_memory(api, keys)
    store = api.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]
    options = AnalyzeMemory(revision=0, weather=True, ai=True)
    value = store.memories.begin(sid, cid, options)
    job = value["analysis"]
    calls = []

    def inspect(*args):
        store.memories.cancel(sid, cid, CancelMemory(analysis_id=job["id"]))
        return {"has_audio": False}

    monkeypatch.setattr("relay.memory_context.inspect_media", inspect)
    monkeypatch.setattr(
        "relay.memory_context.historical_weather", lambda *a: calls.append("weather")
    )
    monkeypatch.setattr("relay.memory_context.suggest_scene", lambda *a: calls.append("ai"))
    with pytest.raises(AtlasError):
        analyze(store.memories, value, options)
    assert calls == []
    plan = store.removal.preview(sid, cid, operator=True)
    assert plan["analysis_pending"]
    store.removal.remove(sid, cid, plan["confirmation"], operator=True)
    store.removal.cleanup()
    assert store.removal.preview(sid, cid, operator=True)["state"] == "cleanup_pending"
    store.memories.finish(sid, cid, job, {"status": "failed"})
    store.removal.cleanup()
    assert store.removal.preview(sid, cid, operator=True)["state"] == "local_removed"


def test_unknown_worker_remains_pinned_across_reopen_and_cannot_be_superseded(api, keys):
    _, created, _, capture, _ = setup_memory(api, keys)
    store = api.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]
    job = store.memories.begin(sid, cid, AnalyzeMemory(revision=0))["analysis"]
    reopened = AtlasStore(store.root, clock=lambda: job["started_at"] + 181_000)
    try:
        assert reopened.memories.get(sid, cid)["analysis"]["status"] == "interrupted"
        with pytest.raises(AtlasError):
            reopened.memories.begin(sid, cid, AnalyzeMemory(revision=0))
        reopened.memories.cancel(sid, cid, CancelMemory(analysis_id=job["id"]))
        with pytest.raises(AtlasError):
            reopened.memories.save(sid, cid, SaveMemory(revision=0, notes=MemoryNotes()))
        assert (
            reopened.db.execute(
                "SELECT count(*) FROM atlas_source_operations WHERE id=?", (job["id"],)
            ).fetchone()[0]
            == 1
        )
        # Only the known worker's actual return releases this original; elapsed time did not.
        store.memories.finish(sid, cid, job, {"status": "complete"})
        assert reopened.memories.get(sid, cid)["analysis"]["status"] == "cancelled"
        next_job = reopened.memories.begin(sid, cid, AnalyzeMemory(revision=0))["analysis"]
        store.memories.finish(
            sid, cid, job, {"status": "complete", "suggestion": "duplicate old callback"}
        )
        assert reopened.memories.get(sid, cid)["analysis"]["id"] == next_job["id"]
        assert (
            reopened.db.execute(
                "SELECT count(*) FROM atlas_source_operations WHERE id=?", (next_job["id"],)
            ).fetchone()[0]
            == 1
        )
    finally:
        reopened.close()


def test_operator_stop_has_no_effect_on_a_finished_draft_and_respects_scope(api, keys):
    _, created, _, capture, base = setup_memory(api, keys)
    store = api.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]
    job = store.memories.begin(sid, cid, AnalyzeMemory(revision=0))["analysis"]
    store.memories.finish(
        sid, cid, job, {"status": "complete", "suggestion": {"summary": "Saved draft"}}
    )
    before = api.get(base, headers=OWNER).json()
    assert (
        api.post(base + "/cancel", headers=OWNER, json={"analysis_id": job["id"]}).json() == before
    )
    wrong = base.replace(sid, str(uuid.uuid4()))
    assert (
        api.post(wrong + "/cancel", headers=OWNER, json={"analysis_id": job["id"]}).status_code
        == 404
    )


@pytest.mark.parametrize("stage", ["inspection", "suggestion"])
@pytest.mark.parametrize("withdraw", [False, True])
def test_http_stop_waits_for_actual_worker_return(api, keys, monkeypatch, stage, withdraw):
    _, created, auth, capture, base = setup_memory(api, keys)
    store = api.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]
    entered, release = Event(), Event()
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-never-sent")
    monkeypatch.setenv("SWEEP_MEMORY_PROVIDER_CALLS_PER_SPACE_DAY", "10")
    monkeypatch.setenv("SWEEP_MEMORY_PROVIDER_CALLS_PER_RELAY_DAY", "10")
    monkeypatch.setattr("relay.memory_context.preview_frame", lambda *a: None)

    def held(result):
        entered.set()
        assert release.wait(10), "test must release its disposable worker"
        return result

    monkeypatch.setattr(
        "relay.memory_context.inspect_media",
        lambda *a: held({"has_audio": False}) if stage == "inspection" else {"has_audio": False},
    )
    suggestions = []

    def suggest(*args):
        suggestions.append(True)
        return held({"summary": "late generated private detail"})

    monkeypatch.setattr("relay.memory_context.suggest_scene", suggest)
    with ThreadPoolExecutor(max_workers=1) as workers:
        request = workers.submit(
            api.post, base + "/analyze", headers=OWNER, json={"revision": 0, "ai": True}
        )
        try:
            assert entered.wait(5)
            job = api.get(base, headers=auth).json()["analysis"]
            assert (
                api.post(base + "/cancel", headers=auth, json={"analysis_id": job["id"]}).json()[
                    "analysis"
                ]["status"]
                == "cancelling"
            )
            assert (
                api.post(base + "/analyze", headers=OWNER, json={"revision": 0}).status_code == 409
            )
            assert api.get(base.removesuffix("/memory") + "/media", headers=auth).status_code == 200
            if withdraw:
                plan = store.removal.preview(sid, cid, operator=True)
                store.removal.remove(sid, cid, plan["confirmation"], operator=True)
                store.removal.cleanup()
                assert store.removal.preview(sid, cid, operator=True)["state"] == "cleanup_pending"
            assert (
                store.db.execute(
                    "SELECT count(*) FROM atlas_source_operations WHERE id=?", (job["id"],)
                ).fetchone()[0]
                == 1
            )
        finally:
            release.set()
        assert request.result(timeout=5).status_code == 202
    assert len(suggestions) == (1 if stage == "suggestion" else 0)
    if withdraw:
        store.removal.cleanup()
        assert store.removal.preview(sid, cid, operator=True)["state"] == "local_removed"
        assert not (store.media / cid).exists()
    else:
        final = api.get(base, headers=auth).json()
        assert final["analysis"]["status"] == "cancelled"
        assert "late generated private detail" not in json.dumps(final)
        assert (store.media / cid).exists()

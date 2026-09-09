"""Removal tests use disposable synthetic originals and stores, never user media."""

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from relay.atlas import AtlasError, AtlasStore
from relay.atlas_timeline import CaptureDate, SaveCaptureDate
from relay.memory_context import analyze
from relay.memory_store import AnalyzeMemory
from relay.tests.test_atlas_accounts import OWNER, join, signed, upload
from relay.tests.test_atlas_accounts import api as api
from relay.tests.test_atlas_accounts import keys as keys
from relay.tests.test_atlas_reconstruction import seed
from relay.tests.test_memory_collaboration import attach, setup_memory


def withdraw(store, space, capture):
    plan = store.removal.preview(space, capture, operator=True)
    return store.removal.remove(space, capture, plan["confirmation"], operator=True)


def test_receipt_directory_is_durable_scoped_and_paged_without_skipping(api, keys):
    url, created, auth, capture, memory = setup_memory(api, keys)
    store = api.app.state.atlas_store
    sid = created["space"]["id"]
    assert api.get(memory, headers=auth).json()["can_remove"]
    viewer = signed(keys, "receipt-viewer")
    other = signed(keys, "other-contributor")
    join(api, url, viewer, "viewer")
    join(api, url, other)
    assert not api.get(memory, headers=viewer).json()["can_remove"]
    for refused in (
        viewer,
        signed(keys, "outsider"),
        {"Authorization": "Bearer " + created["contributor_token"]},
    ):
        assert api.get(url + "/removals", headers=refused).status_code == 403
    # Synthetic receipts exercise pagination without removing real or test media.
    for number in range(23):
        value = {
            "capture_id": f"receipt-{number}",
            "state": "local_removed",
            "requested_at": 1234,
            "requested_by": capture["account_id"],
            "completed_at": 1235,
            "recordings": 0,
            "builds": 0,
            "analysis_released": True,
        }
        with store.lock, store.db:
            store.db.execute(
                "INSERT INTO atlas_removals VALUES (?,?,?,?,?)",
                (
                    value["capture_id"],
                    sid,
                    "private-digest",
                    capture["account_id"],
                    json.dumps(value),
                ),
            )
            other_value = {**value, "capture_id": f"other-{number}", "requested_by": "other-author"}
            store.db.execute(
                "INSERT INTO atlas_removals VALUES (?,?,?,?,?)",
                (
                    other_value["capture_id"],
                    sid,
                    "other-digest",
                    "other-author",
                    json.dumps(other_value),
                ),
            )
    first = api.get(url + "/removals", headers=auth)
    assert first.headers["cache-control"] == "no-store"
    page = first.json()
    assert page["scope"] == "own" and page["completed"] == 23 and page["pending"] == 0
    assert len(page["receipts"]) == 20
    assert page["next_before"] == 4  # Scope-local, not the global insertion counter.
    assert "other-author" not in first.text
    second = api.get(url + f"/removals/{page['next_before']}", headers=auth).json()
    assert second["next_before"] is None
    assert len({r["capture_id"] for r in page["receipts"] + second["receipts"]}) == 23
    assert "private-digest" not in first.text
    assert api.get(url + "/removals", headers=other).json()["receipts"] == []
    assert api.get(url + "/removals", headers=OWNER).json()["scope"] == "space"
    with store.lock, store.db:
        store.db.execute(
            "UPDATE atlas_members SET role='owner' "
            "WHERE space=? AND account!=? AND role='contributor'",
            (sid, capture["account_id"]),
        )
    assert api.get(url + "/removals", headers=other).json()["completed"] == 46
    assert api.get(url + "/removals", headers=other).json()["scope"] == "space"
    for cursor in (0, -1, 9223372036854775808):
        assert api.get(url + f"/removals/{cursor}", headers=auth).status_code == 422
    with store.lock, store.db:
        store.db.execute(
            "DELETE FROM atlas_members WHERE space=? AND account=?", (sid, capture["account_id"])
        )
    assert api.get(url + f"/removals/{page['next_before']}", headers=auth).status_code == 403


def test_account_removal_covers_original_sidecars_context_history_and_retry(api, keys):
    url, created, auth, capture, memory = setup_memory(api, keys)
    store = api.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]
    marker = "PRIVATE-GARDEN-STORY-DO-NOT-RETAIN-847321"
    assert (
        api.post(
            memory, headers=auth, json={"revision": 0, "notes": {"description": marker}}
        ).status_code
        == 200
    )
    asset = attach(api, memory, auth).json()
    store.timeline.save(
        sid,
        cid,
        SaveCaptureDate(revision=0, assertion=CaptureDate(precision="year", value="2024")),
        capture["account_id"],
    )
    endpoint = memory.removesuffix("/memory") + "/removal"
    for other in (signed(keys, "outsider"),):
        assert api.get(endpoint, headers=other).status_code == 403
    viewer = signed(keys, "viewer")
    join(api, url, viewer, "viewer")
    assert api.get(endpoint, headers=viewer).status_code == 403
    legacy = {"Authorization": "Bearer " + created["contributor_token"]}
    assert api.get(endpoint, headers=legacy).status_code == 403
    plan = api.get(endpoint, headers=auth)
    assert plan.headers["cache-control"] == "no-store"
    assert plan.json()["state"] == "preview" and plan.json()["recordings"] == 1
    assert (store.media / cid).exists()  # A preview never removes anything.
    request = {"confirmation": plan.json()["confirmation"]}
    result = api.post(endpoint, headers=auth, json=request)
    assert result.status_code == 200, result.text
    receipt = api.get(endpoint, headers=auth).json()
    assert receipt["state"] == "local_removed", receipt
    assert receipt["completed_at"]
    assert receipt["requested_by"] == capture["account_id"]
    assert api.post(endpoint, headers=auth, json=request).json() == receipt
    assert not (store.media / cid).exists() and not (store.memories.media / asset["id"]).exists()
    for suffix in ("", "/history", f"/assets/{asset['id']}/media"):
        assert api.get(memory + suffix, headers=auth).status_code == 404
    assert api.get(memory.removesuffix("/memory") + "/media", headers=auth).status_code == 404
    assert api.get(url + "/timeline", headers=auth).json()["entries"] == []
    assert api.get(url, headers=auth).json()["captures"] == []
    replay = upload(api, url, auth)
    assert replay.status_code == 409
    assert replay.json()["code"] == "capture_removed"
    assert "digest" not in replay.text and capture["id"] not in replay.text
    for table in (
        "memory_contexts",
        "memory_assets",
        "memory_edits",
        "atlas_capture_dates",
        "capture_responses",
    ):
        assert (
            store.db.execute(
                f"SELECT count(*) FROM {table} WHERE space=? AND capture=?", (sid, cid)
            ).fetchone()[0]
            == 0
        )
    assert marker.encode() not in (store.root / "atlas.sqlite3").read_bytes()
    wal = store.root / "atlas.sqlite3-wal"
    assert not wal.exists() or marker.encode() not in wal.read_bytes()
    assert "digest" not in json.dumps(receipt) and "assets" not in receipt


def test_removal_precondition_includes_notes_dates_recordings_and_builds(api, keys):
    _, created, auth, capture, memory = setup_memory(api, keys)
    endpoint = memory.removesuffix("/memory") + "/removal"
    first = api.get(endpoint, headers=auth).json()["confirmation"]
    api.post(memory, headers=auth, json={"revision": 0, "notes": {"feeling": "Joyful"}})
    assert api.post(endpoint, headers=auth, json={"confirmation": first}).status_code == 409
    assert (
        api.post(endpoint, headers=auth, json={"confirmation": first, "force": True}).status_code
        == 422
    )
    assert api.get(memory, headers=auth).status_code == 200
    assert (
        api.post(
            endpoint, headers=signed(keys, "stranger"), json={"confirmation": first}
        ).status_code
        == 403
    )
    assert (
        api.get(endpoint.replace(created["space"]["id"], "not-a-space"), headers=auth).status_code
        == 403
    )


def test_pending_build_cannot_revive_or_hide_another_removed_source(tmp_path):
    store = AtlasStore(tmp_path / "atlas")
    try:
        sid = seed(store)
        captures = store.captures(sid)
        job = store.queue_reconstruction(sid)
        store.claim_reconstruction()
        directory = store.root / "reconstructions" / job["id"]
        directory.mkdir(parents=True)
        (directory / "cloud.glb").write_bytes(b"private derived fixture")
        withdraw(store, sid, captures[0]["id"])
        store.removal.cleanup()
        assert (
            store.removal.preview(sid, captures[0]["id"], operator=True)["state"]
            == "cleanup_pending"
        )
        assert directory.exists()
        assert not store.progress_reconstruction(job["id"], status="ready")
        assert store.reconstruction_job(job["id"])["sources"] == []
        second = withdraw(store, sid, captures[1]["id"])
        assert second["builds"] == 1  # Dependency retained even after first source was scrubbed.
        (directory / "late-output").write_bytes(b"worker was still alive")
        store.release_reconstruction(job["id"])  # Explicit stopped-supervisor acknowledgement.
        for capture in captures[:2]:
            assert (
                store.removal.preview(sid, capture["id"], operator=True)["state"] == "local_removed"
            )
        assert not directory.exists()
        assert (store.media / captures[2]["id"]).exists()
        assert len(store.detail(sid)["captures"]) == 1
    finally:
        store.close()


def test_queued_build_is_cancelled_without_starting_and_old_ready_build_waits(tmp_path):
    store = AtlasStore(tmp_path / "atlas")
    try:
        sid = seed(store)
        job = store.queue_reconstruction(sid)
        capture = store.captures(sid)[0]["id"]
        withdraw(store, sid, capture)
        assert store.claim_reconstruction() is None
        store.removal.cleanup()
        assert store.removal.preview(sid, capture, operator=True)["state"] == "local_removed"
        sid2 = seed(store)
        second = store.queue_reconstruction(sid2)
        store.claim_reconstruction()
        store.progress_reconstruction(second["id"], status="ready")
        store.db.execute("DELETE FROM atlas_build_sources WHERE job=?", (second["id"],))
        store.db.commit()
        reopened = AtlasStore(store.root)
        try:
            capture2 = reopened.captures(sid2)[0]["id"]
            assert withdraw(reopened, sid2, capture2)["builds"] == 1
            reopened.removal.cleanup()
            assert (
                reopened.removal.preview(sid2, capture2, operator=True)["state"]
                == "cleanup_pending"
            )
            assert reopened.reconstruction_job(second["id"])["source_withdrawn"]
        finally:
            reopened.close()
        assert store.reconstruction_job(job["id"])["status"] == "failed"
    finally:
        store.close()


def test_late_analysis_does_not_write_or_start_external_stages_after_removal(
    api, keys, monkeypatch
):
    _, created, _, capture, _ = setup_memory(api, keys)
    store = api.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]
    options = AnalyzeMemory(revision=0, ai=True, weather=True)
    value = store.memories.begin(sid, cid, options)
    calls = []

    def inspect(*args):
        withdraw(store, sid, cid)
        return {"mime": "image/png", "has_audio": False}

    monkeypatch.setattr("relay.memory_context.inspect_media", inspect)
    monkeypatch.setattr(
        "relay.memory_context.historical_weather", lambda *a: calls.append("weather")
    )
    monkeypatch.setattr("relay.memory_context.suggest_scene", lambda *a: calls.append("ai"))
    with pytest.raises(AtlasError):
        analyze(store.memories, value, options)
    assert calls == []
    store.removal.cleanup()
    assert store.removal.preview(sid, cid, operator=True)["state"] == "cleanup_pending"
    assert store.removal.preview(sid, cid, operator=True)["analysis_pending"]
    assert (
        store.memories.finish(
            sid, cid, value["analysis"], {"status": "complete", "suggestion": "late"}
        )
        is None
    )
    store.removal.cleanup()
    assert store.removal.preview(sid, cid, operator=True)["state"] == "local_removed"
    assert (
        store.db.execute("SELECT count(*) FROM memory_contexts WHERE capture=?", (cid,)).fetchone()[
            0
        ]
        == 0
    )


def test_cleanup_failure_and_pinned_wal_do_not_claim_completion(api, keys, monkeypatch):
    _, created, _, capture, _ = setup_memory(api, keys)
    store = api.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]
    reader = sqlite3.connect(store.root / "atlas.sqlite3")
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM captures").fetchall()
    store.db.execute("PRAGMA busy_timeout=1")
    original = Path.unlink
    with monkeypatch.context() as patch:

        def locked(path, **kwargs):
            if path == store.media / cid:
                raise PermissionError("disposable fixture is locked")
            return original(path, **kwargs)

        patch.setattr(Path, "unlink", locked)
        withdraw(store, sid, cid)
        store.removal.cleanup()
        assert store.removal.preview(sid, cid, operator=True)["state"] == "cleanup_pending"
    store.removal.cleanup()
    assert not (store.media / cid).exists()
    assert store.removal.preview(sid, cid, operator=True)["state"] == "cleanup_pending"
    reader.close()
    store.removal.cleanup()
    assert store.removal.preview(sid, cid, operator=True)["state"] == "local_removed"


def test_cleanup_never_follows_a_reconstruction_directory_link(tmp_path):
    store = AtlasStore(tmp_path / "atlas")
    try:
        sid = seed(store)
        job = store.queue_reconstruction(sid)
        outside = tmp_path / "unrelated"
        outside.mkdir()
        (outside / "keep").write_bytes(b"not an Atlas output")
        directory = store.root / "reconstructions"
        directory.mkdir()
        (directory / job["id"]).symlink_to(outside, target_is_directory=True)
        capture = store.captures(sid)[0]["id"]
        withdraw(store, sid, capture)
        store.removal.cleanup()
        assert (outside / "keep").read_bytes() == b"not an Atlas output"
        assert not (directory / job["id"]).is_symlink()
        assert store.removal.preview(sid, capture, operator=True)["state"] == "local_removed"
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["original_upload", "memory_upload", "inspection"])
def test_removal_waits_for_admitted_transfer_or_inspection(api, keys, monkeypatch, kind):
    url, created, auth, capture, memory = setup_memory(api, keys)
    store = api.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]

    def during_work(path, claimed):
        withdraw(store, sid, cid)
        store.removal.cleanup()
        assert store.removal.preview(sid, cid, operator=True)["state"] == "cleanup_pending"
        if kind != "inspection":
            assert path.exists()  # Staging copy has not been released yet.
        return {"mime": "image/png"} if kind == "inspection" else claimed

    if kind == "original_upload":
        monkeypatch.setattr("relay.atlas_routes.media_type", during_work)
        assert upload(api, url, auth).status_code == 409
    elif kind == "memory_upload":
        monkeypatch.setattr("relay.memory_routes.admitted_asset", during_work)
        assert attach(api, memory, auth).status_code == 404
    else:
        monkeypatch.setattr("relay.memory_routes.inspect_media", during_work)
        assert api.post(memory + "/inspect", headers=auth).status_code == 404
    assert store.db.execute("SELECT count(*) FROM atlas_source_operations").fetchone()[0] == 0
    assert not list(store.root.glob("upload-*")) and not list(store.root.glob("memory-upload-*"))
    store.removal.cleanup()
    assert store.removal.preview(sid, cid, operator=True)["state"] == "local_removed"


def test_unknown_operation_survives_restart_instead_of_guessing_it_stopped(tmp_path):
    root = tmp_path / "atlas"
    store = AtlasStore(root)
    sid = seed(store)
    cid = store.captures(sid)[0]["id"]
    operation = store.removal.begin_operation(sid, cid, "inspection")
    withdraw(store, sid, cid)
    store.close()
    reopened = AtlasStore(root)
    try:
        reopened.removal.cleanup()
        assert reopened.removal.preview(sid, cid, operator=True)["state"] == "cleanup_pending"
        reopened.removal.finish_operation(
            operation
        )  # Test supplies positive stopped-operation evidence.
        reopened.removal.cleanup()
        assert reopened.removal.preview(sid, cid, operator=True)["state"] == "local_removed"
    finally:
        reopened.close()


def test_timed_out_upload_waits_for_its_decoding_thread_before_release(api, keys, monkeypatch):
    import asyncio

    _, created, auth, capture, memory = setup_memory(api, keys)
    store = api.app.state.atlas_store
    sid, cid = created["space"]["id"], capture["id"]
    started, stopped = threading.Event(), threading.Event()
    original_timeout = asyncio.timeout
    monkeypatch.setattr(
        "relay.memory_routes.asyncio.timeout",
        lambda seconds: original_timeout(0.05 if seconds == 90 else seconds),
    )

    def decoding(_path, mime):
        started.set()
        assert stopped.wait(3)
        return mime

    monkeypatch.setattr("relay.memory_routes.admitted_asset", decoding)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(attach, api, memory, auth)
        try:
            assert started.wait(2)
            withdraw(store, sid, cid)
            time.sleep(0.1)
            store.removal.cleanup()
            assert not request.done()
            assert store.removal.preview(sid, cid, operator=True)["state"] == "cleanup_pending"
            assert list(store.root.glob("memory-upload-*"))
        finally:
            stopped.set()
        assert request.result(timeout=3).status_code == 408
    assert not list(store.root.glob("memory-upload-*"))
    assert store.db.execute("SELECT count(*) FROM atlas_source_operations").fetchone()[0] == 0
    store.removal.cleanup()
    assert store.removal.preview(sid, cid, operator=True)["state"] == "local_removed"


def test_generated_file_cleanup_does_not_lock_other_space_reads(tmp_path, monkeypatch):
    import shutil

    store = AtlasStore(tmp_path / "atlas")
    sid = seed(store)
    job = store.queue_reconstruction(sid)
    output = store.root / "reconstructions" / job["id"]
    output.mkdir(parents=True)
    (output / "cloud.glb").write_bytes(b"disposable generated data")
    cid = store.captures(sid)[0]["id"]
    withdraw(store, sid, cid)
    started, proceed = threading.Event(), threading.Event()
    original = shutil.rmtree

    def slow_cleanup(path):
        started.set()
        assert proceed.wait(3)
        original(path)

    monkeypatch.setattr("relay.atlas_removal.shutil.rmtree", slow_cleanup)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            cleaning = pool.submit(store.removal.cleanup)
            try:
                assert started.wait(2)
                assert len(pool.submit(store.detail, sid).result(timeout=2)["captures"]) == 2
            finally:
                proceed.set()
            cleaning.result(timeout=3)
        assert store.removal.preview(sid, cid, operator=True)["state"] == "local_removed"
    finally:
        store.close()

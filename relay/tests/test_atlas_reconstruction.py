"""Durable queue and artifact authorization. Engine acceptance is run on real example images."""

import json

from fastapi.testclient import TestClient

from relay.atlas import AtlasStore, CaptureMetadata, NewSpace
from relay.tests.test_atlas_api import AUTH, BASE, PNG, SPACE, app, capture_meta


def seed(store):
    identifier = store.create("test", NewSpace(**SPACE))["space"]["id"]
    for index in range(3):
        staged = store.root / f"input-{index}"
        staged.write_bytes(PNG + bytes([index]))
        store.add_capture(identifier, CaptureMetadata(**capture_meta()), staged, "image/png")
    return identifier


def test_queue_snapshot_claim_heartbeat_and_stale_worker(tmp_path):
    clock = [100_000_000_000_000]
    with_store = AtlasStore(tmp_path, clock=lambda: clock[0])
    other = AtlasStore(tmp_path, clock=lambda: clock[0])
    try:
        identifier = seed(with_store)
        job = with_store.queue_reconstruction(identifier)
        assert job["status"] == "queued"
        assert "sources" not in job
        assert other.queue_reconstruction(identifier)["id"] == job["id"]
        claimed = other.claim_reconstruction()
        assert len(claimed["sources"]) == 3
        assert with_store.claim_reconstruction() is None
        assert other.progress_reconstruction(job["id"], status="matching", progress=35)
        clock[0] += 120_001
        assert with_store.claim_reconstruction() is None
        assert with_store.reconstruction_job(job["id"])["status"] == "failed"
        assert not other.progress_reconstruction(job["id"], status="ready")
        retried = with_store.queue_reconstruction(identifier)
        assert retried["id"] != job["id"]
    finally:
        other.close()
        with_store.close()


def test_reconstruction_requires_owner_and_completed_artifact(tmp_path):
    with TestClient(app(tmp_path)) as client:
        created = client.post(BASE, headers=AUTH, json=SPACE).json()
        identifier = created["space"]["id"]
        url = f"{BASE}/{identifier}/reconstruction"
        invited = {"Authorization": f"Bearer {created['contributor_token']}"}
        assert client.post(url, headers=invited).status_code == 401
        assert client.post(url, headers=AUTH).status_code == 409
        for index in range(3):
            response = client.post(
                f"{BASE}/{identifier}/captures",
                content=PNG + bytes([index]),
                headers={
                    **AUTH,
                    "Content-Type": "image/png",
                    "X-Sweep-Capture": json.dumps(capture_meta()),
                },
            )
            assert response.status_code == 201
        job = client.post(url, headers=AUTH).json()
        store = client.app.state.atlas_store
        assert client.get(f"{url}/{job['id']}/cloud.glb", headers=invited).status_code == 404
        assert store.claim_reconstruction()["id"] == job["id"]
        directory = store.root / "reconstructions" / job["id"]
        directory.mkdir(parents=True)
        # A transport fixture, deliberately not asserted to be a reconstructed model.
        (directory / "cloud.glb").write_bytes(b"transport-fixture")
        store.progress_reconstruction(job["id"], status="ready")
        assert client.get(f"{url}/{job['id']}/cloud.glb").status_code == 401
        assert (
            client.get(f"{url}/{job['id']}/cloud.glb", headers=invited).content
            == b"transport-fixture"
        )
        assert client.get(f"{url}/{job['id']}/worker.log", headers=invited).status_code == 404
        second = client.post(BASE, headers=AUTH, json=SPACE).json()["space"]["id"]
        assert (
            client.get(
                f"{BASE}/{second}/reconstruction/{job['id']}/cloud.glb", headers=AUTH
            ).status_code
            == 404
        )

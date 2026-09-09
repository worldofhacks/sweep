"""Original-preserving request responses through real HTTP and durable SQLite."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from relay.atlas import AtlasStore, CaptureMetadata, NewSpace
from relay.tests.test_atlas_api import AUTH, BASE, PNG, app, capture_meta
from relay.tests.test_atlas_surface_requests import ready

SPACE = {
    "title": "Austin capture request",
    "latitude": 30.2672,
    "longitude": -97.7431,
    "radius": 80,
}


def upload(client, url, target=None, content=PNG, auth=AUTH, **changes):
    meta = capture_meta(source="import", captured_at=None, position=None, **changes)
    if target is not None:
        meta["response_to"] = target
    return client.post(
        url + "/captures",
        content=content,
        headers={**auth, "Content-Type": "image/png", "X-Sweep-Capture": json.dumps(meta)},
    )


def test_location_response_deduplicates_without_rewriting_source_or_certifying_coverage(tmp_path):
    with TestClient(app(tmp_path)) as client:
        created = client.post(BASE, headers=AUTH, json=SPACE).json()
        url = f"{BASE}/{created['space']['id']}"
        token = {"Authorization": f"Bearer {created['contributor_token']}"}
        original = upload(client, url, name="Original contributor").json()
        target = {"kind": "location", "cell_id": "5:5"}
        assert (
            client.post(url + "/requests", headers=token, json={"cell_id": "5:5"}).status_code
            == 201
        )
        for _ in range(2):
            response = upload(client, url, target, auth=token, name="A different contributor")
            assert response.status_code == 201
            assert response.json() == {**original, "response_to": target}
        detail = client.get(url, headers=token).json()
        assert detail["captures"] == [original]
        assert detail["requests"][0]["capture_ids"] == [original["id"]]
        assert detail["requests"][0]["status"] == "open"
        assert detail["coverage"]["observed"] == 0
        assert client.get(url + f"/captures/{original['id']}/media", headers=token).content == PNG
    with TestClient(app(tmp_path)) as client:
        assert client.get(url, headers=AUTH).json()["requests"][0]["capture_ids"] == [
            original["id"]
        ]


def test_surface_response_keeps_old_build_binding_after_rebuild_and_dismissal(tmp_path):
    with TestClient(app(tmp_path)) as client:
        url, invitation, request = ready(client)
        assert client.post(url + "/surface-requests", headers=AUTH, json=request).status_code == 201
        target = {
            "kind": "surface",
            **{key: request[key] for key in ("job_id", "artifact_sha256", "region_id")},
        }
        client.post(
            url + f"/surface-requests/{request['job_id']}/{request['region_id']}/dismiss",
            headers=AUTH,
        )
        rebuilt = client.post(url + "/reconstruction", headers=AUTH).json()
        assert rebuilt["id"] != target["job_id"]
        result = upload(client, url, target, PNG + b"requested-original", invitation)
        assert result.status_code == 201, result.text
        detail = client.get(url, headers=invitation).json()
        response = detail["surface_requests"][0]
        assert response["status"] == "dismissed"
        assert response["job_id"] == target["job_id"]
        assert response["capture_ids"] == [result.json()["id"]]
        assert detail["reconstruction"]["id"] == rebuilt["id"]


def test_unknown_wrong_build_and_cross_space_targets_cannot_admit_files(tmp_path):
    with TestClient(app(tmp_path)) as client:
        url, invitation, request = ready(client)
        target = {
            "kind": "surface",
            **{key: request[key] for key in ("job_id", "artifact_sha256", "region_id")},
        }
        assert upload(client, url, target).status_code == 409
        client.post(url + "/surface-requests", headers=AUTH, json=request)
        assert upload(client, url, {**target, "artifact_sha256": "b" * 64}).status_code == 409
        assert upload(client, url, {"kind": "location", "cell_id": "5:5"}).status_code == 409
        assert upload(client, url, {**target, "latitude": 30}).status_code == 422
        other = client.post(BASE, headers=AUTH, json=SPACE).json()["space"]["id"]
        assert upload(client, f"{BASE}/{other}", target).status_code == 409
        assert upload(client, f"{BASE}/{other}", target, auth=invitation).status_code == 403
        assert client.get(url, headers=AUTH).json()["space"]["capture_count"] == 3
        assert client.get(f"{BASE}/{other}", headers=AUTH).json()["space"]["capture_count"] == 0


def test_each_original_has_bounded_membership_and_retries_still_work(tmp_path):
    with TestClient(app(tmp_path)) as client:
        space = client.post(BASE, headers=AUTH, json=SPACE).json()["space"]["id"]
        url = f"{BASE}/{space}"
        targets = [
            {"kind": "location", "cell_id": cell["id"]}
            for cell in client.get(url, headers=AUTH).json()["coverage"]["cells"][:9]
        ]
        for i, target in enumerate(targets):
            client.post(url + "/requests", headers=AUTH, json={"cell_id": target["cell_id"]})
            assert upload(client, url, target).status_code == (201 if i < 8 else 409)
        assert upload(client, url, targets[0]).status_code == 201
        detail = client.get(url, headers=AUTH).json()
        assert len(detail["captures"]) == 1
        assert sum(len(item["capture_ids"]) for item in detail["requests"]) == 8


def test_concurrent_responses_share_one_original_and_rollback_leaves_no_orphan(tmp_path):
    store = AtlasStore(tmp_path)
    space = store.create("test", NewSpace(**SPACE))["space"]["id"]
    from relay.atlas import CaptureRequest

    store.request_capture(space, CaptureRequest(cell_id="5:5"))
    meta = CaptureMetadata(
        **capture_meta(
            source="import",
            captured_at=None,
            position=None,
            response_to={"kind": "location", "cell_id": "5:5"},
        )
    )
    second = AtlasStore(tmp_path)
    files = [tmp_path / "first-upload", tmp_path / "retry-upload"]
    for path in files:
        path.write_bytes(PNG)
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda args: args[0].add_capture(space, meta, args[1], "image/png"),
                zip((store, second), files, strict=True),
            )
        )
    assert results[0]["id"] == results[1]["id"]
    assert len(list(store.media.iterdir())) == 1
    assert store.detail(space)["requests"][0]["capture_ids"] == [results[0]["id"]]
    store.db.execute(
        "CREATE TRIGGER fail_response BEFORE INSERT ON capture_responses "
        "BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
    )
    path = tmp_path / "failed-upload"
    path.write_bytes(PNG + b"new")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_capture(space, meta, path, "image/png")
    assert len(store.captures(space)) == len(list(store.media.iterdir())) == 1
    second.close()
    store.close()

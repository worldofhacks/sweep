"""Real HTTP authorization, idempotency and durable build-bound review requests."""

import json

from fastapi.testclient import TestClient

from relay.tests.test_atlas_api import AUTH, BASE, PNG, SPACE, app, capture_meta

REGION = "0123456789abcdef"
CHECKSUM = "a" * 64


def ready(client):
    created = client.post(BASE, headers=AUTH, json=SPACE).json()
    url = f"{BASE}/{created['space']['id']}"
    for i in range(3):
        assert (
            client.post(
                url + "/captures",
                content=PNG + bytes([i]),
                headers={
                    **AUTH,
                    "Content-Type": "image/png",
                    "X-Sweep-Capture": json.dumps(capture_meta()),
                },
            ).status_code
            == 201
        )
    job = client.post(url + "/reconstruction", headers=AUTH).json()
    store = client.app.state.atlas_store
    assert store.claim_reconstruction()["id"] == job["id"]
    # Deliberate transport metadata fixture; no engine or geometry quality claim.
    store.progress_reconstruction(
        job["id"],
        status="ready",
        artifact_sha256=CHECKSUM,
        surface_review={"regions": [{"id": REGION, "label": "Region 1", "segments": [[0, 0, 0]]}]},
    )
    payload = {
        "job_id": job["id"],
        "artifact_sha256": CHECKSUM,
        "region_id": REGION,
        "note": "More overlapping views, if safe and permitted.",
    }
    return url, {"Authorization": f"Bearer {created['contributor_token']}"}, payload


def test_owner_publishes_invitee_reads_retry_is_idempotent_and_restart_preserves(tmp_path):
    with TestClient(app(tmp_path)) as client:
        url, invitation, payload = ready(client)
        endpoint = url + "/surface-requests"
        assert client.post(endpoint, json=payload).status_code == 401
        assert client.post(endpoint, headers=invitation, json=payload).status_code == 401
        assert (
            client.post(
                endpoint.replace("atlas-test", "other"), headers=AUTH, json=payload
            ).status_code
            == 403
        )
        response = client.post(endpoint, headers=AUTH, json=payload)
        assert response.status_code == 201
        assert response.json()["status"] == "open"
        assert "latitude" not in response.json()
        assert (
            client.post(endpoint, headers=AUTH, json={**payload, "note": "Retry"}).json()
            == response.json()
        )
        assert (
            client.post(
                url + "/captures",
                content=PNG + b"new",
                headers={
                    **invitation,
                    "Content-Type": "image/png",
                    "X-Sweep-Capture": json.dumps(capture_meta()),
                },
            ).status_code
            == 201
        )
        requests = client.get(url, headers=invitation).json()["surface_requests"]
        assert len(requests) == 1 and requests[0]["status"] == "open"
        summary = client.get(url, headers=invitation).json()["reconstruction"]["surface_review"]
        assert "segments" not in summary["regions"][0]
        raw = client.app.state.atlas_store.reconstruction_job(payload["job_id"])
        assert "segments" in raw["surface_review"]["regions"][0]
    with TestClient(app(tmp_path)) as client:
        assert client.get(url, headers=invitation).json()["surface_requests"] == requests
        dismiss = f"{endpoint}/{payload['job_id']}/{REGION}/dismiss"
        assert client.post(dismiss, headers=invitation).status_code == 401
        other = client.post(BASE, headers=AUTH, json=SPACE).json()["space"]["id"]
        assert client.post(dismiss.replace(url, f"{BASE}/{other}"), headers=AUTH).status_code == 404
        assert client.post(dismiss, headers=AUTH).json()["status"] == "dismissed"
        assert client.post(endpoint, headers=AUTH, json=payload).json()["status"] == "dismissed"


def test_changed_build_checksum_unknown_region_and_resolved_space_are_refused(tmp_path):
    with TestClient(app(tmp_path)) as client:
        url, invitation, payload = ready(client)
        endpoint = url + "/surface-requests"
        for change in (
            {"artifact_sha256": "b" * 64},
            {"region_id": "f" * 16},
            {"job_id": "0" * 36},
        ):
            assert (
                client.post(endpoint, headers=AUTH, json={**payload, **change}).status_code == 409
            )
        assert (
            client.post(endpoint, headers=AUTH, json={**payload, "latitude": 37}).status_code == 422
        )
        assert client.post(endpoint, headers=AUTH, json=payload).status_code == 201
        new = client.post(url + "/reconstruction", headers=AUTH).json()
        assert new["id"] != payload["job_id"]
        assert client.post(endpoint, headers=AUTH, json=payload).status_code == 409
        # Queuing/rebuilding never changes old geometry bindings or auto-completes requests.
        assert client.get(url, headers=invitation).json()["surface_requests"][0]["status"] == "open"
        store = client.app.state.atlas_store
        store.claim_reconstruction()
        store.progress_reconstruction(
            new["id"],
            status="ready",
            artifact_sha256=CHECKSUM,
            surface_review={"regions": [{"id": REGION, "label": "Region 1"}]},
        )
        client.post(url + "/status", headers=AUTH, json={"status": "resolved"})
        assert (
            client.post(endpoint, headers=AUTH, json={**payload, "job_id": new["id"]}).status_code
            == 409
        )

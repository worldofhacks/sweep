"""Request discovery counts actions, not inferred gaps or stale model targets."""

import json

from fastapi.testclient import TestClient

from relay.tests.test_atlas_api import AUTH, BASE, PNG, SPACE, app, capture_meta
from relay.tests.test_atlas_surface_requests import CHECKSUM, REGION, ready


def counts(client, url, invitation=AUTH):
    detail = client.get(url, headers=invitation).json()
    listed = client.get(BASE, headers=AUTH).json()["spaces"]
    summary = next(item for item in listed if item["id"] == detail["space"]["id"])
    assert summary["open_request_count"] == detail["space"]["open_request_count"]
    return summary["open_request_count"]


def test_empty_coverage_is_not_a_request_and_camera_evidence_updates_discovery(tmp_path):
    with TestClient(app(tmp_path)) as client:
        created = client.post(BASE, headers=AUTH, json=SPACE).json()
        url = f"{BASE}/{created['space']['id']}"
        invitation = {"Authorization": f"Bearer {created['contributor_token']}"}
        assert counts(client, url, invitation) == 0
        request = client.post(
            url + "/requests",
            headers=invitation,
            json={"cell_id": "5:5", "note": "Public path view"},
        )
        assert request.status_code == 201
        assert counts(client, url, invitation) == 1
        # A retry cannot rewrite another contributor's request note or original time.
        assert (
            client.post(
                url + "/requests", headers=invitation, json={"cell_id": "5:5", "note": "Changed"}
            ).json()
            == request.json()
        )
        for source, expected in [("import", 1), ("camera", 0)]:
            meta = capture_meta(source=source)
            assert (
                client.post(
                    url + "/captures",
                    content=PNG + source.encode(),
                    headers={
                        **invitation,
                        "Content-Type": "image/png",
                        "X-Sweep-Capture": json.dumps(meta),
                    },
                ).status_code
                == 201
            )
            assert counts(client, url, invitation) == expected
    with TestClient(app(tmp_path)) as client:
        assert counts(client, url, invitation) == 0


def test_only_current_ready_surface_requests_count_and_dismissal_is_immediate(tmp_path):
    with TestClient(app(tmp_path)) as client:
        url, invitation, payload = ready(client)
        assert counts(client, url) == 0
        assert client.post(url + "/surface-requests", headers=AUTH, json=payload).status_code == 201
        assert counts(client, url, invitation) == 1
        new = client.post(url + "/reconstruction", headers=AUTH).json()
        assert counts(client, url) == 0  # The old target is not a current call for contributions.
        store = client.app.state.atlas_store
        store.claim_reconstruction()
        store.progress_reconstruction(
            new["id"],
            status="ready",
            artifact_sha256=CHECKSUM,
            surface_review={"regions": [{"id": REGION, "label": "Region 1"}]},
        )
        assert counts(client, url) == 0
        assert (
            client.post(
                url + "/surface-requests", headers=AUTH, json={**payload, "job_id": new["id"]}
            ).status_code
            == 201
        )
        assert counts(client, url) == 1
        assert (
            client.post(
                f"{url}/surface-requests/{new['id']}/{REGION}/dismiss", headers=AUTH
            ).status_code
            == 200
        )
        assert counts(client, url) == 0
        assert client.get(url, headers=invitation).json()["surface_requests"][1]["status"] == "open"


def test_resolved_spaces_do_not_solicit_and_reopening_restores_existing_requests(tmp_path):
    with TestClient(app(tmp_path)) as client:
        created = client.post(BASE, headers=AUTH, json=SPACE).json()
        url = f"{BASE}/{created['space']['id']}"
        assert (
            client.post(url + "/requests", headers=AUTH, json={"cell_id": "5:5"}).status_code == 201
        )
        assert counts(client, url) == 1
        client.post(url + "/status", headers=AUTH, json={"status": "resolved"})
        assert counts(client, url) == 0
        assert (
            client.post(url + "/requests", headers=AUTH, json={"cell_id": "6:5"}).status_code == 409
        )
        client.post(url + "/status", headers=AUTH, json={"status": "active"})
        assert counts(client, url) == 1
        other = client.post(BASE.replace("atlas-test", "other"), headers=AUTH, json=SPACE).json()[
            "space"
        ]["id"]
        assert other not in {item["id"] for item in client.get(BASE, headers=AUTH).json()["spaces"]}

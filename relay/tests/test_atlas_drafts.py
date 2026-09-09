"""Publish saved incident drafts once, including ambiguous replies and process restarts."""

import concurrent.futures
import uuid

from fastapi.testclient import TestClient

from relay.atlas import AtlasStore, NewSpace
from relay.tests.test_atlas_api import AUTH, BASE, app

SPACE = {
    "title": "Demo · Austin creek access",
    "description": "Private test report until published.",
    "place": "Shoal Creek, Austin, Texas",
    "latitude": 30.279,
    "longitude": -97.748,
    "radius": 80,
    "category": "survey",
}


def test_lost_reply_restart_returns_original_without_rotating_invitation(tmp_path):
    draft = str(uuid.uuid4())
    route = f"{BASE}/drafts/{draft}/publish"
    with TestClient(app(tmp_path)) as client:
        response = client.post(route, headers=AUTH, json=SPACE)
        assert response.status_code == 201, response.text
        assert response.headers["cache-control"] == "no-store"
        original = response.json()
        identifier = original["space"]["id"]
        invited = {"Authorization": f"Bearer {original['contributor_token']}"}
        # The owner's first response can be lost after the transaction commits.
        client.post(f"{BASE}/{identifier}/status", headers=AUTH, json={"status": "resolved"})
    with TestClient(app(tmp_path)) as client:
        retry = client.post(route, headers=AUTH, json=SPACE)
        assert retry.status_code == 201
        assert retry.json()["draft_id"] == draft
        assert retry.json()["space"]["id"] == identifier
        assert retry.json()["space"]["status"] == "resolved"
        assert retry.json()["contributor_token"] is None
        assert len(client.get(BASE, headers=AUTH).json()["spaces"]) == 1
        assert client.get(f"{BASE}/{identifier}", headers=invited).status_code == 200


def test_conflicting_payload_cannot_overwrite_published_incident(tmp_path):
    with TestClient(app(tmp_path)) as client:
        route = f"{BASE}/drafts/{uuid.uuid4()}/publish"
        original = client.post(route, headers=AUTH, json=SPACE).json()
        changed = client.post(route, headers=AUTH, json={**SPACE, "description": "Changed report"})
        assert changed.status_code == 409
        current = client.get(f"{BASE}/{original['space']['id']}", headers=AUTH).json()
        assert current["space"]["description"] == SPACE["description"]
        assert len(client.get(BASE, headers=AUTH).json()["spaces"]) == 1


def test_publishing_requires_owner_auth_valid_id_and_original_workspace(tmp_path):
    with TestClient(app(tmp_path)) as client:
        route = f"{BASE}/drafts/{uuid.uuid4()}/publish"
        assert client.post(route, json=SPACE).status_code == 401
        created = client.post(BASE, headers=AUTH, json=SPACE).json()
        invited = {"Authorization": f"Bearer {created['contributor_token']}"}
        assert client.post(route, headers=invited, json=SPACE).status_code == 401
        assert (
            client.post(f"{BASE}/drafts/not-a-draft/publish", headers=AUTH, json=SPACE).status_code
            == 422
        )
        assert client.post(route, headers=AUTH, json={**SPACE, "latitude": 100}).status_code == 422
        first = client.post(route, headers=AUTH, json=SPACE).json()
        other = client.post(route.replace("atlas-test", "other"), headers=AUTH, json=SPACE).json()
        assert other["space"]["id"] != first["space"]["id"]


def test_two_connections_publish_same_draft_exactly_once(tmp_path):
    first, second = AtlasStore(tmp_path), AtlasStore(tmp_path)
    draft = str(uuid.uuid4())
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda store: store.create("room", NewSpace(**SPACE), draft), [first, second]
                )
            )
        assert results[0]["space"]["id"] == results[1]["space"]["id"]
        assert sum(result["contributor_token"] is not None for result in results) == 1
        assert len(first.list("room")) == 1
        assert first.db.execute("SELECT count(*) FROM published_drafts").fetchone()[0] == 1
    finally:
        first.close()
        second.close()


def test_retry_is_admitted_even_when_workspace_is_at_limit(tmp_path):
    store = AtlasStore(tmp_path)
    try:
        draft = str(uuid.uuid4())
        created = store.create("room", NewSpace(**SPACE), draft)
        with store.db:
            for index in range(499):
                store.db.execute(
                    "INSERT INTO spaces VALUES (?,?,?,?)", (str(index), "room", "unused", "{}")
                )
        assert (
            store.create("room", NewSpace(**SPACE), draft)["space"]["id"] == created["space"]["id"]
        )
    finally:
        store.close()

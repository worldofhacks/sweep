"""Real HTTP capture lifecycle, invitation isolation, coverage and restart persistence."""

import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.atlas import AtlasStore, CaptureMetadata, NewSpace, coverage
from relay.settings import RelaySettings

TOKEN = "atlas-tests-local-workspace-credential"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = "/api/sessions/atlas-test/atlas/spaces"
SPACE = {"title": "Test space", "latitude": 37.44, "longitude": -122.16, "radius": 80}
# A valid, tiny PNG. The HTTP media surface preserves source bytes.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
)


def app(path):
    return create_app(RelaySettings(relay_token=TOKEN.encode(), log_dir=path))


def capture_meta(**changes):
    now = int(time.time() * 1000)
    return {
        "contributor_id": "person-one",
        "name": "Sam",
        "kind": "photo",
        "source": "camera",
        "captured_at": now,
        "position": {
            "latitude": 37.44001,
            "longitude": -122.15999,
            "accuracy": 2,
            "timestamp": now,
        },
        **changes,
    }


def test_shared_capture_request_media_and_restart(tmp_path):
    with TestClient(app(tmp_path)) as client:
        response = client.post(BASE, headers=AUTH, json=SPACE)
        assert response.status_code == 201, response.text
        created = response.json()
        identifier = created["space"]["id"]
        invited = {"Authorization": f"Bearer {created['contributor_token']}"}
        detail_url = f"{BASE}/{identifier}"
        assert client.get(detail_url).status_code == 401
        assert client.get(BASE, headers=invited).status_code == 401
        initial = client.get(detail_url, headers=invited).json()
        assert initial["coverage"]["percent"] == 0
        requested = client.post(detail_url + "/requests", headers=invited, json={"cell_id": "5:5"})
        assert requested.status_code == 201
        metadata = capture_meta()
        uploaded = client.post(
            detail_url + "/captures",
            content=PNG,
            headers={
                **invited,
                "Content-Type": "image/png",
                "X-Sweep-Capture": json.dumps(metadata),
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        capture = uploaded.json()
        assert capture["sha256"] == hashlib.sha256(PNG).hexdigest()
        data = client.get(detail_url, headers=AUTH).json()
        assert data["coverage"]["observed"] == 1
        assert data["requests"][0]["status"] == "captured"
        assert data["space"]["contributors"] == 1
        media_url = detail_url + f"/captures/{capture['id']}/media"
        assert client.get(media_url, headers=invited).content == PNG
        assert client.get(media_url).status_code == 401
        assert (
            client.get(detail_url.replace("atlas-test", "other"), headers=AUTH).status_code == 403
        )
        assert (
            client.post(
                detail_url + "/status", headers=invited, json={"status": "resolved"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                detail_url + "/status", headers=AUTH, json={"status": "resolved"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                detail_url + "/captures",
                content=PNG,
                headers={
                    **invited,
                    "Content-Type": "image/png",
                    "X-Sweep-Capture": json.dumps(metadata),
                },
            ).status_code
            == 409
        )
    with TestClient(app(tmp_path)) as client:
        detail = client.get(detail_url, headers=invited).json()
        assert detail["space"]["status"] == "resolved"
        assert len(detail["captures"]) == 1
        assert client.get(media_url, headers=invited).content == PNG
        new = client.post(detail_url + "/invitation", headers=AUTH).json()["contributor_token"]
        assert client.get(detail_url, headers=invited).status_code == 403
        assert client.get(detail_url, headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_import_preserves_unknown_capture_time_and_original_across_restart(tmp_path):
    with TestClient(app(tmp_path)) as client:
        created = client.post(BASE, headers=AUTH, json=SPACE).json()
        url = f"{BASE}/{created['space']['id']}"
        invited = {"Authorization": f"Bearer {created['contributor_token']}"}
        metadata = capture_meta(source="import", captured_at=None, position=None)
        response = client.post(
            url + "/captures",
            content=PNG,
            headers={
                **invited,
                "Content-Type": "image/png",
                "X-Sweep-Capture": json.dumps(metadata),
            },
        )
        assert response.status_code == 201, response.text
        item = response.json()
        assert item["captured_at"] is None and item["position"] is None
        assert item["uploaded_at"] > 0 and item["sha256"] == hashlib.sha256(PNG).hexdigest()
    with TestClient(app(tmp_path)) as client:
        detail = client.get(url, headers=invited).json()
        assert detail["captures"][0] == item
        assert detail["coverage"]["observed"] == 0
        assert detail["coverage"]["qualified_captures"] == 0
        assert client.get(url + f"/captures/{item['id']}/media", headers=invited).content == PNG


def test_camera_without_capture_time_is_refused_not_reclassified_as_an_import(tmp_path):
    with TestClient(app(tmp_path)) as client:
        created = client.post(BASE, headers=AUTH, json=SPACE).json()
        url = f"{BASE}/{created['space']['id']}"
        for metadata in [
            capture_meta(captured_at=None),
            {k: v for k, v in capture_meta().items() if k != "captured_at"},
        ]:
            response = client.post(
                url + "/captures",
                content=PNG,
                headers={
                    **AUTH,
                    "Content-Type": "image/png",
                    "X-Sweep-Capture": json.dumps(metadata),
                },
            )
            assert response.status_code == 422
        assert client.get(url, headers=AUTH).json()["captures"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"source": "import"},
        {"position": None},
        {
            "position": {
                "latitude": 37.44001,
                "longitude": -122.15999,
                "accuracy": 200,
                "timestamp": 1,
            }
        },
        {"captured_at": 1},
    ],
)
def test_coverage_never_invents_evidence(change):
    result = coverage(SPACE, [capture_meta(**change)])
    assert result["observed"] == 0
    assert result["percent"] == 0


def test_presence_expires_and_leave_removes_it(tmp_path):
    clock = [100_000]
    store = AtlasStore(tmp_path, clock=lambda: clock[0])
    try:
        from relay.atlas import Contributor

        identifier = store.create("test", NewSpace(**SPACE))["space"]["id"]
        person = Contributor(
            contributor_id="person-one",
            name="Sam",
            position={
                "latitude": 37.44,
                "longitude": -122.16,
                "accuracy": 3,
                "timestamp": clock[0],
            },
        )
        store.publish_presence(identifier, person)
        assert len(store.detail(identifier)["people"]) == 1
        clock[0] += 90_001
        assert store.detail(identifier)["people"] == []
        person.position.timestamp = clock[0]
        store.publish_presence(identifier, person)
        store.leave(identifier, person.contributor_id)
        assert store.detail(identifier)["people"] == []
    finally:
        store.close()


def test_invalid_media_and_metadata_leave_no_files(tmp_path):
    with TestClient(app(tmp_path)) as client:
        identifier = client.post(BASE, headers=AUTH, json=SPACE).json()["space"]["id"]
        endpoint = f"{BASE}/{identifier}/captures"
        assert (
            client.post(
                endpoint,
                headers={
                    **AUTH,
                    "Content-Type": "image/png",
                    "X-Sweep-Capture": json.dumps(capture_meta()),
                },
                content=b"<html>not an image</html>",
            ).status_code
            == 415
        )
        assert client.post(endpoint, headers=AUTH, content=PNG).status_code == 422
        assert list((tmp_path / "atlas" / "media").iterdir()) == []
        assert list((tmp_path / "atlas").glob("upload-*")) == []


def test_nonfinite_coordinates_refused():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NewSpace(**{**SPACE, "latitude": float("nan")})
    with pytest.raises(ValidationError):
        CaptureMetadata(
            **capture_meta(
                position={"latitude": 0, "longitude": 0, "accuracy": float("inf"), "timestamp": 1}
            )
        )

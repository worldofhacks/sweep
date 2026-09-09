"""Public API integration checks for authenticated map mutations and failure recovery."""

import pytest
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.settings import RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    MutableClock,
    acknowledgement_payload,
    membership_payload,
)
from tests.world_bundle_fixtures import fixture_world_draft

HEADERS = {"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}
BASE = f"/api/sessions/{SESSION}"


@pytest.fixture
def platform_client(tmp_path):
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        log_dir=tmp_path,
    )
    app = create_app(settings, clock=MutableClock())
    with TestClient(app) as client:
        app.state.relay_runtime.session(SESSION)
        yield client, app.state.platform_services


def test_authentication_precedes_body_parsing_and_unknown_sessions(platform_client):
    client, service = platform_client
    assert client.post(f"{BASE}/maps/save", content="not json").status_code == 401
    assert (
        client.get(
            f"{BASE}/maps/revisions", headers={"Authorization": f"Bearer {ADAPTER_KEY.decode()}"}
        ).status_code
        == 401
    )
    archived = client.get("/api/sessions/absent/maps/revisions", headers=HEADERS)
    assert archived.status_code == 200
    assert archived.json() == []
    assert client.get("/api/sessions/absent/navigation/catalog", headers=HEADERS).status_code == 409
    assert "absent" not in service.runtime.sessions
    assert service.maps.list(SESSION) == []


@pytest.mark.parametrize(
    ("payload", "content_type", "expected"),
    [
        ('{"draft":{},"draft":{},"expectedRevision":null}', "application/json", 400),
        ('{"draft":{"x":NaN},"expectedRevision":null}', "application/json", 400),
        ("[]", "application/json", 400),
        ("{}", "text/plain", 415),
        ("{", "application/json", 400),
    ],
)
def test_malformed_public_bodies_do_not_create_revision(
    platform_client, payload, content_type, expected
):
    client, service = platform_client
    response = client.post(
        f"{BASE}/maps/save", content=payload, headers={**HEADERS, "Content-Type": content_type}
    )
    assert response.status_code == expected
    assert service.maps.list(SESSION) == []


def test_upload_bound_and_claimed_approval_actor_are_refused(platform_client, monkeypatch):
    client, service = platform_client
    response = client.post(
        f"{BASE}/maps/save",
        json={"draft": fixture_world_draft(), "expectedRevision": None, "approvedBy": "owner"},
        headers=HEADERS,
    )
    assert response.status_code == 400
    monkeypatch.setattr("relay.platform.MAX_REQUEST_BYTES", 64)
    response = client.post(
        f"{BASE}/maps/save",
        content=b" " * 65,
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert service.maps.list(SESSION) == []


def test_conflict_and_cross_session_references_have_typed_public_errors(platform_client):
    client, service = platform_client
    first = client.post(
        f"{BASE}/maps/save",
        json={"draft": fixture_world_draft(), "expectedRevision": None},
        headers=HEADERS,
    )
    assert first.status_code == 200
    reference = first.json()
    assert first.headers["Cache-Control"] == "no-store"
    service.runtime.session("other")
    response = client.post(
        "/api/sessions/other/maps/load", json={"reference": reference}, headers=HEADERS
    )
    assert response.status_code == 404 and response.json()["code"] == "revision_not_found"
    assert (
        client.post(
            f"{BASE}/maps/save",
            json={"draft": fixture_world_draft(), "expectedRevision": reference},
            headers=HEADERS,
        ).status_code
        == 200
    )
    response = client.post(
        f"{BASE}/maps/save",
        json={"draft": fixture_world_draft(), "expectedRevision": reference},
        headers=HEADERS,
    )
    assert response.status_code == 409 and response.json()["code"] == "revision_conflict"
    assert len(service.maps.list(SESSION)) == 2


def test_observation_route_requires_device_bound_credentials(platform_client):
    client, _ = platform_client
    assert client.post(f"{BASE}/observations", json={}, headers=HEADERS).status_code == 401
    assert (
        client.post(
            f"{BASE}/observations",
            json={},
            headers={**HEADERS, "X-Sweep-Source": "adapter", "X-Sweep-Device-Id": "1"},
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "frame",
    [
        membership_payload(action="join", event_id="misrouted-join"),
        membership_payload(action="graceful_leave", event_id="misrouted-leave"),
        acknowledgement_payload(event_id="misrouted-ack"),
    ],
    ids=["membership", "departure", "acknowledgement"],
)
def test_observation_route_does_not_dispatch_non_observation_frames(platform_client, frame):
    client, service = platform_client
    session = service.runtime.session(SESSION)
    before_state = session.current_state()
    before_audit = session.audit_log.replay()

    response = client.post(
        f"{BASE}/observations",
        json=frame,
        headers={
            "Authorization": f"Bearer {ADAPTER_KEY.decode()}",
            "X-Sweep-Source": "adapter",
            "X-Sweep-Device-Id": "1",
        },
    )

    assert response.status_code == 400
    after_state = session.current_state()
    assert after_state["drones"] == before_state["drones"]
    assert after_state["roster_version"] == before_state["roster_version"]
    assert session.audit_log.replay() == before_audit


def test_tracking_failure_retires_reviews_without_losing_saved_revision(
    platform_client, monkeypatch
):
    client, service = platform_client

    def failed_invalidation(_session):
        raise RuntimeError("injected tracking failure")

    monkeypatch.setattr(service.navigation, "invalidate", failed_invalidation)
    response = client.post(
        f"{BASE}/maps/save",
        json={"draft": fixture_world_draft(), "expectedRevision": None},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert service.maps.load(SESSION, response.json())["reference"] == response.json()
    assert service.failed is True
    capabilities = client.get(f"{BASE}/platform", headers=HEADERS).json()
    assert capabilities["navigation"]["review"] is False
    assert client.get(f"{BASE}/navigation/catalog", headers=HEADERS).status_code == 503

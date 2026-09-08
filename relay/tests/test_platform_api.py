"""Production HTTP map workflow using isolated image evidence and no device commands."""

from copy import deepcopy

from fastapi.testclient import TestClient

from relay.app import create_app
from relay.platform import PlatformServices
from relay.settings import RelaySettings
from tests.world_bundle_fixtures import fixture_world_draft

TOKEN = b"platform-api-test-only-console-credential"
SESSION = "map-authoring-http-test"
BASE = f"/api/sessions/{SESSION}"
HEADERS = {"Authorization": f"Bearer {TOKEN.decode()}"}


def make_app(path):
    settings = RelaySettings(relay_token=TOKEN, log_dir=path)
    return create_app(
        settings,
        clock=lambda: 10_000,
        platform_services_factory=lambda runtime: PlatformServices(
            runtime,
            motion_configuration={"fixtureMeasuredConfig": "isolated-test-only"},
            environment={},
        ),
    )


def connect(client, session=SESSION):
    socket = client.websocket_connect(f"/ws/{session}")
    socket.__enter__()
    try:
        socket.send_json({"v": 1, "type": "auth", "source": "console", "token": TOKEN.decode()})
        assert socket.receive_json()["type"] == "auth.accepted"
        assert socket.receive_json()["type"] == "state"
    except BaseException:
        socket.__exit__(None, None, None)
        raise
    return socket


def post(client, operation, payload, *, session=SESSION):
    result = client.post(f"/api/sessions/{session}/maps/{operation}", json=payload, headers=HEADERS)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    return result.json()


def test_real_http_authoring_approval_reload_compare_and_restart(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        socket = connect(client)
        try:
            capabilities = client.get(f"{BASE}/platform", headers=HEADERS).json()
            assert capabilities["navigation"] == {"review": True, "dispatch": False}
            assert "observe" not in capabilities["mapAuthoring"]["operations"]
            assert client.get(f"{BASE}/maps/revisions", headers=HEADERS).json() == []
            draft = fixture_world_draft()
            reference = post(client, "save", {"draft": draft, "expectedRevision": None})
            receipt = post(client, "validate", {"reference": reference})
            assert receipt["valid"] is True
            approval = post(
                client,
                "approve",
                {
                    "reference": reference,
                    "validationId": receipt["validationId"],
                },
            )
            assert approval["approvedBy"] == "console"
            loaded = post(client, "load", {"reference": reference})
            assert loaded == {"reference": reference, "draft": draft}

            catalog = client.get(f"{BASE}/navigation/catalog", headers=HEADERS)
            assert catalog.status_code == 200, catalog.text
            assert catalog.json()["catalog"]["map"]["approvalId"] == approval["auditId"]
            resolved = client.post(
                f"{BASE}/navigation/resolve",
                headers=HEADERS,
                json={"query": "entry", "selected": []},
            )
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()["destination"]["zoneId"] == "lobby"

            changed = deepcopy(draft)
            changed["metadata"]["mapVersion"] = "fixture-v2"
            next_ref = post(client, "save", {"draft": changed, "expectedRevision": reference})
            comparison = post(client, "compare", {"left": reference, "right": next_ref})
            assert comparison["changes"]
            assert client.get(f"{BASE}/navigation/catalog", headers=HEADERS).status_code == 409
            next_receipt = post(client, "validate", {"reference": next_ref})
            next_approval = post(
                client,
                "approve",
                {
                    "reference": next_ref,
                    "validationId": next_receipt["validationId"],
                },
            )
            original_bundle = app.state.platform_services.maps.approved_bundle(SESSION, next_ref)
            assert original_bundle["approval"] == next_approval
            # Exercise the real periodic projection synchronously. Depending on
            # background fanout to persist the audit made restart timing-dependent.
            live_session = app.state.relay_runtime.sessions[SESSION]
            live_session.periodic_events()
            assert live_session.audit_log.last_sequence > 0
            assert (
                client.get("/metrics", headers=HEADERS).json()["sessions"][SESSION][
                    "commands_issued"
                ]
                == 0
            )
        finally:
            socket.__exit__(None, None, None)

    original_audit = live_session.audit_log.path.read_bytes()
    restarted = make_app(tmp_path)
    fresh_session = f"{SESSION}-after-restart"
    fresh_base = f"/api/sessions/{fresh_session}"
    with TestClient(restarted) as client:
        # Immutable map persistence is independent of live-session restoration.
        maps = restarted.state.platform_services.maps
        assert maps.load(SESSION, next_ref) == {"reference": next_ref, "draft": changed}
        assert maps.approved_bundle(SESSION, next_ref) == original_bundle
        assert SESSION not in restarted.state.relay_runtime.sessions
        with client.websocket_connect(f"/ws/{SESSION}") as closed_socket:
            closed_socket.send_json(
                {"v": 1, "type": "auth", "source": "console", "token": TOKEN.decode()}
            )
            refusal = closed_socket.receive_json()
        assert refusal["type"] == "auth.refused"
        assert refusal["reason"] == "session_closed"
        assert SESSION not in restarted.state.relay_runtime.sessions
        assert live_session.audit_log.path.read_bytes() == original_audit

        socket = connect(client, fresh_session)
        try:
            # A fresh session inherits neither a map revision nor its approval.
            assert client.get(f"{fresh_base}/maps/revisions", headers=HEADERS).json() == []
            inherited = client.post(
                f"{fresh_base}/maps/load", json={"reference": next_ref}, headers=HEADERS
            )
            assert inherited.status_code == 404
            assert inherited.json()["code"] == "revision_not_found"
            assert (
                client.get(f"{fresh_base}/navigation/catalog", headers=HEADERS).status_code == 409
            )

            imported = post(
                client, "save", {"draft": changed, "expectedRevision": None}, session=fresh_session
            )
            assert imported["bundleId"] != next_ref["bundleId"]
            receipt = post(client, "validate", {"reference": imported}, session=fresh_session)
            approval = post(
                client,
                "approve",
                {"reference": imported, "validationId": receipt["validationId"]},
                session=fresh_session,
            )
            assert approval["reference"] == imported
            assert approval["auditId"] != next_approval["auditId"]
            catalog = client.get(f"{fresh_base}/navigation/catalog", headers=HEADERS)
            assert catalog.status_code == 200, catalog.text
            assert catalog.json()["catalog"]["map"]["approvalId"] == approval["auditId"]
            assert maps.approved_bundle(SESSION, next_ref) == original_bundle
            assert (
                client.get("/metrics", headers=HEADERS).json()["sessions"][fresh_session][
                    "commands_issued"
                ]
                == 0
            )
        finally:
            socket.__exit__(None, None, None)


def test_invalid_draft_is_saved_but_never_approved(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        socket = connect(client)
        try:
            draft = fixture_world_draft()
            draft["metadata"]["registration"]["residualM"] = 10
            reference = post(client, "save", {"draft": draft, "expectedRevision": None})
            receipt = post(client, "validate", {"reference": reference})
            assert receipt["valid"] is False
            refused = client.post(
                f"{BASE}/maps/approve",
                headers=HEADERS,
                json={
                    "reference": reference,
                    "validationId": receipt["validationId"],
                },
            )
            assert refused.status_code == 422
            assert client.get(f"{BASE}/navigation/catalog", headers=HEADERS).status_code == 409
        finally:
            socket.__exit__(None, None, None)

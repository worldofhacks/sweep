"""HTTP observations must belong to the exact saved map being edited."""

from fastapi.testclient import TestClient

from relay.platform_observations import WorldObservationService
from relay.tests.test_platform_api import BASE, HEADERS, SESSION, make_app, post
from relay.tests.test_platform_observations import Clock, pose, principal, source, state
from tests.world_bundle_fixtures import fixture_world_draft


def test_public_positions_and_capture_refuse_another_map_with_identical_labels(
    tmp_path, monkeypatch
):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        service = app.state.platform_services
        session = service.runtime.session(SESSION)
        draft = fixture_world_draft()
        references = []
        for _ in range(2):
            reference = post(client, "save", {"draft": draft, "expectedRevision": None})
            validation = post(client, "validate", {"reference": reference})
            post(
                client,
                "approve",
                {"reference": reference, "validationId": validation["validationId"]},
            )
            references.append(reference)
        first, second = references
        assert first["bundleId"] != second["bundleId"]
        selected = client.post(
            f"{BASE}/navigation/select-map", headers=HEADERS, json={"reference": first}
        )
        assert selected.status_code == 200, selected.text

        metadata = draft["metadata"]
        clock = Clock()
        clock.value = 10_000
        current = {**state(clock), "session": SESSION}
        monkeypatch.setattr(session, "current_state", lambda: current)
        service.observations = WorldObservationService(
            sources={"world-pose": source()},
            registrations={
                "world-pose": {
                    "reference": first,
                    "mapVersion": metadata["mapVersion"],
                    "floorId": metadata["floorId"],
                    "sourceFrame": metadata["registration"]["sourceFrame"],
                    "transformId": metadata["registration"]["transformId"],
                    "qualifiedWorldPose": True,
                }
            },
            approved_bundle=service.navigation.current_approved_bundle,
            database=tmp_path / "qualified-test-observations.sqlite3",
            clock=clock,
        )
        service.observations.ingest(
            SESSION, {**pose(clock), "session": SESSION}, principal(), current
        )
        position = {
            "mapVersion": metadata["mapVersion"],
            "floorId": metadata["floorId"],
            "reference": first,
        }
        record = {**position, "tagId": 7, "deviceId": 11, "connectionEpoch": 1}
        for operation, payload in (("positions", position), ("record", record)):
            rejected = client.post(
                f"{BASE}/maps/{operation}",
                headers=HEADERS,
                json={**payload, "reference": second},
            )
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["code"] == "reference_changed"
            missing = {key: value for key, value in payload.items() if key != "reference"}
            assert (
                client.post(f"{BASE}/maps/{operation}", headers=HEADERS, json=missing).status_code
                == 400
            )
        assert service.observations.audit_records(SESSION) == []
        projected = post(client, "positions", position)
        assert projected["reference"] == first
        assert projected["observations"][0]["reference"] == first
        receipt = post(client, "record", record)
        assert receipt["reference"] == first
        assert service.observations.audit_records(SESSION)[0]["receipt"] == receipt

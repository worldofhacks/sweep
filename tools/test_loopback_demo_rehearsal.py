from __future__ import annotations

import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from websockets.sync.client import connect

from tools.loopback_demo_rehearsal import (
    LoopbackDemoRehearsal,
    _rehearsal_deployment,
    epoch_ms,
)
from tools.loopback_search_fixture import SYNTHETIC_SOURCE_ID


def _http_json(url: str, token: str, payload: object | None = None) -> dict[str, object]:
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except HTTPError as error:
        raise AssertionError(error.read().decode()) from error


def _wait_for(predicate, *, timeout_s: float = 15.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def _select_aircraft(rehearsal: LoopbackDemoRehearsal, token: str) -> None:
    with connect(f"{rehearsal.relay_url}/ws/{rehearsal.session_id}") as socket:
        socket.send(json.dumps({"v": 1, "type": "auth", "source": "console", "token": token}))
        assert json.loads(socket.recv())["type"] == "auth.accepted"
        assert json.loads(socket.recv())["type"] == "state"
        socket.send(
            json.dumps(
                {
                    "v": 1,
                    "t": epoch_ms(),
                    "type": "intent",
                    "intent_id": "loopback-select",
                    "retry_of": None,
                    "source": "console",
                    "session": rehearsal.session_id,
                    "name": "select",
                    "args": {"ids": [1]},
                    "selection": [],
                    "mode": "indoor",
                    "confirm": False,
                }
            )
        )
        for _ in range(32):
            event = json.loads(socket.recv())
            if event.get("intent_id") == "loopback-select" and event.get("source") == "autonomy":
                return
        raise AssertionError("selection did not complete")


def _submit_console_intent(
    rehearsal: LoopbackDemoRehearsal,
    token: str,
    *,
    intent_id: str,
    name: str,
    args: dict[str, object],
    selection: list[int],
) -> None:
    with connect(f"{rehearsal.relay_url}/ws/{rehearsal.session_id}") as socket:
        socket.send(json.dumps({"v": 1, "type": "auth", "source": "console", "token": token}))
        assert json.loads(socket.recv())["type"] == "auth.accepted"
        assert json.loads(socket.recv())["type"] == "state"
        socket.send(
            json.dumps(
                {
                    "v": 1,
                    "t": epoch_ms(),
                    "type": "intent",
                    "intent_id": intent_id,
                    "retry_of": None,
                    "source": "console",
                    "session": rehearsal.session_id,
                    "name": name,
                    "args": args,
                    "selection": selection,
                    "mode": "indoor",
                    "confirm": True,
                }
            )
        )
        for _ in range(32):
            event = json.loads(socket.recv())
            if event.get("intent_id") == intent_id and event.get("source") == "autonomy":
                assert event["status"] == "accepted", json.dumps(event, sort_keys=True)
                return
        raise AssertionError(f"{intent_id} was not admitted")


def test_rehearsal_deployment_reloads_a_signed_fresh_session(tmp_path) -> None:
    now = epoch_ms()
    deployment, projector, pins = _rehearsal_deployment(
        tmp_path, session_id="loopback-fixture", now_ms=now, lifetime_ms=60_000
    )

    deployment.approval.check(
        session="loopback-fixture",
        configuration_sha256=deployment.approval.configuration_sha256,
        now_ms=now,
        epochs=((1, 1),),
    )
    deployment.validate_projector(projector)
    assert pins.clock_mapping.relay_reference_ms == now
    assert deployment.wire_profiles[1].clock_lease_expires_at_ms == now + 60_000
    artifact = deployment.artifact()
    assert [zone.zone_id for zone in artifact.zones] == ["demo-east", "demo-west", "lobby"]
    assert {
        (zone.zone_id, slot.slot_id) for zone in artifact.zones for slot in zone.arrival_slots
    } == {
        ("demo-west", "demo-west-slot"),
        ("lobby", "lobby-slot"),
        ("demo-east", "demo-east-slot"),
    }
    assert all(zone.owner_approved for zone in artifact.zones)


def test_loopback_rehearsal_publishes_a_fresh_signed_pose_and_private_bootstrap(tmp_path) -> None:
    bootstrap = tmp_path / "bootstrap.json"
    with LoopbackDemoRehearsal(
        start_console=False,
        bootstrap_path=bootstrap,
        console_port=47767,
    ) as rehearsal:
        assert bootstrap.stat().st_mode & 0o077 == 0
        payload = json.loads(bootstrap.read_text())
        assert payload["relay"]["origin"] == rehearsal.relay_url
        assert payload["relay"]["session"] == rehearsal.session_id
        request = Request(
            f"http://127.0.0.1:{rehearsal.relay_port}/api/sessions/"
            f"{rehearsal.session_id}/navigation/catalog",
            headers={"Authorization": f"Bearer {payload['relay']['token']}"},
        )
        with urlopen(request, timeout=3) as response:
            catalog = json.loads(response.read())["catalog"]
        assert [
            destination["zoneId"]
            for destination in catalog["destinations"]
            if not destination["excluded"]
        ] == [
            "demo-west",
            "lobby",
            "demo-east",
        ]
        with urlopen(
            Request(
                f"http://127.0.0.1:{rehearsal.relay_port}/session/{rehearsal.session_id}",
                headers={"Authorization": f"Bearer {payload['relay']['token']}"},
            ),
            timeout=3,
        ) as response:
            replay = json.loads(response.read())
        state = next(
            record["event"]
            for record in reversed(replay["events"])
            if record["event"]["type"] == "state"
        )
        assert state["armed"] is True
        assert state["drones"][0]["telemetry"]["state"] == "hovering"

        runtime = rehearsal._composition.runtime
        autonomy = rehearsal._composition.session(rehearsal.session_id)
        assert rehearsal._app is not None
        approved = rehearsal._app.state.platform_services.maps.approved_bundle(rehearsal.session_id)
        authoring_map_pin = rehearsal._composition.config.navigation.config.authoring_map_pin
        assert authoring_map_pin is not None
        assert authoring_map_pin.version == approved["bundle"]["manifest"]["mapVersion"]
        assert authoring_map_pin.content_sha256 == approved["reference"]["contentHash"]
        assert autonomy.search_runtime is not None
        assert autonomy.search_runtime.config.source_by_drone == {1: SYNTHETIC_SOURCE_ID}
        assert autonomy.search_detection is not None
        deadline = time.monotonic() + 3
        pose = None
        while time.monotonic() < deadline:
            pose = runtime.sessions[rehearsal.session_id].control_pose(1)
            if pose is not None:
                break
            time.sleep(0.05)
        assert pose is not None
        relay_state = runtime.sessions[rehearsal.session_id].current_state()
        assert "test:synthetic" in relay_state["drones"][0]["adapter_capabilities"]
    assert not bootstrap.exists()


def test_loopback_rehearsal_completes_two_stops_and_retrieves_each_still(tmp_path) -> None:
    bootstrap = tmp_path / "bootstrap.json"
    with LoopbackDemoRehearsal(
        start_console=False,
        bootstrap_path=bootstrap,
        console_port=47768,
    ) as rehearsal:
        credentials = json.loads(bootstrap.read_text())["relay"]
        base = f"http://127.0.0.1:{rehearsal.relay_port}/api/sessions/{rehearsal.session_id}"
        _select_aircraft(rehearsal, credentials["token"])

        def ready_pose() -> bool:
            pose = rehearsal._composition.runtime.sessions[rehearsal.session_id].control_pose(1)
            if pose is None:
                return False
            age_ms = epoch_ms() - pose.fix_time_ms
            return 2 <= age_ms <= 100

        _wait_for(ready_pose, timeout_s=3)
        selected = [{"id": 1, "deviceClass": "aircraft", "epoch": 1}]
        preview = _http_json(
            f"{base}/multiview/preview",
            credentials["token"],
            {
                "intentId": "loopback-multiview",
                "selected": selected,
                "viewpoints": [
                    {
                        "viewpointId": "west",
                        "zoneId": "demo-west",
                        "captureId": "loopback-west",
                    },
                    {
                        "viewpointId": "east",
                        "zoneId": "demo-east",
                        "captureId": "loopback-east",
                    },
                ],
            },
        )
        _wait_for(
            lambda: (
                (pose := rehearsal._composition.runtime.sessions[rehearsal.session_id].control_pose(1))
                is not None
                and 0 <= epoch_ms() - pose.fix_time_ms <= 20
            ),
            timeout_s=3,
        )
        accepted = _http_json(
            f"{base}/multiview/confirm",
            credentials["token"],
            {key: preview[key] for key in ("previewId", "intentId", "previewHash")},
        )
        workflow_id = accepted["workflowId"]
        assert isinstance(workflow_id, str)

        last_status: dict[str, object] | None = None

        def terminal_status() -> dict[str, object] | None:
            nonlocal last_status
            status = _http_json(f"{base}/multiview/{workflow_id}", credentials["token"])
            last_status = status
            return status if status["status"] in {"completed", "failed"} else None

        try:
            status = _wait_for(terminal_status, timeout_s=15)
        except AssertionError as error:
            raise AssertionError(last_status) from error
        assert status["status"] == "completed", status
        assert [view["state"] for view in status["views"]] == ["completed", "completed"]
        captures = rehearsal._composition.runtime.sessions[rehearsal.session_id].current_state()[
            "captures"
        ]
        completed = {item["capture_id"]: item for item in captures if item["status"] == "completed"}
        assert set(completed) >= {"loopback-west", "loopback-east"}
        assert all(
            item["files"] and {file["retrieval_status"] for file in item["files"]} == {"completed"}
            for item in (completed["loopback-west"], completed["loopback-east"])
        )


def test_loopback_rehearsal_holds_an_empty_aircraft_survey(tmp_path) -> None:
    bootstrap = tmp_path / "bootstrap.json"
    with LoopbackDemoRehearsal(
        start_console=False,
        bootstrap_path=bootstrap,
        console_port=47769,
    ) as rehearsal:
        token = json.loads(bootstrap.read_text())["relay"]["token"]
        base = f"http://127.0.0.1:{rehearsal.relay_port}/session/{rehearsal.session_id}"
        _select_aircraft(rehearsal, token)
        intent_id = "loopback-empty-survey"
        intent = {
            "v": 1,
            "t": epoch_ms(),
            "type": "intent",
            "intent_id": intent_id,
            "retry_of": None,
            "source": "console",
            "session": rehearsal.session_id,
            "name": "search",
            "args": {"zone_id": "lobby", "mode": "survey"},
            "selection": [1],
            "mode": "indoor",
            "confirm": True,
        }
        preview = _http_json(f"{base}/search/preview", token, {"intent": intent})
        assert preview["preview"]["mode"] == "survey"
        assert preview["preview"]["target_class"] is None

        _submit_console_intent(
            rehearsal,
            token,
            intent_id=intent_id,
            name="search",
            args={"zone_id": "lobby", "mode": "survey"},
            selection=[1],
        )
        search = rehearsal._composition.session(rehearsal.session_id).search_runtime
        assert search is not None
        running = _wait_for(
            lambda: (
                status
                if (status := search.status_payload(intent_id))["state"] == "running"
                else None
            ),
            timeout_s=5,
        )
        assert running["candidates"] == []

        _submit_console_intent(
            rehearsal,
            token,
            intent_id="loopback-survey-hold",
            name="hold",
            args={},
            selection=[1],
        )
        status = _wait_for(
            lambda: (
                payload
                if (payload := search.status_payload(intent_id))["state"] in {"hold", "cancelled"}
                else None
            ),
            timeout_s=5,
        )
        assert status["mode"] == "survey"
        assert status["candidates"] == []

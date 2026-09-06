from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from adapters.sim.runtime import create_m14_sim_app
from relay.capabilities import C2_CAPABILITY_PROFILE
from relay.settings import CapabilityRelease, RelaySettings


def _intent(
    session: str,
    name: str,
    intent_id: str,
    selection: list[int],
    *,
    args: dict[str, object] | None = None,
    confirm: bool = False,
) -> dict[str, object]:
    return {
        "v": 1,
        "t": time.time_ns() // 1_000_000,
        "type": "intent",
        "intent_id": intent_id,
        "retry_of": None,
        "source": "console",
        "session": session,
        "name": name,
        "args": args or {},
        "selection": selection,
        "mode": "indoor",
        "confirm": confirm,
    }


def _terminal(socket: WebSocketTestSession, intent_id: str) -> dict[str, object]:
    for _ in range(2_000):
        event = socket.receive_json()
        if (
            event.get("intent_id") == intent_id
            and event.get("source") == "autonomy"
            and event.get("type") in {"acknowledgement", "refusal"}
        ):
            return event
    raise AssertionError(f"no autonomy terminal result for {intent_id}")


def test_m14_sim_arbiter_uses_configured_relay_freshness(tmp_path) -> None:
    app = create_m14_sim_app(
        RelaySettings(
            relay_token=b"m14-simulator-freshness-test-key",
            log_dir=tmp_path,
            telemetry_freshness_ms=250,
        )
    )

    safety = app.state.sim_bridge_factory.safety
    assert safety.max_link_age_ms == 250
    assert safety.max_position_age_ms == 250


def test_m14_sim_app_threads_the_explicit_c2_profile(tmp_path) -> None:
    settings = RelaySettings(
        relay_token=b"r" * 32,
        log_dir=tmp_path,
        capability_release=CapabilityRelease.C2,
    )
    app = create_m14_sim_app(settings, auto_start_nodes=False)

    with TestClient(app):
        runtime = app.state.relay_runtime
        relay_session = runtime.session("sim-c2")

        profile = app.state.sim_bridge_factory.capability_profile

        assert settings.capability_profile is C2_CAPABILITY_PROFILE
        assert profile.supports("disarm")
        assert profile.supports("sweep")
        assert runtime.capability_profile is profile
        assert relay_session.capability_profile is profile
        assert app.state.sim_bridge_factory.bridges["sim-c2"].capability_profile is profile


@pytest.mark.parametrize("aircraft_count", [4, 5, 6])
def test_deployable_c2_sim_completes_the_bounded_m15_mission(
    tmp_path: Path, aircraft_count: int
) -> None:
    session = f"m15-{aircraft_count}"
    relay_key = b"m15-relay-key-that-is-at-least-32"
    adapter_keys = {
        drone_id: f"m15-adapter-{drone_id}-key-that-is-at-least-32".encode()
        for drone_id in range(1, aircraft_count + 1)
    }
    settings = RelaySettings(
        relay_token=relay_key,
        adapter_keys=adapter_keys,
        log_dir=tmp_path,
        capability_release=CapabilityRelease.C2,
        sim_aircraft_count=aircraft_count,
    )
    app = create_m14_sim_app(settings)
    selection = list(range(1, aircraft_count + 1))
    mission = (
        ("arm", "m15-arm", [], {}, False),
        ("select", "m15-select", [], {"ids": selection}, False),
        ("takeoff", "m15-takeoff", selection, {}, True),
        ("formation_set", "m15-formation", selection, {"name": "line"}, False),
        ("altitude", "m15-altitude", selection, {"delta": 1}, False),
        ("spacing", "m15-spacing", selection, {"delta": 1}, False),
        ("sweep", "m15-sweep", selection, {}, True),
        ("land_all", "m15-land", [], {}, True),
        ("disarm", "m15-disarm", [], {}, False),
    )
    started = time.monotonic()
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{session}") as socket:
            socket.send_json(
                {"v": 1, "type": "auth", "source": "console", "token": relay_key.decode()}
            )
            assert socket.receive_json()["type"] == "auth.accepted"
            initial = socket.receive_json()
            assert initial["type"] == "state"
            assert len(initial["drones"]) == aircraft_count
            outcomes = []
            for name, intent_id, targets, args, confirm in mission:
                socket.send_json(
                    _intent(
                        session,
                        name,
                        intent_id,
                        targets,
                        args=args,
                        confirm=confirm,
                    )
                )
                outcomes.append(_terminal(socket, intent_id))
        replay = client.get(
            f"/session/{session}",
            headers={"Authorization": f"Bearer {relay_key.decode()}"},
        ).json()

    assert time.monotonic() - started < 180
    assert all(outcome["status"] == "completed" for outcome in outcomes)
    flight = app.state.sim_bridge_factory.flights[session]
    assert all(not aircraft.armed for aircraft in flight.aircraft.values())
    assert all(aircraft.flight_state.value == "landed" for aircraft in flight.aircraft.values())
    records = [record["event"] for record in replay["events"]]
    assert not any(record["type"] in {"refusal", "safety_action"} for record in records)
    assert all(
        record["outcome"] == "accepted" for record in records if record["type"] == "intent_record"
    )
    plans = {
        record["result"]["plan"]["intent_name"]: record["result"]["plan"]
        for record in records
        if record["type"] == "autonomy_result"
        and record["status"] == "completed"
        and isinstance(record["result"].get("plan"), dict)
    }
    assert set(plans) >= {
        "formation_set",
        "altitude",
        "spacing",
        "sweep",
        "land_all",
        "disarm",
    }
    assert len(plans["formation_set"]["commands"]) == aircraft_count
    assert len(plans["altitude"]["commands"]) == 2 * aircraft_count
    assert plans["altitude"]["altitude_grounding"] == {
        "step_m": 0.5,
        "floor_z_m": 0.0,
        "configuration_id": "sim-ground-plane-v1",
        "completion_tolerance_m": 0.05,
    }
    assert len(plans["spacing"]["commands"]) == aircraft_count
    assert len(plans["sweep"]["commands"]) == 2 * aircraft_count
    assert plans["disarm"]["commands"] == []

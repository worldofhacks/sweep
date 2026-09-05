from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from planner.models import ExecutionResult, LifecycleStatus, Plan, Position
from relay.app import RelayRuntime
from relay.auth import Principal
from relay.autonomy import (
    LIFECYCLE_SOURCE,
    AutonomyComposition,
    AutonomyConfig,
    control_projection,
    create_autonomy_app,
    relay_snapshot,
)
from relay.intent_v1 import IntentName
from relay.session import RelaySession
from relay.settings import AdapterBackend, RelaySettings, SettingsError
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    capabilities_payload,
    capture_readiness_payload,
    membership_payload,
    telemetry_payload,
)
from tests.autonomy_fixtures import camera_config, planning_config, safety_config

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTONOMY_VARIABLES = ("SWEEP_PLANNING_JSON", "SWEEP_SAFETY_JSON", "SWEEP_SIM_CAMERA_JSON")


def _env_example() -> dict[str, str]:
    """Parse the dotenv file the way ``uv run --env-file`` does for single-quoted values."""
    values: dict[str, str] = {}
    for line in (REPO_ROOT / ".env.example").read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[key] = value
    return values


def _config(*, sim_camera: bool = True) -> AutonomyConfig:
    return AutonomyConfig(
        planning=planning_config(),
        safety=safety_config(),
        sim_camera=camera_config() if sim_camera else None,
    )


def _settings(log_dir: Path, backend: AdapterBackend = AdapterBackend.SIM) -> RelaySettings:
    return RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        log_dir=log_dir,
        adapter_backend=backend,
    )


def _intent(
    name: str,
    *,
    intent_id: str,
    selection: list[int],
    args: dict[str, object] | None = None,
    confirm: bool = False,
    timestamp: int = 1_756_700_000_000,
) -> dict[str, object]:
    return {
        "v": 1,
        "t": timestamp,
        "type": "intent",
        "intent_id": intent_id,
        "retry_of": None,
        "source": "console",
        "session": SESSION,
        "name": name,
        "args": args or {},
        "selection": selection,
        "mode": "indoor",
        "confirm": confirm,
    }


def _plan(name: IntentName, **changes: object) -> Plan:
    arguments: dict[str, object] = {
        "plan_id": "plan:intent-1",
        "intent_id": "intent-1",
        "intent_name": name,
        "roster_version": 3,
        "selection": (1,),
        "confirmed": False,
        "commands": (),
    }
    arguments.update(changes)
    return Plan(**arguments)  # type: ignore[arg-type]


def _result(plan: Plan | None, status: LifecycleStatus) -> ExecutionResult:
    return ExecutionResult(intent_id="intent-1", roster_version=3, status=status, plan=plan)


def _authenticate(socket: WebSocketTestSession, *, source: str) -> None:
    frame: dict[str, object] = {
        "v": 1,
        "type": "auth",
        "source": source,
        "token": CONSOLE_KEY.decode(),
    }
    if source == "adapter":
        frame.update(drone_id=1, token=ADAPTER_KEY.decode())
    socket.send_json(frame)
    assert socket.receive_json()["type"] == "auth.accepted"
    assert socket.receive_json()["type"] == "state"


def _receive_until(
    socket: WebSocketTestSession,
    predicate: Callable[[dict[str, object]], bool],
    *,
    maximum: int = 200,
) -> dict[str, object]:
    for _ in range(maximum):
        event = socket.receive_json()
        if predicate(event):
            return event
    raise AssertionError("expected event did not arrive")


def _autonomy_outcome(socket: WebSocketTestSession, intent_id: str) -> dict[str, object]:
    return _receive_until(
        socket,
        lambda event: (
            event["type"] in {"acknowledgement", "refusal"}
            and event.get("intent_id") == intent_id
            and event.get("source") == LIFECYCLE_SOURCE
        ),
    )


def test_env_example_autonomy_values_are_the_ci_fixtures() -> None:
    config = AutonomyConfig.from_env(_env_example())

    assert config.planning == planning_config()
    assert config.safety == safety_config()
    assert config.sim_camera == camera_config()


def test_missing_sim_camera_is_allowed_only_off_the_sim_backend(tmp_path: Path) -> None:
    environment = {
        key: value for key, value in _env_example().items() if key != AUTONOMY_VARIABLES[2]
    }

    config = AutonomyConfig.from_env(environment)
    remote_app, remote = create_autonomy_app(_settings(tmp_path, AdapterBackend.REMOTE), config)
    remote.close()

    assert config.sim_camera is None
    assert remote_app.title == "Sweep relay"
    with pytest.raises(SettingsError, match="SWEEP_SIM_CAMERA_JSON"):
        create_autonomy_app(_settings(tmp_path), config)


@pytest.mark.parametrize(
    ("variable", "value", "match"),
    [
        ("SWEEP_SAFETY_JSON", "", "required"),
        ("SWEEP_PLANNING_JSON", "{not json", "valid JSON"),
        ("SWEEP_PLANNING_JSON", "[]", "JSON object"),
        ("SWEEP_SIM_CAMERA_JSON", '{"panorama_width_px": 4096}', "keys must be exactly"),
        ("SWEEP_SAFETY_JSON", "__unordered_geofence__", "geofence"),
        ("SWEEP_SAFETY_JSON", "__zero_spacing__", "min_spacing_m"),
        ("SWEEP_PLANNING_JSON", "__headings_object__", "JSON array"),
    ],
)
def test_autonomy_config_fails_closed_on_missing_extra_or_gate_disabling_values(
    variable: str, value: str, match: str
) -> None:
    environment = _env_example()
    if value == "__unordered_geofence__":
        value = environment[variable].replace('"max_x":10.0', '"max_x":-10.0')
    elif value == "__zero_spacing__":
        value = environment[variable].replace('"min_spacing_m":0.8', '"min_spacing_m":0')
    elif value == "__headings_object__":
        value = environment[variable].replace("[0,45,90,135,180,225,270,315]", "{}")
    environment[variable] = value

    with pytest.raises(SettingsError, match=match):
        AutonomyConfig.from_env(environment)


def test_relay_snapshot_derives_safety_facts_and_excludes_silent_aircraft(
    relay_session: RelaySession,
    adapter_principal: Principal,
    clock: MutableClock,
) -> None:
    second = Principal(source="adapter", drone_id=2, signing_key=ADAPTER_KEY)
    relay_session.process_membership(
        membership_payload(action="join", event_id="join-1"), adapter_principal
    )
    relay_session.process_telemetry(
        telemetry_payload(event_id="telemetry-1", state="landed"), adapter_principal
    )
    relay_session.process_membership(
        membership_payload(action="readiness", event_id="ready-1"), adapter_principal
    )
    relay_session.process_membership(
        membership_payload(action="join", event_id="join-2", drone_id=2), second
    )
    before_capabilities = relay_snapshot(
        relay_session.current_state(),
        operator_last_seen_ms=None,
        capture_readiness=relay_session.capture_readiness,
    )
    relay_session.process_node_frame(
        capabilities_payload(event_id="capabilities-1"), adapter_principal
    )
    relay_session.process_node_frame(
        capture_readiness_payload(event_id="readiness-1", storage_ok=False), adapter_principal
    )
    storage_not_ok = relay_snapshot(
        relay_session.current_state(),
        operator_last_seen_ms=clock.value,
        capture_readiness=relay_session.capture_readiness,
    )
    relay_session.process_node_frame(
        capture_readiness_payload(event_id="readiness-2", timestamp=clock.value + 1),
        adapter_principal,
    )
    landed = relay_snapshot(
        relay_session.current_state(),
        operator_last_seen_ms=clock.value,
        capture_readiness=relay_session.capture_readiness,
    )
    without_readiness_source = relay_snapshot(
        relay_session.current_state(), operator_last_seen_ms=clock.value
    )
    relay_session.process_telemetry(
        telemetry_payload(event_id="telemetry-2", timestamp=clock.value + 1, state="hovering"),
        adapter_principal,
    )
    hovering = relay_snapshot(relay_session.current_state(), operator_last_seen_ms=clock.value)

    assert set(landed.aircraft) == {1}, "aircraft 2 has no telemetry and is excluded"
    assert before_capabilities.operator_present is False
    assert before_capabilities.aircraft[1].camera_ready is False
    assert before_capabilities.aircraft[1].storage_remaining_bytes == 0
    assert storage_not_ok.aircraft[1].camera_ready is False, "storage_ok gates readiness"
    assert without_readiness_source.aircraft[1].camera_ready is False, "no frame, not ready"
    aircraft = landed.aircraft[1]
    assert aircraft.armed is False
    assert aircraft.physical_rc_available is True
    assert aircraft.camera_ready is True
    assert aircraft.storage_remaining_bytes == 50_000_000
    assert aircraft.active_task_id is None
    assert aircraft.position_loss_since_ms is None
    assert aircraft.home == Position(1.0, 2.0, 0.5)
    assert landed.operator_present is True
    assert landed.operator_last_seen_ms == clock.value
    assert landed.now_ms == clock.value
    assert landed.armed is False
    assert hovering.aircraft[1].armed is True
    assert hovering.aircraft[1].airborne is True


def test_control_projection_latches_estop_from_the_intent_and_earns_the_rest() -> None:
    arm = _plan(IntentName.ARM, selection=(), armed_update=True)
    select = _plan(IntentName.SELECT, selection=(), selection_update=(1, 2))
    estop = _plan(IntentName.ESTOP, selection=(), estop_update=True)
    takeoff = _plan(IntentName.TAKEOFF, confirmed=True)
    summary = {
        "plan_id": "plan:intent-1",
        "intent_id": "intent-1",
        "intent_name": "takeoff",
        "roster_version": 3,
        "selection": [1],
    }

    assert control_projection(IntentName.ARM, _result(arm, LifecycleStatus.COMPLETED)) == {
        "armed": True,
        "accepted_plan": None,
    }
    assert control_projection(IntentName.ARM, _result(arm, LifecycleStatus.REFUSED)) == {
        "accepted_plan": None
    }
    assert control_projection(IntentName.SELECT, _result(select, LifecycleStatus.COMPLETED)) == {
        "selection": (1, 2),
        "accepted_plan": None,
    }
    assert control_projection(IntentName.SELECT, _result(select, LifecycleStatus.FAILED)) == {
        "accepted_plan": None
    }
    # The network stop latches from the intent, whatever the planner or dispatcher did.
    assert control_projection(IntentName.ESTOP, _result(estop, LifecycleStatus.FAILED)) == {
        "estop": True,
        "accepted_plan": None,
    }
    assert control_projection(IntentName.ESTOP, _result(None, LifecycleStatus.REFUSED)) == {
        "estop": True,
        "accepted_plan": None,
    }
    assert control_projection(IntentName.ESTOP, _result(None, LifecycleStatus.FAILED)) == {
        "estop": True,
        "accepted_plan": None,
    }
    assert control_projection(IntentName.TAKEOFF, _result(takeoff, LifecycleStatus.COMPLETED)) == {
        "accepted_plan": None
    }
    assert control_projection(IntentName.TAKEOFF, _result(takeoff, LifecycleStatus.EXECUTING)) == {
        "accepted_plan": summary
    }
    assert control_projection(IntentName.TAKEOFF, _result(None, LifecycleStatus.EXECUTING)) == {}


def test_sim_backend_runs_the_checkpoint_intents_in_process_without_wire_commands(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    app, composition = create_autonomy_app(
        _settings(tmp_path), _config(), clock=clock, event_ids=event_ids
    )
    headers = {"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}
    try:
        with TestClient(app) as client:
            with (
                client.websocket_connect(f"/ws/{SESSION}") as console,
                client.websocket_connect(f"/ws/{SESSION}") as adapter,
            ):
                _authenticate(console, source="console")
                _authenticate(adapter, source="adapter")
                adapter.send_json(membership_payload(action="join", event_id="join-1"))
                adapter.send_json(telemetry_payload(event_id="telemetry-1", state="landed"))
                adapter.send_json(membership_payload(action="readiness", event_id="ready-1"))
                _receive_until(
                    console,
                    lambda event: (
                        event["type"] == "state" and event["drones"][0]["membership"] == "ready"
                    ),
                )

                console.send_json(_intent("arm", intent_id="arm-1", selection=[]))
                arm = _autonomy_outcome(console, "arm-1")
                armed_state = _receive_until(
                    console, lambda event: event["type"] == "state" and event["armed"] is True
                )
                console.send_json(
                    _intent("select", intent_id="select-1", selection=[], args={"ids": [1]})
                )
                select = _autonomy_outcome(console, "select-1")
                console.send_json(
                    _intent("takeoff", intent_id="takeoff-1", selection=[1], confirm=True)
                )
                takeoff = _autonomy_outcome(console, "takeoff-1")
                console.send_json(
                    _intent(
                        "translate",
                        intent_id="translate-1",
                        selection=[1],
                        args={"dx": 1, "dy": 0},
                    )
                )
                translate = _autonomy_outcome(console, "translate-1")
                console.send_json(_intent("estop", intent_id="estop-1", selection=[]))
                estop = _autonomy_outcome(console, "estop-1")
                stopped_state = _receive_until(
                    console, lambda event: event["type"] == "state" and event["estop"] is True
                )
            replay = client.get(f"/session/{SESSION}", headers=headers).json()
    finally:
        composition.close()

    assert (arm["type"], arm["status"], arm["command_id"]) == ("acknowledgement", "completed", None)
    assert armed_state["selection"] == []
    assert (select["status"], select["reason"]) == ("completed", None)
    assert (takeoff["status"], takeoff["reason"]) == ("completed", None)
    # The simulator flew in process; the authoritative relay state is still the node's
    # last telemetry, so the arbiter refuses motion from a landed aircraft.
    assert (translate["type"], translate["reason"]) == ("refusal", "invalid_state")
    assert translate["drone_id"] == 1
    assert estop["status"] == "completed"
    assert stopped_state["selection"] == [1]
    assert stopped_state["drones"][0]["flight_state"] == "landed"

    records = [record["event"] for record in replay["events"]]
    assert "command" not in {record["type"] for record in records}
    outcomes = [
        (record["intent_id"], record["status"])
        for record in records
        if record["type"] in {"acknowledgement", "refusal"} and record["source"] == LIFECYCLE_SOURCE
    ]
    assert outcomes == [
        ("arm-1", "completed"),
        ("select-1", "completed"),
        ("takeoff-1", "completed"),
        ("translate-1", "refused"),
        ("estop-1", "completed"),
    ]
    assert all(
        record["outcome"] == "accepted" for record in records if record["type"] == "intent_record"
    )


def test_graceful_leave_is_authorized_only_for_a_landed_disarmed_aircraft(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    composition = AutonomyComposition(_config())
    runtime = RelayRuntime(
        _settings(tmp_path),
        clock=clock,
        event_ids=event_ids,
        intent_sink_factory=composition.intent_sink_factory,
        leave_authorizer_factory=composition.leave_authorizer_factory,
    )
    composition.bind(runtime)
    adapter = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
    try:
        session = runtime.session(SESSION)
        session.process_membership(membership_payload(action="join", event_id="join-1"), adapter)
        session.process_telemetry(
            telemetry_payload(event_id="telemetry-1", state="hovering"), adapter
        )
        session.process_membership(
            membership_payload(action="readiness", event_id="ready-1"), adapter
        )
        airborne = session.process_membership(
            membership_payload(action="graceful_leave", event_id="leave-1"), adapter
        )
        session.process_telemetry(
            telemetry_payload(event_id="telemetry-2", timestamp=clock.value + 1, state="landed"),
            adapter,
        )
        landed = session.process_membership(
            membership_payload(
                action="graceful_leave", event_id="leave-2", timestamp=clock.value + 1
            ),
            adapter,
        )
    finally:
        composition.close()

    assert airborne[0]["type"] == "refusal"
    assert airborne[0]["reason"] == "graceful_leave_not_authorized"
    assert [event["type"] for event in landed] == ["membership", "state"]
    assert landed[0]["action"] == "graceful_leave"
    assert landed[1]["drones"][0]["membership"] == "leaving"


def test_relay_snapshot_marks_a_requested_stop_before_the_relay_latches_it(
    relay_session: RelaySession, adapter_principal: Principal, clock: MutableClock
) -> None:
    relay_session.process_membership(
        membership_payload(action="join", event_id="join-1"), adapter_principal
    )
    relay_session.process_telemetry(
        telemetry_payload(event_id="telemetry-1", state="hovering"), adapter_principal
    )
    state = relay_session.current_state()

    plain = relay_snapshot(state, operator_last_seen_ms=clock.value)
    stopped = relay_snapshot(state, operator_last_seen_ms=clock.value, estop_requested=True)

    assert state["estop"] is False
    assert plain.estop_active is False
    assert stopped.estop_active is True
    assert stopped.aircraft == plain.aircraft

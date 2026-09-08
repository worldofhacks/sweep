from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import uvicorn

from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig
from adapters.ohmni.fake import FakeGroundDevice
from adapters.ohmni.runtime import GroundRuntimeConfig, OhmniRuntime
from planner.models import FlightState, LocalHeightEvidence
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.contracts import NodeType
from relay.intent_v1 import IntentName
from relay.observation_ingress import ObservationConfiguration
from relay.observations import FrameDeclaration, FrameRegistry, SourceBinding
from relay.settings import AdapterBackend, RelaySettings, SettingsError
from relay.supervised_vertical import SupervisedVerticalArbiter, SupervisedVerticalConfig
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, SESSION
from relay.tests.test_bridge_roundtrip import ConsoleProbe, RelayServer
from tests.autonomy_fixtures import make_intent, make_snapshot

WAIT_S = 10.0
GROUND_ID = 9
GROUND_KEY = b"ground-adapter-key-that-is-at-least-32-bytes"


def _vertical_config() -> SupervisedVerticalConfig:
    return SupervisedVerticalConfig(
        takeoff_altitude_m=1.8,
        maximum_height_m=2.0,
        operator_declared_vertical_clearance_m=2.0,
        min_battery_fraction=0.3,
        min_link_quality=0.5,
        max_link_age_ms=5_000,
        max_local_height_age_ms=500,
        operator_timeout_ms=5_000,
        max_future_clock_skew_ms=1_000,
        motion_conflict_window_ms=500,
    )


@pytest.fixture
def vertical_server(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[RelayServer]:
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY, GROUND_ID: GROUND_KEY},
        node_types={1: NodeType.AIRCRAFT, GROUND_ID: NodeType.GROUND},
        log_dir=tmp_path,
        adapter_backend=AdapterBackend.REMOTE,
        telemetry_freshness_ms=5_000,
        node_watchdog_hold_ms=5_000,
        node_watchdog_failsafe_ms=10_000,
        observation_configuration=_ground_observation_configuration(),
    )
    config = getattr(request, "param", _vertical_config())
    app, composition = create_autonomy_app(settings, AutonomyConfig(supervised_vertical=config))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on", timeout_graceful_shutdown=2)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    _wait_until(lambda: server.started, "relay startup")
    try:
        yield RelayServer(runtime=app.state.relay_runtime, port=port)
    finally:
        server.should_exit = True
        thread.join(timeout=WAIT_S)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=WAIT_S)
        composition.close()


def test_supervised_vertical_round_trip_without_home_pose_or_gps_quality(
    vertical_server: RelayServer,
) -> None:
    console = ConsoleProbe(vertical_server.url)
    node = FakeNode(
        FakeNodeConfig(
            relay_url=vertical_server.url,
            session=SESSION,
            drone_id=1,
            token=ADAPTER_KEY.decode(),
            adapter_id="vertical-fake-node",
            telemetry_hz=5.0,
            home_pose_confirmed=False,
        )
    )
    node._aircraft.pos_quality = 0.0
    console.start()
    node.start()
    try:
        _wait_until(
            lambda: (
                _home_unconfirmed_ready_with_height(vertical_server)
                and _position_quality(vertical_server) == 0.0
            ),
            "GPS-denied node ready without a world home pose and with SDK height",
        )

        select_id, select = _run(console, "select", [], {"ids": [1]})
        assert select["status"] == "completed", select
        arm_id, arm = _run(console, "arm", [])
        assert arm["status"] == "completed", arm
        takeoff_id, takeoff = _run(console, "takeoff", [1], confirm=True)
        assert takeoff["status"] == "completed", takeoff

        _wait_until(lambda: _telemetry_state(vertical_server) == "hovering", "hovering telemetry")
        records = [event["event"] for event in vertical_server.runtime.replay(SESSION)["events"]]
        commands = [event for event in records if event.get("type") == "command"]
        takeoff_command = next(event for event in commands if event["intent_id"] == takeoff_id)
        assert takeoff_command["operation"] == "takeoff"
        assert takeoff_command["args"] == {
            "z_mm": 1_800,
            "maximum_height_mm": 2_000,
            "max_local_height_age_ms": 500,
        }
        assert [select_id, arm_id] != ["", ""]

        translate_id, translate = _run(console, "translate", [1], {"dx": 1, "dy": 0})
        assert translate["reason"] == "unsupported"
        assert not any(
            event.get("intent_id") == translate_id and event.get("type") == "command"
            for event in records
        )
    finally:
        node.stop()
        console.stop()


@pytest.mark.parametrize(
    "vertical_server",
    [
        replace(
            _vertical_config(),
            takeoff_altitude_m=1.2,
            maximum_height_m=2.4384,
            operator_declared_vertical_clearance_m=2.5908,
        )
    ],
    indirect=True,
)
def test_reviewed_12m_profile_signs_the_2438mm_hard_ceiling(
    vertical_server: RelayServer,
) -> None:
    console = ConsoleProbe(vertical_server.url)
    node = FakeNode(
        FakeNodeConfig(
            relay_url=vertical_server.url,
            session=SESSION,
            drone_id=1,
            token=ADAPTER_KEY.decode(),
            adapter_id="vertical-12m-fake-node",
            telemetry_hz=5.0,
            home_pose_confirmed=False,
        )
    )
    node._aircraft.pos_quality = 0.0
    console.start()
    node.start()
    try:
        _wait_until(
            lambda: _home_unconfirmed_ready_with_height(vertical_server),
            "GPS-denied node ready with SDK height",
        )
        _, select = _run(console, "select", [], {"ids": [1]})
        assert select["status"] == "completed", select
        _, arm = _run(console, "arm", [])
        assert arm["status"] == "completed", arm
        takeoff_id, takeoff = _run(console, "takeoff", [1], confirm=True)
        assert takeoff["status"] == "completed", takeoff
        records = [event["event"] for event in vertical_server.runtime.replay(SESSION)["events"]]
        command = next(
            event
            for event in records
            if event.get("type") == "command" and event.get("intent_id") == takeoff_id
        )
        assert command["args"] == {
            "z_mm": 1_200,
            "maximum_height_mm": 2_438,
            "max_local_height_age_ms": 500,
        }
    finally:
        node.stop()
        console.stop()


def test_supervised_vertical_keeps_the_ground_velocity_route_with_one_aircraft(
    vertical_server: RelayServer,
) -> None:
    console = ConsoleProbe(vertical_server.url)
    aircraft = FakeNode(
        FakeNodeConfig(
            relay_url=vertical_server.url,
            session=SESSION,
            drone_id=1,
            token=ADAPTER_KEY.decode(),
            adapter_id="vertical-fake-node",
            telemetry_hz=5.0,
        )
    )
    aircraft._aircraft.pos_quality = 0.0
    ground_device = FakeGroundDevice()
    ground = OhmniRuntime(
        GroundRuntimeConfig(
            relay_url=vertical_server.url,
            session=SESSION,
            device_id=GROUND_ID,
            token=GROUND_KEY.decode(),
            adapter_id="vertical-ground-node",
            heartbeat_hold_ms=5_000,
            heartbeat_failsafe_ms=10_000,
            telemetry_hz=1.0,
            lidar_mount_x_m=0.0,
            lidar_mount_y_m=0.0,
            lidar_mount_z_m=0.25,
            lidar_mount_yaw_deg=0.0,
        ),
        ground_device,
    )
    console.start()
    aircraft.start()
    ground.start()
    try:
        _wait_until(
            lambda: _ready_with_height(vertical_server) and _ground_ready(vertical_server),
            "mixed aircraft and ground readiness",
        )
        _, ground_select = _run(console, "select", [], {"ids": [GROUND_ID]})
        assert ground_select["status"] == "completed", ground_select
        _wait_until(lambda: _ground_ready(vertical_server), "ground readiness after selection")
        ground_id, result = _run(
            console,
            "ground_velocity",
            [GROUND_ID],
            {"linear_mm_s": 100, "angular_mrad_s": 0, "duration_ms": 25},
            confirm=True,
        )
        assert result["status"] == "completed", result
        assert ground_device.x > 0
        _, select = _run(console, "select", [], {"ids": [1]})
        assert select["status"] == "completed", select
        _, arm = _run(console, "arm", [])
        assert arm["status"] == "completed", arm
        takeoff_id, takeoff = _run(console, "takeoff", [1], confirm=True)
        assert takeoff["status"] == "completed", takeoff
        _wait_until(lambda: _telemetry_state(vertical_server) == "hovering", "aircraft hover")
        records = [event["event"] for event in vertical_server.runtime.replay(SESSION)["events"]]
        assert any(
            event.get("type") == "command"
            and event.get("intent_id") == takeoff_id
            and event.get("operation") == "takeoff"
            for event in records
        )
        assert any(
            event.get("type") == "command"
            and event.get("intent_id") == ground_id
            and event.get("operation") == "ground_velocity"
            for event in records
        )
    finally:
        ground.stop()
        aircraft.stop()
        console.stop()


def test_supervised_vertical_lands_with_low_battery_and_missing_height(
    vertical_server: RelayServer,
) -> None:
    console = ConsoleProbe(vertical_server.url)
    node = FakeNode(
        FakeNodeConfig(
            relay_url=vertical_server.url,
            session=SESSION,
            drone_id=1,
            token=ADAPTER_KEY.decode(),
            adapter_id="vertical-fake-node",
            telemetry_hz=5.0,
        )
    )
    node._aircraft.pos_quality = 0.0
    console.start()
    node.start()
    try:
        _wait_until(lambda: _ready_with_height(vertical_server), "vertical node readiness")
        assert _run(console, "select", [], {"ids": [1]})[1]["status"] == "completed"
        assert _run(console, "arm", [])[1]["status"] == "completed"
        assert _run(console, "takeoff", [1], confirm=True)[1]["status"] == "completed"
        _wait_until(lambda: _telemetry_state(vertical_server) == "hovering", "hover telemetry")

        node._aircraft.battery = 0.1
        node.config = replace(node.config, local_height_source=False)
        _wait_until(
            lambda: _low_battery_without_height(vertical_server),
            "low-battery telemetry and omitted local height",
        )

        land_id, land = _run(console, "land", [1], confirm=True)
        assert land["status"] == "completed", land
        _wait_until(lambda: _telemetry_state(vertical_server) == "landed", "land telemetry")
        records = [event["event"] for event in vertical_server.runtime.replay(SESSION)["events"]]
        assert any(
            event.get("type") == "command"
            and event.get("intent_id") == land_id
            and event.get("operation") == "land"
            for event in records
        )
    finally:
        node.stop()
        console.stop()


def test_supervised_vertical_rejects_a_high_local_height_for_takeoff() -> None:
    snapshot = make_snapshot(1, selection=(1,), flight_state=FlightState.LANDED, armed=True)
    snapshot = replace(
        snapshot,
        aircraft={
            1: replace(
                snapshot.aircraft[1],
                local_height=LocalHeightEvidence(
                    z_m=1.8,
                    observed_at_ms=snapshot.now_ms,
                    source="flight_controller_altitude",
                ),
            )
        },
    )

    refusal = SupervisedVerticalArbiter(_vertical_config()).check_intent(
        make_intent(IntentName.TAKEOFF, selection=(1,), confirm=True), snapshot
    )

    assert refusal is not None
    assert refusal.reason.value == "local_height_unavailable"


def test_supervised_vertical_allows_stops_without_an_aircraft() -> None:
    snapshot = make_snapshot(0, selection=(GROUND_ID,), ground_ids=(GROUND_ID,))
    arbiter = SupervisedVerticalArbiter(_vertical_config())

    assert (
        arbiter.check_intent(make_intent(IntentName.HOLD, selection=(GROUND_ID,)), snapshot) is None
    )
    assert arbiter.check_intent(make_intent(IntentName.ESTOP, selection=()), snapshot) is None


def test_supervised_vertical_env_rejects_world_localization_and_navigation() -> None:
    vertical = json.dumps(asdict(_vertical_config()))

    with pytest.raises(SettingsError, match="cannot be combined"):
        AutonomyConfig.from_env(
            {
                "SWEEP_SUPERVISED_VERTICAL_JSON": vertical,
                "SWEEP_CONTROL_LOCALIZATION_JSON": "{}",
            }
        )


def test_supervised_vertical_takeoff_requires_fresh_local_height() -> None:
    snapshot = make_snapshot(1, selection=(1,), flight_state=FlightState.LANDED, armed=True)
    snapshot = replace(snapshot, aircraft={1: replace(snapshot.aircraft[1], local_height=None)})

    refusal = SupervisedVerticalArbiter(_vertical_config()).check_intent(
        make_intent(IntentName.TAKEOFF, selection=(1,), confirm=True), snapshot
    )

    assert refusal is not None
    assert refusal.reason.value == "local_height_unavailable"


def test_supervised_vertical_rejects_a_ceiling_above_eight_and_a_half_feet() -> None:
    with pytest.raises(ValueError, match="8.5 foot"):
        SupervisedVerticalConfig(**(asdict(_vertical_config()) | {"maximum_height_m": 2.5909}))


def _ready_with_height(server: RelayServer) -> bool:
    session = server.runtime.sessions.get(SESSION)
    if session is None:
        return False
    drones = session.current_state()["drones"]
    return bool(
        drones
        and drones[0]["membership"] == "ready"
        and isinstance(drones[0]["node_status"], dict)
        and drones[0]["node_status"]["local_height"] is not None
    )


def _ground_ready(server: RelayServer) -> bool:
    session = server.runtime.sessions.get(SESSION)
    if session is None:
        return False
    ground = next(
        (
            drone
            for drone in session.current_state()["drones"]
            if drone.get("drone_id") == GROUND_ID
        ),
        None,
    )
    return (
        isinstance(ground, dict)
        and ground.get("membership") == "ready"
        and ground.get("selectable") is True
        and ground.get("control_authority") is True
        and isinstance(ground.get("adapter_capabilities"), list)
        and "ground_drive" in ground["adapter_capabilities"]
    )


def _ground_observation_configuration() -> ObservationConfiguration:
    return ObservationConfiguration(
        bindings=(
            SourceBinding(
                session=SESSION,
                device_id=GROUND_ID,
                connection_epoch=1,
                source_id="ohmni-pose",
                node_type="ground",
                allowed_frames=("odom", "body"),
                allowed_payload_kinds=("pose",),
            ),
        ),
        frames=FrameRegistry(
            (
                FrameDeclaration(
                    frame_id="odom",
                    kind="odom",
                    axis_convention="right_handed_z_up",
                    unit="m",
                    session=SESSION,
                    device_id=GROUND_ID,
                    connection_epoch=1,
                    source_id="ohmni-pose",
                ),
                FrameDeclaration(
                    frame_id="body",
                    kind="body",
                    axis_convention="forward_left_up",
                    unit="m",
                    session=SESSION,
                    device_id=GROUND_ID,
                    connection_epoch=1,
                    source_id="ohmni-pose",
                ),
            )
        ),
    )


def _position_quality(server: RelayServer) -> float | None:
    session = server.runtime.sessions.get(SESSION)
    if session is None:
        return None
    drones = session.current_state()["drones"]
    return None if not drones else drones[0]["telemetry"]["pos_quality"]


def _home_unconfirmed_ready_with_height(server: RelayServer) -> bool:
    session = server.runtime.sessions.get(SESSION)
    if session is None:
        return False
    aircraft = next(
        (drone for drone in session.current_state()["drones"] if drone.get("drone_id") == 1), None
    )
    return (
        isinstance(aircraft, dict)
        and aircraft.get("membership") == "ready"
        and aircraft.get("readiness_reasons") == []
        and aircraft.get("selectable") is True
        and aircraft.get("home_pose") is None
        and isinstance(aircraft.get("node_status"), dict)
        and aircraft["node_status"].get("local_height") is not None
    )


def _low_battery_without_height(server: RelayServer) -> bool:
    session = server.runtime.sessions.get(SESSION)
    if session is None:
        return False
    aircraft = next(
        (drone for drone in session.current_state()["drones"] if drone.get("drone_id") == 1), None
    )
    return (
        isinstance(aircraft, dict)
        and isinstance(aircraft.get("telemetry"), dict)
        and aircraft["telemetry"].get("battery") == 0.1
        and isinstance(aircraft.get("node_status"), dict)
        and aircraft["node_status"].get("local_height") is None
    )


def _telemetry_state(server: RelayServer) -> str | None:
    session = server.runtime.sessions.get(SESSION)
    if session is None:
        return None
    drones = session.current_state()["drones"]
    return None if not drones else drones[0]["telemetry"]["state"]


def _run(
    console: ConsoleProbe,
    name: str,
    selection: list[int],
    args: dict[str, object] | None = None,
    *,
    confirm: bool = False,
) -> tuple[str, dict[str, object]]:
    intent_id = f"vertical-{name}-{uuid.uuid4().hex[:8]}"
    console.send(
        {
            "v": 1,
            "t": time.time_ns() // 1_000_000,
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
    )
    return intent_id, _outcome(console, intent_id)


def _outcome(console: ConsoleProbe, intent_id: str) -> dict[str, object]:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        for event in list(console.events):
            if event.get("intent_id") != intent_id:
                continue
            if event.get("source") == "autonomy" and event.get("status") in {
                "completed",
                "refused",
                "failed",
                "invalidated",
            }:
                return event
            if event.get("type") == "refusal" and event.get("source") == "relay":
                return event
        time.sleep(0.02)
    raise AssertionError(f"no outcome for {intent_id}")


def _wait_until(predicate: object, what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_takeoff_policy_is_bounded_by_declared_clearance_and_rounds_down() -> None:
    config = replace(
        _vertical_config(), maximum_height_m=2.1, operator_declared_vertical_clearance_m=1.9009
    )
    assert config.takeoff_parameters() == {
        "z": 1.8,
        "maximum_height_mm": 1900,
        "max_local_height_age_ms": 500,
    }


@pytest.mark.parametrize(
    "reason,admitted", [("virtual_stick_dropped", True), ("rc_takeover", False)]
)
def test_signed_current_node_status_controls_land_recovery(vertical_server, reason, admitted):
    class RecoveringNode(FakeNode):
        loss_reason = None
        announced_loss = False

        def _node_status_frame(self):
            if self.loss_reason is not None and not self.announced_loss:
                self.announced_loss = True
                self._enqueue(
                    self._signed_membership(
                        "readiness",
                        connection_epoch=self._connection_epoch,
                        home_pose_confirmed=True,
                        control_authority=False,
                        rc_safety_operator_present=True,
                    )
                )
            frame = super()._node_status_frame()
            if self.loss_reason is not None:
                frame.update(control_authority=False, authority_change_reason=self.loss_reason)
            return frame

    console = ConsoleProbe(vertical_server.url)
    node = RecoveringNode(
        FakeNodeConfig(
            relay_url=vertical_server.url,
            session=SESSION,
            drone_id=1,
            token=ADAPTER_KEY.decode(),
            adapter_id="recovery-test-node",
            telemetry_hz=5.0,
        )
    )
    console.start()
    node.start()
    try:
        _wait_until(lambda: _ready_with_height(vertical_server), "recovery node ready")
        assert _run(console, "select", [], {"ids": [1]})[1]["status"] == "completed"
        assert _run(console, "arm", [])[1]["status"] == "completed"
        assert _run(console, "takeoff", [1], confirm=True)[1]["status"] == "completed"
        node.loss_reason = reason

        def loss_visible():
            state = vertical_server.runtime.sessions[SESSION].current_state()["drones"][0]
            status = state.get("node_status")
            return (
                state["control_authority"] is False
                and isinstance(status, dict)
                and status.get("authority_change_reason") == reason
            )

        _wait_until(loss_visible, "signed authority loss")
        land_id, result = _run(console, "land", [1], confirm=True)
        if admitted:
            assert result["status"] == "completed", result
            _wait_until(lambda: _telemetry_state(vertical_server) == "landed", "recovered land")
        else:
            assert result["status"] == "refused", result
            assert result["reason"] == "control_authority", result
        events = [item["event"] for item in vertical_server.runtime.replay(SESSION)["events"]]
        commands = [
            event
            for event in events
            if event.get("type") == "command" and event.get("intent_id") == land_id
        ]
        assert bool(commands) is admitted
        assert all(event["operation"] == "land" for event in commands)
    finally:
        node.stop()
        console.stop()

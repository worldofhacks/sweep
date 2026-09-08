"""Stationary aircraft camera controls require current signed/runtime evidence."""

from dataclasses import replace

import pytest

from adapters.dispatch import AdapterDispatcher
from adapters.dji_mini3.remote import RemoteBridgeAdapter
from adapters.dji_mini3.test_remote import ScriptedLink
from arbiter.safety import SafetyArbiter
from planner.controller import AutonomyController
from planner.models import (
    CameraControlEvidence,
    CommandOperation,
    DeviceClass,
    DriveState,
    FleetSnapshot,
    FlightState,
    MembershipState,
    Plan,
    Position,
)
from planner.planner import DeterministicPlanner
from planner.test_models import relay_state
from relay.autonomy import relay_snapshot
from relay.camera_control import camera_capture_id
from relay.camera_control_args import CAMERA_CONTROL_CAPABILITY
from relay.intent_v1 import AcceptedIntent, IntentName, RejectedIntent, validate_intent
from relay.tests.test_intent_v1 import _c1_payload
from tests.autonomy_fixtures import (
    NOW_MS,
    make_intent,
    make_snapshot,
    make_stack,
    planning_config,
    replace_aircraft,
    safety_config,
)


def evidence(**changes):
    return replace(
        CameraControlEvidence(
            NOW_MS,
            1,
            True,
            "nominal",
            frozenset({"camera_ready", "capture_photo", "set_gimbal_pitch"}),
            -90.0,
            20.0,
        ),
        **changes,
    )


def camera_snapshot(**changes):
    return replace_aircraft(
        make_snapshot(count=1, selection=(), armed=False),
        1,
        **{
            "membership": MembershipState.DEGRADED,
            "flight_state": FlightState.LANDED,
            "armed": False,
            "home": None,
            "position_quality": 0.0,
            "camera_control": evidence(),
            "capabilities": frozenset({"flight", CAMERA_CONTROL_CAPABILITY}),
            **changes,
        },
    )


def camera_intent(args=None, **kwargs):
    return make_intent(
        IntentName.CAMERA_CONTROL,
        selection=kwargs.pop("selection", (1,)),
        confirm=kwargs.pop("confirm", True),
        args={"kind": "ready"} if args is None else args,
        **kwargs,
    )


def stack(snapshot, scripts=None):
    link = ScriptedLink(
        epochs={key: row.connection_epoch for key, row in snapshot.aircraft.items()},
        scripts=scripts,
    )
    adapter = RemoteBridgeAdapter(link, epochs=link.epochs, acknowledgement_timeout_ms=50)
    arbiter = SafetyArbiter(safety_config())
    planner = DeterministicPlanner(planning_config())
    controller = AutonomyController(
        planner=planner,
        arbiter=arbiter,
        dispatcher=AdapterDispatcher(flight=adapter, camera=adapter, arbiter=arbiter),
    )
    return controller, arbiter, planner, link


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"kind": []},
        {"kind": "panorama"},
        {"kind": "retrieve_media"},
        {"kind": "photo", "capture_id": "arbitrary"},
        {"kind": "ready", "force": True},
        {"kind": "gimbal", "pitch_mdeg": True},
        {"kind": "gimbal", "pitch_mdeg": 1.0},
        {"kind": "gimbal", "pitch_mdeg": 180001},
        {"kind": "gimbal", "pitch_mdeg": -180001},
        {"kind": "gimbal", "pitch": 0},
    ],
)
def test_camera_intent_rejects_nonexact_arguments(args):
    assert isinstance(
        validate_intent({**_c1_payload("console", IntentName.CAMERA_CONTROL), "args": args}),
        RejectedIntent,
    )


@pytest.mark.parametrize("source", ["language", "keyboard", "webcam"])
def test_camera_intent_is_console_only(source):
    assert isinstance(
        validate_intent(_c1_payload(source, IntentName.CAMERA_CONTROL)), RejectedIntent
    )


@pytest.mark.parametrize(
    "args,operation,parameters",
    [
        ({"kind": "ready"}, CommandOperation.CAMERA_READY, {}),
        (
            {"kind": "gimbal", "pitch_mdeg": -90000},
            CommandOperation.SET_GIMBAL_PITCH,
            {"pitch_mdeg": -90000},
        ),
        (
            {"kind": "photo"},
            CommandOperation.CAPTURE_PHOTO,
            {"capture_id": camera_capture_id("camera-intent")},
        ),
    ],
)
def test_exact_camera_operations_work_landed_without_gps_lidar_home_or_session_arm(
    args, operation, parameters
):
    snapshot = camera_snapshot()
    intent = camera_intent(args, intent_id="camera-intent")
    assert isinstance(
        validate_intent({**_c1_payload("console", IntentName.CAMERA_CONTROL), "args": args}),
        AcceptedIntent,
    )
    controller, _, _, link = stack(snapshot)
    result = controller.execute(intent, snapshot)
    assert result.refusal is None
    assert result.capture_bundle is None
    assert len(link.sent) == 1
    assert link.sent[0].operation is operation
    assert link.sent[0].args == parameters
    assert link.sent[0].drone_id == 1 and link.sent[0].connection_epoch == 1
    assert result.plan.selection_update is None and result.plan.armed_update is None
    assert snapshot.selection == () and not snapshot.armed


@pytest.mark.parametrize(
    "change",
    [
        {"membership": MembershipState.DISCONNECTED},
        {
            "device_class": DeviceClass.GROUND_VEHICLE,
            "flight_state": None,
            "drive_state": DriveState.IDLE,
        },
        {"capabilities": frozenset({"camera"})},
        {"control_authority": False},
        {"rc_safety_operator_present": False},
        {"camera_control": None},
        {"camera_control": evidence(t_ms=NOW_MS - 5001)},
        {"camera_control": evidence(t_ms=NOW_MS + 1)},
        {"camera_control": evidence(connection_epoch=2)},
        {"camera_control": evidence(control_authority=False)},
        {"camera_control": evidence(watchdog_state="hold")},
        {"camera_control": evidence(supported_operations=frozenset({"capture_photo"}))},
        {"connection_epoch": 2},
    ],
)
def test_camera_readiness_rechecked_after_preview_without_io(change):
    snapshot = camera_snapshot()
    controller, _, _, link = stack(snapshot)
    prepared = controller.prepare(camera_intent(), snapshot)
    result = controller.dispatch_prepared(
        prepared, current_snapshot=lambda: replace_aircraft(snapshot, 1, **change)
    )
    assert result.refusal is not None
    assert link.sent == []


@pytest.mark.parametrize(
    "changes",
    [
        {"estop_active": True},
        {"operator_present": False},
        {"operator_last_seen_ms": NOW_MS - 10001},
    ],
)
def test_camera_operator_and_stop_gate(changes):
    snapshot = replace(camera_snapshot(), **changes)
    controller, _, _, link = stack(snapshot)
    assert controller.execute(camera_intent(), snapshot).refusal is not None
    assert link.sent == []


@pytest.mark.parametrize("kwargs", [{"selection": ()}, {"selection": (1, 2)}, {"confirm": False}])
def test_camera_requires_one_confirmed_target(kwargs):
    snapshot = camera_snapshot()
    controller, _, _, link = stack(snapshot)
    assert controller.execute(camera_intent(**kwargs), snapshot).refusal is not None
    assert link.sent == []


@pytest.mark.parametrize(
    "facts,pitch",
    [
        ({}, -90001),
        ({}, 20001),
        ({"pitch_min_deg": None}, 0),
        ({"pitch_max_deg": float("nan")}, 0),
        ({"pitch_min_deg": 30}, 0),
        ({"pitch_min_deg": -181}, 0),
        ({"pitch_max_deg": True}, 0),
    ],
)
def test_gimbal_requires_measured_limits_and_in_range_target(facts, pitch):
    snapshot = camera_snapshot(camera_control=evidence(**facts))
    controller, _, _, link = stack(snapshot)
    result = controller.execute(camera_intent({"kind": "gimbal", "pitch_mdeg": pitch}), snapshot)
    assert result.refusal is not None and link.sent == []


@pytest.mark.parametrize(
    "mutation",
    ["motion", "retrieval", "state", "target", "extra", "safety", "scope", "epoch", "args"],
)
def test_camera_plan_cannot_add_motion_retrieval_or_change_approved_identity(mutation):
    snapshot = camera_snapshot()
    _, arbiter, planner, _ = stack(snapshot)
    plan = planner.plan(camera_intent({"kind": "photo"}), snapshot)
    assert isinstance(plan, Plan)
    command = plan.commands[0]
    forged = {
        "motion": lambda: replace(
            plan, commands=(replace(command, operation=CommandOperation.HOVER),)
        ),
        "retrieval": lambda: replace(
            plan, commands=(replace(command, operation=CommandOperation.RETRIEVE_MEDIA),)
        ),
        "state": lambda: replace(plan, armed_update=True),
        "target": lambda: replace(plan, commands=(replace(command, drone_id=2),)),
        "extra": lambda: replace(plan, commands=(command, command)),
        "safety": lambda: replace(plan, commands=(replace(command, safety_action=True),)),
        "scope": lambda: replace(plan, selection_update=(1,)),
        "epoch": lambda: replace(plan, commands=(replace(command, connection_epoch=2),)),
        "args": lambda: replace(
            plan, commands=(replace(command, parameters={"capture_id": "another"}),)
        ),
    }[mutation]()
    assert arbiter.check_plan(forged, snapshot) is not None
    assert arbiter.check_plan_structure(forged, snapshot) is not None
    assert arbiter.check_command(forged, forged.commands[0], snapshot) is not None


def test_camera_evidence_projection_binds_registry_epoch_and_preserves_missing_facts():
    raw = {
        "t": NOW_MS,
        "control_authority": True,
        "watchdog_state": "nominal",
        "device_telemetry": {"controls": {"supported_operations": ["camera_ready"]}},
    }
    projected = CameraControlEvidence.from_node_status(raw, 3)
    assert projected is not None and projected.connection_epoch == 3
    assert projected.pitch_min_deg is None and projected.pitch_max_deg is None
    assert CameraControlEvidence.from_mapping(projected.to_dict()) == projected
    assert CameraControlEvidence.from_node_status({**raw, "connection_epoch": 2}, 3) is None
    assert CameraControlEvidence.from_node_status({**raw, "t": True}, 3) is None


def test_real_relay_projection_retains_camera_evidence_without_navigation_readiness():
    raw = relay_state()
    raw["t"] = NOW_MS
    raw["selection"] = []
    row = raw["drones"][0]
    row.update(
        membership="degraded",
        adapter_capabilities=["flight", CAMERA_CONTROL_CAPABILITY],
        home_pose=None,
        pos_quality=0,
        node_status={
            "t": NOW_MS,
            "control_authority": True,
            "watchdog_state": "nominal",
            "device_telemetry": {
                "controls": {"supported_operations": ["camera_ready"]},
                "gimbal": {"pitch_min_deg": None, "pitch_max_deg": None},
            },
        },
    )
    row["telemetry"].update(t=NOW_MS, pos_quality=0)
    snapshot = relay_snapshot(raw, operator_last_seen_ms=NOW_MS)
    camera = snapshot.aircraft[1].camera_control
    assert camera is not None and camera.connection_epoch == 2
    assert snapshot.aircraft[1].home is None and not snapshot.aircraft[1].camera_ready
    assert FleetSnapshot.from_mapping(snapshot.to_dict()) == snapshot
    controller, _, _, link = stack(snapshot)
    assert controller.execute(camera_intent(), snapshot).refusal is None
    assert len(link.sent) == 1 and link.sent[0].connection_epoch == 2


def test_missing_adapter_camera_executor_does_not_fall_back_to_simulated_capture():
    snapshot = camera_snapshot(home=Position(0, 0, 0))
    controller, _, _, _, _, camera = make_stack(snapshot)
    result = controller.execute(camera_intent({"kind": "photo"}), snapshot)
    assert result.refusal is not None
    assert result.capture_bundle is None
    assert camera.calls == []


@pytest.mark.parametrize("script", [[], [("failed", "camera_unsupported", "fixture unavailable")]])
def test_failed_standalone_camera_never_issues_flight_recovery_commands(script):
    snapshot = camera_snapshot(flight_state=FlightState.HOVERING, armed=True)
    controller, _, _, link = stack(snapshot, {CommandOperation.CAMERA_READY: script})
    result = controller.execute(camera_intent(), snapshot)
    assert result.refusal is not None
    assert [(request.drone_id, request.operation) for request in link.sent] == [
        (1, CommandOperation.CAMERA_READY)
    ]

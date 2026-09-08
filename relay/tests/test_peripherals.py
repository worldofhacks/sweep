"""Per-device peripheral controls retain explicit confirmation and dispatch guards."""

from dataclasses import replace

import pytest

from adapters.dispatch import AdapterDispatcher
from adapters.dji_mini3.remote import RemoteBridgeAdapter
from adapters.dji_mini3.test_remote import ScriptedLink
from arbiter.safety import SafetyArbiter
from nodekit import protocol
from nodekit.peripherals import PERIPHERAL_CAPABILITY
from planner.controller import AutonomyController
from planner.models import CommandOperation, MembershipState, NodeControlEvidence, Plan
from planner.planner import DeterministicPlanner
from relay.auth import verify_event_signature
from relay.contracts import ContractError, parse_command
from relay.intent_v1 import IntentName, RejectedIntent, validate_intent
from relay.tests.conftest import ADAPTER_KEY, command_payload
from relay.tests.test_intent_v1 import _c1_payload
from tests.autonomy_fixtures import (
    NOW_MS,
    make_intent,
    make_mixed_snapshot,
    planning_config,
    replace_aircraft,
    safety_config,
)

ARGS = {"kind": "screen", "text": "Please leave the path clear."}


def peripheral_snapshot():
    return replace_aircraft(
        make_mixed_snapshot(selection=(1,), armed=False),
        11,
        membership=MembershipState.DEGRADED,
        control_authority=False,
        position_quality=0,
        node_control=NodeControlEvidence(NOW_MS, 1, False, "nominal"),
        capabilities=frozenset({PERIPHERAL_CAPABILITY, "neck", "screen", "lights", "speech"}),
    )


def peripheral_intent(*, args=None, **kwargs):
    return make_intent(
        IntentName.ROBOT_PERIPHERAL,
        selection=kwargs.pop("selection", (11,)),
        confirm=kwargs.pop("confirm", True),
        args=ARGS if args is None else args,
        **kwargs,
    )


def stack(snapshot):
    link = ScriptedLink(
        epochs={key: device.connection_epoch for key, device in snapshot.aircraft.items()}
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
        {"kind": "neck", "position": True},
        {"kind": "neck", "position": 299},
        {"kind": "neck", "position": 651},
        {"kind": "neck", "position": 512.0},
        {"kind": "neck", "position": 512, "wake": True},
        {"kind": "lights", "h": 0, "s": 0, "v": 256},
        {"kind": "speech", "text": ""},
        {"kind": "speech", "text": "a\nb"},
        {"kind": "speech", "text": " a"},
        {"kind": "screen", "text": "a" * 241},
        {"kind": "screen", "text": "a\u00a0b"},
        {"kind": "screen", "text": "\ue000"},
        {"kind": "screen", "text": "x", "html": True},
    ],
)
def test_exact_arguments_rejected_by_intent_and_both_signed_command_parsers(args):
    assert isinstance(
        validate_intent({**_c1_payload("console", IntentName.ROBOT_PERIPHERAL), "args": args}),
        RejectedIntent,
    )
    raw = command_payload(event_id="peripheral-command", operation="robot_peripheral", args=args)
    with pytest.raises(ContractError):
        parse_command(raw)
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_command(raw)


@pytest.mark.parametrize(
    "args",
    [
        ARGS,
        {"kind": "neck", "position": 300},
        {"kind": "neck", "position": 650},
        {"kind": "screen", "text": ""},
        {"kind": "lights", "h": 0, "s": 255, "v": 0},
        {"kind": "speech", "text": "hello world 🚗"},
    ],
)
def test_signed_arguments_and_target_are_bound(args):
    raw = command_payload(event_id="peripheral-command", operation="robot_peripheral", args=args)
    frame = parse_command(raw)
    assert verify_event_signature(frame.unsigned_event(), frame.signature, ADAPTER_KEY)
    assert protocol.parse_command(raw).args == args
    tampered = parse_command({**raw, "drone_id": 12})
    assert not verify_event_signature(tampered.unsigned_event(), tampered.signature, ADAPTER_KEY)


def test_connected_robot_uses_explicit_target_while_disarmed_and_lidar_blocked():
    snapshot = peripheral_snapshot()
    controller, _, _, link = stack(snapshot)
    result = controller.execute(peripheral_intent(), snapshot)
    assert result.refusal is None
    assert len(link.sent) == 1
    assert link.sent[0].operation is CommandOperation.ROBOT_PERIPHERAL
    assert link.sent[0].drone_id == 11 and link.sent[0].args == ARGS
    assert snapshot.selection == (1,) and not snapshot.armed
    assert result.plan.selection_update is None and result.plan.armed_update is None


@pytest.mark.parametrize(
    "change",
    [
        {"membership": MembershipState.DISCONNECTED},
        {"capabilities": frozenset({"screen"})},
        {"capabilities": frozenset({PERIPHERAL_CAPABILITY})},
        {"connection_epoch": 2},
        {"node_control": None},
        {"node_control": NodeControlEvidence(NOW_MS, 2, True, "nominal")},
        {"node_control": NodeControlEvidence(NOW_MS - 5001, 1, True, "nominal")},
        {"node_control": NodeControlEvidence(NOW_MS + 1, 1, True, "nominal")},
        {"node_control": NodeControlEvidence(NOW_MS, 1, True, "hold")},
        {"node_control": NodeControlEvidence(NOW_MS, 1, True, "failsafe")},
    ],
)
def test_capability_membership_and_epoch_are_rechecked_after_preview(change):
    snapshot = peripheral_snapshot()
    controller, _, _, link = stack(snapshot)
    prepared = controller.prepare(peripheral_intent(), snapshot)
    result = controller.dispatch_prepared(
        prepared, current_snapshot=lambda: replace_aircraft(snapshot, 11, **change)
    )
    assert result.refusal is not None
    assert link.sent == []


@pytest.mark.parametrize(
    "kwargs", [{"selection": (1,)}, {"selection": ()}, {"selection": (11, 12)}, {"confirm": False}]
)
def test_unsupported_target_or_unconfirmed_request_has_no_io(kwargs):
    snapshot = peripheral_snapshot()
    controller, _, _, link = stack(snapshot)
    result = controller.execute(peripheral_intent(**kwargs), snapshot)
    assert result.refusal is not None
    assert link.sent == []


def test_operator_loss_refuses_and_stop_blocks_neck_but_keeps_stationary_peripherals():
    for kind, args in [("screen", ARGS), ("neck", {"kind": "neck", "position": 512})]:
        snapshot = replace(peripheral_snapshot(), estop_active=True)
        controller, _, _, link = stack(snapshot)
        result = controller.execute(peripheral_intent(args=args), snapshot)
        assert (result.refusal is None) == (kind == "screen")
        assert len(link.sent) == (1 if kind == "screen" else 0)
    snapshot = replace(peripheral_snapshot(), operator_present=False)
    controller, _, _, link = stack(snapshot)
    assert controller.execute(peripheral_intent(), snapshot).refusal is not None
    assert link.sent == []


def test_neck_requires_current_node_authority_before_sending_exact_request():
    args = {"kind": "neck", "position": 512}
    snapshot = peripheral_snapshot()
    controller, _, _, link = stack(snapshot)
    assert controller.execute(peripheral_intent(args=args), snapshot).refusal is not None
    assert link.sent == []
    snapshot = replace_aircraft(
        snapshot, 11, node_control=NodeControlEvidence(NOW_MS, 1, True, "nominal")
    )
    controller, _, _, link = stack(snapshot)
    result = controller.execute(peripheral_intent(args=args), snapshot)
    assert result.refusal is None
    assert link.sent[0].args == args
    assert snapshot.aircraft[11].node_control.control_authority is True


@pytest.mark.parametrize("script", [[], [("failed", "unsupported", "fixture unavailable")]])
def test_failed_peripheral_never_issues_wheel_recovery_commands(script):
    snapshot = peripheral_snapshot()
    controller, _, _, link = stack(snapshot)
    link.scripts[CommandOperation.ROBOT_PERIPHERAL] = script
    result = controller.execute(peripheral_intent(), snapshot)
    assert result.refusal is not None
    assert [(request.drone_id, request.operation) for request in link.sent] == [
        (11, CommandOperation.ROBOT_PERIPHERAL)
    ]


def test_capability_withdrawn_during_io_preserves_ack_without_reporting_current_completion():
    snapshot = peripheral_snapshot()
    controller, _, _, link = stack(snapshot)
    current = snapshot
    send = link.send

    def withdraw(request):
        nonlocal current
        send(request)
        current = replace_aircraft(snapshot, 11, capabilities=frozenset())

    link.send = withdraw
    result = controller.dispatch_prepared(
        controller.prepare(peripheral_intent(), snapshot), current_snapshot=lambda: current
    )
    assert result.refusal is not None
    assert any(ack.status.value == "completed" for ack in result.acknowledgements)
    assert [request.operation for request in link.sent] == [CommandOperation.ROBOT_PERIPHERAL]


@pytest.mark.parametrize("source", ["language", "keyboard", "webcam"])
def test_peripheral_intent_is_console_only(source):
    result = validate_intent(_c1_payload(source, IntentName.ROBOT_PERIPHERAL))
    assert isinstance(result, RejectedIntent)
    assert result.reason.value == "source_not_allowed"


@pytest.mark.parametrize(
    "mutation", ["motion", "state", "target", "extra", "source", "scope", "roster"]
)
def test_peripheral_plan_cannot_smuggle_motion_or_fleet_state(mutation):
    snapshot = peripheral_snapshot()
    _, arbiter, planner, _ = stack(snapshot)
    plan = planner.plan(peripheral_intent(), snapshot)
    assert isinstance(plan, Plan)
    command = plan.commands[0]
    forged = {
        "motion": lambda: replace(
            plan, commands=(replace(command, operation=CommandOperation.HOVER),)
        ),
        "state": lambda: replace(plan, armed_update=True),
        "target": lambda: replace(plan, commands=(replace(command, drone_id=12),)),
        "extra": lambda: replace(plan, commands=(command, command)),
        "source": lambda: replace(plan, commands=(replace(command, safety_action=True),)),
        "scope": lambda: replace(plan, selection_update=(11,)),
        "roster": lambda: replace(plan, roster_version=plan.roster_version + 1),
    }[mutation]()
    assert arbiter.check_plan(forged, snapshot) is not None

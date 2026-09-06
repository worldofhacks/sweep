from dataclasses import replace

from planner.control_provenance import ControlProvenance
from planner.models import CommandOperation, PreparedExecution
from planner.test_navigation_runtime import stack
from relay.control_config import ControlRuntimeConfig
from relay.control_localization import ClockMapping, ControlLocalizationPins
from relay.navigation_control import NavigationControl, NavigationControlConfig

KEY = b"navigation-node-key-at-least-32bytes"


def _control(runtime) -> NavigationControl:
    clock = ClockMapping("capture", "relay", 0, 100_000, 1000, 5, True)
    pin = ControlLocalizationPins(
        1, 1, "map", "geometry", "camera", "body", "capture", "relay", ("tag",), clock
    )
    return NavigationControl(
        NavigationControlConfig(
            runtime,
            ControlRuntimeConfig({1: pin}, 5, 500, 0.2, 2_000),
            "navigation-config",
            {1: KEY},
        )
    )


def _localized(snapshot):
    provenance = ControlProvenance(
        "map",
        "geometry",
        "camera",
        "body",
        "capture",
        "relay",
        ("tag",),
        1.0,
        5,
        "ready",
        100_000,
        0.01,
    )
    return replace(
        snapshot,
        aircraft={1: replace(snapshot.aircraft[1], control_provenance=provenance)},
    )


def test_authorization_precedes_mapped_goto_and_pose_uses_p95_radius():
    controller, _, _, snapshot, current, _, intent = stack()
    runtime = controller.planner.navigation
    runtime.require_phone_authorization = True
    prepared = controller.prepare(intent, snapshot, current_snapshot=current)
    assert isinstance(prepared, PreparedExecution)
    command = next(
        item for item in prepared.plan.commands if item.operation is CommandOperation.GOTO
    )
    localized = _localized(snapshot)
    control = _control(runtime)

    authorization = control.authorize(prepared.plan, command, localized, "session")
    pose = control.pose(localized, "session")[0]

    assert authorization["command_id"] == command.command_id
    assert authorization["route_id"] == intent.intent_id
    assert authorization["expires_at_ms"] > authorization["t"]
    assert pose["status"] == "ready"
    assert pose["position_uncertainty_mm"] == 28


def test_missing_localization_emits_nullable_hold_pose_without_fabricated_coordinates():
    controller, _, _, snapshot, current, _, intent = stack()
    runtime = controller.planner.navigation
    runtime.require_phone_authorization = True
    prepared = controller.prepare(intent, snapshot, current_snapshot=current)
    assert isinstance(prepared, PreparedExecution)
    command = next(
        item for item in prepared.plan.commands if item.operation is CommandOperation.GOTO
    )
    control = _control(runtime)
    control.authorize(prepared.plan, command, _localized(snapshot), "session")

    packet = control.pose(snapshot, "session")[0]

    assert packet["status"] == "hold"
    assert all(
        packet[field] is None
        for field in (
            "pose_time_ms",
            "fix_time_ms",
            "x_mm",
            "y_mm",
            "z_mm",
            "position_uncertainty_mm",
        )
    )

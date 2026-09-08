"""Independent authorization for connected robot peripherals, never wheel authority."""

from nodekit.peripherals import PERIPHERAL_CAPABILITY, valid_peripheral_arguments
from planner.models import (
    CommandOperation,
    DeviceClass,
    FleetSnapshot,
    MembershipState,
    Plan,
    Refusal,
    RefusalReason,
)
from relay.capabilities import IntentName


def peripheral_refusal(
    intent_id, selection, args, confirmed, snapshot: FleetSnapshot
) -> Refusal | None:
    def refuse(reason, detail, device=None):
        return Refusal(
            intent_id,
            snapshot.roster_version,
            None if device is None else device.drone_id,
            None if device is None else device.connection_epoch,
            reason,
            detail,
        )

    if not confirmed:
        return refuse(
            RefusalReason.CONFIRMATION_REQUIRED,
            "robot peripheral requests require exact confirmation",
        )
    if (
        len(selection) != 1
        or type(selection[0]) is not int
        or selection[0] <= 0
        or not valid_peripheral_arguments(args)
    ):
        return refuse(
            RefusalReason.INVALID_SELECTION,
            "one robot and bounded peripheral arguments are required",
        )
    device = snapshot.aircraft.get(selection[0])
    if device is None or device.membership not in {
        MembershipState.REGISTERED,
        MembershipState.READY,
        MembershipState.DEGRADED,
    }:
        return refuse(
            RefusalReason.AIRCRAFT_NOT_REGISTERED, "peripheral target is not connected", device
        )
    if device.device_class is not DeviceClass.GROUND_VEHICLE:
        return refuse(
            RefusalReason.UNSUPPORTED_FOR_DEVICE_CLASS,
            "robot peripherals require a ground vehicle",
            device,
        )
    if PERIPHERAL_CAPABILITY not in device.capabilities or args["kind"] not in device.capabilities:
        return refuse(
            RefusalReason.UNSUPPORTED,
            "robot has not advertised this versioned peripheral capability",
            device,
        )
    evidence = device.node_control
    if (
        evidence is None
        or evidence.connection_epoch != device.connection_epoch
        or not 0 <= snapshot.now_ms - evidence.t_ms <= 5000
        or evidence.watchdog_state != "nominal"
    ):
        return refuse(
            RefusalReason.CONTROL_AUTHORITY,
            "current-epoch node status and a nominal control lease are required",
            device,
        )
    # Wheel/LiDAR readiness does not authorize or prohibit stationary LEDs, speech or screen.
    # Neck physically moves the head: stop is a hard limit.
    # The device also checks its local awake gate.
    if args["kind"] == "neck" and snapshot.estop_active:
        return refuse(RefusalReason.ESTOP_ACTIVE, "network stop blocks neck movement", device)
    if args["kind"] == "neck" and not evidence.control_authority:
        return refuse(
            RefusalReason.CONTROL_AUTHORITY, "node authority blocks neck movement", device
        )
    return None


def peripheral_plan_refusal(plan: Plan, snapshot: FleetSnapshot) -> Refusal | None:
    def invalid(detail):
        return Refusal(
            plan.intent_id, snapshot.roster_version, None, None, RefusalReason.INVALID_PLAN, detail
        )

    if (
        plan.intent_name is not IntentName.ROBOT_PERIPHERAL
        or len(plan.commands) != 1
        or len(plan.selection) != 1
    ):
        return invalid(
            "peripheral plan requires exactly one typed command and explicit robot target"
        )
    if any(
        getattr(plan, key) is not None
        for key in (
            "selection_update",
            "armed_update",
            "estop_update",
            "formation_update",
            "spacing_update",
            "hold_scope",
            "altitude_grounding",
        )
    ):
        return invalid("peripheral plan cannot change fleet control state")
    command = plan.commands[0]
    if (
        command.operation is not CommandOperation.ROBOT_PERIPHERAL
        or command.safety_action
        or command.intent_id != plan.intent_id
        or command.drone_id != plan.selection[0]
        or command.roster_version != plan.roster_version
    ):
        return invalid("peripheral command differs from its approved plan or attempts motion")
    if plan.roster_version != snapshot.roster_version:
        return Refusal(
            plan.intent_id,
            snapshot.roster_version,
            None,
            None,
            RefusalReason.STALE_ROSTER,
            "peripheral roster changed",
        )
    refusal = peripheral_refusal(
        plan.intent_id, plan.selection, command.parameters, plan.confirmed, snapshot
    )
    if refusal is not None:
        return refusal
    device = snapshot.aircraft[command.drone_id]
    if command.connection_epoch != device.connection_epoch:
        return Refusal(
            plan.intent_id,
            snapshot.roster_version,
            command.drone_id,
            device.connection_epoch,
            RefusalReason.STALE_CONNECTION_EPOCH,
            "peripheral connection epoch changed",
        )
    return None

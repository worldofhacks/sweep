"""Confirmed camera requests with current aircraft authority and SDK evidence."""

from hashlib import sha256
from math import isfinite

from planner.models import (
    CommandOperation,
    DeviceClass,
    FleetSnapshot,
    MembershipState,
    Plan,
    Refusal,
    RefusalReason,
)
from relay.camera_control_args import CAMERA_CONTROL_CAPABILITY, camera_control_arguments
from relay.capabilities import IntentName

CAMERA_OPERATIONS = {
    "ready": CommandOperation.CAMERA_READY,
    "photo": CommandOperation.CAPTURE_PHOTO,
    "gimbal": CommandOperation.SET_GIMBAL_PITCH,
}


def camera_capture_id(intent_id: str) -> str:
    return "camera-" + sha256(intent_id.encode("utf-8")).hexdigest()[:32]


def camera_command(intent_id: str, raw: object) -> tuple[CommandOperation, dict[str, int | str]]:
    args = camera_control_arguments(raw)
    if args["kind"] == "photo":
        parameters = {"capture_id": camera_capture_id(intent_id)}
    elif args["kind"] == "gimbal":
        parameters = {"pitch_mdeg": args["pitch_mdeg"]}
    else:
        parameters = {}
    return CAMERA_OPERATIONS[args["kind"]], parameters


def camera_refusal(intent_id, selection, raw, confirmed, snapshot: FleetSnapshot) -> Refusal | None:
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
            RefusalReason.CONFIRMATION_REQUIRED, "camera control requires exact confirmation"
        )
    try:
        args = camera_control_arguments(raw)
    except (TypeError, ValueError):
        return refuse(
            RefusalReason.INVALID_PLAN, "camera control arguments are outside the exact contract"
        )
    if len(selection) != 1 or type(selection[0]) is not int or selection[0] <= 0:
        return refuse(RefusalReason.INVALID_SELECTION, "camera control requires one aircraft")
    device = snapshot.aircraft.get(selection[0])
    if device is None or device.membership not in {
        MembershipState.REGISTERED,
        MembershipState.READY,
        MembershipState.DEGRADED,
    }:
        return refuse(
            RefusalReason.AIRCRAFT_NOT_REGISTERED, "camera target is not connected", device
        )
    if device.device_class is not DeviceClass.AIRCRAFT:
        return refuse(
            RefusalReason.UNSUPPORTED_FOR_DEVICE_CLASS,
            "camera control requires an aircraft",
            device,
        )
    if CAMERA_CONTROL_CAPABILITY not in device.capabilities:
        return refuse(
            RefusalReason.CAMERA_UNSUPPORTED,
            "aircraft has not advertised camera_control_v1",
            device,
        )
    if snapshot.estop_active:
        return refuse(RefusalReason.ESTOP_ACTIVE, "network stop blocks camera control", device)
    if not device.control_authority:
        return refuse(
            RefusalReason.CONTROL_AUTHORITY, "aircraft control authority is not granted", device
        )
    if not device.rc_safety_operator_present:
        return refuse(
            RefusalReason.RC_SAFETY_OPERATOR_ABSENT, "an RC safety operator is required", device
        )
    evidence = device.camera_control
    if evidence is None or evidence.connection_epoch != device.connection_epoch:
        return refuse(
            RefusalReason.CAMERA_NOT_READY, "current-epoch camera status is missing", device
        )
    if not 0 <= snapshot.now_ms - evidence.t_ms <= 5000:
        return refuse(
            RefusalReason.CAMERA_NOT_READY, "camera status is stale or future-dated", device
        )
    if not evidence.control_authority or evidence.watchdog_state != "nominal":
        return refuse(
            RefusalReason.CONTROL_AUTHORITY,
            "node authority or control lease is unavailable",
            device,
        )
    operation = CAMERA_OPERATIONS[args["kind"]]
    if operation.value not in evidence.supported_operations:
        return refuse(
            RefusalReason.CAMERA_UNSUPPORTED,
            "SDK has not reported support for this camera operation",
            device,
        )
    if args["kind"] == "gimbal":
        low, high = evidence.pitch_min_deg, evidence.pitch_max_deg
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                for value in (low, high)
            )
            or not -180 <= low <= high <= 180
        ):
            return refuse(
                RefusalReason.CAMERA_NOT_READY,
                "measured gimbal pitch limits are unavailable",
                device,
            )
        if not low * 1000 <= args["pitch_mdeg"] <= high * 1000:
            return refuse(
                RefusalReason.CAMERA_UNSUPPORTED,
                "requested pitch exceeds measured gimbal limits",
                device,
            )
    return None


def camera_plan_refusal(plan: Plan, snapshot: FleetSnapshot) -> Refusal | None:
    def invalid(detail):
        return Refusal(
            plan.intent_id, snapshot.roster_version, None, None, RefusalReason.INVALID_PLAN, detail
        )

    if (
        plan.intent_name is not IntentName.CAMERA_CONTROL
        or len(plan.commands) != 1
        or len(plan.selection) != 1
    ):
        return invalid(
            "camera control requires one explicit aircraft and one existing camera command"
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
        return invalid("camera control cannot change fleet or motion state")
    command = plan.commands[0]
    kind = next(
        (kind for kind, operation in CAMERA_OPERATIONS.items() if operation is command.operation),
        None,
    )
    if kind is None:
        return invalid("camera control cannot contain motion, panorama or media retrieval")
    args = {"kind": kind}
    if kind == "gimbal":
        args["pitch_mdeg"] = command.parameters.get("pitch_mdeg")
    try:
        operation, parameters = camera_command(plan.intent_id, args)
    except (TypeError, ValueError):
        return invalid("camera command arguments are malformed")
    if (
        command.operation is not operation
        or dict(command.parameters) != parameters
        or command.safety_action
        or command.intent_id != plan.intent_id
        or command.drone_id != plan.selection[0]
        or command.roster_version != plan.roster_version
    ):
        return invalid("camera command differs from the approved plan identity or exact operation")
    if plan.roster_version != snapshot.roster_version:
        return Refusal(
            plan.intent_id,
            snapshot.roster_version,
            None,
            None,
            RefusalReason.STALE_ROSTER,
            "camera roster changed",
        )
    refusal = camera_refusal(plan.intent_id, plan.selection, args, plan.confirmed, snapshot)
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
            "camera connection epoch changed",
        )
    return None

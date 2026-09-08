"""The node side of the Sweep relay wire: canonical JSON, HMAC signing, builders, parsers.

Every function here is pure, so a device integration can be tested without a socket.
The module deliberately imports nothing from ``relay`` (or anything else in this
repository): the kit is copied onto the robot on its own and runs there against a stock
Python with ``websockets`` and nothing else.

That independence is a duplication risk, so it is checked rather than trusted.
``nodekit/vectors.py`` renders ``nodekit/tests/vectors/*.json`` **from the relay code**
and ``nodekit/tests/test_vectors.py`` asserts that these builders, this canonicalization,
and this signature agree with those vectors byte for byte, the same way the Android
bridge's Kotlin encoders are held to ``adapters/dji_mini3/vectors.py``.

Python 3.9 is the floor: no ``StrEnum``, no ``match``, no ``slots=True`` dataclasses.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1

AIRCRAFT = "aircraft"
GROUND_VEHICLE = "ground_vehicle"
DEVICE_CLASSES = frozenset({AIRCRAFT, GROUND_VEHICLE})

# A join declares its class inside the existing capability list, so no frame gains a key.
DEVICE_CLASS_CAPABILITY_PREFIX = "class:"

# The capability each class must advertise before the relay will call it ready.
REQUIRED_CLASS_CAPABILITY = {AIRCRAFT: "flight", GROUND_VEHICLE: "ground_drive"}

# Exact per-operation ``args`` (``relay.contracts.COMMAND_ARGUMENT_FIELDS``): "integer",
# "positive" (a positive integer), or "id" (a non-empty string). Millimetres and
# millidegrees keep the signed canonical JSON free of floats.
COMMAND_ARGUMENT_FIELDS = {
    "takeoff": {"z_mm": "integer"},
    "goto": {"x_mm": "integer", "y_mm": "integer", "z_mm": "integer", "speed_mm_s": "positive"},
    "body_pulse": {"forward_mm_s": "integer", "duration_ms": "integer"},
    "robot_peripheral": {},
    "rotate_to": {"yaw_mdeg": "integer", "speed_mdeg_s": "positive"},
    "hover": {},
    "land": {},
    "estop": {},
    "camera_capabilities": {},
    "set_gimbal_pitch": {"pitch_mdeg": "integer"},
    "camera_ready": {},
    "capture_panorama": {"capture_id": "id"},
    "capture_photo": {"capture_id": "id"},
    "retrieve_media": {"file_id": "id"},
}
COMMAND_OPERATIONS = frozenset(COMMAND_ARGUMENT_FIELDS)

# The operations a lost control authority refuses; hover, land, and estop are the ways a
# device is asked to stop and are never withheld.
MOTION_OPERATIONS = frozenset({"takeoff", "goto", "body_pulse", "rotate_to"})

# ``relay.contracts.NodeAcknowledgementReason``: the machine-readable reasons a node
# reports when it does not run a command.
STALE_COMMAND = "stale_command"
OUT_OF_ORDER_COMMAND = "out_of_order_command"
AUTHORITY_LOST = "authority_lost"
WATCHDOG_HOLD = "watchdog_hold"
WATCHDOG_FAILSAFE = "watchdog_failsafe"
UNSUPPORTED_OPERATION = "unsupported_operation"
NODE_ACKNOWLEDGEMENT_REASONS = frozenset(
    {
        STALE_COMMAND,
        OUT_OF_ORDER_COMMAND,
        AUTHORITY_LOST,
        WATCHDOG_HOLD,
        WATCHDOG_FAILSAFE,
        UNSUPPORTED_OPERATION,
    }
)

# Lifecycle statuses a node may report on a command it admitted.
ACCEPTED = "accepted"
EXECUTING = "executing"
COMPLETED = "completed"
FAILED = "failed"
INVALIDATED = "invalidated"

# node_status.watchdog_state
NOMINAL = "nominal"
HOLD = "hold"
FAILSAFE = "failsafe"

# node_status.video_publish_state
VIDEO_PUBLISH_STATES = frozenset({"stopped", "connecting", "publishing", "failed"})

LIDAR_SCAN = "lidar_scan"
SENSOR_KINDS = frozenset({LIDAR_SCAN})
SENSOR_ANGLE_INCREMENTS_DEG = (0.5, 1.0, 2.0)
MAX_SENSOR_RANGES = 720
MAX_SENSOR_RANGE_CM = 65_535
MAX_SENSOR_FRAME_CANONICAL_BYTES = 8 * 1024

MAX_CAPABILITY_LIST_ITEMS = 64
MAX_CAPABILITY_ITEM_UTF8_BYTES = 512


class ProtocolError(ValueError):
    """A frame the node refuses to send or to admit, with the relay's own reason code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def canonical_event_bytes(event: Mapping[str, Any]) -> bytes:
    """Encode an event the one way both sides sign (``relay.auth.canonical_event_bytes``).

    Signed frames carry integers, booleans, strings, and string lists only, which keeps
    the encoding free of cross-language floating-point canonicalization.
    """
    try:
        serialized = json.dumps(
            event,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ProtocolError("invalid_signature_payload", str(error)) from None
    return serialized.encode("utf-8")


def sign_event(unsigned_event: Mapping[str, Any], key: bytes | str) -> str:
    """HMAC-SHA256 over the canonical bytes, lowercase hex (``relay.auth.sign_event``)."""
    secret = key.encode() if isinstance(key, str) else key
    return hmac.new(secret, canonical_event_bytes(unsigned_event), hashlib.sha256).hexdigest()


def verify_event_signature(
    unsigned_event: Mapping[str, Any], signature: object, key: bytes | str
) -> bool:
    if not isinstance(signature, str) or len(signature) != 64 or signature != signature.lower():
        return False
    if any(character not in "0123456789abcdef" for character in signature):
        return False
    return hmac.compare_digest(signature, sign_event(unsigned_event, key))


def class_capability(device_class: str) -> str:
    if device_class not in DEVICE_CLASSES:
        raise ProtocolError(
            "device_class_mismatch",
            "device_class must be one of " + ", ".join(sorted(DEVICE_CLASSES)),
        )
    return DEVICE_CLASS_CAPABILITY_PREFIX + device_class


def join_capabilities(device_class: str, capabilities: Sequence[str]) -> list[str]:
    """The join's capability list: the device's own claims plus its one ``class:`` entry.

    A device never declares its own class entry; a second one refuses the join with
    ``device_class_mismatch``, so the kit adds exactly one and refuses to add a second.
    """
    declared = [str(capability) for capability in capabilities]
    if any(item.startswith(DEVICE_CLASS_CAPABILITY_PREFIX) for item in declared):
        raise ProtocolError(
            "device_class_mismatch",
            "a device declares its class through Device.device_class, not a capability string",
        )
    result = declared + [class_capability(device_class)]
    if len(result) > MAX_CAPABILITY_LIST_ITEMS:
        raise ProtocolError(
            "invalid_membership",
            f"capabilities may hold at most {MAX_CAPABILITY_LIST_ITEMS} items",
        )
    if len(set(result)) != len(result):
        raise ProtocolError("invalid_membership", "capabilities may not contain duplicates")
    for item in result:
        if not item or item != item.strip() or not item.isprintable():
            raise ProtocolError(
                "invalid_membership", "capabilities items must be canonical printable strings"
            )
        if len(item.encode("utf-8")) > MAX_CAPABILITY_ITEM_UTF8_BYTES:
            raise ProtocolError("invalid_membership", "a capability item is too long")
    return result


@dataclass(frozen=True)
class Command:
    """A relay-authored command as it arrived, already shape-checked."""

    v: int
    t: int
    type: str
    event_id: str
    session: str
    command_id: str
    intent_id: str
    roster_version: int
    drone_id: int
    connection_epoch: int
    seq: int
    issued_at: int
    ttl_ms: int
    operation: str
    args: Mapping[str, Any]
    signature: str

    def unsigned_event(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "command_id": self.command_id,
            "intent_id": self.intent_id,
            "roster_version": self.roster_version,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "seq": self.seq,
            "issued_at": self.issued_at,
            "ttl_ms": self.ttl_ms,
            "operation": self.operation,
            "args": dict(self.args),
        }

    def verifies(self, key: bytes | str) -> bool:
        return verify_event_signature(self.unsigned_event(), self.signature, key)


@dataclass(frozen=True)
class ControlHeartbeat:
    """The relay's per-connection control lease: the node's only liveness evidence."""

    v: int
    t: int
    type: str
    event_id: str
    session: str
    source: str
    drone_id: int
    connection_epoch: int
    roster_version: int
    seq: int
    signature: str

    def unsigned_event(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "source": self.source,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "roster_version": self.roster_version,
            "seq": self.seq,
        }

    def verifies(self, key: bytes | str) -> bool:
        return verify_event_signature(self.unsigned_event(), self.signature, key)


_ENVELOPE_FIELDS = frozenset({"v", "t", "type", "event_id", "session"})


def parse_command(raw: object) -> Command:
    code = "invalid_command"
    value = _mapping(raw, code, "command frame must be an object")
    fields = set(_ENVELOPE_FIELDS) | {
        "command_id",
        "intent_id",
        "roster_version",
        "drone_id",
        "connection_epoch",
        "seq",
        "issued_at",
        "ttl_ms",
        "operation",
        "args",
        "signature",
    }
    _exact_fields(value, fields, code)
    _check_envelope(value, "command", code)
    operation = value["operation"]
    if operation not in COMMAND_OPERATIONS:
        raise ProtocolError(code, "unknown command operation")
    return Command(
        PROTOCOL_VERSION,
        value["t"],
        "command",
        value["event_id"],
        value["session"],
        _text(value["command_id"], "command_id", code),
        _text(value["intent_id"], "intent_id", code),
        _nonnegative_int(value["roster_version"], "roster_version", code),
        _positive_int(value["drone_id"], "drone_id", code),
        _positive_int(value["connection_epoch"], "connection_epoch", code),
        _positive_int(value["seq"], "seq", code),
        _nonnegative_int(value["issued_at"], "issued_at", code),
        _positive_int(value["ttl_ms"], "ttl_ms", code),
        str(operation),
        command_arguments(str(operation), value["args"]),
        _text(value["signature"], "signature", "invalid_signature"),
    )


def command_arguments(operation: str, raw: object) -> dict[str, Any]:
    """Validate one command's ``args`` exactly the way the relay built them."""
    code = "invalid_command"
    if operation == "robot_peripheral":
        from .peripherals import peripheral_arguments
        try:
            return peripheral_arguments(raw)
        except (ValueError, TypeError) as error:
            raise ProtocolError(code, str(error)) from None
    spec = COMMAND_ARGUMENT_FIELDS.get(operation)
    if spec is None:
        raise ProtocolError(code, "unknown command operation")
    value = _mapping(raw, code, "command args must be an object")
    if set(value) != set(spec):
        raise ProtocolError(code, operation + " arguments do not match the v1 contract")
    result = {}
    for field, kind in spec.items():
        if kind == "id":
            result[field] = _text(value[field], field, code)
        elif kind == "positive":
            result[field] = _positive_int(value[field], field, code)
        else:
            result[field] = _integer(value[field], field, code)
    if operation == "body_pulse" and not (
        0 < abs(result["forward_mm_s"]) <= 250 and 100 <= result["duration_ms"] <= 500
    ):
        raise ProtocolError(code, "body_pulse arguments exceed the bounded integer contract")
    return result


def parse_control_heartbeat(raw: object) -> ControlHeartbeat:
    code = "invalid_control_heartbeat"
    value = _mapping(raw, code, "control_heartbeat must be an object")
    fields = set(_ENVELOPE_FIELDS) | {
        "source",
        "drone_id",
        "connection_epoch",
        "roster_version",
        "seq",
        "signature",
    }
    _exact_fields(value, fields, code)
    _check_envelope(value, "control_heartbeat", code)
    if value["source"] != "relay":
        raise ProtocolError(code, "control_heartbeat source must be relay")
    return ControlHeartbeat(
        PROTOCOL_VERSION,
        value["t"],
        "control_heartbeat",
        value["event_id"],
        value["session"],
        "relay",
        _positive_int(value["drone_id"], "drone_id", code),
        _positive_int(value["connection_epoch"], "connection_epoch", code),
        _nonnegative_int(value["roster_version"], "roster_version", code),
        _positive_int(value["seq"], "seq", code),
        _text(value["signature"], "signature", "invalid_signature"),
    )


def envelope(frame_type: str, *, t: int, event_id: str, session: str) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "t": int(t),
        "type": frame_type,
        "event_id": str(event_id),
        "session": str(session),
    }


def auth_frame(*, device_id: int, token: str) -> dict[str, Any]:
    """The unsigned first frame; the relay answers ``auth.accepted`` or ``auth.refused``."""
    return {
        "v": PROTOCOL_VERSION,
        "type": "auth",
        "source": "adapter",
        "drone_id": int(device_id),
        "token": token,
    }


def membership_frame(
    action: str,
    *,
    t: int,
    event_id: str,
    session: str,
    device_id: int,
    key: bytes | str,
    **claims: Any,
) -> dict[str, Any]:
    """Build and sign one membership claim (``join``, ``readiness``, ``graceful_leave``)."""
    if action not in {"join", "readiness", "graceful_leave"}:
        raise ProtocolError("invalid_membership", "unknown membership action")
    frame = envelope("membership", t=t, event_id=event_id, session=session)
    frame["drone_id"] = int(device_id)
    frame["action"] = action
    frame.update(claims)
    frame["signature"] = sign_event(frame, key)
    return frame


def join_frame(
    *,
    t: int,
    event_id: str,
    session: str,
    device_id: int,
    adapter_id: str,
    device_class: str,
    capabilities: Sequence[str],
    key: bytes | str,
) -> dict[str, Any]:
    return membership_frame(
        "join",
        t=t,
        event_id=event_id,
        session=session,
        device_id=device_id,
        key=key,
        adapter_id=adapter_id,
        capabilities=join_capabilities(device_class, capabilities),
    )


def readiness_frame(
    *,
    t: int,
    event_id: str,
    session: str,
    device_id: int,
    connection_epoch: int,
    home_pose_confirmed: bool,
    control_authority: bool,
    rc_safety_operator_present: bool,
    key: bytes | str,
) -> dict[str, Any]:
    return membership_frame(
        "readiness",
        t=t,
        event_id=event_id,
        session=session,
        device_id=device_id,
        key=key,
        connection_epoch=int(connection_epoch),
        home_pose_confirmed=bool(home_pose_confirmed),
        control_authority=bool(control_authority),
        rc_safety_operator_present=bool(rc_safety_operator_present),
    )


def graceful_leave_frame(
    *,
    t: int,
    event_id: str,
    session: str,
    device_id: int,
    connection_epoch: int,
    key: bytes | str,
) -> dict[str, Any]:
    return membership_frame(
        "graceful_leave",
        t=t,
        event_id=event_id,
        session=session,
        device_id=device_id,
        key=key,
        connection_epoch=int(connection_epoch),
    )


def telemetry_frame(
    *,
    t: int,
    event_id: str,
    session: str,
    device_id: int,
    connection_epoch: int,
    x: float,
    y: float,
    z: float,
    vx: float,
    vy: float,
    vz: float,
    battery: float,
    state: str,
    link: float,
    pos_quality: float,
) -> dict[str, Any]:
    """Telemetry v1, unsigned; its device key is ``drone`` and a ground vehicle sends z = 0."""
    frame = envelope("telemetry", t=t, event_id=event_id, session=session)
    frame["drone"] = int(device_id)
    frame["connection_epoch"] = int(connection_epoch)
    frame["x"] = float(x)
    frame["y"] = float(y)
    frame["z"] = float(z)
    frame["vx"] = float(vx)
    frame["vy"] = float(vy)
    frame["vz"] = float(vz)
    frame["battery"] = _unit_interval(battery, "battery")
    frame["state"] = str(state)
    frame["link"] = _unit_interval(link, "link")
    frame["pos_quality"] = _unit_interval(pos_quality, "pos_quality")
    return frame


def sensor_frame(
    *,
    t: int,
    event_id: str,
    session: str,
    device_id: int,
    connection_epoch: int,
    kind: str,
    pose: Sequence[float],
    angle_min_deg: float,
    angle_increment_deg: float,
    range_min_m: float,
    range_max_m: float,
    ranges_cm: Sequence[int],
) -> dict[str, Any]:
    """Build a scan frame, refusing locally anything the relay would refuse.

    A malformed sensor frame is refused *and* claims its transport event, so the bounds
    are checked here rather than discovered on the wire.
    """
    code = "invalid_sensor"
    if kind not in SENSOR_KINDS:
        raise ProtocolError(code, "kind must be one of " + ", ".join(sorted(SENSOR_KINDS)))
    if float(angle_increment_deg) not in SENSOR_ANGLE_INCREMENTS_DEG:
        raise ProtocolError(code, "angle_increment_deg must be 0.5, 1.0, or 2.0")
    expected = int(round(360 / float(angle_increment_deg)))
    ranges = [int(value) for value in ranges_cm]
    if len(ranges) != expected or len(ranges) > MAX_SENSOR_RANGES:
        raise ProtocolError(code, f"ranges_cm must hold exactly {expected} entries")
    if any(value < 0 or value > MAX_SENSOR_RANGE_CM for value in ranges):
        raise ProtocolError(code, "ranges_cm entries must be 0 through 65535")
    if float(range_min_m) < 0 or float(range_max_m) <= float(range_min_m):
        raise ProtocolError(code, "range_min_m must be non-negative and below range_max_m")
    if len(pose) != 3:
        raise ProtocolError(code, "pose must be (x, y, yaw_deg)")
    frame = envelope("sensor", t=t, event_id=event_id, session=session)
    frame["drone_id"] = int(device_id)
    frame["connection_epoch"] = int(connection_epoch)
    frame["kind"] = kind
    frame["pose"] = {
        "x": float(pose[0]),
        "y": float(pose[1]),
        "yaw_deg": _azimuth(pose[2], "yaw_deg"),
    }
    frame["angle_min_deg"] = _azimuth(angle_min_deg, "angle_min_deg")
    frame["angle_increment_deg"] = float(angle_increment_deg)
    frame["range_min_m"] = float(range_min_m)
    frame["range_max_m"] = float(range_max_m)
    frame["ranges_cm"] = ranges
    if len(canonical_event_bytes(frame)) > MAX_SENSOR_FRAME_CANONICAL_BYTES:
        raise ProtocolError(
            code,
            "sensor frame canonical JSON may hold at most "
            f"{MAX_SENSOR_FRAME_CANONICAL_BYTES} bytes",
        )
    return frame


CAPABILITIES_PROFILE_FIELDS = (
    "native_panorama_modes",
    "photo_capture",
    "gimbal_pitch_min_deg",
    "gimbal_pitch_max_deg",
    "horizontal_fov_deg",
    "storage_remaining_bytes",
    "media_retrieval",
    "aircraft_model",
    "aircraft_firmware",
    "rc_firmware",
    "phone_model",
    "android_version",
    "sdk_version",
    "measured_hfov_deg",
)

# The capabilities frame is aircraft-shaped and every field is required, so a device with
# no camera and no gimbal still answers: the narrowest ordered pitch range the contract
# admits and its own model strings. ``Device.hardware_profile`` overrides any of them.
DEFAULT_HARDWARE_PROFILE = {
    "native_panorama_modes": [],
    "photo_capture": False,
    "gimbal_pitch_min_deg": -1.0,
    "gimbal_pitch_max_deg": 1.0,
    "horizontal_fov_deg": 60.0,
    "storage_remaining_bytes": 0,
    "media_retrieval": False,
    "aircraft_model": "unreported",
    "aircraft_firmware": "unreported",
    "rc_firmware": "unreported",
    "phone_model": "unreported",
    "android_version": "unreported",
    "sdk_version": "unreported",
    "measured_hfov_deg": None,
}


def capabilities_frame(
    *,
    t: int,
    event_id: str,
    session: str,
    device_id: int,
    connection_epoch: int,
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    unknown = sorted(set(profile) - set(CAPABILITIES_PROFILE_FIELDS))
    if unknown:
        raise ProtocolError(
            "invalid_capabilities",
            "hardware_profile has fields the capabilities frame has no room for: "
            + ", ".join(unknown),
        )
    frame = envelope("capabilities", t=t, event_id=event_id, session=session)
    frame["drone_id"] = int(device_id)
    frame["connection_epoch"] = int(connection_epoch)
    for field in CAPABILITIES_PROFILE_FIELDS:
        frame[field] = profile.get(field, DEFAULT_HARDWARE_PROFILE[field])
    return frame


def node_status_frame(
    *,
    t: int,
    event_id: str,
    session: str,
    device_id: int,
    connection_epoch: int,
    virtual_stick_enabled: bool,
    control_authority: bool,
    authority_change_reason: str | None,
    watchdog_state: str,
    video_publish_state: str,
    phone_battery_percent: int,
    phone_thermal_state: str = "none",
    device_telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if watchdog_state not in {NOMINAL, HOLD, FAILSAFE}:
        raise ProtocolError("invalid_node_status", "unknown watchdog state")
    if video_publish_state not in VIDEO_PUBLISH_STATES:
        raise ProtocolError("invalid_node_status", "unknown video publish state")
    frame = envelope("node_status", t=t, event_id=event_id, session=session)
    frame["drone_id"] = int(device_id)
    frame["connection_epoch"] = int(connection_epoch)
    frame["virtual_stick_enabled"] = bool(virtual_stick_enabled)
    frame["control_authority"] = bool(control_authority)
    frame["authority_change_reason"] = authority_change_reason
    frame["watchdog_state"] = watchdog_state
    frame["video_publish_state"] = video_publish_state
    frame["phone_battery_percent"] = max(0, min(100, int(phone_battery_percent)))
    frame["phone_thermal_state"] = phone_thermal_state
    if device_telemetry is not None:
        from .telemetry import device_telemetry_payload

        frame["device_telemetry"] = device_telemetry_payload(device_telemetry)
    return frame


def acknowledgement_frame(
    *,
    t: int,
    event_id: str,
    session: str,
    device_id: int,
    command: Command,
    status: str,
    reason: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    if status not in {ACCEPTED, EXECUTING, COMPLETED, FAILED, INVALIDATED}:
        raise ProtocolError("invalid_acknowledgement", "unknown lifecycle status")
    if status in {FAILED, INVALIDATED} and not reason:
        raise ProtocolError("invalid_acknowledgement", "terminal failure requires a reason")
    frame = envelope("acknowledgement", t=t, event_id=event_id, session=session)
    frame["intent_id"] = command.intent_id
    frame["command_id"] = command.command_id
    frame["status"] = status
    frame["drone_id"] = int(device_id)
    frame["connection_epoch"] = command.connection_epoch
    frame["roster_version"] = command.roster_version
    frame["reason"] = reason
    frame["detail"] = detail
    return frame


def _mapping(raw: object, code: str, detail: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise ProtocolError(code, detail)
    return raw


def _exact_fields(value: Mapping[str, Any], fields: set, code: str) -> None:
    if set(value) != fields:
        raise ProtocolError(code, "frame fields do not match the v1 contract")


def _check_envelope(value: Mapping[str, Any], expected_type: str, code: str) -> None:
    if value["v"] != PROTOCOL_VERSION or isinstance(value["v"], bool):
        raise ProtocolError(code, "v must be integer 1")
    _nonnegative_int(value["t"], "t", code)
    if value["type"] != expected_type:
        raise ProtocolError(code, "type must be " + expected_type)
    _text(value["event_id"], "event_id", code)
    _text(value["session"], "session", code)


def _text(value: object, field: str, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ProtocolError(code, field + " must be a non-empty string of at most 512 chars")
    return value


def _integer(value: object, field: str, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(code, field + " must be an integer")
    return value


def _nonnegative_int(value: object, field: str, code: str) -> int:
    result = _integer(value, field, code)
    if result < 0:
        raise ProtocolError(code, field + " must be a non-negative integer")
    return result


def _positive_int(value: object, field: str, code: str) -> int:
    result = _integer(value, field, code)
    if result <= 0:
        raise ProtocolError(code, field + " must be a positive integer")
    return result


def _unit_interval(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError("invalid_telemetry", field + " must be a number")
    return max(0.0, min(1.0, float(value)))


def _azimuth(value: object, field: str) -> float:
    """Normalize a heading into the relay's [0, 360) azimuth window."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError("invalid_sensor", field + " must be a number")
    return float(value) % 360.0

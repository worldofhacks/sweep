"""Cross-language wire vectors for the Android bridge's ``bridge-core`` JVM tests.

The Kotlin encoders must byte-match ``json.dumps(sort_keys=True, separators=(",", ":"),
ensure_ascii=False, allow_nan=False)`` and ``relay.auth.sign_event``.  Rather than trust a
port, this module produces the vectors from the relay code itself and writes them under
``pilot-app/bridge-core/src/test/resources/vectors/``.  ``test_vectors.py`` fails whenever
the committed files drift from what this module renders.

The command, capabilities, capture_readiness, node_status, auth.accepted, membership event,
state, and refusal shapes follow the node protocol in ``relay/README.md`` (integer-only
command arguments, flat capabilities with a hardware profile, ``phone_battery_percent``,
``nominal`` as the quiet watchdog state).

Run ``uv run python -m adapters.dji_mini3.vectors`` from the repository root to refresh.
"""

from __future__ import annotations

import json
from pathlib import Path

from relay.auth import canonical_event_bytes, sign_event, verify_event_signature
from relay.contracts import MembershipAction, MembershipRequest, TelemetryV1

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "pilot-app"
    / "bridge-core"
    / "src"
    / "test"
    / "resources"
    / "vectors"
)

NODE_KEY = "adapter-key-0123456789abcdef0123456789abcdef"
SHORT_KEY = "k"
SESSION = "session-a"

JsonValue = object


def canonical(value: object) -> str:
    return canonical_event_bytes(value).decode("utf-8")


def _case(name: str, value: object) -> dict[str, object]:
    return {"name": name, "value": value, "canonical": canonical(value)}


def canonical_json_cases() -> list[dict[str, object]]:
    astral = chr(0x1D51E)  # surrogate pair in UTF-16, sorts after U+FF5A by code point
    return [
        _case("empty_object", {}),
        _case("empty_array", []),
        _case("nested_unsorted_keys", {"b": 1, "a": {"d": 2, "c": [3, 4]}}),
        _case("integers", [0, -1, 42, 9223372036854775807, -9223372036854775808]),
        _case("simple_floats", [1.0, -2.5, 0.1, 100.0, 0.5]),
        _case(
            "float_notation_boundaries",
            [1e15, 1e16, 1.5e16, 1e22, 0.0001, 0.00001, 1.25e-7, 2.5e-5, -1e-7],
        ),
        _case("float_extremes", [5e-324, 1.7976931348623157e308, 2.2250738585072014e-308]),
        _case("float_round_trip", [0.30000000000000004, 123456789.123, 9007199254740993.0, 1 / 3]),
        _case("signed_zero", [0.0, -0.0]),
        _case("unicode_text", "héllo wörld 🚀 日本語 ñ"),
        _case("escapes", 'quote" backslash\\ newline\n tab\t cr\r bs\b ff\f slash/'),
        _case("control_characters", "\x01\x1f\x7f"),
        _case("line_separators_not_escaped", "  "),
        _case("literals", [True, False, None]),
        _case(
            "code_point_key_order",
            {"z": 1, "a": 2, "B": 3, "_": 4, "ä": 5, "ｚ": 6, astral: 7, "": 8, "10": 9, "9": 10},
        ),
        _case(
            "telemetry_like",
            {
                "v": 1,
                "t": 2000,
                "type": "telemetry",
                "drone": 1,
                "x": 1.5,
                "y": -0.25,
                "z": 1.0,
                "battery": 0.87,
                "state": "hovering",
            },
        ),
        _case("deep_nesting", [[[[{"a": [[[]]]}]]]]),
        _case("mixed_array", [1, 1.0, "1", True, None, {"k": [1.5, -2]}]),
        _case("unicode_keys", {"ключ": "значение", "键": "值"}),
        _case("float_ints_keep_point", {"count": 3, "ratio": 3.0}),
        _case("negative_exponent_padding", [1e-100, 1e-10, 1.5e-300]),
        _case("positive_exponent_padding", [1e100, 1e300, 12345678901234567890.0]),
        _case("long_string", "x" * 512),
    ]


def hmac_cases() -> list[dict[str, object]]:
    join = MembershipRequest(
        1,
        1000,
        "membership",
        "evt-join-1",
        SESSION,
        1,
        MembershipAction.JOIN,
        "",
        adapter_id="dji_mini3",
        capabilities=("flight", "camera"),
    ).unsigned_event()
    readiness = MembershipRequest(
        1,
        1001,
        "membership",
        "evt-ready-1",
        SESSION,
        1,
        MembershipAction.READINESS,
        "",
        connection_epoch=1,
        home_pose_confirmed=True,
        control_authority=True,
        rc_safety_operator_present=True,
    ).unsigned_event()
    unicode_event = {"v": 1, "type": "note", "text": "ünïcödé 🚀", "n": [1, 2.5, None, False]}
    cases = []
    for name, key, event in (
        ("membership_join", NODE_KEY, join),
        ("membership_readiness", NODE_KEY, readiness),
        ("unicode_event_short_key", SHORT_KEY, unicode_event),
        ("command", NODE_KEY, command_unsigned()),
    ):
        signature = sign_event(event, key)
        assert verify_event_signature(event, signature, key.encode())
        cases.append(
            {
                "name": name,
                "key": key,
                "unsigned_event": event,
                "canonical": canonical(event),
                "signature": signature,
            }
        )
    return cases


def command_args() -> dict[str, dict[str, object]]:
    """Exact per-operation ``args`` from ``relay.contracts.COMMAND_ARGUMENT_FIELDS``."""
    return {
        "takeoff": {"z_mm": 1200},
        "goto": {"x_mm": 1000, "y_mm": 2500, "z_mm": 1200, "speed_mm_s": 500},
        "body_pulse": {"forward_mm_s": 250, "duration_ms": 500},
        "rotate_to": {"yaw_mdeg": 90000, "speed_mdeg_s": 30000},
        "hover": {},
        "land": {},
        "estop": {},
        "camera_capabilities": {},
        "set_gimbal_pitch": {"pitch_mdeg": -45000},
        "camera_ready": {},
        "capture_panorama": {"capture_id": "cap-0042"},
        "capture_photo": {"capture_id": "cap-0042"},
        "retrieve_media": {"file_id": "file-7"},
    }


def command_unsigned() -> dict[str, object]:
    return {
        "v": 1,
        "t": 4000,
        "type": "command",
        "event_id": "evt-cmd-1",
        "session": SESSION,
        "command_id": "cmd-1",
        "intent_id": "intent-1",
        "roster_version": 3,
        "drone_id": 1,
        "connection_epoch": 1,
        "seq": 7,
        "issued_at": 4000,
        "ttl_ms": 1500,
        "operation": "goto",
        "args": command_args()["goto"],
    }


def _signed(unsigned: dict[str, object], key: str) -> dict[str, object]:
    return {**unsigned, "signature": sign_event(unsigned, key)}


def node_settings() -> dict[str, int]:
    """The relay's default ``RelaySettings.node_settings()`` values."""
    return {
        "command_ttl_ms": 2000,
        "virtual_stick_hz": 10,
        "watchdog_hold_ms": 2000,
        "watchdog_failsafe_ms": 10000,
    }


def frame_vectors() -> dict[str, object]:
    join = MembershipRequest(
        1,
        1000,
        "membership",
        "evt-join-1",
        SESSION,
        1,
        MembershipAction.JOIN,
        "",
        adapter_id="dji_mini3",
        capabilities=("flight", "camera"),
    )
    readiness = MembershipRequest(
        1,
        1001,
        "membership",
        "evt-ready-1",
        SESSION,
        1,
        MembershipAction.READINESS,
        "",
        connection_epoch=1,
        home_pose_confirmed=True,
        control_authority=True,
        rc_safety_operator_present=True,
    )
    leave = MembershipRequest(
        1,
        1002,
        "membership",
        "evt-leave-1",
        SESSION,
        1,
        MembershipAction.GRACEFUL_LEAVE,
        "",
        connection_epoch=1,
    )
    telemetry = TelemetryV1(
        1,
        2000,
        "telemetry",
        "evt-telemetry-1",
        SESSION,
        1,
        1,
        1.5,
        -0.25,
        1.0,
        0.1,
        0.0,
        -0.05,
        0.87,
        "hovering",
        0.95,
        0.6,
    )
    acknowledgement = {
        "v": 1,
        "t": 3000,
        "type": "acknowledgement",
        "event_id": "evt-ack-1",
        "session": SESSION,
        "intent_id": "intent-1",
        "command_id": "cmd-1",
        "status": "failed",
        "drone_id": 1,
        "connection_epoch": 1,
        "roster_version": 3,
        "reason": "stale_command",
        "detail": "issued_at plus ttl_ms elapsed",
    }
    capabilities = {
        "v": 1,
        "t": 5000,
        "type": "capabilities",
        "event_id": "evt-cap-1",
        "session": SESSION,
        "drone_id": 1,
        "connection_epoch": 1,
        "native_panorama_modes": [],
        "photo_capture": True,
        "gimbal_pitch_min_deg": -90.0,
        "gimbal_pitch_max_deg": 20.0,
        "horizontal_fov_deg": 82.1,
        "storage_remaining_bytes": 12_000_000_000,
        "media_retrieval": True,
        "aircraft_model": "DJI Mini 3",
        "aircraft_firmware": "unreported",
        "rc_firmware": "unreported",
        "phone_model": "Solana Seeker",
        "android_version": "16",
        "sdk_version": "5.18.0",
        "measured_hfov_deg": None,
    }
    capture_readiness = {
        "v": 1,
        "t": 6000,
        "type": "capture_readiness",
        "event_id": "evt-readiness-1",
        "session": SESSION,
        "drone_id": 1,
        "connection_epoch": 1,
        "room_id": "office-101",
        "capture_id": "cap-0042",
        "guidance_mode": "visual_advisory",
        "pose_source": "operator_approved",
        "pose_ok": True,
        "clearance_ok": True,
        "camera_ok": True,
        "storage_ok": True,
        "motion_ok": True,
        "image_quality_ok": False,
        "coverage_missing": [90.0, 135.0],
        "next_heading_deg": 90.0,
        "suggested_delta": {"kind": "yaw", "degrees": 12.0},
    }
    node_status = {
        "v": 1,
        "t": 7000,
        "type": "node_status",
        "event_id": "evt-status-1",
        "session": SESSION,
        "drone_id": 1,
        "connection_epoch": 1,
        "virtual_stick_enabled": False,
        "control_authority": True,
        "authority_change_reason": None,
        "watchdog_state": "nominal",
        "video_publish_state": "stopped",
        "phone_battery_percent": 72,
        "phone_thermal_state": "none",
    }
    auth_accepted = {
        "v": 1,
        "t": 900,
        "type": "auth.accepted",
        "event_id": "evt-auth-1",
        "session": SESSION,
        "source": "adapter",
        "drone_id": 1,
        "node": node_settings(),
    }
    auth_refused = {
        "v": 1,
        "t": 901,
        "type": "auth.refused",
        "event_id": "evt-auth-2",
        "session": SESSION,
        "status": "refused",
        "reason": "session_closed",
        "detail": "persisted sessions are replay-only after a relay process restart; "
        "use a new session ID",
    }
    membership_event = {
        "v": 1,
        "t": 1100,
        "type": "membership",
        "event_id": "evt-mem-1",
        "session": SESSION,
        "action": "join",
        "drone_id": 1,
        "connection_epoch": 2,
        "membership": "registered",
        "roster_version": 4,
        "reason": "authenticated_rejoin",
        "readiness_reasons": ["telemetry_missing", "home_pose_missing"],
        "adapter_id": "dji_mini3",
        "capabilities": ["flight"],
        "provenance": "adapter_signature",
    }
    state = {
        "v": 1,
        "t": 1300,
        "type": "state",
        "event_id": "evt-state-1",
        "session": SESSION,
        "roster_version": 3,
        "armed": False,
        "estop": False,
        "selection": [],
        "formation": "line",
        "spacing": 1.0,
        "mode": "indoor",
        "pending": None,
        "accepted_plan": None,
        "drones": [
            {
                "drone_id": 1,
                "connection_epoch": 1,
                "membership": "ready",
                "readiness_reasons": [],
                "flight_state": "landed",
                "battery": 0.87,
                "link": 0.95,
                "pos_quality": 0.6,
                "control_authority": True,
                "last_seen_at": 1299,
                "camera_patterns": [],
                "selectable": True,
                "adapter_id": "dji_mini3",
                "adapter_capabilities": ["flight"],
                "home_pose": {"x": 0.0, "y": 0.0, "z": 0.0},
                "rc_safety_operator_present": True,
                "telemetry": None,
                "membership_history": [],
                "camera_capabilities": None,
                "node_status": None,
            }
        ],
    }
    refusal = {
        "v": 1,
        "t": 1200,
        "type": "refusal",
        "event_id": "evt-ref-1",
        "session": SESSION,
        "intent_id": None,
        "command_id": None,
        "status": "refused",
        "source": "adapter",
        "drone_id": 1,
        "connection_epoch": 1,
        "roster_version": 3,
        "reason": "stale_timestamp",
        "detail": "transport event is outside the freshness window",
    }
    return {
        "auth": {
            "wire": {
                "v": 1,
                "type": "auth",
                "source": "adapter",
                "drone_id": 1,
                "token": "adapter-token-1",
            }
        },
        "auth_accepted": {"wire": auth_accepted},
        "auth_refused": {"wire": auth_refused},
        "membership_join": {"key": NODE_KEY, "wire": _signed(join.unsigned_event(), NODE_KEY)},
        "membership_readiness": {
            "key": NODE_KEY,
            "wire": _signed(readiness.unsigned_event(), NODE_KEY),
        },
        "membership_graceful_leave": {
            "key": NODE_KEY,
            "wire": _signed(leave.unsigned_event(), NODE_KEY),
        },
        "membership_event": {"wire": membership_event},
        "state": {"wire": state},
        "refusal": {"wire": refusal},
        "telemetry": {"wire": telemetry.to_event()},
        "acknowledgement": {"wire": acknowledgement},
        "command": {"key": NODE_KEY, "wire": _signed(command_unsigned(), NODE_KEY)},
        "command_args": command_args(),
        "capabilities": {"wire": capabilities},
        "capture_readiness": {"wire": capture_readiness},
        "node_status": {"wire": node_status},
    }


def render() -> dict[str, str]:
    documents = {
        "canonical_json.json": {"cases": canonical_json_cases()},
        "hmac_sha256.json": {"cases": hmac_cases()},
        "frames.json": frame_vectors(),
    }
    return {
        name: json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        for name, document in documents.items()
    }


def write(directory: Path = FIXTURE_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in render().items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in write():
        print(path)

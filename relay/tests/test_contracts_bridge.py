from __future__ import annotations

import pytest

from planner.models import CommandOperation
from relay.auth import sign_event, verify_event_signature
from relay.contracts import (
    ContractError,
    DeltaKind,
    GuidanceMode,
    NodeAcknowledgementReason,
    WatchdogState,
    command_event,
    parse_adapter_acknowledgement,
    parse_capabilities,
    parse_capture_bundle,
    parse_capture_readiness,
    parse_command,
    parse_media_file,
    parse_node_status,
)
from relay.tests.conftest import (
    ADAPTER_KEY,
    acknowledgement_payload,
    capabilities_payload,
    capture_bundle_payload,
    capture_readiness_payload,
    command_payload,
    media_file_payload,
    node_status_payload,
)


def test_command_signature_round_trips_through_relay_auth_canonical_json() -> None:
    raw = command_payload(event_id="command-1")

    frame = parse_command(raw)

    assert frame.operation is CommandOperation.GOTO
    assert dict(frame.args) == {"x_mm": 1_000, "y_mm": -400, "z_mm": 1_000, "speed_mm_s": 500}
    assert frame.unsigned_event() == {
        key: value for key, value in raw.items() if key != "signature"
    }
    assert verify_event_signature(frame.unsigned_event(), frame.signature, ADAPTER_KEY)
    assert frame.to_event() == raw
    assert "signature" not in frame.audit_event()

    tampered = dict(raw)
    tampered["args"] = {**raw["args"], "z_mm": 3_000}  # type: ignore[dict-item]
    tampered_frame = parse_command(tampered)
    assert not verify_event_signature(
        tampered_frame.unsigned_event(), tampered_frame.signature, ADAPTER_KEY
    )


def test_command_event_builder_produces_a_frame_the_parser_and_node_accept() -> None:
    event = command_event(
        t=1_756_700_000_000,
        event_id="command-hover",
        session="session-test",
        command_id="command-2",
        intent_id="intent-1",
        roster_version=3,
        drone_id=1,
        connection_epoch=2,
        seq=7,
        issued_at=1_756_700_000_000,
        ttl_ms=2_000,
        operation=CommandOperation.HOVER,
        args={},
    )
    event["signature"] = sign_event(event, ADAPTER_KEY)

    frame = parse_command(event)

    assert frame.seq == 7
    assert frame.ttl_ms == 2_000
    assert frame.connection_epoch == 2
    assert verify_event_signature(frame.unsigned_event(), frame.signature, ADAPTER_KEY)
    with pytest.raises(ValueError, match="arguments"):
        command_event(
            t=1,
            event_id="command-bad",
            session="session-test",
            command_id="command-3",
            intent_id="intent-1",
            roster_version=3,
            drone_id=1,
            connection_epoch=2,
            seq=8,
            issued_at=1,
            ttl_ms=2_000,
            operation=CommandOperation.HOVER,
            args={"z_mm": 1_000},
        )


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"operation": "fly"}, "unknown command operation"),
        ({"args": {"x_mm": 1.5, "y_mm": 0, "z_mm": 1_000, "speed_mm_s": 500}}, "integer"),
        ({"args": {"x_mm": 0, "y_mm": 0, "z_mm": 1_000, "speed_mm_s": 0}}, "positive"),
        ({"operation": "hover", "args": {"z_mm": 1}}, "arguments"),
        ({"ttl_ms": 0}, "positive"),
        ({"seq": 0}, "positive"),
    ],
)
def test_command_frame_rejects_untyped_arguments_and_unknown_operations(
    changes: dict[str, object], match: str
) -> None:
    raw = command_payload(event_id="command-invalid", **changes)

    with pytest.raises(ContractError, match=match) as error:
        parse_command(raw)
    assert error.value.code == "invalid_command"


def test_command_frame_requires_exact_fields() -> None:
    raw = command_payload(event_id="command-1")
    del raw["issued_at"]

    with pytest.raises(ContractError, match="do not match"):
        parse_command(raw)


def test_capabilities_frame_carries_camera_and_hardware_profile() -> None:
    raw = capabilities_payload(event_id="capabilities-1", measured_hfov_deg=64.5)

    frame = parse_capabilities(raw)

    assert frame.native_panorama_modes == ("pano_360",)
    assert frame.aircraft_model == "DJI Mini 3"
    assert frame.measured_hfov_deg == 64.5
    assert frame.to_event() == raw
    payload = frame.state_payload()
    assert payload["type"] == "capabilities"
    assert payload["sdk_version"] == "5.18.0"
    assert "event_id" not in payload
    assert "session" not in payload


def test_capabilities_frame_rejects_an_inverted_gimbal_range() -> None:
    raw = capabilities_payload(
        event_id="capabilities-bad", gimbal_pitch_min_deg=30.0, gimbal_pitch_max_deg=-90.0
    )

    with pytest.raises(ContractError, match="gimbal") as error:
        parse_capabilities(raw)
    assert error.value.code == "invalid_capabilities"


def test_media_file_frame_mirrors_the_adapter_media_file_shape() -> None:
    raw = media_file_payload(event_id="media-1")

    frame = parse_media_file(raw)

    assert frame.file.file_id == "capture-1-pano-360"
    assert frame.file.capture_id == "capture-1"
    assert frame.file.pose.z == 1.0
    assert frame.file.intrinsics.projection == "equirectangular"
    assert frame.file.retrieval_status == "completed"
    assert frame.to_event() == raw


def test_media_file_frame_accepts_a_pending_capture_time_record() -> None:
    raw = media_file_payload(
        event_id="media-pending", retrieval_status="pending", checksum_sha256="0" * 64
    )

    frame = parse_media_file(raw)

    assert frame.file.retrieval_status == "pending"
    assert frame.file.checksum_sha256 == "0" * 64
    with pytest.raises(ContractError, match="retrieval_status"):
        parse_media_file(media_file_payload(event_id="media-bad", retrieval_status="queued"))
    # A bundle closes a set; its status vocabulary has no pending value.
    with pytest.raises(ContractError, match="status"):
        parse_capture_bundle(capture_bundle_payload(event_id="bundle-bad", status="pending"))


def test_media_file_frame_rejects_a_malformed_checksum() -> None:
    raw = media_file_payload(event_id="media-bad", checksum_sha256="abc")

    with pytest.raises(ContractError, match="checksum") as error:
        parse_media_file(raw)
    assert error.value.code == "invalid_media_file"


def test_capture_bundle_frame_nests_media_records() -> None:
    raw = capture_bundle_payload(event_id="bundle-1")

    frame = parse_capture_bundle(raw)

    assert frame.pattern == "pano_360"
    assert frame.coverage == "full_equirectangular"
    assert frame.status == "completed"
    assert frame.media[0].file_id == "capture-1-pano-360"
    assert frame.to_event() == raw


def test_capture_bundle_failure_requires_a_machine_readable_reason() -> None:
    missing = capture_bundle_payload(event_id="bundle-failed", status="failed", media=[])
    with pytest.raises(ContractError, match="reason") as error:
        parse_capture_bundle(missing)
    assert error.value.code == "invalid_capture_bundle"

    display_only = capture_bundle_payload(
        event_id="bundle-failed-2", status="failed", media=[], reason="Camera broke"
    )
    with pytest.raises(ContractError, match="snake_case"):
        parse_capture_bundle(display_only)


def test_capture_readiness_frame_keeps_guidance_mode_and_delta() -> None:
    raw = capture_readiness_payload(event_id="readiness-1")

    frame = parse_capture_readiness(raw)

    assert frame.guidance_mode is GuidanceMode.VISUAL_ADVISORY
    assert frame.coverage_missing == (90.0, 135.0)
    assert frame.next_heading_deg == 90.0
    assert frame.suggested_delta is not None
    assert frame.suggested_delta.kind is DeltaKind.YAW
    assert frame.suggested_delta.degrees == 12.0
    assert frame.to_event() == raw

    unassigned = capture_readiness_payload(
        event_id="readiness-2",
        room_id=None,
        capture_id=None,
        next_heading_deg=None,
        suggested_delta=None,
        coverage_missing=[],
    )
    assert parse_capture_readiness(unassigned).suggested_delta is None


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"suggested_delta": {"kind": "translate", "degrees": 1}}, "yaw or gimbal"),
        ({"coverage_missing": [360]}, "azimuth"),
        ({"guidance_mode": "metric"}, "guidance_mode"),
    ],
)
def test_capture_readiness_rejects_unknown_kinds_and_out_of_range_azimuths(
    changes: dict[str, object], match: str
) -> None:
    raw = capture_readiness_payload(event_id="readiness-bad", **changes)

    with pytest.raises(ContractError, match=match) as error:
        parse_capture_readiness(raw)
    assert error.value.code == "invalid_capture_readiness"


def test_node_status_frame_reports_watchdog_and_phone_health() -> None:
    raw = node_status_payload(
        event_id="status-1", authority_change_reason="rc_takeover", control_authority=False
    )

    frame = parse_node_status(raw)

    assert frame.watchdog_state is WatchdogState.NOMINAL
    assert frame.control_authority is False
    assert frame.authority_change_reason == "rc_takeover"
    assert frame.phone_battery_percent == 81
    assert frame.to_event() == raw
    payload = frame.state_payload()
    assert payload["type"] == "node_status"
    assert "event_id" not in payload


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"phone_battery_percent": 101}, "between 0 and 100"),
        ({"authority_change_reason": "RC took over"}, "snake_case"),
        ({"watchdog_state": "armed"}, "watchdog_state"),
    ],
)
def test_node_status_rejects_out_of_range_and_display_only_values(
    changes: dict[str, object], match: str
) -> None:
    raw = node_status_payload(event_id="status-bad", **changes)

    with pytest.raises(ContractError, match=match) as error:
        parse_node_status(raw)
    assert error.value.code == "invalid_node_status"


def test_node_acknowledgement_reasons_are_machine_readable_wire_values() -> None:
    assert {reason.value for reason in NodeAcknowledgementReason} == {
        "stale_command",
        "out_of_order_command",
        "authority_lost",
        "watchdog_hold",
        "watchdog_failsafe",
    }
    for reason in NodeAcknowledgementReason:
        acknowledgement = parse_adapter_acknowledgement(
            acknowledgement_payload(
                event_id=f"ack-{reason.value}", status="failed", reason=reason.value
            )
        )
        assert acknowledgement.reason == reason.value

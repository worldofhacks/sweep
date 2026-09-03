from __future__ import annotations

import pytest

from relay.auth import (
    AuthenticationError,
    StaticCredentialResolver,
    authenticate,
    sign_event,
    verify_event_signature,
)
from relay.contracts import (
    ContractError,
    LifecycleStatus,
    acknowledgement_event,
    parse_adapter_acknowledgement,
    parse_membership_request,
    parse_telemetry,
)
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    acknowledgement_payload,
    membership_payload,
    telemetry_payload,
)


def test_console_and_keyboard_authentication_are_bound_to_their_source() -> None:
    resolver = StaticCredentialResolver(relay_token=CONSOLE_KEY)

    console = authenticate(
        {"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()},
        resolver,
    )
    keyboard = authenticate(
        {"v": 1, "type": "auth", "source": "keyboard", "token": CONSOLE_KEY.decode()},
        resolver,
    )

    assert console.source == "console"
    assert keyboard.source == "keyboard"
    assert console.drone_id is None


def test_adapter_authentication_uses_the_bound_aircraft_key() -> None:
    resolver = StaticCredentialResolver(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
    )

    principal = authenticate(
        {
            "v": 1,
            "type": "auth",
            "source": "adapter",
            "drone_id": 1,
            "token": ADAPTER_KEY.decode(),
        },
        resolver,
    )

    assert principal.drone_id == 1
    with pytest.raises(AuthenticationError, match="credential"):
        authenticate(
            {
                "v": 1,
                "type": "auth",
                "source": "adapter",
                "drone_id": 2,
                "token": ADAPTER_KEY.decode(),
            },
            resolver,
        )


def test_shared_adapter_token_is_an_explicit_demo_only_fallback() -> None:
    frame = {
        "v": 1,
        "type": "auth",
        "source": "adapter",
        "drone_id": 4,
        "token": CONSOLE_KEY.decode(),
    }
    secure = StaticCredentialResolver(relay_token=CONSOLE_KEY)
    demo = StaticCredentialResolver(
        relay_token=CONSOLE_KEY,
        allow_shared_adapter_token=True,
    )

    with pytest.raises(AuthenticationError):
        authenticate(frame, secure)
    assert authenticate(frame, demo).drone_id == 4


def test_membership_signature_detects_tampering() -> None:
    raw = membership_payload(action="join", event_id="join-1")
    request = parse_membership_request(raw)

    assert verify_event_signature(request.unsigned_event(), request.signature, ADAPTER_KEY)

    raw["capabilities"] = ["flight", "reconstruct_8"]
    tampered = parse_membership_request(raw)
    assert not verify_event_signature(tampered.unsigned_event(), tampered.signature, ADAPTER_KEY)
    assert not verify_event_signature(
        request.unsigned_event(), request.signature.upper(), ADAPTER_KEY
    )


def test_membership_signatures_are_stable_for_key_order() -> None:
    first = {"v": 1, "type": "membership", "drone_id": 1, "capabilities": ["flight"]}
    second = {"capabilities": ["flight"], "drone_id": 1, "type": "membership", "v": 1}

    assert sign_event(first, ADAPTER_KEY) == sign_event(second, ADAPTER_KEY)


@pytest.mark.parametrize("action", ["unexpected_loss", "telemetry_stale"])
def test_relay_internal_membership_actions_are_not_accepted_from_wire(action: str) -> None:
    raw = membership_payload(action="join", event_id="event-1")
    raw["action"] = action
    raw["signature"] = sign_event(
        {key: value for key, value in raw.items() if key != "signature"}, ADAPTER_KEY
    )

    with pytest.raises(ContractError, match="relay-internal"):
        parse_membership_request(raw)


def test_telemetry_contract_rejects_non_finite_and_out_of_range_values() -> None:
    raw = telemetry_payload(event_id="telemetry-1")
    raw["battery"] = 1.1
    with pytest.raises(ContractError, match="between 0 and 1"):
        parse_telemetry(raw)

    raw = telemetry_payload(event_id="telemetry-2")
    raw["x"] = float("nan")
    with pytest.raises(ContractError, match="finite"):
        parse_telemetry(raw)


def test_telemetry_v1_remains_valid_without_heading_and_rejects_extensions() -> None:
    raw = telemetry_payload(event_id="telemetry-v1")

    assert parse_telemetry(raw).to_event() == raw
    raw["heading_deg"] = 90.0
    with pytest.raises(ContractError, match="fields"):
        parse_telemetry(raw)


def test_acknowledgement_contract_keeps_command_and_machine_reason() -> None:
    raw = acknowledgement_payload(
        event_id="ack-1",
        status="failed",
        reason="adapter_timeout",
        detail="No response before the configured deadline",
    )

    acknowledgement = parse_adapter_acknowledgement(raw)

    assert acknowledgement.command_id == "command-1"
    assert acknowledgement.status is LifecycleStatus.FAILED
    assert acknowledgement.to_event()["reason"] == "adapter_timeout"


def test_failed_acknowledgement_requires_machine_readable_reason() -> None:
    raw = acknowledgement_payload(event_id="ack-1", status="failed", reason=None)
    with pytest.raises(ContractError, match="requires a reason"):
        parse_adapter_acknowledgement(raw)

    raw["reason"] = "Not safe"
    with pytest.raises(ContractError, match="snake_case"):
        parse_adapter_acknowledgement(raw)

    raw["reason"] = "error_１"
    with pytest.raises(ContractError, match="snake_case"):
        parse_adapter_acknowledgement(raw)


def test_adapter_acknowledgement_requires_command_id() -> None:
    raw = acknowledgement_payload(event_id="ack-1", command_id=None)

    with pytest.raises(ContractError, match="command_id"):
        parse_adapter_acknowledgement(raw)


def test_trusted_lifecycle_builder_also_enforces_machine_reason() -> None:
    with pytest.raises(ValueError, match="snake_case"):
        acknowledgement_event(
            t=1,
            event_id="event-1",
            session="session-1",
            intent_id="intent-1",
            status=LifecycleStatus.FAILED,
            roster_version=1,
            reason="Not safe",
        )

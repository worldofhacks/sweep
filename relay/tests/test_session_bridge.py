from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from planner.models import CommandOperation
from relay.auth import Principal, verify_event_signature
from relay.contracts import LifecycleStatus, parse_command
from relay.session import RelaySession
from relay.state import RegistryError
from relay.tests.conftest import (
    ADAPTER_KEY,
    acknowledgement_payload,
    capabilities_payload,
    capture_bundle_payload,
    capture_readiness_payload,
    intent_payload,
    media_file_payload,
    membership_payload,
    node_status_payload,
    telemetry_payload,
)


def _join(session: RelaySession, principal: Principal, event_id: str = "join-1") -> None:
    session.process_membership(membership_payload(action="join", event_id=event_id), principal)


def _issue_hover(session: RelaySession, command_id: str, **changes: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "command_id": command_id,
        "intent_id": "intent-1",
        "roster_version": session.registry.roster_version,
        "drone_id": 1,
        "connection_epoch": 1,
        "operation": CommandOperation.HOVER,
        "args": {},
        "signing_key": ADAPTER_KEY,
    }
    arguments.update(changes)
    return session.issue_command(**arguments)  # type: ignore[arg-type]


def test_capabilities_and_node_status_update_state_and_fan_out(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    _join(relay_session, adapter_principal)

    capabilities = relay_session.process_frame(
        capabilities_payload(event_id="capabilities-1"), adapter_principal
    )
    status = relay_session.process_frame(
        node_status_payload(event_id="status-1", watchdog_state="hold"), adapter_principal
    )

    assert [event["type"] for event in capabilities] == ["capabilities", "state"]
    assert [event["type"] for event in status] == ["node_status", "state"]
    drone = status[1]["drones"][0]
    assert drone["camera_capabilities"]["sdk_version"] == "5.18.0"
    assert drone["camera_capabilities"]["native_panorama_modes"] == ["pano_360"]
    assert "event_id" not in drone["camera_capabilities"]
    assert drone["node_status"]["watchdog_state"] == "hold"
    assert drone["membership"] == "registered"
    replayed = [record["event"]["type"] for record in relay_session.replay()["events"]]
    assert replayed[-4:] == ["capabilities", "state", "node_status", "state"]
    assert relay_session.metrics()["node_events"] == 2


def test_capture_readiness_is_fanned_out_without_a_state_change(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    _join(relay_session, adapter_principal)

    events = relay_session.process_frame(
        capture_readiness_payload(event_id="readiness-1"), adapter_principal
    )

    assert [event["type"] for event in events] == ["capture_readiness"]
    assert events[0]["next_heading_deg"] == 90.0
    assert relay_session.replay()["events"][-1]["event"]["type"] == "capture_readiness"


def test_media_file_and_capture_bundle_are_audited_and_retained_for_the_wire(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    _join(relay_session, adapter_principal)

    media = relay_session.process_frame(media_file_payload(event_id="media-1"), adapter_principal)
    bundle = relay_session.process_frame(
        capture_bundle_payload(event_id="bundle-1", timestamp=1_756_700_000_001),
        adapter_principal,
    )

    assert media == []
    assert bundle == []
    replayed = [record["event"]["type"] for record in relay_session.replay()["events"]]
    assert replayed[-2:] == ["media_file", "capture_bundle"]
    files = relay_session.media_files(1, "capture-1")
    assert [record.file_id for record in files] == ["capture-1-pano-360"]
    assert relay_session.media_files(1, "capture-unknown") == ()


def test_node_frames_require_binding_current_epoch_and_fresh_event_ids(
    relay_session: RelaySession,
    adapter_principal: Principal,
    console_principal: Principal,
) -> None:
    before_join = relay_session.process_frame(
        node_status_payload(event_id="status-early"), adapter_principal
    )
    _join(relay_session, adapter_principal)
    accepted = relay_session.process_frame(
        node_status_payload(event_id="status-1"), adapter_principal
    )
    replayed = relay_session.process_frame(
        node_status_payload(event_id="status-1"), adapter_principal
    )
    other_identity = Principal(source="adapter", drone_id=2, signing_key=ADAPTER_KEY)
    mismatched = relay_session.process_frame(
        node_status_payload(event_id="status-2"), other_identity
    )
    from_console = relay_session.process_frame(
        node_status_payload(event_id="status-3"), console_principal
    )
    malformed = relay_session.process_frame(
        node_status_payload(event_id="status-4", phone_battery_percent=101), adapter_principal
    )
    relay_session.handle_adapter_disconnect(drone_id=1, connection_epoch=1)
    _join(relay_session, adapter_principal, "join-2")
    prior_epoch = relay_session.process_frame(
        node_status_payload(event_id="status-5", connection_epoch=1), adapter_principal
    )

    assert before_join[0]["reason"] == "unknown_aircraft"
    assert accepted[0]["type"] == "node_status"
    assert replayed[0]["reason"] == "replayed_event"
    assert mismatched[0]["reason"] == "drone_identity_mismatch"
    assert from_console[0]["reason"] == "frame_not_allowed"
    assert malformed[0]["reason"] == "invalid_node_status"
    assert prior_epoch[0]["reason"] == "stale_connection_epoch"


def test_rejoin_clears_camera_capabilities_and_node_status(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    _join(relay_session, adapter_principal)
    relay_session.process_frame(capabilities_payload(event_id="capabilities-1"), adapter_principal)
    relay_session.process_frame(node_status_payload(event_id="status-1"), adapter_principal)
    relay_session.handle_adapter_disconnect(drone_id=1, connection_epoch=1)

    rejoined = _join_events(relay_session, adapter_principal, "join-2")

    drone = rejoined[1]["drones"][0]
    assert drone["connection_epoch"] == 2
    assert drone["camera_capabilities"] is None
    assert drone["node_status"] is None


def test_issue_command_signs_audits_and_sequences_per_epoch(
    relay_session: RelaySession,
    adapter_principal: Principal,
    console_principal: Principal,
) -> None:
    _join(relay_session, adapter_principal)
    relay_session.process_intent(intent_payload(), console_principal)

    first = _issue_hover(relay_session, "command-1")
    second = _issue_hover(
        relay_session,
        "command-2",
        operation=CommandOperation.GOTO,
        args={"x_mm": 1_000, "y_mm": 0, "z_mm": 1_000, "speed_mm_s": 500},
    )

    frame = parse_command(first)
    assert verify_event_signature(frame.unsigned_event(), frame.signature, ADAPTER_KEY)
    assert (first["seq"], second["seq"]) == (1, 2)
    assert first["ttl_ms"] == relay_session.limits.command_ttl_ms
    assert first["issued_at"] == first["t"]
    records = [
        record["event"]
        for record in relay_session.replay()["events"]
        if record["event"]["type"] == "command"
    ]
    assert [record["command_id"] for record in records] == ["command-1", "command-2"]
    assert all("signature" not in record for record in records)
    assert relay_session.metrics()["commands_issued"] == 2
    with pytest.raises(RegistryError) as stale:
        _issue_hover(relay_session, "command-3", connection_epoch=2)
    assert stale.value.code == "stale_connection_epoch"
    with pytest.raises(ValueError, match="command_id"):
        _issue_hover(relay_session, "command-1")


def test_issue_command_preserves_an_authorized_navigation_route_id(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    _join(relay_session, adapter_principal)
    issued = _issue_hover(
        relay_session,
        "navigation-command",
        operation=CommandOperation.GOTO,
        args={
            "x_mm": 1_000,
            "y_mm": -400,
            "z_mm": 1_000,
            "speed_mm_s": 500,
            "navigation_route_id": "route-1",
        },
    )

    command = parse_command(issued)
    assert command.args["navigation_route_id"] == "route-1"
    unsigned = dict(issued)
    signature = unsigned.pop("signature")
    assert verify_event_signature(unsigned, signature, ADAPTER_KEY)


def test_issue_command_registers_autonomy_intents_and_refuses_terminal_ones(
    relay_session: RelaySession,
    adapter_principal: Principal,
    console_principal: Principal,
) -> None:
    _join(relay_session, adapter_principal)
    relay_session.process_telemetry(telemetry_payload(event_id="telemetry-1"), adapter_principal)
    safety_intent = "safety:planner-failure:intent-9"

    _issue_hover(relay_session, "hold-1", intent_id=safety_intent)
    acknowledged = relay_session.process_acknowledgement(
        acknowledgement_payload(
            event_id="ack-hold",
            intent_id=safety_intent,
            command_id="hold-1",
            status="completed",
        ),
        adapter_principal,
    )
    invalid = intent_payload(intent_id="intent-bad")
    invalid["args"] = {"not": "empty"}
    relay_session.process_intent(invalid, console_principal)

    assert acknowledged[0]["type"] == "acknowledgement"
    assert acknowledged[0]["status"] == "completed"
    with pytest.raises(ValueError, match="terminal"):
        _issue_hover(relay_session, "command-bad", intent_id="intent-bad")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"command_id": "never-issued"}, "unknown_command_id"),
        ({"intent_id": "intent-other"}, "command_intent_mismatch"),
        ({"roster_version": 2}, "command_roster_mismatch"),
    ],
)
def test_acknowledgement_must_match_the_exact_issued_command(
    relay_session: RelaySession,
    adapter_principal: Principal,
    console_principal: Principal,
    changes: dict[str, object],
    reason: str,
) -> None:
    _join(relay_session, adapter_principal)
    relay_session.process_intent(intent_payload(), console_principal)
    _issue_hover(relay_session, "command-1")

    refused = relay_session.process_acknowledgement(
        acknowledgement_payload(event_id=f"ack-{reason}", status="completed", **changes),
        adapter_principal,
    )

    assert refused[0]["type"] == "refusal"
    assert refused[0]["reason"] == reason
    audited_adapter_acks = [
        record["event"]
        for record in relay_session.replay()["events"]
        if record["event"]["type"] == "acknowledgement"
        and record["event"].get("source") == "adapter"
    ]
    assert audited_adapter_acks == []


def test_await_command_acknowledgement_returns_node_acknowledgements_in_order(
    relay_session: RelaySession,
    adapter_principal: Principal,
    console_principal: Principal,
) -> None:
    _join(relay_session, adapter_principal)
    relay_session.process_intent(intent_payload(), console_principal)
    _issue_hover(relay_session, "command-1")
    _issue_hover(relay_session, "command-2")

    timed_out = relay_session.await_command_acknowledgement("command-1", timeout_ms=10)
    for status in ("accepted", "executing", "completed"):
        relay_session.process_acknowledgement(
            acknowledgement_payload(
                event_id=f"ack-{status}", command_id="command-2", status=status
            ),
            adapter_principal,
        )
    ordered = [
        relay_session.await_command_acknowledgement("command-2", timeout_ms=100) for _ in range(3)
    ]
    after_terminal = relay_session.await_command_acknowledgement("command-2", timeout_ms=10)
    late = relay_session.process_acknowledgement(
        acknowledgement_payload(event_id="ack-late", command_id="command-1", status="completed"),
        adapter_principal,
    )

    assert timed_out is None
    assert [ack.status for ack in ordered if ack is not None] == [
        LifecycleStatus.ACCEPTED,
        LifecycleStatus.EXECUTING,
        LifecycleStatus.COMPLETED,
    ]
    assert after_terminal is None
    assert late[0]["type"] == "acknowledgement"


def test_await_command_acknowledgement_wakes_a_waiting_thread(
    relay_session: RelaySession,
    adapter_principal: Principal,
    console_principal: Principal,
) -> None:
    _join(relay_session, adapter_principal)
    relay_session.process_intent(intent_payload(), console_principal)
    _issue_hover(relay_session, "command-1")

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(
            relay_session.await_command_acknowledgement, "command-1", timeout_ms=2_000
        )
        relay_session.process_acknowledgement(
            acknowledgement_payload(event_id="ack-1", command_id="command-1", status="accepted"),
            adapter_principal,
        )
        acknowledgement = waiting.result(timeout=2)

    assert acknowledgement is not None
    assert acknowledgement.status is LifecycleStatus.ACCEPTED
    assert acknowledgement.command_id == "command-1"


def _join_events(
    session: RelaySession, principal: Principal, event_id: str
) -> list[dict[str, object]]:
    return session.process_membership(
        membership_payload(action="join", event_id=event_id), principal
    )


def test_capture_readiness_is_retained_for_the_current_epoch_only(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    _join(relay_session, adapter_principal)
    assert relay_session.capture_readiness(1) is None

    relay_session.process_frame(
        capture_readiness_payload(event_id="readiness-1", camera_ok=False), adapter_principal
    )
    first = relay_session.capture_readiness(1)
    relay_session.process_frame(
        capture_readiness_payload(event_id="readiness-2", timestamp=1_756_700_000_001),
        adapter_principal,
    )
    latest = relay_session.capture_readiness(1)
    relay_session.handle_adapter_disconnect(drone_id=1, connection_epoch=1)
    after_loss = relay_session.capture_readiness(1)
    relay_session.process_membership(
        membership_payload(action="join", event_id="join-2", timestamp=1_756_700_000_002),
        adapter_principal,
    )

    assert first is not None and first.camera_ok is False
    assert latest is not None and latest.camera_ok is True and latest.storage_ok is True
    assert after_loss is None, "a lost aircraft has no current readiness"
    assert relay_session.registry.connection_epoch(1) == 2
    assert relay_session.capture_readiness(1) is None, "a rejoin starts a new epoch"
    assert relay_session.capture_readiness(2) is None
